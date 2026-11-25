"""In-memory task state.

Nothing here is persisted: tasks live in a Python dict for the lifetime of the
server process. There are two layers of state:

* ``AgentState`` - the small, serialisable dict that LangGraph checkpoints (in
  memory, via ``InMemorySaver``) between nodes. It is what makes pause/resume,
  clarification, approval interrupts and retry-from-failure possible.
* ``TaskRecord`` - the live, UI-facing view of a task (timeline, logs, results,
  pending approval...). Every change is pushed to the frontend over WebSocket.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, TypedDict

from models.task_models import (
    ChatMessage,
    HumanActionRequest,
    LogEntry,
    ResultCollection,
    StepStatus,
    TaskStatus,
    TimelineStep,
    TERMINAL_STATUSES,
    Verification,
)
from realtime.connection_manager import manager


class TaskCancelled(Exception):
    """Raised inside the agent when the user stops a task."""


class AgentState(TypedDict, total=False):
    instruction: str
    details: dict[str, str]  # structured answers from the task launcher form (authoritative)
    clarifications: list[str]  # "Q: ... / A: ..." pairs gathered from the user
    understanding: dict[str, Any]  # planner output (goal, plan, success criteria...)
    history: list[dict[str, Any]]  # compact record of executed actions + outcomes
    step_count: int
    consecutive_failures: int
    pending_action: dict[str, Any] | None  # action chosen by the LLM, not yet executed
    approval_id: str | None
    reapprovals: int
    final: dict[str, Any] | None  # the LLM's "done" payload
    verification: dict[str, Any] | None
    outcome: str | None  # completed | unverified | refused | rejected | failed | cancelled


# Fixed top-level stages. Individual browser actions are appended dynamically
# between "browser" and "verify" as the agent works.
STAGES: list[tuple[str, str]] = [
    ("understand", "Understanding your request"),
    ("plan", "Planning the task"),
    ("browser", "Opening browser"),
    ("verify", "Verifying the task was completed"),
    ("complete", "Task completed"),
]


class TaskControl:
    """Cooperative pause / stop / human-handoff signalling for one task."""

    def __init__(self) -> None:
        self._running = asyncio.Event()
        self._running.set()
        self._human_done = asyncio.Event()
        self.cancelled = False

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def cancel(self) -> None:
        self.cancelled = True
        self._running.set()
        self._human_done.set()

    async def checkpoint(self) -> None:
        """Called before every browser action and between graph nodes."""
        if self.cancelled:
            raise TaskCancelled()
        if not self._running.is_set():
            await self._running.wait()
        if self.cancelled:
            raise TaskCancelled()

    def human_completed(self) -> None:
        self._human_done.set()

    async def wait_for_human(self) -> None:
        self._human_done.clear()
        await self._human_done.wait()
        if self.cancelled:
            raise TaskCancelled()


class TaskRecord:
    MAX_LOGS = 300

    def __init__(self, instruction: str, *, capability: str | None = None, details: dict[str, str] | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.instruction = instruction
        self.capability = capability
        self.details: dict[str, str] = dict(details or {})
        self.created_at = time.time()
        self.status: TaskStatus = TaskStatus.QUEUED
        self.status_before_pause: TaskStatus | None = None
        self.goal: str | None = None
        self.category: str | None = None
        self.plan: list[str] = []
        self.timeline: list[TimelineStep] = [TimelineStep(id=k, label=l) for k, l in STAGES]
        self.logs: list[LogEntry] = []
        self.chat: list[ChatMessage] = [ChatMessage(ts=self.created_at, role="user", text=instruction)]
        self.results: list[ResultCollection] = []
        self.clarification: dict[str, Any] | None = None
        self.approval_id: str | None = None
        self.human_action: HumanActionRequest | None = None
        self.current_url: str | None = None
        self.page_title: str | None = None
        self.current_action: str | None = None
        self.thought: str | None = None
        self.step_count = 0
        self.downloads: list[dict[str, Any]] = []
        self.error: dict[str, Any] | None = None
        self.summary: str | None = None
        self.verification: Verification | None = None
        self.can_retry = False
        self.browser_open = False
        self.control = TaskControl()
        self.last_screenshot: dict[str, Any] | None = None
        self.model_in_use: str | None = None
        self.model_is_local = False
        self._action_seq = 0

    # -- helpers -------------------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def _find_step(self, step_id: str) -> TimelineStep | None:
        return next((s for s in self.timeline if s.id == step_id), None)

    async def step(self, step_id: str, status: StepStatus, detail: str | None = None, label: str | None = None) -> None:
        step = self._find_step(step_id)
        if step is None:
            return
        now = time.time()
        if status == StepStatus.ACTIVE and step.started_at is None:
            step.started_at = now
        if status in (StepStatus.DONE, StepStatus.ERROR, StepStatus.SKIPPED):
            step.finished_at = now
        if status in (StepStatus.ACTIVE, StepStatus.WAITING):
            step.finished_at = None
        step.status = status
        if detail is not None:
            step.detail = detail
        if label is not None:
            step.label = label
        await self.publish()

    async def add_action_step(self, label: str, status: StepStatus = StepStatus.ACTIVE) -> str:
        """Insert a dynamic action step just before the 'verify' stage."""
        self._action_seq += 1
        step = TimelineStep(id=f"a{self._action_seq}", label=label, kind="action", status=status, started_at=time.time())
        verify_index = next(i for i, s in enumerate(self.timeline) if s.id == "verify")
        self.timeline.insert(verify_index, step)
        await self.publish()
        return step.id

    async def reset_timeline_tail(self) -> None:
        for step in self.timeline:
            if step.id in ("verify", "complete"):
                step.status, step.detail, step.started_at, step.finished_at = StepStatus.PENDING, None, None, None
        await self.publish()

    async def note_model(self, model: str, is_local: bool, reason: str | None) -> None:
        """Show which AI model is driving the task; warn once when it drops to the slow local fallback."""
        if model == self.model_in_use:
            return
        moved_to_local = is_local and not self.model_is_local
        self.model_in_use, self.model_is_local = model, is_local
        if moved_to_local:
            why = f" because {reason}" if reason else ""
            await self.log(f"Using the local model {model}{why}. It runs on this computer and takes about "
                           "1-2 minutes per step, so this task will be slow.", "warning")
        else:
            await self.log(f"AI model: {model}", "info")

    async def set_status(self, status: TaskStatus) -> None:
        self.status = status
        await self.publish()

    async def log(self, message: str, level: str = "info") -> None:
        entry = LogEntry(ts=time.time(), level=level, message=message[:500])  # type: ignore[arg-type]
        self.logs.append(entry)
        if len(self.logs) > self.MAX_LOGS:
            self.logs = self.logs[-self.MAX_LOGS :]
        if level == "action":
            self.current_action = entry.message
        await self.publish()

    async def say(self, text: str, role: str = "agent") -> None:
        self.chat.append(ChatMessage(ts=time.time(), role=role, text=text))  # type: ignore[arg-type]
        await self.publish()

    async def publish(self) -> None:
        await manager.broadcast(self.id, {"type": "snapshot", "task_id": self.id, "data": self.snapshot()})

    async def publish_screenshot(self, image_b64: str, url: str | None, title: str | None) -> None:
        self.last_screenshot = {"image": image_b64, "url": url, "title": title, "ts": time.time()}
        await manager.broadcast(self.id, {"type": "screenshot", "task_id": self.id, "data": self.last_screenshot})

    def snapshot(self) -> dict[str, Any]:
        from services.approval_service import approval_service  # local import: avoid cycle

        approval = approval_service.get(self.approval_id) if self.approval_id else None
        return {
            "id": self.id,
            "instruction": self.instruction,
            "capability": self.capability,
            "details": self.details,
            "created_at": self.created_at,
            "status": self.status.value,
            "goal": self.goal,
            "category": self.category,
            "plan": self.plan,
            "timeline": [s.model_dump() for s in self.timeline],
            "logs": [l.model_dump() for l in self.logs[-150:]],
            "chat": [c.model_dump() for c in self.chat],
            "results": [r.model_dump() for r in self.results],
            "clarification": self.clarification,
            "approval": approval.model_dump() if approval else None,
            "human_action": self.human_action.model_dump() if self.human_action else None,
            "current_url": self.current_url,
            "page_title": self.page_title,
            "current_action": self.current_action,
            "thought": self.thought,
            "step_count": self.step_count,
            "downloads": self.downloads,
            "error": self.error,
            "summary": self.summary,
            "verification": self.verification.model_dump() if self.verification else None,
            "can_retry": self.can_retry,
            "paused": self.control.paused,
            "browser_open": self.browser_open,
            "model_in_use": self.model_in_use,
            "model_is_local": self.model_is_local,
        }


class TaskStore:
    """Process-wide in-memory registry of tasks (no database by design)."""

    MAX_TASKS = 50

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}

    def create(self, instruction: str, *, capability: str | None = None, details: dict[str, str] | None = None) -> TaskRecord:
        record = TaskRecord(instruction, capability=capability, details=details)
        self._tasks[record.id] = record
        if len(self._tasks) > self.MAX_TASKS:  # keep memory bounded
            for tid, rec in sorted(self._tasks.items(), key=lambda kv: kv[1].created_at):
                if rec.is_terminal and len(self._tasks) > self.MAX_TASKS:
                    self._tasks.pop(tid, None)
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def all(self) -> list[TaskRecord]:
        return sorted(self._tasks.values(), key=lambda r: r.created_at, reverse=True)


task_store = TaskStore()
