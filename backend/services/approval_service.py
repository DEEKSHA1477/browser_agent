"""Centralised human-in-the-loop approval checkpoints (in memory).

Guarantees enforced here, in the backend:
  * An approval is bound to one task and to a *fingerprint* - a hash of every
    material detail of the proposed action (kind, target, destination,
    values typed, amounts visible near the target, agent-stated details).
  * The user's decision must echo the fingerprint of the card they saw; a stale
    card cannot approve a changed action.
  * Immediately before execution the executor re-derives the fingerprint from
    the live page and calls ``require_approved``. Any difference means the
    action changed -> the old approval is superseded and a fresh one is needed.
  * Approvals are single-use (``consume``).
  * Payments additionally require an explicit ``payment_confirmation``.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from models.approval_models import Approval, ApprovalDetail, ApprovalKind, ApprovalStatus


class ApprovalError(Exception):
    pass


def fingerprint_of(kind: ApprovalKind, details: list[ApprovalDetail]) -> str:
    payload = json.dumps(
        {"kind": kind.value, "details": [[d.label, d.value] for d in details]},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ApprovalService:
    def __init__(self) -> None:
        self._approvals: dict[str, Approval] = {}

    def get(self, approval_id: str | None) -> Approval | None:
        return self._approvals.get(approval_id) if approval_id else None

    def for_task(self, task_id: str) -> list[Approval]:
        return sorted((a for a in self._approvals.values() if a.task_id == task_id), key=lambda a: a.created_at)

    def create(
        self,
        *,
        task_id: str,
        kind: ApprovalKind,
        title: str,
        description: str,
        details: list[ApprovalDetail],
        action_summary: str,
        sources: list[str],
        warnings: list[str] | None = None,
    ) -> Approval:
        fp = fingerprint_of(kind, details)
        # Idempotent: the same pending request is reused (LangGraph re-runs a
        # node from the top when resuming an interrupt).
        for existing in self._approvals.values():
            if existing.task_id == task_id and existing.fingerprint == fp and existing.status in (
                ApprovalStatus.PENDING,
                ApprovalStatus.APPROVED,
            ):
                return existing
        # Any other pending request for this task is now out of date.
        for existing in self._approvals.values():
            if existing.task_id == task_id and existing.status == ApprovalStatus.PENDING:
                existing.status = ApprovalStatus.SUPERSEDED
                existing.decided_at = time.time()
        approval = Approval(
            id=uuid.uuid4().hex[:12],
            task_id=task_id,
            kind=kind,
            title=title,
            description=description,
            details=details,
            fingerprint=fp,
            created_at=time.time(),
            warnings=warnings or [],
            sources=sources,
            action_summary=action_summary,
            requires_payment_confirmation=kind == ApprovalKind.PAYMENT,
        )
        self._approvals[approval.id] = approval
        return approval

    def decide(
        self,
        approval_id: str,
        *,
        decision: str,
        fingerprint: str,
        payment_confirmation: bool = False,
        reason: str | None = None,
    ) -> Approval:
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise ApprovalError("Approval request not found.")
        if approval.status != ApprovalStatus.PENDING:
            raise ApprovalError(f"This approval request is no longer pending (status: {approval.status.value}).")
        if fingerprint != approval.fingerprint:
            raise ApprovalError("The proposed action changed since this card was shown. Review the updated request.")
        if decision == "approve":
            if approval.kind == ApprovalKind.PAYMENT and not payment_confirmation:
                raise ApprovalError("Payments require the separate explicit payment confirmation.")
            approval.status = ApprovalStatus.APPROVED
        elif decision == "reject":
            approval.status = ApprovalStatus.REJECTED
        else:
            raise ApprovalError("Decision must be 'approve' or 'reject'.")
        approval.decided_at = time.time()
        approval.reason = reason
        return approval

    def require_approved(self, approval_id: str, *, task_id: str, fingerprint: str) -> Approval:
        """Final gate called by the executor right before a sensitive action."""
        approval = self._approvals.get(approval_id)
        if approval is None or approval.task_id != task_id:
            raise ApprovalError("No approval exists for this action.")
        if approval.status != ApprovalStatus.APPROVED:
            raise ApprovalError(f"Action is not approved (status: {approval.status.value}).")
        if approval.fingerprint != fingerprint:
            approval.status = ApprovalStatus.SUPERSEDED
            approval.decided_at = time.time()
            raise ApprovalError("The action's details changed after approval; a fresh approval is required.")
        return approval

    def consume(self, approval_id: str) -> None:
        approval = self._approvals.get(approval_id)
        if approval and approval.status == ApprovalStatus.APPROVED:
            approval.status = ApprovalStatus.CONSUMED

    def supersede(self, approval_id: str) -> None:
        approval = self._approvals.get(approval_id)
        if approval and approval.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
            approval.status = ApprovalStatus.SUPERSEDED
            approval.decided_at = time.time()

    def expire_task(self, task_id: str) -> None:
        for approval in self._approvals.values():
            if approval.task_id == task_id and approval.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
                approval.status = ApprovalStatus.EXPIRED
                approval.decided_at = time.time()


approval_service = ApprovalService()
