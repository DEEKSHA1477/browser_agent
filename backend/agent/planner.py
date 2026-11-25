"""Task understanding, planning and clarification (LangGraph nodes)."""
from __future__ import annotations

import re
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from agent.prompts import PLANNER_SYSTEM, planner_prompt
from agent.state import AgentState, TaskRecord, task_store
from config import settings
from models.task_models import StepStatus, TaskStatus
from services.llm_service import llm_service

MAX_CLARIFICATIONS = 3

_BLOCKED_INTENT = re.compile(
    r"\b((bypass|solve|crack|break|defeat)\s+(the\s+)?(captcha|recaptcha|2fa|two[-\s]factor|otp|paywall)|"
    r"steal\s+(passwords?|credentials|cookies)|phishing|credential\s+stuffing|brute[-\s]?force|ddos|"
    r"card(ing)?\s+fraud)\b",
    re.I,
)


class InvalidInstruction(ValueError):
    pass


def validate_instruction(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text or "").strip()
    if len(text) < 3:
        raise InvalidInstruction("Please describe what you would like me to do.")
    if len(text) > settings.max_instruction_length:
        raise InvalidInstruction(f"Instructions are limited to {settings.max_instruction_length} characters.")
    if _BLOCKED_INTENT.search(text):
        raise InvalidInstruction("I can't help with bypassing security mechanisms or other harmful activities.")
    return text


def record_for(config: RunnableConfig) -> TaskRecord:
    task_id = config["configurable"]["thread_id"]
    record = task_store.get(task_id)
    if record is None:
        raise RuntimeError(f"Task {task_id} not found")
    return record


def _clean_understanding(data: dict[str, Any], instruction: str) -> dict[str, Any]:
    def as_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v)[:300] for v in value if v][:10]
        return [str(value)[:300]] if value else []

    user_data = data.get("user_data") if isinstance(data.get("user_data"), dict) else {}
    return {
        "refuse": bool(data.get("refuse")),
        "refuse_reason": str(data.get("refuse_reason") or "")[:300] or None,
        "goal": str(data.get("goal") or instruction)[:300],
        "category": str(data.get("category") or "general")[:30],
        "needs_clarification": bool(data.get("needs_clarification")) and bool(data.get("question")),
        "question": str(data.get("question") or "")[:300] or None,
        "plan": as_list(data.get("plan")),
        "start_url": str(data.get("start_url") or "")[:500] or None,
        "success_criteria": as_list(data.get("success_criteria")),
        "user_data": {str(k)[:60]: str(v)[:300] for k, v in list(user_data.items())[:20]},
        "assumptions": as_list(data.get("assumptions")),
    }


async def understand_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    record = record_for(config)
    await record.control.checkpoint()
    await record.set_status(TaskStatus.UNDERSTANDING)
    await record.step("understand", StepStatus.ACTIVE)
    clarifications = state.get("clarifications") or []
    details = state.get("details") or {}
    raw = await llm_service.complete_json(PLANNER_SYSTEM, planner_prompt(state["instruction"], clarifications, details),
                                          utility=True)
    await record.note_model(*llm_service.last_used())
    understanding = _clean_understanding(raw, state["instruction"])
    # Form answers come straight from the user: they always win over the model's transcription.
    understanding["user_data"] = {**understanding["user_data"], **details}

    if understanding["refuse"]:
        await record.step("understand", StepStatus.ERROR, understanding["refuse_reason"] or "Request declined")
        return {"understanding": understanding, "outcome": "refused"}

    if understanding["needs_clarification"] and len(clarifications) < MAX_CLARIFICATIONS:
        return {"understanding": understanding}

    record.goal = understanding["goal"]
    record.category = understanding["category"]
    record.plan = understanding["plan"]
    await record.step("understand", StepStatus.DONE, understanding["goal"])
    await record.step("plan", StepStatus.DONE, f"{len(understanding['plan'])} steps planned")
    plan_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(understanding["plan"]))
    assumptions = understanding["assumptions"]
    message = f"Goal: {understanding['goal']}\n\nPlan:\n{plan_text}"
    if assumptions:
        message += "\n\nAssumptions: " + "; ".join(assumptions)
    await record.say(message)
    return {"understanding": understanding}


def route_after_understand(state: AgentState) -> str:
    if state.get("outcome") == "refused":
        return "finalize"
    u = state.get("understanding") or {}
    if u.get("needs_clarification") and len(state.get("clarifications") or []) < MAX_CLARIFICATIONS:
        return "clarify"
    return "init_browser"


async def clarify_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    record = record_for(config)
    question = (state.get("understanding") or {}).get("question") or "Could you give me more details?"
    if not record.clarification or record.clarification.get("question") != question:
        record.clarification = {"question": question}
        await record.say(question)
    await record.step("understand", StepStatus.WAITING, "Waiting for your answer")
    await record.set_status(TaskStatus.AWAITING_CLARIFICATION)
    answer = interrupt({"type": "clarification", "question": question})
    record.clarification = None
    return {"clarifications": (state.get("clarifications") or []) + [f"Q: {question} A: {str(answer)[:1000]}"]}
