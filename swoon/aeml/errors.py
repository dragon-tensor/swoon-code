"""Structured failures raised by the AEML protocol boundary."""

from __future__ import annotations


class AEMLError(Exception):
    """Base class carrying a stable machine-readable error code."""

    def __init__(self, code: str, message: str, *, action_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.action_id = action_id


class AEMLParseError(AEMLError):
    """The assistant response is not structurally valid AEML."""

    def __init__(self, message: str) -> None:
        super().__init__("parse_error", message)


class AEMLTruncatedError(AEMLError):
    """An AEML envelope began but did not close."""

    def __init__(self, message: str = "AEML response ended before </aeml>") -> None:
        super().__init__("likely_truncated_by_message_limit", message)


class AEMLValidationError(AEMLError):
    """The parsed message violates the AEML protocol or a tool schema."""

    def __init__(self, code: str, message: str, *, action_id: str | None = None) -> None:
        super().__init__(code, message, action_id=action_id)
