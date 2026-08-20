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
from .workspace import (
    WorkspaceSessionManager,
    default_work_directory,
    session_id_for_workspace,
    validate_workspace_name,
    workspace_name_for_session,
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
    "WorkspaceSessionManager",
    "default_session_directory",
    "default_work_directory",
    "session_id_for_workspace",
    "validate_workspace_name",
    "workspace_name_for_session",
]
