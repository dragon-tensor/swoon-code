"""Descriptor-relative, no-follow access for already-authorized paths."""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from swoon.policy import PathPolicy, PathPolicyError, ResolvedPath
from swoon.policy.models import ComponentFingerprint

from .errors import ToolExecutionError


def _supports_secure_openat() -> bool:
    return (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    name: str
    mode: int
    size: int
    device: int
    inode: int
    link_count: int

    @property
    def is_file(self) -> bool:
        return stat.S_ISREG(self.mode)

    @property
    def is_directory(self) -> bool:
        return stat.S_ISDIR(self.mode)

    @property
    def is_symlink(self) -> bool:
        return stat.S_ISLNK(self.mode)


class SafeIO:
    """Open files relative to verified directory descriptors.

    Platforms without descriptor-relative no-follow support fail closed. This
    keeps an unsupported host from silently weakening AEML's trust boundary.
    """

    def __init__(self, policy: PathPolicy) -> None:
        self.policy = policy
        self.secure_openat = _supports_secure_openat()

    @contextmanager
    def open_file(self, resolved: ResolvedPath) -> Iterator[BinaryIO]:
        if not self.secure_openat:
            raise ToolExecutionError(
                "platform_unsupported",
                "Secure no-follow file access is unavailable on this platform",
            )
        try:
            fresh = self.policy.revalidate(resolved)
            descriptors = self._open_chain(fresh, final_directory=False)
        except PathPolicyError as error:
            raise ToolExecutionError(
                error.code,
                str(error),
                retryable=error.code == "path_changed",
            ) from error
        try:
            final_descriptor = descriptors[-1]
            with os.fdopen(os.dup(final_descriptor), "rb") as stream:
                yield stream
        finally:
            self._close_all(descriptors)

    @contextmanager
    def open_directory(self, resolved: ResolvedPath) -> Iterator[int]:
        if not self.secure_openat:
            raise ToolExecutionError(
                "platform_unsupported",
                "Secure no-follow directory access is unavailable on this platform",
            )
        try:
            fresh = self.policy.revalidate(resolved)
            descriptors = self._open_chain(fresh, final_directory=True)
        except PathPolicyError as error:
            raise ToolExecutionError(
                error.code,
                str(error),
                retryable=error.code == "path_changed",
            ) from error
        try:
            yield descriptors[-1]
        finally:
            self._close_all(descriptors)

    @contextmanager
    def open_parent(self, resolved: ResolvedPath) -> Iterator[tuple[int, str, ResolvedPath]]:
        """Open and verify a target's parent, including when the target is absent."""

        if not self.secure_openat:
            raise ToolExecutionError(
                "platform_unsupported",
                "Secure no-follow file access is unavailable on this platform",
            )
        try:
            fresh = self.policy.revalidate(resolved)
        except PathPolicyError as error:
            raise ToolExecutionError(
                error.code,
                str(error),
                retryable=error.code == "path_changed",
            ) from error
        parts = tuple() if fresh.reference.value == "." else tuple(
            fresh.reference.value.split("/")
        )
        if not parts:
            raise ToolExecutionError("path_escape", "This operation cannot target the root")

        expected_fingerprints = len(parts) + (1 if fresh.exists else 0)
        if len(fresh.fingerprints) != expected_fingerprints:
            raise ToolExecutionError("path_changed", "Authorized path fingerprint is stale")

        descriptors: list[int] = []
        try:
            root_fd = self._open_absolute_root(fresh)
            descriptors.append(root_fd)
            self._verify_fingerprint(os.fstat(root_fd), fresh.fingerprints[0])
            for index, part in enumerate(parts[:-1], start=1):
                descriptor = self._open_at(descriptors[-1], part, directory=True)
                descriptors.append(descriptor)
                self._verify_fingerprint(os.fstat(descriptor), fresh.fingerprints[index])
            yield descriptors[-1], parts[-1], fresh
        finally:
            self._close_all(descriptors)

    def entries(self, directory_fd: int) -> tuple[DirectoryEntry, ...]:
        try:
            names = os.listdir(directory_fd)
            entries: list[DirectoryEntry] = []
            for name in sorted(names):
                item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                entries.append(
                    DirectoryEntry(
                        name=name,
                        mode=item_stat.st_mode,
                        size=item_stat.st_size,
                        device=item_stat.st_dev,
                        inode=item_stat.st_ino,
                        link_count=item_stat.st_nlink,
                    )
                )
            return tuple(entries)
        except OSError as error:
            raise ToolExecutionError(
                "tool_failed",
                f"Directory could not be inspected ({error.__class__.__name__})",
                retryable=True,
            ) from error

    @contextmanager
    def open_child_directory(self, parent_fd: int, entry: DirectoryEntry) -> Iterator[int]:
        if not entry.is_directory or entry.is_symlink:
            raise ToolExecutionError("path_escape", "Directory entry changed type")
        descriptor = self._open_at(parent_fd, entry.name, directory=True)
        try:
            self._verify_stat(os.fstat(descriptor), entry.device, entry.inode, stat.S_IFDIR)
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def open_child_file(self, parent_fd: int, entry: DirectoryEntry) -> Iterator[BinaryIO]:
        if not entry.is_file or entry.is_symlink:
            raise ToolExecutionError("path_escape", "File entry changed type")
        if self.policy.reject_hardlinks and entry.link_count > 1:
            raise ToolExecutionError("path_escape", "Hard-linked files are not readable")
        descriptor = self._open_at(parent_fd, entry.name, directory=False)
        try:
            opened = os.fstat(descriptor)
            self._verify_stat(opened, entry.device, entry.inode, stat.S_IFREG)
            if self.policy.reject_hardlinks and opened.st_nlink > 1:
                raise ToolExecutionError("path_escape", "Hard-linked files are not readable")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                yield stream
        finally:
            os.close(descriptor)

    def _open_chain(self, resolved: ResolvedPath, *, final_directory: bool) -> list[int]:
        fingerprints = resolved.fingerprints
        if not fingerprints:
            raise ToolExecutionError("path_changed", "Authorized path has no fingerprint")

        descriptors: list[int] = []
        try:
            root_fd = self._open_absolute_root(resolved)
            descriptors.append(root_fd)
            self._verify_fingerprint(os.fstat(root_fd), fingerprints[0])

            parts = tuple() if resolved.reference.value == "." else tuple(
                resolved.reference.value.split("/")
            )
            if len(fingerprints) != len(parts) + 1:
                raise ToolExecutionError("path_changed", "Authorized path fingerprint is stale")
            for index, part in enumerate(parts, start=1):
                is_final = index == len(parts)
                descriptor = self._open_at(
                    descriptors[-1],
                    part,
                    directory=not is_final or final_directory,
                )
                descriptors.append(descriptor)
                self._verify_fingerprint(os.fstat(descriptor), fingerprints[index])
            return descriptors
        except Exception:
            self._close_all(descriptors)
            raise

    @staticmethod
    def _open_absolute_root(resolved: ResolvedPath) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            return os.open(resolved.host_root, flags)
        except OSError as error:
            raise ToolExecutionError(
                "path_changed",
                f"Authorized root could not be opened ({error.__class__.__name__})",
                retryable=True,
            ) from error

    @staticmethod
    def _open_at(parent_fd: int, name: str, *, directory: bool) -> int:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        if directory:
            flags |= os.O_DIRECTORY
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise ToolExecutionError(
                "path_changed",
                f"Authorized path could not be opened ({error.__class__.__name__})",
                retryable=True,
            ) from error

    @staticmethod
    def _verify_fingerprint(item_stat: os.stat_result, expected: ComponentFingerprint) -> None:
        SafeIO._verify_stat(
            item_stat,
            expected.device,
            expected.inode,
            expected.file_type,
        )

    @staticmethod
    def _verify_stat(
        item_stat: os.stat_result,
        expected_device: int,
        expected_inode: int,
        expected_type: int,
    ) -> None:
        if (
            item_stat.st_dev != expected_device
            or item_stat.st_ino != expected_inode
            or stat.S_IFMT(item_stat.st_mode) != expected_type
        ):
            raise ToolExecutionError(
                "path_changed",
                "Authorized path changed before it was opened",
                retryable=True,
            )

    @staticmethod
    def _close_all(descriptors: list[int]) -> None:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


class SafeMutationIO:
    """Atomic, descriptor-relative publication of bounded file payloads."""

    def __init__(self, policy: PathPolicy) -> None:
        self.policy = policy
        self.io = SafeIO(policy)

    def atomic_create(
        self,
        resolved: ResolvedPath,
        payload: bytes,
        *,
        executable: bool = False,
    ) -> None:
        if resolved.exists:
            raise ToolExecutionError("path_exists", "Target already exists")
        with self.io.open_parent(resolved) as (parent_fd, name, fresh):
            if fresh.exists:
                raise ToolExecutionError("path_exists", "Target already exists")
            temporary = self._temporary_name()
            descriptor = self._create_temporary(parent_fd, temporary, executable=executable)
            published = False
            try:
                self._write_payload(descriptor, payload, executable=executable)
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    published = True
                except FileExistsError as error:
                    raise ToolExecutionError("path_exists", "Target already exists") from error
                except OSError as error:
                    raise ToolExecutionError(
                        "tool_failed",
                        f"File could not be published ({error.__class__.__name__})",
                        retryable=True,
                    ) from error
                os.unlink(temporary, dir_fd=parent_fd)
                temporary = ""
                self._fsync_directory(parent_fd)
            finally:
                os.close(descriptor)
                if temporary:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                if not published:
                    self._fsync_directory(parent_fd)

    def atomic_replace(
        self,
        resolved: ResolvedPath,
        payload: bytes,
        *,
        executable: bool,
        require_empty: bool = False,
    ) -> None:
        if not resolved.exists:
            raise ToolExecutionError("path_not_found", "Target does not exist")
        with self.io.open_parent(resolved) as (parent_fd, name, fresh):
            opened = self._verify_existing_target(parent_fd, name, fresh)
            self._require_empty(opened, require_empty)
            temporary = self._temporary_name()
            descriptor = self._create_temporary(parent_fd, temporary, executable=executable)
            try:
                self._write_payload(descriptor, payload, executable=executable)
                opened = self._verify_existing_target(parent_fd, name, fresh)
                self._require_empty(opened, require_empty)
                try:
                    os.replace(
                        temporary,
                        name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    temporary = ""
                except OSError as error:
                    raise ToolExecutionError(
                        "tool_failed",
                        f"File could not be replaced ({error.__class__.__name__})",
                        retryable=True,
                    ) from error
                self._fsync_directory(parent_fd)
            finally:
                os.close(descriptor)
                if temporary:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass

    def _verify_existing_target(
        self,
        parent_fd: int,
        name: str,
        resolved: ResolvedPath,
    ) -> os.stat_result:
        try:
            item_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise ToolExecutionError(
                "path_changed",
                f"Authorized target changed ({error.__class__.__name__})",
                retryable=True,
            ) from error
        expected = resolved.fingerprints[-1]
        SafeIO._verify_fingerprint(item_stat, expected)
        if not stat.S_ISREG(item_stat.st_mode):
            raise ToolExecutionError("not_file", "Target is not a regular file")
        if self.policy.reject_hardlinks and item_stat.st_nlink > 1:
            raise ToolExecutionError("path_escape", "Hard-linked files are not writable")
        return item_stat

    @staticmethod
    def _require_empty(item_stat: os.stat_result, required: bool) -> None:
        if required and item_stat.st_size > 0:
            raise ToolExecutionError(
                "confirmation_required",
                "Overwrite target became non-empty before atomic replacement",
            )

    @staticmethod
    def _temporary_name() -> str:
        return f".swoon-tmp-{secrets.token_hex(16)}"

    @staticmethod
    def _create_temporary(parent_fd: int, name: str, *, executable: bool) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            return os.open(
                name,
                flags,
                0o700 if executable else 0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise ToolExecutionError(
                "tool_failed",
                f"Temporary file could not be created ({error.__class__.__name__})",
                retryable=True,
            ) from error

    @staticmethod
    def _write_payload(descriptor: int, payload: bytes, *, executable: bool) -> None:
        try:
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written < 1:
                    raise OSError("short write")
                offset += written
            os.fchmod(descriptor, 0o700 if executable else 0o600)
            os.fsync(descriptor)
        except OSError as error:
            raise ToolExecutionError(
                "tool_failed",
                f"File payload could not be written ({error.__class__.__name__})",
                retryable=True,
            ) from error

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
