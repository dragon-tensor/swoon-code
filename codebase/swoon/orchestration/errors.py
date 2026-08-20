"""Stable failures raised by the autonomous orchestration boundary."""

from __future__ import annotations


class OrchestrationError(Exception):
    """An orchestration invariant failed before a normal run stop."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
