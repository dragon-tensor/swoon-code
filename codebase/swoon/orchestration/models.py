"""Public result and limit models for bounded AEML orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from swoon.aeml.models import ProtocolError, ResultStatus, ToolEffect
from swoon.session.models import Session


class RunStopReason(str, Enum):
    """Why control returned from an otherwise successful orchestration run."""

    COMPLETED = "completed"
    DONE = "done"
    AWAITING_USER = "awaiting_user"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    STEP_LIMIT = "step_limit"
    ABORTED = "aborted"
    PROTOCOL_ERROR = "protocol_error"


class OrchestrationEventKind(str, Enum):
    """Progress information that a presentation layer may render or ignore."""

    PLAN = "plan"
    ACTION_PENDING = "action_pending"
    ACTION_START = "action_start"
    ACTION_RESULT = "action_result"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class OrchestrationEvent:
    """A concise public event; never contains private AEML thought text."""

    kind: OrchestrationEventKind
    text: str
    action_id: str | None = None
    tool: str | None = None
    effect: ToolEffect | None = None
    status: ResultStatus | None = None


@dataclass(frozen=True, slots=True)
class OrchestrationLimits:
    """Hard retry limits separate from the session's protocol-turn budget."""

    max_protocol_retries: int = 2

    def __post_init__(self) -> None:
        if (
            type(self.max_protocol_retries) is not int
            or not 0 <= self.max_protocol_retries <= 10
        ):
            raise ValueError("max_protocol_retries must be between 0 and 10")


@dataclass(frozen=True, slots=True)
class RunResult:
    """A bounded run's updated session and human-facing outcome."""

    session: Session
    reason: RunStopReason
    updates: tuple[str, ...] = ()
    question: str | None = None
    summary: str | None = None
    error: ProtocolError | None = None
    last_turn: int | None = None
