"""Guarded output lifecycle operations for validated AEML actions."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from typing import Any

from swoon.aeml.models import PathRef, Result, ResultStatus, Root, ValidatedAction
from swoon.policy import PathAccess, PathExistence, PathKind, PathPolicy, PathPolicyError
from swoon.policy.models import ResolvedPath

from .errors import ToolExecutionError
from .models import MutationToolLimits
from .safe_io import SafeIO


_RENAME_NOREPLACE = 1


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    """Bounded metadata snapshot used as an opaque confirmation guard."""

    guard: str
    entries: int
    files: int
    total_bytes: int


class FilesystemLifecycleTools:
    """Delete, relocate, and change modes without following filesystem links."""

    def __init__(self, policy: PathPolicy, limits: MutationToolLimits) -> None:
        self.policy = policy
        self.limits = limits
        self.io = SafeIO(policy)

    def confirmation_details(self, action: ValidatedAction) -> tuple[str, str]:
        """Describe and fingerprint one always-confirmed deletion."""

        path = self._required_path(action)
        if action.spec.name == "delete-file":
            resolved = self._resolve_existing(path, kind=PathKind.FILE)
            snapshot = self._snapshot_file(resolved)
            reason = (
                f"delete-file will permanently remove output file "
                f"{resolved.reference.value!r} ({snapshot.total_bytes} bytes)"
            )
            return reason, snapshot.guard
        if action.spec.name == "delete-dir":
            resolved = self._resolve_existing(path, kind=PathKind.DIRECTORY)
            snapshot = self._snapshot_directory(resolved)
            reason = (
                f"delete-dir will permanently remove output directory "
                f"{resolved.reference.value!r} ({snapshot.entries} entries, "
                f"{snapshot.files} files, {snapshot.total_bytes} bytes)"
            )
            return reason, snapshot.guard
        raise ToolExecutionError(
            "invalid_confirmation",
            "Lifecycle confirmation is only available for delete-file and delete-dir",
        )

    def delete_file(self, action: ValidatedAction) -> Result:
        path = self._required_path(action)
        resolved = self._resolve_existing(path, kind=PathKind.FILE)
        self._snapshot_file(resolved)
        with self.io.open_parent(resolved) as (parent_fd, name, fresh):
            self._verify_entry(parent_fd, name, fresh, kind=PathKind.FILE)
            try:
                os.unlink(name, dir_fd=parent_fd)
                self._fsync_directory(parent_fd)
            except OSError as error:
                raise self._operation_error("File could not be deleted", error) from error
        return self._result(action, f"Deleted output file {resolved.reference.value!r}.")

    def delete_directory(self, action: ValidatedAction) -> Result:
        path = self._required_path(action)
        resolved = self._resolve_existing(path, kind=PathKind.DIRECTORY)
        self._snapshot_directory(resolved)
        base_parts = self._parts(resolved.reference.value)
        counters = {"entries": 0, "bytes": 0}
        with self.io.open_parent(resolved) as (parent_fd, name, fresh):
            opened = self._verify_entry(
                parent_fd,
                name,
                fresh,
                kind=PathKind.DIRECTORY,
            )
            directory_fd = self._open_verified_directory(parent_fd, name, opened)
            try:
                self._remove_directory_contents(
                    directory_fd,
                    base_parts=base_parts,
                    relative_parts=(),
                    counters=counters,
                )
                self._fsync_directory(directory_fd)
            finally:
                os.close(directory_fd)
            try:
                os.rmdir(name, dir_fd=parent_fd)
                self._fsync_directory(parent_fd)
            except OSError as error:
                raise self._operation_error("Directory could not be deleted", error) from error
        return self._result(
            action,
            f"Deleted output directory {resolved.reference.value!r}.",
        )

    def move(self, action: ValidatedAction) -> Result:
        return self._relocate(action, same_parent=False)

    def rename(self, action: ValidatedAction) -> Result:
        return self._relocate(action, same_parent=True)

    def chmod(self, action: ValidatedAction) -> Result:
        path = self._required_path(action)
        raw_mode = action.argument("mode")
        if not isinstance(raw_mode, str):
            raise ToolExecutionError("invalid_argument", "mode must be octal text")
        try:
            selected_mode = int(raw_mode, 8)
        except ValueError as error:
            raise ToolExecutionError("invalid_argument", "mode must be octal text") from error
        if selected_mode not in {0o600, 0o700}:
            raise ToolExecutionError(
                "unsafe_mode",
                "chmod permits only owner-private mode 600 or 700 on regular files",
            )

        resolved = self._resolve_existing(path, kind=PathKind.FILE)
        with self.io.open_file(resolved) as stream:
            try:
                os.fchmod(stream.fileno(), selected_mode)
                os.fsync(stream.fileno())
            except OSError as error:
                raise self._operation_error("File mode could not be changed", error) from error
        return self._result(
            action,
            f"Changed output file {resolved.reference.value!r} to mode {selected_mode:03o}.",
        )

    def _relocate(self, action: ValidatedAction, *, same_parent: bool) -> Result:
        source_ref = self._path_argument(action, "from")
        target_ref = self._path_argument(action, "to")
        source = self._resolve_existing(source_ref, kind=PathKind.ANY)
        target = self._resolve_missing(target_ref)
        source_parts = self._parts(source.reference.value)
        target_parts = self._parts(target.reference.value)

        if same_parent and source_parts[:-1] != target_parts[:-1]:
            raise ToolExecutionError(
                "rename_parent_mismatch",
                "rename must keep the entry in its existing parent; use move to relocate it",
            )
        source_is_directory = source.fingerprints[-1].file_type == stat.S_IFDIR
        if source_is_directory and target_parts[: len(source_parts)] == source_parts:
            raise ToolExecutionError(
                "recursive_move",
                "An output directory cannot be moved into itself or its descendant",
            )

        if source_is_directory:
            self._snapshot_directory(source)
        else:
            self._snapshot_file(source)

        with self.io.open_parent(source) as (source_parent, source_name, fresh_source):
            source_stat = self._verify_entry(
                source_parent,
                source_name,
                fresh_source,
                kind=PathKind.ANY,
            )
            with self.io.open_parent(target) as (target_parent, target_name, fresh_target):
                if fresh_target.exists or self._entry_exists(target_parent, target_name):
                    raise ToolExecutionError("path_exists", "Move destination already exists")
                self._rename_noreplace(
                    source_parent,
                    source_name,
                    target_parent,
                    target_name,
                )
                try:
                    moved = os.stat(
                        target_name,
                        dir_fd=target_parent,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise self._operation_error(
                        "Moved entry could not be verified",
                        error,
                        changed=True,
                    ) from error
                self._verify_same_entry(moved, source_stat)
                self._fsync_directory(target_parent)
                self._fsync_directory(source_parent)

        verb = "Renamed" if same_parent else "Moved"
        return self._result(
            action,
            (
                f"{verb} output:{source.reference.value} to "
                f"output:{target.reference.value}."
            ),
        )

    def _snapshot_file(self, resolved: ResolvedPath) -> LifecycleSnapshot:
        digest = self._new_guard(resolved, "file")
        with self.io.open_file(resolved) as stream:
            opened = os.fstat(stream.fileno())
            if opened.st_size > self.limits.max_copy_bytes:
                raise ToolExecutionError(
                    "lifecycle_too_large",
                    f"Lifecycle target exceeds {self.limits.max_copy_bytes} bytes",
                )
            self._update_guard(digest, (), opened)
        return LifecycleSnapshot(digest.hexdigest(), 1, 1, opened.st_size)

    def _snapshot_directory(self, resolved: ResolvedPath) -> LifecycleSnapshot:
        digest = self._new_guard(resolved, "directory")
        counters = {"entries": 0, "files": 0, "bytes": 0}
        base_parts = self._parts(resolved.reference.value)
        with self.io.open_directory(resolved) as directory_fd:
            self._update_guard(digest, (), os.fstat(directory_fd))
            self._snapshot_directory_contents(
                directory_fd,
                base_parts=base_parts,
                relative_parts=(),
                digest=digest,
                counters=counters,
            )
        return LifecycleSnapshot(
            digest.hexdigest(),
            counters["entries"],
            counters["files"],
            counters["bytes"],
        )

    def _snapshot_directory_contents(
        self,
        directory_fd: int,
        *,
        base_parts: tuple[str, ...],
        relative_parts: tuple[str, ...],
        digest: Any,
        counters: dict[str, int],
    ) -> None:
        for entry in self.io.entries(directory_fd):
            child_relative = relative_parts + (entry.name,)
            self._count_entry(
                counters,
                entry.size if entry.is_file else 0,
                depth=len(child_relative),
            )
            self._require_allowed_child(base_parts, child_relative)
            if entry.is_symlink:
                raise ToolExecutionError(
                    "path_escape",
                    "Lifecycle target contains a symbolic link",
                )
            if entry.is_directory:
                with self.io.open_child_directory(directory_fd, entry) as child_fd:
                    self._update_guard(digest, child_relative, os.fstat(child_fd))
                    self._snapshot_directory_contents(
                        child_fd,
                        base_parts=base_parts,
                        relative_parts=child_relative,
                        digest=digest,
                        counters=counters,
                    )
                continue
            if not entry.is_file:
                raise ToolExecutionError(
                    "unsupported_file_type",
                    "Lifecycle target contains a special file",
                )
            with self.io.open_child_file(directory_fd, entry) as child_stream:
                opened = os.fstat(child_stream.fileno())
                self._update_guard(digest, child_relative, opened)
            counters["files"] += 1

    def _remove_directory_contents(
        self,
        directory_fd: int,
        *,
        base_parts: tuple[str, ...],
        relative_parts: tuple[str, ...],
        counters: dict[str, int],
    ) -> None:
        for entry in self.io.entries(directory_fd):
            child_relative = relative_parts + (entry.name,)
            self._count_entry(
                counters,
                entry.size if entry.is_file else 0,
                depth=len(child_relative),
            )
            self._require_allowed_child(base_parts, child_relative)
            if entry.is_symlink:
                raise ToolExecutionError(
                    "path_escape",
                    "Delete target contains a symbolic link",
                )
            if entry.is_directory:
                with self.io.open_child_directory(directory_fd, entry) as child_fd:
                    self._remove_directory_contents(
                        child_fd,
                        base_parts=base_parts,
                        relative_parts=child_relative,
                        counters=counters,
                    )
                    self._fsync_directory(child_fd)
                try:
                    os.rmdir(entry.name, dir_fd=directory_fd)
                except OSError as error:
                    raise self._operation_error(
                        "Directory entry could not be deleted",
                        error,
                        changed=True,
                    ) from error
                continue
            if not entry.is_file:
                raise ToolExecutionError(
                    "unsupported_file_type",
                    "Delete target contains a special file",
                )
            with self.io.open_child_file(directory_fd, entry):
                pass
            try:
                os.unlink(entry.name, dir_fd=directory_fd)
            except OSError as error:
                raise self._operation_error(
                    "File entry could not be deleted",
                    error,
                    changed=True,
                ) from error
        self._fsync_directory(directory_fd)

    def _count_entry(self, counters: dict[str, int], size: int, *, depth: int) -> None:
        if depth > self.limits.max_lifecycle_depth:
            raise ToolExecutionError(
                "lifecycle_too_large",
                (
                    "Lifecycle target exceeds "
                    f"{self.limits.max_lifecycle_depth} directory levels"
                ),
            )
        counters["entries"] += 1
        if counters["entries"] > self.limits.max_copy_entries:
            raise ToolExecutionError(
                "lifecycle_too_large",
                f"Lifecycle target exceeds {self.limits.max_copy_entries} entries",
            )
        counters["bytes"] += size
        if counters["bytes"] > self.limits.max_copy_bytes:
            raise ToolExecutionError(
                "lifecycle_too_large",
                f"Lifecycle target exceeds {self.limits.max_copy_bytes} bytes",
            )

    def _require_allowed_child(
        self,
        base_parts: tuple[str, ...],
        relative_parts: tuple[str, ...],
    ) -> None:
        reference = PathRef("/".join(base_parts + relative_parts), Root.OUTPUT)
        try:
            denied = self.policy.is_denied(reference)
        except PathPolicyError as error:
            raise self._path_error(error) from error
        if denied:
            raise ToolExecutionError(
                "credential_path",
                "Lifecycle target contains a protected credential-shaped path",
            )

    def _resolve_existing(self, path: PathRef, *, kind: PathKind) -> ResolvedPath:
        try:
            return self.policy.resolve(
                path,
                access=PathAccess.WRITE,
                existence=PathExistence.MUST_EXIST,
                kind=kind,
                allow_root=False,
            )
        except PathPolicyError as error:
            raise self._path_error(error) from error

    def _resolve_missing(self, path: PathRef) -> ResolvedPath:
        try:
            return self.policy.resolve(
                path,
                access=PathAccess.WRITE,
                existence=PathExistence.MUST_NOT_EXIST,
                kind=PathKind.ANY,
                allow_root=False,
            )
        except PathPolicyError as error:
            raise self._path_error(error) from error

    def _verify_entry(
        self,
        parent_fd: int,
        name: str,
        resolved: ResolvedPath,
        *,
        kind: PathKind,
    ) -> os.stat_result:
        try:
            opened = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise self._operation_error(
                "Authorized entry changed",
                error,
                changed=True,
            ) from error
        SafeIO._verify_fingerprint(opened, resolved.fingerprints[-1])
        file_type = stat.S_IFMT(opened.st_mode)
        if kind is PathKind.FILE and file_type != stat.S_IFREG:
            raise ToolExecutionError("not_file", "Target is not a regular file")
        if kind is PathKind.DIRECTORY and file_type != stat.S_IFDIR:
            raise ToolExecutionError("not_directory", "Target is not a directory")
        if kind is PathKind.ANY and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
            raise ToolExecutionError(
                "unsupported_file_type",
                "Target is not a regular file or directory",
            )
        if self.policy.reject_hardlinks and stat.S_ISREG(opened.st_mode) and opened.st_nlink > 1:
            raise ToolExecutionError("path_escape", "Hard-linked files are not writable")
        return opened

    @staticmethod
    def _open_verified_directory(
        parent_fd: int,
        name: str,
        expected: os.stat_result,
    ) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise ToolExecutionError(
                "path_changed",
                f"Authorized directory changed ({error.__class__.__name__})",
                retryable=True,
            ) from error
        try:
            FilesystemLifecycleTools._verify_same_entry(os.fstat(descriptor), expected)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _verify_same_entry(actual: os.stat_result, expected: os.stat_result) -> None:
        if (
            actual.st_dev != expected.st_dev
            or actual.st_ino != expected.st_ino
            or stat.S_IFMT(actual.st_mode) != stat.S_IFMT(expected.st_mode)
        ):
            raise ToolExecutionError(
                "path_changed",
                "Authorized entry changed during execution",
                retryable=True,
            )

    @staticmethod
    def _entry_exists(parent_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ToolExecutionError(
                "path_changed",
                f"Move destination could not be inspected ({error.__class__.__name__})",
                retryable=True,
            ) from error
        return True

    @staticmethod
    def _rename_noreplace(
        source_parent: int,
        source_name: str,
        target_parent: int,
        target_name: str,
    ) -> None:
        if not sys.platform.startswith("linux"):
            raise ToolExecutionError(
                "platform_unsupported",
                "Atomic no-overwrite move requires Linux renameat2 support",
            )
        library = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(library, "renameat2", None)
        if renameat2 is None:
            raise ToolExecutionError(
                "platform_unsupported",
                "Atomic no-overwrite move is unavailable on this host",
            )
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renameat2(
            source_parent,
            os.fsencode(source_name),
            target_parent,
            os.fsencode(target_name),
            _RENAME_NOREPLACE,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise ToolExecutionError("path_exists", "Move destination already exists")
        if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise ToolExecutionError(
                "platform_unsupported",
                "Atomic no-overwrite move is unavailable on this filesystem",
            )
        if error_number == errno.EXDEV:
            raise ToolExecutionError(
                "cross_device_move",
                "Move source and destination are not on the same filesystem",
            )
        error = OSError(error_number, os.strerror(error_number))
        raise FilesystemLifecycleTools._operation_error(
            "Entry could not be moved",
            error,
            changed=error_number in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP},
        )

    @staticmethod
    def _new_guard(resolved: ResolvedPath, kind: str):
        digest = hashlib.sha256()
        payload = {
            "kind": kind,
            "path": resolved.reference.value,
            "root": resolved.reference.root.value,
        }
        digest.update(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
        return digest

    @staticmethod
    def _update_guard(
        digest,
        relative_parts: tuple[str, ...],
        item_stat: os.stat_result,
    ) -> None:
        payload = {
            "path": "/".join(relative_parts) if relative_parts else ".",
            "device": item_stat.st_dev,
            "inode": item_stat.st_ino,
            "mode": item_stat.st_mode,
            "links": item_stat.st_nlink,
            "size": item_stat.st_size,
            "uid": item_stat.st_uid,
            "gid": item_stat.st_gid,
            "mtime_ns": item_stat.st_mtime_ns,
            "ctime_ns": item_stat.st_ctime_ns,
        }
        digest.update(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")

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
    def _parts(value: str) -> tuple[str, ...]:
        return () if value == "." else tuple(value.split("/"))

    @staticmethod
    def _result(action: ValidatedAction, body: str) -> Result:
        return Result(action.source.id, ResultStatus.SUCCESS, body=body)

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
    def _operation_error(
        message: str,
        error: OSError,
        *,
        changed: bool = False,
    ) -> ToolExecutionError:
        code = "path_changed" if changed else "tool_failed"
        return ToolExecutionError(
            code,
            f"{message} ({error.__class__.__name__})",
            retryable=changed,
        )

    @staticmethod
    def _path_error(error: PathPolicyError) -> ToolExecutionError:
        return ToolExecutionError(
            error.code,
            str(error),
            retryable=error.code in {"path_changed", "path_unavailable"},
        )
