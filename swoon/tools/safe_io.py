"""Descriptor-relative, no-follow access for already-authorized paths."""

from __future__ import annotations

import os
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
