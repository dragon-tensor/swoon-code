"""Public result and limit models for bounded AEML orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from swoon.aeml.models import ProtocolError
from swoon.session.models import Session


class RunStopReason(str, Enum):
    """Why control returned from an otherwise successful orchestration run."""

    COMPLETED = "completed"
    DONE = "done"
    AWAITING_USER = "awaiting_user"
    STEP_LIMIT = "step_limit"
    ABORTED = "aborted"
    PROTOCOL_ERROR = "protocol_error"


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
