"""Shared settings for bounded tool execution."""

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


@dataclass(frozen=True, slots=True)
class MutationToolLimits:
    """Hard bounds for interpreter-side filesystem mutations."""

    max_content_bytes: int = 512 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    max_copy_entries: int = 100_000
    max_copy_bytes: int = 512 * 1024 * 1024
    max_lifecycle_depth: int = 256

    def __post_init__(self) -> None:
        values = (
            self.max_content_bytes,
            self.max_file_bytes,
            self.max_copy_entries,
            self.max_copy_bytes,
            self.max_lifecycle_depth,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("Mutation-tool limits must be positive integers")
        if self.max_content_bytes > self.max_file_bytes:
            raise ValueError("max_content_bytes cannot exceed max_file_bytes")


@dataclass(frozen=True, slots=True)
class CommandToolLimits:
    """Hard bounds for disposable foreground command sandboxes."""

    default_timeout_seconds: int = 30
    managed_timeout_seconds: int = 120
    max_capture_bytes: int = 8 * 1024 * 1024
    max_result_bytes: int = 64 * 1024
    default_output_lines: int = 1_000
    max_command_bytes: int = 16 * 1024
    max_argument_bytes: int = 8 * 1024
    max_arguments: int = 256
    max_snapshot_entries: int = 100_000
    max_snapshot_bytes: int = 512 * 1024 * 1024
    max_snapshot_file_bytes: int = 64 * 1024 * 1024
    workspace_bytes: int = 512 * 1024 * 1024
    temporary_bytes: int = 256 * 1024 * 1024
    address_space_bytes: int = 2 * 1024 * 1024 * 1024
    max_file_bytes: int = 256 * 1024 * 1024
    max_processes: int = 256
    max_open_files: int = 256
    background_max_runtime_seconds: int = 3_600
    background_startup_timeout_seconds: int = 30
    background_default_output_lines: int = 10_000
    max_background_processes: int = 8
    max_background_records: int = 128

    def __post_init__(self) -> None:
        values = (
            self.default_timeout_seconds,
            self.managed_timeout_seconds,
            self.max_capture_bytes,
            self.max_result_bytes,
            self.default_output_lines,
            self.max_command_bytes,
            self.max_argument_bytes,
            self.max_arguments,
            self.max_snapshot_entries,
            self.max_snapshot_bytes,
            self.max_snapshot_file_bytes,
            self.workspace_bytes,
            self.temporary_bytes,
            self.address_space_bytes,
            self.max_file_bytes,
            self.max_processes,
            self.max_open_files,
            self.background_max_runtime_seconds,
            self.background_startup_timeout_seconds,
            self.background_default_output_lines,
            self.max_background_processes,
            self.max_background_records,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("Command-tool limits must be positive integers")
        if self.max_result_bytes > self.max_capture_bytes:
            raise ValueError("max_result_bytes cannot exceed max_capture_bytes")
        if self.max_snapshot_file_bytes > self.max_snapshot_bytes:
            raise ValueError("max_snapshot_file_bytes cannot exceed max_snapshot_bytes")
        if self.max_file_bytes > self.workspace_bytes:
            raise ValueError("max_file_bytes cannot exceed workspace_bytes")
        if self.background_default_output_lines > 100_000:
            raise ValueError("background_default_output_lines cannot exceed 100000")
        if self.max_background_processes > self.max_background_records:
            raise ValueError("max_background_processes cannot exceed max_background_records")
