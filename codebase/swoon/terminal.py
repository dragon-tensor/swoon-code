"""Minimal ANSI presentation for the interactive terminal agent."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import TextIO

from .aeml.models import ResultStatus


_RESET = "\033[0m"
_BRIGHT = "\033[1;97m"
_DIM = "\033[2;90m"
_PROCESS = "\033[1;90m"
_RED = "\033[1;31m"
_GREEN = "\033[1;32m"
_YELLOW = "\033[1;33m"


class TerminalUI:
    """Render a small semantic hierarchy without owning application behavior."""

    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        color: bool | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        selected_environment = os.environ if environment is None else environment
        self.color = (
            self._supports_color(self.stdout, selected_environment)
            if color is None
            else color
        )

    def agent(self, message: str) -> None:
        """Print a direct, high-contrast message from the coding agent."""

        self._write("[swoon-code] ", message, _BRIGHT)

    def plan(self, message: str) -> None:
        """Print an explicit model plan as lower-contrast passive context."""

        self._write("-->> [plan] ", message, _DIM)

    def status(self, message: str) -> None:
        """Print passive host status without competing with agent speech."""

        self._write("-->> ", message, _DIM)

    def process(self, message: str, *, powerful: bool = False) -> None:
        """Print a tool/process invocation, highlighting impactful operations."""

        self._write(">> ", message, _RED if powerful else _PROCESS)

    def result(self, message: str, status: ResultStatus) -> None:
        """Print a compact, color-coded tool result on the chronological stream."""

        if status is ResultStatus.SUCCESS:
            label, style = "[success] ", _GREEN
        elif status is ResultStatus.FAILURE:
            label, style = "[error] ", _RED
        else:
            label, style = f"[{status.value}] ", _YELLOW
        self._write(f">> {label}", message, style)

    def warning(self, message: str, *, stderr: bool = False) -> None:
        stream = self.stderr if stderr else self.stdout
        self._write("-->> [warning] ", message, _YELLOW, stream=stream)

    def error(self, message: str) -> None:
        self._write("[swoon-code] ", message, _RED, stream=self.stderr)

    def success(self, message: str) -> None:
        self._write("[swoon-code] ", message, _GREEN)

    def prompt(self, message: str | None = None) -> str:
        """Return the stable user prompt, optionally followed by a question."""

        suffix = "" if message is None else f" {message}"
        return self._styled(f"[user@swoon-code]{suffix} ", _BRIGHT)

    def _write(
        self,
        prefix: str,
        message: str,
        style: str,
        *,
        stream: TextIO | None = None,
    ) -> None:
        selected = self.stdout if stream is None else stream
        lines = str(message).splitlines() or [""]
        continuation = " " * len(prefix)
        rendered = "\n".join(
            f"{prefix if index == 0 else continuation}{line}"
            for index, line in enumerate(lines)
        )
        print(self._styled(rendered, style), file=selected, flush=True)

    def _styled(self, value: str, style: str) -> str:
        if not self.color:
            return value
        return f"{style}{value}{_RESET}"

    @staticmethod
    def _supports_color(stream: TextIO, environment: Mapping[str, str]) -> bool:
        if "NO_COLOR" in environment or environment.get("TERM") == "dumb":
            return False
        try:
            return bool(stream.isatty())
        except (AttributeError, OSError):
            return False
