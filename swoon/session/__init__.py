"""Session lifecycle and persistent state management."""

from .errors import (
    SessionConflictError,
    SessionError,
    SessionImportError,
    SessionNotFoundError,
    StepLimitReachedError,
)
from .manager import DEFAULT_MAX_STEPS, SessionManager, default_session_directory
from .models import (
    ActionRecord,
    ChunkRecord,
    ImportLimits,
    PendingConfirmation,
    ProcessRecord,
    ProcessStatus,
    ProcessTerminationReason,
    Session,
    SessionPaths,
    SessionState,
    SessionStatus,
)

__all__ = [
    "ActionRecord",
    "ChunkRecord",
    "DEFAULT_MAX_STEPS",
    "ImportLimits",
    "PendingConfirmation",
    "ProcessRecord",
    "ProcessStatus",
    "ProcessTerminationReason",
    "Session",
    "SessionConflictError",
    "SessionError",
    "SessionImportError",
    "SessionManager",
    "SessionNotFoundError",
    "SessionPaths",
    "SessionState",
    "SessionStatus",
    "StepLimitReachedError",
    "default_session_directory",
]
