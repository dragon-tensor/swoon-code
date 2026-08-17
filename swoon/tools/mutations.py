"""Output-only filesystem mutation handlers for validated AEML actions."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from typing import BinaryIO, Iterator

from swoon.aeml.models import PathRef, Result, ResultStatus, Root, ValidatedAction
from swoon.policy import PathAccess, PathExistence, PathKind, PathPolicy, PathPolicyError
from swoon.policy.models import ResolvedPath

from .errors import ToolExecutionError
from .models import MutationToolLimits
from .safe_io import SafeIO, SafeMutationIO


class FilesystemMutationTools:
    """Perform bounded mutations without ever following a filesystem link."""

    def __init__(self, policy: PathPolicy, limits: MutationToolLimits) -> None:
        self.policy = policy
        self.limits = limits
        self.io = SafeIO(policy)
        self.mutations = SafeMutationIO(policy)

    def overwrite_confirmation_reason(self, action: ValidatedAction) -> str | None:
        reason, _ = self.overwrite_confirmation_details(action)
        return reason

    def overwrite_confirmation_details(
        self,
        action: ValidatedAction,
    ) -> tuple[str | None, str]:
        path = self._required_path(action)
        replacement_size = len(self._content(action))
        resolved = self._resolve_file(path, access=PathAccess.WRITE)
        with self.io.open_file(resolved) as stream:
            opened = os.fstat(stream.fileno())
            if opened.st_size > self.limits.max_file_bytes:
                raise ToolExecutionError(
                    "file_too_large",
                    f"Overwrite target exceeds {self.limits.max_file_bytes} bytes",
                )
            digest = hashlib.sha256()
            copied = 0
            while block := stream.read(1024 * 1024):
                copied += len(block)
                if copied > self.limits.max_file_bytes:
                    raise ToolExecutionError(
                        "file_too_large",
                        "Overwrite target grew beyond its size limit",
                    )
                digest.update(block)
        guard_payload = json.dumps(
            {
                "device": opened.st_dev,
                "inode": opened.st_ino,
                "size": copied,
                "mtime_ns": opened.st_mtime_ns,
                "ctime_ns": opened.st_ctime_ns,
                "sha256": digest.hexdigest(),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        guard = hashlib.sha256(guard_payload).hexdigest()
        size = copied
        if size < 1:
            return None, guard
        return (
            (
                f"overwrite-file will replace the non-empty output file "
                f"{resolved.reference.value!r} ({size} bytes) with "
                f"{replacement_size} bytes"
            ),
            guard,
        )

    def create_file(self, action: ValidatedAction) -> Result:
        path = self._required_path(action)
        payload = self._content(action)
        resolved = self._resolve_missing_file(path)
        self.mutations.atomic_create(resolved, payload)
        return self._result(
            action,
            f"Created {resolved.reference.value!r} ({len(payload)} bytes).",
        )

    def overwrite_file(
        self,
        action: ValidatedAction,
        *,
        allow_nonempty: bool = False,
    ) -> Result:
        path = self._required_path(action)
        payload = self._content(action)
        resolved = self._resolve_file(path, access=PathAccess.WRITE)
        with self.io.open_file(resolved) as stream:
            opened = os.fstat(stream.fileno())
            executable = bool(opened.st_mode & 0o111)
            if opened.st_size > 0 and not allow_nonempty:
                raise ToolExecutionError(
                    "confirmation_required",
                    "Overwrite target is non-empty and lacks human approval",
                )
        self.mutations.atomic_replace(
            resolved,
            payload,
            executable=executable,
            require_empty=not allow_nonempty,
        )
        return self._result(
            action,
            f"Overwrote {resolved.reference.value!r} ({len(payload)} bytes).",
        )

    def append_file(self, action: ValidatedAction) -> Result:
        path = self._required_path(action)
        addition = self._content(action)
        resolved = self._resolve_file(path, access=PathAccess.WRITE)
        with self.io.open_file(resolved) as stream:
            original, executable = self._read_bounded(stream)
        total = len(original) + len(addition)
        if total > self.limits.max_file_bytes:
            raise ToolExecutionError(
                "file_too_large",
                f"Resulting file exceeds {self.limits.max_file_bytes} bytes",
            )
        self.mutations.atomic_replace(
            resolved,
            original + addition,
            executable=executable,
        )
        return self._result(
            action,
            (
                f"Appended {len(addition)} bytes to {resolved.reference.value!r} "
                f"({total} bytes total)."
            ),
        )

    def edit_file(self, action: ValidatedAction) -> Result:
        path = self._required_path(action)
        old_value = action.argument("old_str")
        new_value = action.argument("new_str")
        if not isinstance(old_value, str) or not old_value:
            raise ToolExecutionError("invalid_argument", "old_str must be non-empty text")
        if not isinstance(new_value, str):
            raise ToolExecutionError("invalid_argument", "new_str must be text")
        self._bounded_text(old_value, "old_str")
        self._bounded_text(new_value, "new_str")

        resolved = self._resolve_file(path, access=PathAccess.WRITE)
        with self.io.open_file(resolved) as stream:
            original, executable = self._read_bounded(stream)
        if b"\x00" in original:
            raise ToolExecutionError("binary_unsupported", "Binary files cannot be edited")
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError(
                "binary_unsupported",
                "Binary files cannot be edited",
            ) from error

        occurrences = text.count(old_value)
        if occurrences == 0:
            raise ToolExecutionError("old_str_not_found", "old_str was not found exactly")
        if occurrences > 1:
            raise ToolExecutionError(
                "ambiguous_edit",
                f"old_str occurs {occurrences} times; provide a unique match",
            )
        replacement = text.replace(old_value, new_value, 1).encode("utf-8")
        if len(replacement) > self.limits.max_file_bytes:
            raise ToolExecutionError(
                "file_too_large",
                f"Edited file exceeds {self.limits.max_file_bytes} bytes",
            )
        self.mutations.atomic_replace(resolved, replacement, executable=executable)
        return self._result(
            action,
            (
                f"Edited {resolved.reference.value!r}; replaced one exact occurrence "
                f"({len(replacement)} bytes total)."
            ),
        )

    def copy_file(self, action: ValidatedAction) -> Result:
        source_ref = self._path_argument(action, "from")
        target_ref = self._path_argument(action, "to")
        source = self._resolve_file(source_ref, access=PathAccess.READ)
        target = self._resolve_missing_file(target_ref)
        with self.io.open_file(source) as stream:
            payload, executable = self._read_bounded(stream)
        if len(payload) > self.limits.max_copy_bytes:
            raise ToolExecutionError(
                "copy_too_large",
                f"Copy exceeds {self.limits.max_copy_bytes} bytes",
            )
        self.mutations.atomic_create(target, payload, executable=executable)
        return self._result(
            action,
            (
                f"Copied {source.reference.root.value}:{source.reference.value} to "
                f"output:{target.reference.value} ({len(payload)} bytes)."
            ),
        )

    def copy_directory(self, action: ValidatedAction) -> Result:
        source_ref = self._path_argument(action, "from")
        target_ref = self._path_argument(action, "to")
        source = self._resolve_directory(source_ref, access=PathAccess.READ, allow_root=True)
        target = self._resolve_copy_directory_target(target_ref)
        self._reject_recursive_copy(source, target)

        counters = {"entries": 0, "files": 0, "bytes": 0, "skipped": 0}
        source_parts = self._parts(source.reference.value)
        with self.io.open_directory(source) as source_fd:
            with self._copy_destination(target) as (target_fd, created_names):
                self._copy_directory_contents(
                    source_fd,
                    target_fd,
                    source_root=source.reference.root,
                    source_parts=source_parts,
                    relative_parts=(),
                    counters=counters,
                    created_names=created_names,
                )
                self._fsync_directory(target_fd)

        skipped = (
            f", skipped {counters['skipped']} denied entr"
            f"{'y' if counters['skipped'] == 1 else 'ies'}"
            if counters["skipped"]
            else ""
        )
        return self._result(
            action,
            (
                f"Copied directory {source.reference.root.value}:{source.reference.value} to "
                f"output:{target.reference.value} ({counters['files']} files, "
                f"{counters['bytes']} bytes{skipped})."
            ),
        )

    @contextmanager
    def _copy_destination(
        self,
        target: ResolvedPath,
    ) -> Iterator[tuple[int, list[str]]]:
        if target.reference.value == ".":
            with self.io.open_directory(target) as target_fd:
                if self.io.entries(target_fd):
                    raise ToolExecutionError(
                        "destination_not_empty",
                        "copy-dir may target the output root only while it is empty",
                    )
                created_names: list[str] = []
                try:
                    yield target_fd, created_names
                except Exception:
                    self._cleanup_names(target_fd, created_names)
                    raise
            return

        with self.io.open_parent(target) as (parent_fd, name, fresh):
            if fresh.exists:
                raise ToolExecutionError("path_exists", "Copy destination already exists")
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError as error:
                raise ToolExecutionError("path_exists", "Copy destination already exists") from error
            except OSError as error:
                raise ToolExecutionError(
                    "tool_failed",
                    f"Copy destination could not be created ({error.__class__.__name__})",
                    retryable=True,
                ) from error
            try:
                target_fd = self._open_new_directory(parent_fd, name)
            except Exception:
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except OSError:
                    pass
                raise
            created_names = []
            try:
                yield target_fd, created_names
                self._fsync_directory(target_fd)
                self._fsync_directory(parent_fd)
            except Exception:
                self._cleanup_names(target_fd, created_names)
                os.close(target_fd)
                target_fd = -1
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                    self._fsync_directory(parent_fd)
                except OSError:
                    pass
                raise
            finally:
                if target_fd >= 0:
                    os.close(target_fd)

    def _copy_directory_contents(
        self,
        source_fd: int,
        target_fd: int,
        *,
        source_root: Root,
        source_parts: tuple[str, ...],
        relative_parts: tuple[str, ...],
        counters: dict[str, int],
        created_names: list[str],
    ) -> None:
        for entry in self.io.entries(source_fd):
            counters["entries"] += 1
            if counters["entries"] > self.limits.max_copy_entries:
                raise ToolExecutionError(
                    "copy_too_large",
                    f"Directory copy exceeds {self.limits.max_copy_entries} entries",
                )
            child_relative = relative_parts + (entry.name,)
            child_source = source_parts + child_relative
            try:
                denied = self.policy.is_denied(
                    PathRef("/".join(child_source), source_root)
                )
            except PathPolicyError:
                denied = True
            if denied:
                counters["skipped"] += 1
                continue
            if entry.is_symlink:
                raise ToolExecutionError("path_escape", "Directory copy contains a symbolic link")
            if entry.is_directory:
                try:
                    os.mkdir(entry.name, 0o700, dir_fd=target_fd)
                except FileExistsError as error:
                    raise ToolExecutionError(
                        "path_exists",
                        "Copy destination changed during execution",
                    ) from error
                created_names.append(entry.name)
                child_target_fd = self._open_new_directory(target_fd, entry.name)
                try:
                    with self.io.open_child_directory(source_fd, entry) as child_source_fd:
                        child_created: list[str] = []
                        self._copy_directory_contents(
                            child_source_fd,
                            child_target_fd,
                            source_root=source_root,
                            source_parts=source_parts,
                            relative_parts=child_relative,
                            counters=counters,
                            created_names=child_created,
                        )
                        self._fsync_directory(child_target_fd)
                finally:
                    os.close(child_target_fd)
                continue
            if not entry.is_file:
                raise ToolExecutionError(
                    "unsupported_file_type",
                    "Directory copy contains a special file",
                )
            if self.policy.reject_hardlinks and entry.link_count > 1:
                raise ToolExecutionError("path_escape", "Directory copy contains a hard-linked file")
            if entry.size > self.limits.max_file_bytes:
                raise ToolExecutionError("copy_too_large", "Directory copy contains an oversized file")
            try:
                with self.io.open_child_file(source_fd, entry) as source_stream:
                    copied = self._copy_new_file(
                        source_stream,
                        target_fd,
                        entry.name,
                        executable=bool(entry.mode & 0o111),
                    )
            except Exception:
                try:
                    os.unlink(entry.name, dir_fd=target_fd)
                except OSError:
                    pass
                raise
            created_names.append(entry.name)
            counters["files"] += 1
            counters["bytes"] += copied
            if counters["bytes"] > self.limits.max_copy_bytes:
                raise ToolExecutionError(
                    "copy_too_large",
                    f"Directory copy exceeds {self.limits.max_copy_bytes} bytes",
                )

    def _copy_new_file(
        self,
        source: BinaryIO,
        target_fd: int,
        name: str,
        *,
        executable: bool,
    ) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(
                name,
                flags,
                0o700 if executable else 0o600,
                dir_fd=target_fd,
            )
        except FileExistsError as error:
            raise ToolExecutionError(
                "path_exists",
                "Copy destination changed during execution",
            ) from error
        copied = 0
        try:
            while block := source.read(1024 * 1024):
                copied += len(block)
                if copied > self.limits.max_file_bytes:
                    raise ToolExecutionError(
                        "copy_too_large",
                        "A copied file grew beyond its size limit",
                    )
                offset = 0
                while offset < len(block):
                    written = os.write(descriptor, block[offset:])
                    if written < 1:
                        raise OSError("short write")
                    offset += written
            os.fchmod(descriptor, 0o700 if executable else 0o600)
            os.fsync(descriptor)
        except OSError as error:
            raise ToolExecutionError(
                "tool_failed",
                f"Copied file could not be written ({error.__class__.__name__})",
                retryable=True,
            ) from error
        finally:
            os.close(descriptor)
        return copied

    def _cleanup_names(self, directory_fd: int, names: list[str]) -> None:
        for name in reversed(names):
            try:
                item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(item_stat.st_mode) and not stat.S_ISLNK(item_stat.st_mode):
                    child_fd = self._open_new_directory(directory_fd, name)
                    try:
                        self._cleanup_names(child_fd, list(os.listdir(child_fd)))
                    finally:
                        os.close(child_fd)
                    os.rmdir(name, dir_fd=directory_fd)
                else:
                    os.unlink(name, dir_fd=directory_fd)
            except Exception:
                pass

    def _resolve_file(self, path: PathRef, *, access: PathAccess) -> ResolvedPath:
        try:
            return self.policy.resolve(
                path,
                access=access,
                existence=PathExistence.MUST_EXIST,
                kind=PathKind.FILE,
            )
        except PathPolicyError as error:
            raise self._path_error(error) from error

    def _resolve_missing_file(self, path: PathRef) -> ResolvedPath:
        try:
            return self.policy.resolve(
                path,
                access=PathAccess.WRITE,
                existence=PathExistence.MUST_NOT_EXIST,
                kind=PathKind.FILE,
            )
        except PathPolicyError as error:
            raise self._path_error(error) from error

    def _resolve_directory(
        self,
        path: PathRef,
        *,
        access: PathAccess,
        allow_root: bool,
    ) -> ResolvedPath:
        try:
            return self.policy.resolve(
                path,
                access=access,
                existence=PathExistence.MUST_EXIST,
                kind=PathKind.DIRECTORY,
                allow_root=allow_root,
            )
        except PathPolicyError as error:
            raise self._path_error(error) from error

    def _resolve_copy_directory_target(self, path: PathRef) -> ResolvedPath:
        try:
            return self.policy.resolve(
                path,
                access=PathAccess.WRITE,
                existence=(
                    PathExistence.MUST_EXIST
                    if path.value == "."
                    else PathExistence.MUST_NOT_EXIST
                ),
                kind=PathKind.DIRECTORY,
                allow_root=path.value == ".",
            )
        except PathPolicyError as error:
            raise self._path_error(error) from error

    def _read_bounded(self, stream: BinaryIO) -> tuple[bytes, bool]:
        opened = os.fstat(stream.fileno())
        if opened.st_size > self.limits.max_file_bytes:
            raise ToolExecutionError(
                "file_too_large",
                f"File exceeds {self.limits.max_file_bytes} bytes",
            )
        payload = stream.read(self.limits.max_file_bytes + 1)
        if len(payload) > self.limits.max_file_bytes:
            raise ToolExecutionError(
                "file_too_large",
                f"File grew beyond {self.limits.max_file_bytes} bytes",
            )
        return payload, bool(opened.st_mode & 0o111)

    def _content(self, action: ValidatedAction) -> bytes:
        value = action.argument("content")
        if not isinstance(value, str):
            raise ToolExecutionError("invalid_argument", "content must be text")
        return self._bounded_text(value, "content")

    def _bounded_text(self, value: str, name: str) -> bytes:
        try:
            payload = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ToolExecutionError("invalid_argument", f"{name} is not valid Unicode") from error
        if len(payload) > self.limits.max_content_bytes:
            raise ToolExecutionError(
                "content_too_large",
                f"{name} exceeds {self.limits.max_content_bytes} bytes",
            )
        return payload

    @staticmethod
    def _required_path(action: ValidatedAction) -> PathRef:
        if action.source.path is None:
            raise ToolExecutionError("missing_path", "Tool requires a path")
        return action.source.path

    @staticmethod
    def _path_argument(action: ValidatedAction, name: str) -> PathRef:
        value = action.argument(name)
        if not isinstance(value, PathRef):
            raise ToolExecutionError("invalid_argument", f"{name} must be a path")
        return value

    @staticmethod
    def _result(action: ValidatedAction, body: str) -> Result:
        return Result(action.source.id, ResultStatus.SUCCESS, body=body)

    @staticmethod
    def _parts(value: str) -> tuple[str, ...]:
        return () if value == "." else tuple(value.split("/"))

    @staticmethod
    def _open_new_directory(parent_fd: int, name: str) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise ToolExecutionError(
                "path_changed",
                f"New directory could not be opened ({error.__class__.__name__})",
                retryable=True,
            ) from error
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            os.close(descriptor)
            raise ToolExecutionError("path_changed", "New directory changed type")
        return descriptor

    @staticmethod
    def _fsync_directory(descriptor: int) -> None:
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise ToolExecutionError(
                "tool_failed",
                f"Directory metadata could not be synchronized ({error.__class__.__name__})",
                retryable=True,
            ) from error

    @staticmethod
    def _path_error(error: PathPolicyError) -> ToolExecutionError:
        return ToolExecutionError(
            error.code,
            str(error),
            retryable=error.code in {"path_changed", "path_unavailable"},
        )

    @staticmethod
    def _reject_recursive_copy(source: ResolvedPath, target: ResolvedPath) -> None:
        if source.reference.root is not Root.OUTPUT:
            return
        source_parts = FilesystemMutationTools._parts(source.reference.value)
        target_parts = FilesystemMutationTools._parts(target.reference.value)
        if target_parts[: len(source_parts)] == source_parts:
            raise ToolExecutionError(
                "recursive_copy",
                "An output directory cannot be copied into itself or its descendant",
            )
