"""Approval endpoints. Decisions are validated server-side (fingerprint,
pending status, payment confirmation) before the paused agent is resumed."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from agent.state import task_store
from agent.workflow import runner
from models.approval_models import ApprovalDecisionRequest
from services.approval_service import ApprovalError, approval_service

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.get("")
async def list_approvals(task_id: str) -> list[dict]:
    return [a.model_dump() for a in approval_service.for_task(task_id)]


@router.get("/{approval_id}")
async def get_approval(approval_id: str) -> dict:
    approval = approval_service.get(approval_id)
    if approval is None:
        raise HTTPException(404, "Approval not found")
    return approval.model_dump()


@router.post("/{approval_id}/decision")
async def decide(approval_id: str, body: ApprovalDecisionRequest) -> dict:
    approval = approval_service.get(approval_id)
    if approval is None:
        raise HTTPException(404, "Approval not found")
    record = task_store.get(approval.task_id)
    if record is None:
        raise HTTPException(404, "Task not found")
    await runner.wait_until_suspended(record.id)
    if runner.is_running(record.id) or await runner.waiting_for_input(record.id) != "approval":
        raise HTTPException(409, "The agent is not waiting for this approval.")
    try:
        decided = approval_service.decide(
            approval_id,
            decision=body.decision,
            fingerprint=body.fingerprint,
            payment_confirmation=body.payment_confirmation,
            reason=body.reason,
        )
    except ApprovalError as exc:
        raise HTTPException(409, str(exc)) from exc
    await record.say(("Approved: " if decided.status.value == "approved" else "Rejected: ") + decided.action_summary, role="user")
    runner.resume(record, {"approval_id": approval_id})
    return decided.model_dump()
