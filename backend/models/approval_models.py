"""Models for human-in-the-loop approval checkpoints."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ApprovalKind(str, Enum):
    FLIGHT_BOOKING = "flight_booking"
    PAYMENT = "payment"
    ORDER = "order"
    JOB_APPLICATION = "job_application"
    MESSAGE = "message"
    CANCELLATION = "cancellation"
    DELETION = "deletion"
    ACCOUNT_CHANGE = "account_change"
    SENSITIVE_FORM = "sensitive_form"
    DOWNLOAD = "download"
    OTHER = "other"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"  # details changed -> a fresh approval replaced it
    CONSUMED = "consumed"  # approved and the action was executed (single use)
    EXPIRED = "expired"


class ApprovalDetail(BaseModel):
    label: str
    value: str


class Approval(BaseModel):
    id: str
    task_id: str
    kind: ApprovalKind
    title: str
    description: str
    details: list[ApprovalDetail]
    fingerprint: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float
    decided_at: float | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)  # "policy" and/or "agent": who flagged it
    action_summary: str = ""
    requires_payment_confirmation: bool = False


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    # The client echoes back the fingerprint of the card it displayed. If the
    # proposed action changed in the meantime, the decision is refused.
    fingerprint: str = Field(min_length=16, max_length=128)
    reason: str | None = Field(default=None, max_length=500)
    # Payments need a second, explicit confirmation in addition to "approve".
    payment_confirmation: bool = False
