"""Stable path-policy failures returned before tool execution."""

from __future__ import annotations


class PathPolicyError(Exception):
    def __init__(self, code: str, message: str, *, virtual_path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.virtual_path = virtual_path
