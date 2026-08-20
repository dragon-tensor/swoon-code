"""UTF-8-safe reactive output truncation."""

from __future__ import annotations

from dataclasses import dataclass, field

from swoon.aeml.models import Result, ResultStatus, Truncation


@dataclass(slots=True)
class OutputCollector:
    max_bytes: int
    _preview: bytearray = field(default_factory=bytearray, init=False)
    _total_bytes: int = 0

    def __post_init__(self) -> None:
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise ValueError("max_bytes must be positive")

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def truncated(self) -> bool:
        return self._total_bytes > len(self._preview)

    def add(self, text: str) -> None:
        encoded = text.encode("utf-8")
        self._total_bytes += len(encoded)
        remaining = self.max_bytes - len(self._preview)
        if remaining > 0:
            self._preview.extend(encoded[:remaining])

    def text(self) -> str:
        payload = bytes(self._preview)
        while payload:
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError as error:
                if error.reason == "unexpected end of data":
                    payload = payload[: error.start]
                    continue
                raise
        return ""

    def result(self, action_id: str, *, lines: str | None = None) -> Result:
        truncation = None
        status = ResultStatus.SUCCESS
        if self.truncated:
            status = ResultStatus.PARTIAL
            truncation = Truncation(total_bytes=self.total_bytes, offset=0)
        return Result(
            action_id=action_id,
            status=status,
            body=self.text(),
            lines=lines,
            truncation=truncation,
        )


def bounded_result(
    action_id: str,
    text: str,
    *,
    max_bytes: int,
    lines: str | None = None,
) -> Result:
    collector = OutputCollector(max_bytes)
    collector.add(text)
    return collector.result(action_id, lines=lines)
