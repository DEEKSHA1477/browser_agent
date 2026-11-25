"""Browser execution nodes: observe -> decide -> safety gate -> (approval) -> act.

The loop is fully generic: no per-website workflow exists. The LLM decides the
next action from a fresh DOM/accessibility observation (plus a screenshot when
useful); the backend safety gate - not the LLM - decides whether that action
must be approved by the human first.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from playwright.async_api import Error as PlaywrightError

from agent.planner import record_for
from agent.prompts import VERIFIER_SYSTEM, agent_prompt, agent_system, verifier_prompt
from agent.state import AgentState, TaskRecord
from agent.tools import REGISTRY, ToolContext, describe_action, validate_action
from browser.browser_actions import ActionResult, BrowserActions
from browser.browser_manager import BrowserCrashed, BrowserSession, browser_manager
from browser.page_extractor import ElementInfo, Observation, element_context, observe
from config import settings
from models.approval_models import ApprovalDetail, ApprovalKind, ApprovalStatus
from models.task_models import HumanActionRequest, StepStatus, TaskStatus, Verification
from services import comparison_service, safety_policy
from services.approval_service import ApprovalError, approval_service, fingerprint_of
from services.llm_service import LLMError, llm_service

logger = logging.getLogger(__name__)

# Per-task runtime objects that are too large / not serialisable for the
# LangGraph checkpoint. Lost on restart, which is fine (no persistence by design).
_observations: dict[str, Observation] = {}
_last_vision_url: dict[str, str] = {}

KIND_TITLES = {
    ApprovalKind.PAYMENT: "Payment",
    ApprovalKind.ORDER: "Place order",
    ApprovalKind.FLIGHT_BOOKING: "Booking / reservation",
    ApprovalKind.JOB_APPLICATION: "Job application",
    ApprovalKind.MESSAGE: "Send message",
    ApprovalKind.CANCELLATION: "Cancellation",
    ApprovalKind.DELETION: "Delete information",
    ApprovalKind.ACCOUNT_CHANGE: "Account change",
    ApprovalKind.SENSITIVE_FORM: "Submit sensitive information",
    ApprovalKind.DOWNLOAD: "Download executable file",
    ApprovalKind.OTHER: "Sensitive action",
}


# ----------------------------------------------------------------------------- helpers


def results_summary(record: TaskRecord) -> str:
    """The verified records themselves (not just counts): final answers must be built from these."""
    return comparison_service.results_digest(record.results)


async def _session(record: TaskRecord) -> BrowserSession:
    """A usable browser for the task: relaunched after a crash, re-opened if its tabs were closed."""
    session = browser_manager.get(record.id)
    if session is None or not session.alive:
        if session is not None:
            await record.log("Browser is not running - relaunching it.", "warning")
        session = await browser_manager.ensure_session(record)
    try:
        if await session.ensure_page():
            await record.log("The browser tab was closed - opened a new one and continuing.", "warning")
    except BrowserCrashed:
        await record.log("The browser window was closed - relaunching it.", "warning")
        session = await browser_manager.ensure_session(record)
    return session


async def _observe(record: TaskRecord, session: BrowserSession, *, want_screenshot: bool) -> Observation:
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            page = session.page
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except PlaywrightError:
                pass
            obs = await observe(page)
            obs.tabs = session.tabs()
            if want_screenshot:
                try:
                    obs.screenshot = await session.screenshot(quality=60)
                except PlaywrightError:
                    obs.screenshot = None
            record.current_url, record.page_title = obs.url, obs.title
            return obs
        except BrowserCrashed:
            raise
        except PlaywrightError as exc:  # e.g. "Execution context was destroyed" during navigation
            last_exc = exc
            await asyncio.sleep(1.0 + attempt)
    raise RuntimeError(f"Could not read the page: {last_exc}")


async def human_handoff(record: TaskRecord, kind: str, message: str) -> None:
    record.human_action = HumanActionRequest(kind=kind if kind in ("captcha", "login", "blocked") else "other",  # type: ignore[arg-type]
                                             message=message, url=record.current_url)
    await record.set_status(TaskStatus.AWAITING_HUMAN_ACTION)
    await record.say(message)
    await record.log(f"Waiting for you: {message}", "warning")
    await record.control.wait_for_human()
    record.human_action = None
    await record.set_status(TaskStatus.RUNNING)
    await record.log("Resuming after your action.", "info")


def _host_path(url: str | None) -> str:
    if not url:
        return ""
    p = urlparse(url)
    return f"{p.hostname or ''}{p.path or ''}"[:120]


def material_details(action: dict[str, Any], element: ElementInfo | None, context: dict[str, Any] | None,
                     page_url: str, secret: bool) -> list[ApprovalDetail]:
    """Every fact that, if changed, must invalidate an approval."""
    params = action.get("params") or {}
    details = [
        # NB: never include the element index - it changes on every observation.
        ApprovalDetail(label="Action", value=action["name"] + (" and submit" if params.get("submit") else "")),
        ApprovalDetail(label="Website", value=_host_path(page_url)),
    ]
    if element is not None:
        details.append(ApprovalDetail(label="Target", value=element.descriptor))
        if element.href:
            details.append(ApprovalDetail(label="Link destination", value=_host_path(element.href)))
    if action["name"] == "navigate":
        details.append(ApprovalDetail(label="Destination", value=str(params.get("url", ""))[:200]))
    if action["name"] == "type":
        text = str(params.get("text", ""))
        details.append(ApprovalDetail(label="Text to enter", value=f"•••••• ({len(text)} characters, hidden)" if secret else text[:300]))
    if action["name"] == "press":
        details.append(ApprovalDetail(label="Key", value=str(params.get("key"))))
    amounts = safety_policy.extract_amounts((context or {}).get("container_text", ""))
    if amounts:
        details.append(ApprovalDetail(label="Amounts shown next to it", value=", ".join(amounts)))
    for label, value in list((action.get("approval_details") or {}).items())[:8]:
        if value not in (None, ""):
            details.append(ApprovalDetail(label=str(label)[:40], value=str(value)[:200]))
    return details


def _find_same_element(obs: Observation, original: dict[str, Any]) -> ElementInfo | None:
    """Re-locate the approved target after the page was re-observed."""
    matches = [e for e in obs.elements if e.tag == original["tag"] and e.role == original["role"] and e.name == original["name"]
               and (e.href or None) == (original.get("href") or None)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        same_index = [e for e in matches if e.index == original["index"]]
        return same_index[0] if same_index else None  # ambiguous -> treat as changed
    return None


# ----------------------------------------------------------------------------- nodes


async def init_browser_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    record = record_for(config)
    await record.control.checkpoint()
    await record.set_status(TaskStatus.RUNNING)
    await record.step("browser", StepStatus.ACTIVE)
    await browser_manager.ensure_session(record)
    await record.step("browser", StepStatus.DONE, "Chromium is open and visible")
    await record.log("Browser opened.", "success")
    return {}


async def observe_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    record = record_for(config)
    await record.control.checkpoint()
    if record.status not in (TaskStatus.PAUSED,):
        await record.set_status(TaskStatus.RUNNING)
    session = await _session(record)
    history = state.get("history") or []
    last_failed = bool(history) and not history[-1]["success"]
    mode = settings.vision_mode
    page_url = session.page.url
    want = mode == "always" or (mode == "auto" and (last_failed or _last_vision_url.get(record.id) != page_url))
    obs = await _observe(record, session, want_screenshot=want)
    handoffs = 0
    while obs.blocker == "captcha" and handoffs < 3:
        handoffs += 1
        await human_handoff(record, "captcha",
                            "The website is asking for human verification (CAPTCHA). Please complete it in the browser window, then click \"I've completed it\".")
        obs = await _observe(record, session, want_screenshot=want)
    if obs.screenshot is not None:
        _last_vision_url[record.id] = obs.url
    if mode == "auto" and len(obs.elements) < 3 and obs.screenshot is None:
        obs.screenshot = await session.screenshot(quality=60)
    _observations[record.id] = obs
    return {}


async def decide_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    record = record_for(config)
    await record.control.checkpoint()
    obs = _observations.get(record.id)
    step = (state.get("step_count") or 0) + 1
    if step > settings.max_steps:
        return {"pending_action": None, "final": {"success": False, "summary": f"Stopped after reaching the limit of {settings.max_steps} steps."}}
    history = state.get("history") or []
    last_error = history[-1]["result"] if history and not history[-1]["success"] else None
    understanding = dict(state.get("understanding") or {})
    if understanding.get("start_url") and not history:
        understanding.setdefault("plan", [])
        understanding["plan"] = understanding["plan"] + [f"(suggested start URL: {understanding['start_url']})"]
    prompt = agent_prompt(
        task=state["instruction"],
        understanding=understanding,
        clarifications=state.get("clarifications") or [],
        history=history,
        observation_text=obs.to_prompt() if obs else "(no page yet)",
        step=step,
        max_steps=settings.max_steps,
        results_summary=results_summary(record),
        last_error=last_error,
    )
    response = await llm_service.complete_json(agent_system(), prompt, image=obs.screenshot if obs else None)
    await record.note_model(*llm_service.last_used())
    action, error = validate_action(response.get("action"))
    record.thought = str(response.get("next_goal") or response.get("thought") or "")[:300] or None
    if error:
        await record.log(f"Model proposed an invalid action: {error}", "warning")
        entry = {"step": step, "label": "Invalid action", "success": False, "result": error}
        return {
            "pending_action": None,
            "history": history + [entry],
            "step_count": step,
            "consecutive_failures": (state.get("consecutive_failures") or 0) + 1,
        }
    assert action is not None
    action["next_goal"] = record.thought
    action["sensitive"] = response.get("sensitive") if isinstance(response.get("sensitive"), dict) else None
    details = response.get("approval_details")
    action["approval_details"] = details if isinstance(details, dict) else {}
    if record.thought:
        await record.log(f"Next: {record.thought}", "info")
    if action["name"] == "done":
        p = action["params"]
        summary = str(p.get("summary", ""))[:2000]
        ungrounded = comparison_service.ungrounded_amounts(summary, record.results, obs.corpus if obs else "")
        if ungrounded:
            already_warned = any(h.get("label") == "Unverified figures in summary" for h in history)
            if not already_warned:
                # Reject the summary once and make the model rewrite it from the verified data.
                msg = (f"Your final summary cites {', '.join(ungrounded[:5])}, which do not appear in RESULTS COLLECTED "
                       "or on the current page. Rewrite the summary using only the verified records (copy names, "
                       "prices and ratings exactly), then call done again.")
                await record.log("Final summary cited figures that were never verified; asking the agent to rewrite it.", "warning")
                return {"pending_action": None, "step_count": step,
                        "history": history + [{"step": step, "label": "Unverified figures in summary", "success": False, "result": msg}]}
            summary += ("\n\n⚠ These figures in the summary could not be matched to any verified page data: "
                        + ", ".join(ungrounded[:8]) + ". Rely on the verified comparison table instead.")
            await record.log("Final summary still contains unverified figures; flagged them for you.", "warning")
        return {"pending_action": None, "final": {"success": bool(p.get("success", True)), "summary": summary}}
    return {"pending_action": action}


def route_after_decide(state: AgentState) -> str:
    if state.get("final") is not None:
        return "verify"
    action = state.get("pending_action")
    if action is None:
        if (state.get("consecutive_failures") or 0) >= settings.max_consecutive_failures:
            return "finalize"
        return "observe"
    if action["name"] == "ask_user":
        return "ask_user"
    return "gate"


async def ask_user_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    record = record_for(config)
    action = state["pending_action"] or {}
    question = str((action.get("params") or {}).get("question") or "Could you clarify?")[:500]
    if not record.clarification or record.clarification.get("question") != question:
        record.clarification = {"question": question}
        await record.say(question)
    await record.set_status(TaskStatus.AWAITING_CLARIFICATION)
    answer = interrupt({"type": "question", "question": question})
    record.clarification = None
    await record.set_status(TaskStatus.RUNNING)
    step = (state.get("step_count") or 0) + 1
    history = (state.get("history") or []) + [{"step": step, "label": "Asked the user", "success": True, "result": f"Q: {question} A: {answer}"}]
    return {
        "pending_action": None,
        "history": history,
        "step_count": step,
        "clarifications": (state.get("clarifications") or []) + [f"Q: {question} A: {str(answer)[:1000]}"],
    }


async def gate_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Backend safety gate. Runs for EVERY action before it can execute."""
    record = record_for(config)
    action = dict(state["pending_action"] or {})
    obs = _observations.get(record.id)
    session = await _session(record)
    element = obs.element(action["params"]["element"]) if obs and "element" in action["params"] else None
    context = None
    if element is not None:
        try:
            context = await element_context(session.page, element.index)
        except PlaywrightError:
            context = None
    assessment = safety_policy.assess(action, element=element, context=context, page_url=obs.url if obs else "",
                                      agent_flag=action.get("sensitive"))
    action["safety"] = {
        "requires_approval": assessment.requires_approval,
        "kind": assessment.kind.value if assessment.kind else None,
        "reasons": assessment.reasons,
        "sources": assessment.sources,
        "secret": assessment.secret_values,
    }
    action["target"] = (
        {"index": element.index, "tag": element.tag, "role": element.role, "name": element.name, "href": element.href}
        if element else None
    )
    if assessment.requires_approval:
        details = material_details(action, element, context, obs.url if obs else "", assessment.secret_values)
        action["material"] = [d.model_dump() for d in details]
        await record.log(f"Sensitive action detected ({assessment.kind.value if assessment.kind else 'other'}): approval required.", "warning")
    return {"pending_action": action}


def route_after_gate(state: AgentState) -> str:
    action = state.get("pending_action") or {}
    return "request_approval" if (action.get("safety") or {}).get("requires_approval") else "act"


async def request_approval_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Create the approval request (all side effects live here).

    Kept separate from ``approval_node`` on purpose: LangGraph re-executes an
    interrupted node from its first line when it resumes, so the node that
    calls ``interrupt`` must not create or mutate anything before it.
    """
    record = record_for(config)
    action = state["pending_action"] or {}
    if (state.get("reapprovals") or 0) > settings.max_reapprovals:
        await record.log("The proposed action kept changing; stopping for safety.", "error")
        return {"outcome": "failed", "approval_id": None, "message": "The proposed action kept changing; stopped for safety."}
    safety = action["safety"]
    kind = ApprovalKind(safety["kind"] or "other")
    details = [ApprovalDetail(**d) for d in action["material"]]
    obs = _observations.get(record.id)
    summary = describe_action(action, obs, mask_text=safety.get("secret", False))
    warnings = list(safety.get("reasons") or [])
    if (state.get("reapprovals") or 0) > 0:
        warnings.insert(0, "The details changed since your previous approval - please review again.")
    approval = approval_service.create(
        task_id=record.id,
        kind=kind,
        title=f"ACTION REQUIRED: {KIND_TITLES.get(kind, 'Sensitive action').upper()}",
        description=action.get("next_goal") or summary,
        details=details,
        action_summary=summary,
        sources=safety.get("sources") or ["policy"],
        warnings=warnings,
    )
    if record.approval_id != approval.id:
        record.approval_id = approval.id
        await record.say(f"I need your approval before I continue: {summary}.")
    await record.step("complete", StepStatus.PENDING)
    await record.set_status(TaskStatus.AWAITING_APPROVAL)
    return {"approval_id": approval.id, "outcome": None}


def route_after_request_approval(state: AgentState) -> str:
    return "finalize" if state.get("outcome") == "failed" else "approval"


async def approval_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Pause until the user decides. No side effects before ``interrupt``."""
    record = record_for(config)
    approval_id = state.get("approval_id")
    interrupt({"type": "approval", "approval_id": approval_id})
    # Resumed: the decision recorded by the approval service (validated against
    # the fingerprint server-side) is authoritative - not the resume payload.
    decided = approval_service.get(approval_id)
    if decided is None or decided.task_id != record.id or decided.status != ApprovalStatus.APPROVED:
        status = decided.status.value if decided else "missing"
        return {"outcome": "rejected" if status == "rejected" else "failed", "message": f"Approval {status}"}
    await record.log("You approved the action. Re-validating it on the live page before executing.", "success")
    return {}


def route_after_approval(state: AgentState) -> str:
    return "finalize" if state.get("outcome") in ("rejected", "failed") else "act"


async def _revalidate(record: TaskRecord, session: BrowserSession, action: dict[str, Any], approval_id: str) -> tuple[dict[str, Any], str | None]:
    """Re-observe the page and re-derive the approval fingerprint.

    Returns (possibly updated action, error). error != None means the action
    changed materially and needs a fresh approval.
    """
    obs = await _observe(record, session, want_screenshot=False)
    _observations[record.id] = obs
    element = None
    context = None
    if action.get("target"):
        element = _find_same_element(obs, action["target"])
        if element is None:
            return action, "The approved element is no longer on the page (or is ambiguous)."
        action = {**action, "params": {**action["params"], "element": element.index}}
        try:
            context = await element_context(session.page, element.index)
        except PlaywrightError:
            context = None
    details = material_details(action, element, context, obs.url, action["safety"].get("secret", False))
    kind = ApprovalKind(action["safety"]["kind"] or "other")
    try:
        approval_service.require_approved(approval_id, task_id=record.id, fingerprint=fingerprint_of(kind, details))
    except ApprovalError as exc:
        action = {**action, "material": [d.model_dump() for d in details]}
        return action, str(exc)
    return action, None


async def act_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    record = record_for(config)
    await record.control.checkpoint()
    action = dict(state["pending_action"] or {})
    session = await _session(record)
    approval_id = state.get("approval_id")
    needs_approval = bool((action.get("safety") or {}).get("requires_approval"))

    if needs_approval:
        # Defence in depth: never execute a sensitive action without a valid,
        # matching, approved and unused approval - regardless of graph routing.
        if not approval_id:
            return {"pending_action": None, "outcome": "failed", "message": "Sensitive action reached execution without approval."}
        action, error = await _revalidate(record, session, action, approval_id)
        if error:
            await record.log(f"{error} Requesting a fresh approval.", "warning")
            approval_service.supersede(approval_id)
            return {"pending_action": action, "approval_id": None, "reapprovals": (state.get("reapprovals") or 0) + 1, "outcome": "reapprove"}
        if action["safety"]["kind"] == ApprovalKind.DOWNLOAD.value:
            session.allow_dangerous_download_once = True

    await record.set_status(TaskStatus.EXECUTING if needs_approval else TaskStatus.RUNNING)
    obs = _observations.get(record.id)
    label = describe_action(action, obs, mask_text=bool((action.get("safety") or {}).get("secret")))
    step_id = await record.add_action_step(label)
    await record.log(label, "action")
    spec = REGISTRY[action["name"]]
    ctx = ToolContext(record=record, session=session, actions=BrowserActions(session, obs), observation=obs)
    try:
        if action["name"] == "request_human_help":
            p = action["params"]
            await human_handoff(record, str(p.get("kind", "other")),
                                f"I need your help in the browser: {str(p.get('reason', ''))[:300]} Click \"I've completed it\" when done.")
            result = ActionResult(True, "The user completed the requested step.")
        else:
            result = await spec.handler(ctx, action["params"])  # type: ignore[misc]
    except BrowserCrashed as exc:
        result = ActionResult(False, f"Browser crashed: {exc}")
    except (PlaywrightError, ValueError, TypeError, LookupError) as exc:
        result = ActionResult(False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}")

    if needs_approval and approval_id:
        approval_service.consume(approval_id)
        record.approval_id = None
        await record.log("Approved action executed (approval consumed - it cannot be reused).", "success")

    await record.step(step_id, StepStatus.DONE if result.success else StepStatus.ERROR, result.message[:300])
    await record.log(result.message, "success" if result.success else "warning")
    step = (state.get("step_count") or 0) + 1
    record.step_count = step
    history = (state.get("history") or []) + [{"step": step, "label": label, "success": result.success, "result": result.message}]
    failures = 0 if result.success else (state.get("consecutive_failures") or 0) + 1
    return {"pending_action": None, "approval_id": None, "history": history, "step_count": step,
            "consecutive_failures": failures, "outcome": None, "reapprovals": 0 if result.success else state.get("reapprovals") or 0}


def route_after_act(state: AgentState) -> str:
    if state.get("outcome") == "reapprove":
        return "request_approval"
    if state.get("outcome") == "failed":
        return "finalize"
    if (state.get("consecutive_failures") or 0) >= settings.max_consecutive_failures:
        return "finalize"
    return "observe"


async def verify_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    record = record_for(config)
    await record.control.checkpoint()
    final = state.get("final") or {"success": False, "summary": ""}
    await record.set_status(TaskStatus.VERIFYING)
    await record.step("verify", StepStatus.ACTIVE)
    obs_text = "(browser not available)"
    image = None
    session = browser_manager.get(record.id)
    if session and session.alive:
        try:
            obs = await _observe(record, session, want_screenshot=settings.vision_mode != "off")
            _observations[record.id] = obs
            obs_text, image = obs.to_prompt(), obs.screenshot
        except Exception as exc:
            obs_text = f"(could not read final page: {exc})"
    try:
        raw = await llm_service.complete_json(
            VERIFIER_SYSTEM,
            verifier_prompt(task=state["instruction"], understanding=state.get("understanding") or {}, final=final,
                            history=state.get("history") or [], results_summary=results_summary(record), observation_text=obs_text),
            image=image,
            utility=True,
        )
        await record.note_model(*llm_service.last_used())
        verification = Verification(completed=bool(raw.get("completed")), evidence=str(raw.get("evidence") or "")[:800],
                                    missing=(str(raw.get("missing"))[:400] if raw.get("missing") else None))
    except LLMError as exc:
        verification = Verification(completed=False, evidence=f"Verification could not run: {exc}", missing=None)
    record.verification = verification
    await record.step("verify", StepStatus.DONE if verification.completed else StepStatus.ERROR, verification.evidence[:300])
    if final.get("success") and verification.completed:
        outcome = "completed"
    elif final.get("success"):
        outcome = "unverified"
    else:
        outcome = "incomplete"
    return {"verification": verification.model_dump(), "outcome": outcome}


async def finalize_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    record = record_for(config)
    outcome = state.get("outcome")
    final = state.get("final") or {}
    understanding = state.get("understanding") or {}
    approval_service.expire_task(record.id)
    pending = state.get("pending_action") or {}

    if outcome == "refused":
        record.summary = f"I can't help with this request. {understanding.get('refuse_reason') or ''}".strip()
        record.error = {"message": record.summary}
        await record.say(record.summary)
        await record.set_status(TaskStatus.FAILED)
        return {}
    if outcome == "rejected":
        label = describe_action(pending, None, mask_text=True) if pending else "the proposed action"
        record.summary = (f"You rejected \"{label}\", so I stopped before performing it. Nothing was submitted, "
                          "and the browser is left as it was for you to review.")
        record.approval_id = None
        await record.step("verify", StepStatus.SKIPPED, "Stopped after rejection")
        await record.step("complete", StepStatus.DONE, "Stopped at your request")
        await record.say(record.summary)
        await record.set_status(TaskStatus.COMPLETED)
        return {}
    if outcome in ("completed", "unverified", "incomplete"):
        summary = final.get("summary") or "Task finished."
        verification = record.verification
        if outcome == "completed":
            record.summary = summary
            await record.step("complete", StepStatus.DONE, "Verified")
            await record.set_status(TaskStatus.COMPLETED)
        elif outcome == "unverified":
            record.summary = summary + "\n\n⚠ I could not verify this from the page: " + (verification.missing or verification.evidence if verification else "")
            await record.step("complete", StepStatus.ERROR, "Finished but not verified")
            record.can_retry = True
            await record.set_status(TaskStatus.COMPLETED)
        else:
            record.summary = summary or "The task could not be completed."
            record.error = {"message": record.summary}
            record.can_retry = True
            await record.step("complete", StepStatus.ERROR, "Not completed")
            await record.set_status(TaskStatus.FAILED)
        await record.say(record.summary)
        return {}
    # Failure paths (too many failed actions, unapproved sensitive action, ...)
    history = state.get("history") or []
    last = history[-1]["result"] if history else state.get("message") or "Unknown error"
    record.summary = f"I couldn't complete the task. Last problem: {last}"
    record.error = {"message": record.summary}
    record.can_retry = True
    record.approval_id = None
    await record.step("complete", StepStatus.ERROR, "Failed")
    await record.say(record.summary + " You can retry, or give me a different instruction.")
    await record.set_status(TaskStatus.FAILED)
    return {}


def forget(task_id: str) -> None:
    _observations.pop(task_id, None)
    _last_vision_url.pop(task_id, None)
