"""LangGraph workflow + runner.

Graph:

    understand ──► clarify (interrupt) ──► understand
        │
        ▼
    init_browser ─► observe ─► decide ─┬─► gate ─┬─► act ───────────► observe ...
                      ▲                │         └─► approval (interrupt) ─► act
                      │                ├─► ask_user (interrupt) ─► observe
                      └────────────────┘ (invalid action)
                                       └─► verify ─► finalize

* State is checkpointed in memory (``InMemorySaver``) per task (thread_id).
* Clarifications, agent questions and approvals use LangGraph ``interrupt``;
  the run stops until the user answers through the REST API, then resumes
  with ``Command(resume=...)``.
* Retry after a failure resumes from the last successful checkpoint.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from agent import executor, planner
from agent.state import AgentState, TaskCancelled, TaskRecord
from browser.browser_manager import browser_manager
from models.task_models import StepStatus, TaskStatus
from services.approval_service import approval_service
from services.llm_service import LLMError

logger = logging.getLogger(__name__)


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("understand", planner.understand_node)
    g.add_node("clarify", planner.clarify_node)
    g.add_node("init_browser", executor.init_browser_node)
    g.add_node("observe", executor.observe_node)
    g.add_node("decide", executor.decide_node)
    g.add_node("ask_user", executor.ask_user_node)
    g.add_node("gate", executor.gate_node)
    g.add_node("request_approval", executor.request_approval_node)
    g.add_node("approval", executor.approval_node)
    g.add_node("act", executor.act_node)
    g.add_node("verify", executor.verify_node)
    g.add_node("finalize", executor.finalize_node)

    g.add_edge(START, "understand")
    g.add_conditional_edges("understand", planner.route_after_understand, ["clarify", "init_browser", "finalize"])
    g.add_edge("clarify", "understand")
    g.add_edge("init_browser", "observe")
    g.add_edge("observe", "decide")
    g.add_conditional_edges("decide", executor.route_after_decide, ["verify", "observe", "ask_user", "gate", "finalize"])
    g.add_edge("ask_user", "observe")
    g.add_conditional_edges("gate", executor.route_after_gate, ["request_approval", "act"])
    g.add_conditional_edges("request_approval", executor.route_after_request_approval, ["approval", "finalize"])
    g.add_conditional_edges("approval", executor.route_after_approval, ["act", "finalize"])
    g.add_conditional_edges("act", executor.route_after_act, ["request_approval", "observe", "finalize"])
    g.add_edge("verify", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=InMemorySaver())


graph = build_graph()


class AgentRunner:
    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task] = {}

    @staticmethod
    def _config(task_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": task_id}, "recursion_limit": 1000}

    def is_running(self, task_id: str) -> bool:
        task = self._running.get(task_id)
        return task is not None and not task.done()

    async def wait_until_suspended(self, task_id: str, timeout: float = 5.0) -> None:
        """The UI shows an approval card / question slightly before the graph
        finishes suspending at its ``interrupt``. Give it a moment instead of
        rejecting a fast click."""
        task = self._running.get(task_id)
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    async def waiting_for_input(self, task_id: str) -> str | None:
        """Type of the pending interrupt (clarification/question/approval), if any."""
        snapshot = await graph.aget_state(self._config(task_id))
        for task in snapshot.tasks:
            for intr in task.interrupts:
                value = intr.value if isinstance(intr.value, dict) else {}
                return value.get("type", "unknown")
        return None

    def start(self, record: TaskRecord) -> None:
        self._launch(record, {"instruction": record.instruction, "details": record.details, "clarifications": [],
                              "history": [], "step_count": 0,
                              "consecutive_failures": 0, "reapprovals": 0, "final": None, "outcome": None})

    def resume(self, record: TaskRecord, value: Any) -> None:
        self._launch(record, Command(resume=value))

    async def retry(self, record: TaskRecord) -> None:
        config = self._config(record.id)
        record.error = None
        record.can_retry = False
        record.summary = None
        record.verification = None
        snapshot = await graph.aget_state(config)
        if not snapshot.next:
            # Run reached the end (e.g. step limit / not verified): start again
            # from observation with a fresh failure budget, keeping history.
            await graph.aupdate_state(config, {"final": None, "outcome": None, "consecutive_failures": 0,
                                               "step_count": 0, "pending_action": None}, as_node="act")
            await record.reset_timeline_tail()
        else:
            await graph.aupdate_state(config, {"consecutive_failures": 0})
        await record.log("Retrying the task.", "info")
        self._launch(record, None)

    def _launch(self, record: TaskRecord, payload: Any) -> None:
        if self.is_running(record.id):
            raise RuntimeError("The agent is already working on this task.")
        self._running[record.id] = asyncio.create_task(self._drive(record, payload))

    async def _drive(self, record: TaskRecord, payload: Any) -> None:
        config = self._config(record.id)
        try:
            await graph.ainvoke(payload, config)
        except (TaskCancelled, asyncio.CancelledError):
            await self._mark_cancelled(record)
        except LLMError as exc:
            await self._mark_failed(record, f"AI model error: {exc}")
        except Exception as exc:  # browser launch failure, unexpected bugs
            logger.exception("Task %s failed", record.id)
            await self._mark_failed(record, f"{type(exc).__name__}: {exc}")
        finally:
            self._running.pop(record.id, None)

    async def _mark_failed(self, record: TaskRecord, message: str) -> None:
        if record.control.cancelled:
            await self._mark_cancelled(record)
            return
        record.error = {"message": message}
        record.can_retry = True
        for step in record.timeline:
            if step.status in (StepStatus.ACTIVE, StepStatus.WAITING):
                step.status = StepStatus.ERROR
                step.detail = message[:300]
        await record.log(message, "error")
        await record.say(f"Something went wrong: {message}. You can retry.")
        await record.set_status(TaskStatus.FAILED)

    async def _mark_cancelled(self, record: TaskRecord) -> None:
        approval_service.expire_task(record.id)
        record.approval_id = None
        record.clarification = None
        record.human_action = None
        record.can_retry = False
        for step in record.timeline:
            if step.status in (StepStatus.ACTIVE, StepStatus.WAITING):
                step.status = StepStatus.SKIPPED
                step.detail = "Stopped by you"
        if record.status != TaskStatus.CANCELLED:
            record.summary = "Task stopped at your request. No further actions were taken."
            await record.say(record.summary)
            await record.set_status(TaskStatus.CANCELLED)

    async def stop(self, record: TaskRecord) -> None:
        record.control.cancel()
        task = self._running.get(record.id)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        await self._mark_cancelled(record)

    async def close_browser(self, record: TaskRecord) -> None:
        await browser_manager.close(record.id)
        executor.forget(record.id)
        record.browser_open = False
        await record.publish()


runner = AgentRunner()
