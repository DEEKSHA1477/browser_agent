"""Pydantic models describing tasks, their status and the REST payloads."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    QUEUED = "queued"
    UNDERSTANDING = "understanding"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_HUMAN_ACTION = "awaiting_human_action"  # CAPTCHA / login the user must handle
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"  # running an action the user just approved
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


class StepStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    WAITING = "waiting"
    ERROR = "error"
    SKIPPED = "skipped"


class TimelineStep(BaseModel):
    id: str
    label: str
    kind: Literal["stage", "action"] = "stage"
    status: StepStatus = StepStatus.PENDING
    detail: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class LogEntry(BaseModel):
    ts: float
    level: Literal["info", "action", "warning", "error", "success"] = "info"
    message: str


class ChatMessage(BaseModel):
    ts: float
    role: Literal["user", "agent", "system"]
    text: str


class ResultItem(BaseModel):
    """One extracted record (product, flight, job, video...).

    Values come from the live page. Items whose key values cannot be found in the
    page text are flagged ``verified=False`` and never presented as facts.
    """

    id: str
    fields: dict[str, str]
    url: str | None = None
    verified: bool = True
    tags: list[str] = Field(default_factory=list)


class ResultCollection(BaseModel):
    id: str
    title: str
    source_url: str | None = None
    source_title: str | None = None
    columns: list[str]
    items: list[ResultItem]
    recommendation_id: str | None = None
    recommendation_reason: str | None = None
    dropped_unverified: int = 0
    extracted_at: float


class HumanActionRequest(BaseModel):
    kind: Literal["captcha", "login", "blocked", "other"]
    message: str
    url: str | None = None


class Verification(BaseModel):
    completed: bool
    evidence: str
    missing: str | None = None


# --- REST payloads -----------------------------------------------------------------


class CreateTaskRequest(BaseModel):
    """Either a free-text ``instruction`` or a launcher ``capability`` + its form ``details``."""

    instruction: str | None = Field(default=None, max_length=1000)
    capability: str | None = Field(default=None, max_length=40)
    details: dict[str, str] | None = Field(default=None, max_length=20)


class SelectRequest(BaseModel):
    item_id: str = Field(min_length=1, max_length=40)


class ClarifyRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=1000)


class TaskCreatedResponse(BaseModel):
    task_id: str
    snapshot: dict[str, Any]
