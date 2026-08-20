"""Filtered, bounded snapshots for disposable command sandboxes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from swoon.aeml.models import PathRef, Root
from swoon.policy import PathAccess, PathExistence, PathKind, PathPolicy, PathPolicyError

from .errors import ToolExecutionError
from .models import CommandToolLimits
from .safe_io import SafeIO


@dataclass(frozen=True, slots=True)
class CommandSnapshotStats:
    entries: int
    bytes: int
    denied_entries: int


class CommandSnapshotBuilder:
    """Copy both virtual roots without links, special files, or denied paths."""

    def __init__(self, policy: PathPolicy, limits: CommandToolLimits) -> None:
        self.policy = policy
        self.limits = limits
        self.io = SafeIO(policy)

    def build(self, output_destination: Path, input_destination: Path) -> CommandSnapshotStats:
        counters = {"entries": 0, "bytes": 0, "denied": 0}
        self._build_root(Root.OUTPUT, output_destination, counters)
        self._build_root(Root.INPUT, input_destination, counters)
        return CommandSnapshotStats(
            entries=counters["entries"],
            bytes=counters["bytes"],
            denied_entries=counters["denied"],
        )

    def _build_root(
        self,
        root: Root,
        destination: Path,
        counters: dict[str, int],
    ) -> None:
        try:
            resolved = self.policy.resolve(
                PathRef(".", root),
                access=PathAccess.READ,
                existence=PathExistence.MUST_EXIST,
                kind=PathKind.DIRECTORY,
            )
        except PathPolicyError as error:
            raise ToolExecutionError(error.code, str(error)) from error

        with self.io.open_directory(resolved) as root_fd:
            self._copy_directory(root_fd, destination, root, (), counters)

    def _copy_directory(
        self,
        source_fd: int,
        destination: Path,
        root: Root,
        relative_parts: tuple[str, ...],
        counters: dict[str, int],
    ) -> None:
        for entry in self.io.entries(source_fd):
            child_parts = relative_parts + (entry.name,)
            try:
                denied = self.policy.is_denied(PathRef("/".join(child_parts), root))
            except PathPolicyError as error:
                raise ToolExecutionError(error.code, str(error)) from error
            if denied:
                counters["denied"] += 1
                continue

            counters["entries"] += 1
            if counters["entries"] > self.limits.max_snapshot_entries:
                raise ToolExecutionError(
                    "snapshot_limit_exceeded",
                    f"Command snapshot exceeds {self.limits.max_snapshot_entries} entries",
                )

            destination_path = destination / entry.name
            if entry.is_symlink:
                raise ToolExecutionError(
                    "path_escape",
                    "Command snapshot contains a symbolic link",
                )
            if entry.is_directory:
                destination_path.mkdir(mode=0o700)
                with self.io.open_child_directory(source_fd, entry) as child_fd:
                    self._copy_directory(
                        child_fd,
                        destination_path,
                        root,
                        child_parts,
                        counters,
                    )
                continue
            if not entry.is_file:
                raise ToolExecutionError(
                    "unsupported_file_type",
                    "Command snapshot contains a special file",
                )
            if self.policy.reject_hardlinks and entry.link_count > 1:
                raise ToolExecutionError(
                    "path_escape",
                    "Command snapshot contains a hard-linked file",
                )
            if entry.size > self.limits.max_snapshot_file_bytes:
                raise ToolExecutionError(
                    "snapshot_limit_exceeded",
                    "Command snapshot contains an oversized file",
                )
            if counters["bytes"] + entry.size > self.limits.max_snapshot_bytes:
                raise ToolExecutionError(
                    "snapshot_limit_exceeded",
                    f"Command snapshot exceeds {self.limits.max_snapshot_bytes} bytes",
                )

            with self.io.open_child_file(source_fd, entry) as source:
                copied = self._copy_file(
                    source,
                    destination_path,
                    executable=bool(entry.mode & 0o111),
                )
            counters["bytes"] += copied
            if counters["bytes"] > self.limits.max_snapshot_bytes:
                raise ToolExecutionError(
                    "snapshot_limit_exceeded",
                    f"Command snapshot exceeds {self.limits.max_snapshot_bytes} bytes",
                )

    def _copy_file(self, source, destination: Path, *, executable: bool) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(destination, flags, 0o700 if executable else 0o600)
        copied = 0
        try:
            with os.fdopen(os.dup(descriptor), "wb") as target:
                while block := source.read(1024 * 1024):
                    copied += len(block)
                    if copied > self.limits.max_snapshot_file_bytes:
                        raise ToolExecutionError(
                            "snapshot_limit_exceeded",
                            "Command snapshot file grew beyond its size limit",
                        )
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            os.fchmod(descriptor, 0o700 if executable else 0o600)
        finally:
            os.close(descriptor)
        return copied
