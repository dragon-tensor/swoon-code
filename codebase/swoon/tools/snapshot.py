"""Safe disposable snapshots used by fixed read-only Git commands."""

from __future__ import annotations

import os
from pathlib import Path

from swoon.policy import PathAccess, PathExistence, PathKind, PathPolicy, PathPolicyError
from swoon.aeml.models import PathRef, Root

from .errors import ToolExecutionError
from .models import ReadToolLimits
from .safe_io import DirectoryEntry, SafeIO


_UNSAFE_GIT_METADATA = {
    (".git", "commondir"),
    (".git", "gitdir"),
    (".git", "objects", "info", "alternates"),
}


class GitSnapshotBuilder:
    def __init__(self, policy: PathPolicy, limits: ReadToolLimits) -> None:
        self.policy = policy
        self.limits = limits
        self.io = SafeIO(policy)

    def build(self, destination: Path) -> None:
        try:
            resolved = self.policy.resolve(
                PathRef(".", Root.OUTPUT),
                access=PathAccess.READ,
                existence=PathExistence.MUST_EXIST,
                kind=PathKind.DIRECTORY,
            )
        except PathPolicyError as error:
            raise ToolExecutionError(error.code, str(error)) from error

        counters = {"entries": 0, "bytes": 0}
        with self.io.open_directory(resolved) as root_fd:
            self._copy_directory(root_fd, destination, (), counters)

        git_directory = destination / ".git"
        if not git_directory.is_dir() or git_directory.is_symlink():
            raise ToolExecutionError("not_repository", "Output root is not a Git repository")

    def _copy_directory(
        self,
        source_fd: int,
        destination: Path,
        relative_parts: tuple[str, ...],
        counters: dict[str, int],
    ) -> None:
        for entry in self.io.entries(source_fd):
            counters["entries"] += 1
            if counters["entries"] > self.limits.max_walk_entries:
                raise ToolExecutionError(
                    "tool_failed",
                    f"Git snapshot exceeds {self.limits.max_walk_entries} entries",
                )
            child_parts = relative_parts + (entry.name,)
            if tuple(part.casefold() for part in child_parts) in _UNSAFE_GIT_METADATA:
                raise ToolExecutionError(
                    "path_escape",
                    "Repository uses external Git metadata",
                )
            try:
                hidden = self.policy.is_denied(PathRef("/".join(child_parts), Root.OUTPUT))
            except PathPolicyError:
                hidden = True
            if hidden:
                continue
            destination_path = destination / entry.name
            if entry.is_symlink:
                raise ToolExecutionError("path_escape", "Git snapshot contains a symbolic link")
            if entry.is_directory:
                destination_path.mkdir(mode=0o700)
                with self.io.open_child_directory(source_fd, entry) as child_fd:
                    self._copy_directory(child_fd, destination_path, child_parts, counters)
                continue
            if not entry.is_file:
                raise ToolExecutionError(
                    "unsupported_file_type",
                    "Git snapshot contains a special file",
                )
            if self.policy.reject_hardlinks and entry.link_count > 1:
                raise ToolExecutionError("path_escape", "Git snapshot contains a hard-linked file")
            if entry.size > self.limits.max_file_bytes:
                raise ToolExecutionError("tool_failed", "Git snapshot contains an oversized file")
            if counters["bytes"] + entry.size > self.limits.max_git_snapshot_bytes:
                raise ToolExecutionError(
                    "tool_failed",
                    f"Git snapshot exceeds {self.limits.max_git_snapshot_bytes} bytes",
                )
            with self.io.open_child_file(source_fd, entry) as source:
                copied = self._copy_file(
                    source,
                    destination_path,
                    executable=bool(entry.mode & 0o111),
                    maximum=self.limits.max_file_bytes,
                )
            counters["bytes"] += copied
            if counters["bytes"] > self.limits.max_git_snapshot_bytes:
                raise ToolExecutionError(
                    "tool_failed",
                    f"Git snapshot exceeds {self.limits.max_git_snapshot_bytes} bytes",
                )

    @staticmethod
    def _copy_file(source, destination: Path, *, executable: bool, maximum: int) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(destination, flags, 0o700 if executable else 0o600)
        copied = 0
        try:
            with os.fdopen(os.dup(descriptor), "wb") as target:
                while block := source.read(1024 * 1024):
                    copied += len(block)
                    if copied > maximum:
                        raise ToolExecutionError("tool_failed", "Git snapshot file grew too large")
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            os.fchmod(descriptor, 0o700 if executable else 0o600)
        finally:
            os.close(descriptor)
        return copied
