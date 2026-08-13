"""Read-only filesystem tools implemented without shell commands."""

from __future__ import annotations

import stat
import unicodedata
from pathlib import PurePosixPath
from typing import BinaryIO

from swoon.aeml.models import PathRef, Result, Root, ValidatedAction
from swoon.policy import PathAccess, PathExistence, PathKind, PathPolicy, PathPolicyError

from .errors import ToolExecutionError
from .models import ReadToolLimits
from .output import OutputCollector
from .safe_io import DirectoryEntry, SafeIO


class FilesystemReadTools:
    def __init__(self, policy: PathPolicy, limits: ReadToolLimits) -> None:
        self.policy = policy
        self.limits = limits
        self.io = SafeIO(policy)

    def read_file(self, action: ValidatedAction) -> Result:
        path = self._required_path(action)
        try:
            resolved = self.policy.resolve(
                path,
                access=PathAccess.READ,
                existence=PathExistence.MUST_EXIST,
                kind=PathKind.FILE,
            )
        except PathPolicyError as error:
            raise self._path_error(error) from error

        start_argument = action.argument("start_line")
        end_argument = action.argument("end_line")
        start_line = start_argument if isinstance(start_argument, int) else 1
        end_line = end_argument if isinstance(end_argument, int) else None
        collector = OutputCollector(self.limits.max_output_bytes)
        last_selected: int | None = None

        with self.io.open_file(resolved) as stream:
            file_size = self._stream_size(stream)
            if file_size > self.limits.max_file_bytes and end_line is None:
                raise ToolExecutionError(
                    "tool_failed",
                    f"File exceeds the {self.limits.max_file_bytes}-byte read safety limit",
                )
            self._reject_binary_probe(stream)
            scanned_bytes = 0
            for line_number, raw_line in self._binary_lines(stream):
                scanned_bytes += len(raw_line)
                if scanned_bytes > self.limits.max_scan_bytes:
                    raise ToolExecutionError(
                        "tool_failed",
                        f"File scan exceeds {self.limits.max_scan_bytes} bytes",
                    )
                if end_line is not None and line_number > end_line:
                    break
                text = self._decode_text(raw_line)
                if line_number == 1 and text.startswith("\ufeff"):
                    text = text[1:]
                if line_number < start_line:
                    continue
                collector.add(text)
                last_selected = line_number

        lines = None
        if start_argument is not None or end_argument is not None:
            if end_line is not None:
                lines = f"{start_line}-{end_line}"
            else:
                lines = f"{start_line}-{last_selected or start_line}"
        return collector.result(action.source.id, lines=lines)

    def list_dir(self, action: ValidatedAction) -> Result:
        path = self._required_path(action)
        try:
            resolved = self.policy.resolve(
                path,
                access=PathAccess.READ,
                existence=PathExistence.MUST_EXIST,
                kind=PathKind.DIRECTORY,
            )
        except PathPolicyError as error:
            raise self._path_error(error) from error

        recursive = action.argument("recursive") is True
        pattern_value = action.argument("pattern")
        pattern = pattern_value if isinstance(pattern_value, str) else None
        if pattern is not None:
            self._validate_glob(pattern)

        collector = OutputCollector(self.limits.max_output_bytes)
        base_parts = self._parts(resolved.reference.value)
        scanned = [0]
        with self.io.open_directory(resolved) as directory_fd:
            self._list_directory(
                directory_fd,
                base_parts=base_parts,
                display_parts=(),
                root=resolved.reference.root,
                recursive=recursive,
                pattern=pattern,
                collector=collector,
                scanned=scanned,
            )
        return collector.result(action.source.id)

    def grep(self, action: ValidatedAction) -> Result:
        path = self._required_path(action)
        pattern_value = action.argument("pattern")
        if not isinstance(pattern_value, str) or not pattern_value:
            raise ToolExecutionError("invalid_argument", "grep pattern must be non-empty text")
        if len(pattern_value.encode("utf-8")) > 4_096:
            raise ToolExecutionError("invalid_argument", "grep pattern is too large")

        max_results_value = action.argument("max_results")
        context_value = action.argument("context_lines")
        max_results = max_results_value if isinstance(max_results_value, int) else 100
        context_lines = context_value if isinstance(context_value, int) else 0

        try:
            resolved = self.policy.resolve(
                path,
                access=PathAccess.READ,
                existence=PathExistence.MUST_EXIST,
                kind=PathKind.ANY,
            )
        except PathPolicyError as error:
            raise self._path_error(error) from error

        collector = OutputCollector(self.limits.max_output_bytes)
        remaining = [max_results]
        scanned = [0]
        scanned_bytes = [0]
        file_type = resolved.fingerprints[-1].file_type
        if file_type == stat.S_IFREG:
            with self.io.open_file(resolved) as stream:
                self._grep_stream(
                    stream,
                    display_path=resolved.reference.value,
                    pattern=pattern_value,
                    context_lines=context_lines,
                    remaining=remaining,
                    collector=collector,
                    direct_file=True,
                    scanned_bytes=scanned_bytes,
                )
        elif file_type == stat.S_IFDIR:
            base_parts = self._parts(resolved.reference.value)
            with self.io.open_directory(resolved) as directory_fd:
                self._grep_directory(
                    directory_fd,
                    base_parts=base_parts,
                    display_parts=(),
                    root=resolved.reference.root,
                    pattern=pattern_value,
                    context_lines=context_lines,
                    remaining=remaining,
                    collector=collector,
                    scanned=scanned,
                    scanned_bytes=scanned_bytes,
                )
        else:
            raise ToolExecutionError(
                "unsupported_file_type",
                "grep target is not a file or directory",
            )
        return collector.result(action.source.id)

    def _list_directory(
        self,
        directory_fd: int,
        *,
        base_parts: tuple[str, ...],
        display_parts: tuple[str, ...],
        root: Root,
        recursive: bool,
        pattern: str | None,
        collector: OutputCollector,
        scanned: list[int],
    ) -> None:
        entries = self.io.entries(directory_fd)
        visible_directories: list[tuple[DirectoryEntry, tuple[str, ...]]] = []
        for entry in entries:
            scanned[0] += 1
            if scanned[0] > self.limits.max_walk_entries:
                raise ToolExecutionError(
                    "tool_failed",
                    f"Directory traversal exceeds {self.limits.max_walk_entries} entries",
                )
            relative_parts = display_parts + (entry.name,)
            rooted_parts = base_parts + relative_parts
            if not self._entry_visible(rooted_parts, root):
                continue
            display = "/".join(relative_parts)
            if entry.is_directory:
                if self._matches(display, pattern):
                    collector.add(f"d {display}/\n")
                if recursive:
                    visible_directories.append((entry, relative_parts))
            elif entry.is_file:
                if self.policy.reject_hardlinks and entry.link_count > 1:
                    continue
                if self._matches(display, pattern):
                    collector.add(f"f {display} {entry.size}\n")

        for entry, relative_parts in visible_directories:
            with self.io.open_child_directory(directory_fd, entry) as child_fd:
                self._list_directory(
                    child_fd,
                    base_parts=base_parts,
                    display_parts=relative_parts,
                    root=root,
                    recursive=True,
                    pattern=pattern,
                    collector=collector,
                    scanned=scanned,
                )

    def _grep_directory(
        self,
        directory_fd: int,
        *,
        base_parts: tuple[str, ...],
        display_parts: tuple[str, ...],
        root: Root,
        pattern: str,
        context_lines: int,
        remaining: list[int],
        collector: OutputCollector,
        scanned: list[int],
        scanned_bytes: list[int],
    ) -> None:
        if remaining[0] <= 0:
            return
        directories: list[tuple[DirectoryEntry, tuple[str, ...]]] = []
        for entry in self.io.entries(directory_fd):
            scanned[0] += 1
            if scanned[0] > self.limits.max_walk_entries:
                raise ToolExecutionError(
                    "tool_failed",
                    f"grep traversal exceeds {self.limits.max_walk_entries} entries",
                )
            relative_parts = display_parts + (entry.name,)
            if not self._entry_visible(base_parts + relative_parts, root):
                continue
            if entry.is_directory:
                directories.append((entry, relative_parts))
                continue
            if not entry.is_file or (self.policy.reject_hardlinks and entry.link_count > 1):
                continue
            try:
                with self.io.open_child_file(directory_fd, entry) as stream:
                    self._grep_stream(
                        stream,
                        display_path="/".join(relative_parts),
                        pattern=pattern,
                        context_lines=context_lines,
                        remaining=remaining,
                        collector=collector,
                        direct_file=False,
                        scanned_bytes=scanned_bytes,
                    )
            except ToolExecutionError as error:
                if error.code != "binary_unsupported":
                    raise
            if remaining[0] <= 0:
                return

        for entry, relative_parts in directories:
            with self.io.open_child_directory(directory_fd, entry) as child_fd:
                self._grep_directory(
                    child_fd,
                    base_parts=base_parts,
                    display_parts=relative_parts,
                    root=root,
                    pattern=pattern,
                    context_lines=context_lines,
                    remaining=remaining,
                    collector=collector,
                    scanned=scanned,
                    scanned_bytes=scanned_bytes,
                )
            if remaining[0] <= 0:
                return

    def _grep_stream(
        self,
        stream: BinaryIO,
        *,
        display_path: str,
        pattern: str,
        context_lines: int,
        remaining: list[int],
        collector: OutputCollector,
        direct_file: bool,
        scanned_bytes: list[int],
    ) -> None:
        size = self._stream_size(stream)
        if size > self.limits.max_file_bytes:
            if direct_file:
                raise ToolExecutionError(
                    "tool_failed",
                    f"File exceeds the {self.limits.max_file_bytes}-byte grep safety limit",
                )
            return
        payload = stream.read(self.limits.max_file_bytes + 1)
        scanned_bytes[0] += len(payload)
        if scanned_bytes[0] > self.limits.max_scan_bytes:
            raise ToolExecutionError(
                "tool_failed",
                f"grep scan exceeds {self.limits.max_scan_bytes} bytes",
            )
        if b"\x00" in payload:
            raise ToolExecutionError("binary_unsupported", "Binary files cannot be searched")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError(
                "binary_unsupported",
                "Binary files cannot be searched",
            ) from error

        lines = text.splitlines()
        if any(len(line.encode("utf-8")) > self.limits.max_line_bytes for line in lines):
            if direct_file:
                raise ToolExecutionError("tool_failed", "File contains an excessively long line")
            return
        matches = [index for index, line in enumerate(lines) if pattern in line]
        if not matches or remaining[0] <= 0:
            return
        matches = matches[: remaining[0]]
        remaining[0] -= len(matches)

        selected: set[int] = set()
        for index in matches:
            selected.update(
                range(max(0, index - context_lines), min(len(lines), index + context_lines + 1))
            )
        match_set = set(matches)
        previous: int | None = None
        for index in sorted(selected):
            if previous is not None and index > previous + 1:
                collector.add("--\n")
            separator = ":" if index in match_set else "-"
            collector.add(f"{display_path}{separator}{index + 1}{separator}{lines[index]}\n")
            previous = index

    def _binary_lines(self, stream: BinaryIO):
        line_number = 0
        while True:
            raw_line = stream.readline(self.limits.max_line_bytes + 1)
            if not raw_line:
                break
            if len(raw_line) > self.limits.max_line_bytes and not raw_line.endswith(b"\n"):
                raise ToolExecutionError("tool_failed", "File contains an excessively long line")
            line_number += 1
            yield line_number, raw_line

    @staticmethod
    def _decode_text(payload: bytes) -> str:
        if b"\x00" in payload:
            raise ToolExecutionError("binary_unsupported", "Binary files cannot be read")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError("binary_unsupported", "Binary files cannot be read") from error

    @staticmethod
    def _stream_size(stream: BinaryIO) -> int:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        return size

    @staticmethod
    def _reject_binary_probe(stream: BinaryIO) -> None:
        stream.seek(0)
        probe = stream.read(8192)
        stream.seek(0)
        if b"\x00" in probe:
            raise ToolExecutionError("binary_unsupported", "Binary files cannot be read")

    @staticmethod
    def _parts(value: str) -> tuple[str, ...]:
        return () if value == "." else tuple(value.split("/"))

    def _entry_visible(self, parts: tuple[str, ...], root: Root) -> bool:
        try:
            return not self.policy.is_denied(PathRef("/".join(parts), root))
        except PathPolicyError:
            return False

    @staticmethod
    def _required_path(action: ValidatedAction):
        path = action.source.path
        if path is None:
            raise ToolExecutionError("missing_path", "Tool requires a path")
        return path

    @staticmethod
    def _path_error(error: PathPolicyError) -> ToolExecutionError:
        return ToolExecutionError(
            error.code,
            str(error),
            retryable=error.code in {"path_changed", "path_unavailable"},
        )

    @staticmethod
    def _validate_glob(pattern: str) -> None:
        if not pattern or len(pattern.encode("utf-8")) > 512:
            raise ToolExecutionError("invalid_argument", "Glob pattern has an invalid size")
        if pattern.startswith(("/", "\\")) or "\\" in pattern or "\x00" in pattern:
            raise ToolExecutionError("invalid_argument", "Glob pattern must be root-relative")
        if any(part == ".." for part in pattern.split("/")):
            raise ToolExecutionError("invalid_argument", "Glob pattern cannot traverse parents")
        if any(unicodedata.category(character).startswith("C") for character in pattern):
            raise ToolExecutionError("invalid_argument", "Glob pattern contains control characters")

    @staticmethod
    def _matches(path: str, pattern: str | None) -> bool:
        return pattern is None or PurePosixPath(path).match(pattern)
