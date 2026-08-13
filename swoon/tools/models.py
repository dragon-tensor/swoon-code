"""Shared settings for bounded read-only execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReadToolLimits:
    max_output_bytes: int = 64 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    max_manifest_bytes: int = 2 * 1024 * 1024
    max_line_bytes: int = 1024 * 1024
    max_walk_entries: int = 100_000
    max_scan_bytes: int = 256 * 1024 * 1024
    max_git_snapshot_bytes: int = 512 * 1024 * 1024
    max_git_capture_bytes: int = 4 * 1024 * 1024
    git_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        values = (
            self.max_output_bytes,
            self.max_file_bytes,
            self.max_manifest_bytes,
            self.max_line_bytes,
            self.max_walk_entries,
            self.max_scan_bytes,
            self.max_git_snapshot_bytes,
            self.max_git_capture_bytes,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("Read-tool limits must be positive integers")
        if self.git_timeout_seconds <= 0:
            raise ValueError("Git timeout must be positive")
