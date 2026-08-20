"""Structured failures for persistent Swoon sessions."""

from __future__ import annotations


class SessionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SessionNotFoundError(SessionError):
    def __init__(self, session_id: str) -> None:
        super().__init__("session_not_found", f"Session {session_id!r} does not exist")


class SessionConflictError(SessionError):
    def __init__(self, session_id: str) -> None:
        super().__init__(
            "session_conflict",
            f"Session {session_id!r} changed on disk; reload it before updating",
        )


class SessionImportError(SessionError):
    def __init__(self, message: str) -> None:
        super().__init__("project_import_failed", message)


class StepLimitReachedError(SessionError):
    def __init__(self, session_id: str, max_steps: int) -> None:
        super().__init__(
            "step_limit_reached",
            f"Session {session_id!r} reached its {max_steps}-step limit",
        )
