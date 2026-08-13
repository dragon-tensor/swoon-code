"""Virtual-root path authorization without filesystem mutation."""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from swoon.aeml.models import PathRef, Root
from swoon.session.models import SessionPaths

from .denylist import CredentialDenylist
from .errors import PathPolicyError
from .models import (
    ComponentFingerprint,
    PathAccess,
    PathExistence,
    PathKind,
    ResolvedPath,
)


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_WINDOWS_FORBIDDEN = set('<>:"|?*')
MAX_VIRTUAL_PATH_BYTES = 4_096
MAX_SEGMENT_BYTES = 255


class PathPolicy:
    """Authorize one AEML path inside a session's fixed virtual roots.

    The returned host path is an authorization snapshot, not an open file. Tool
    implementations must call :meth:`revalidate` immediately before use; later
    filesystem executors will additionally use no-follow/open-at primitives.
    """

    def __init__(
        self,
        session_paths: SessionPaths,
        *,
        denylist: CredentialDenylist | None = None,
        reject_hardlinks: bool = True,
    ) -> None:
        self.session_paths = session_paths
        self.denylist = denylist or CredentialDenylist()
        self.reject_hardlinks = reject_hardlinks

    def resolve(
        self,
        reference: PathRef,
        *,
        access: PathAccess = PathAccess.READ,
        existence: PathExistence = PathExistence.MUST_EXIST,
        kind: PathKind = PathKind.ANY,
        allow_root: bool | None = None,
    ) -> ResolvedPath:
        if not isinstance(reference, PathRef):
            raise PathPolicyError("invalid_path", "A typed PathRef is required")
        if not isinstance(reference.root, Root):
            raise PathPolicyError("invalid_root", "Unknown virtual root")
        if not isinstance(access, PathAccess):
            raise PathPolicyError("invalid_path_policy", "Unknown path access mode")
        if not isinstance(existence, PathExistence) or not isinstance(kind, PathKind):
            raise PathPolicyError("invalid_path_policy", "Unknown path policy constraint")
        if allow_root is not None and type(allow_root) is not bool:
            raise PathPolicyError("invalid_path_policy", "allow_root must be boolean or null")
        if access is PathAccess.WRITE and reference.root is Root.INPUT:
            raise PathPolicyError("input_readonly", "The input root is read-only")

        parts = self._portable_parts(reference.value)
        root_allowed = access is PathAccess.READ if allow_root is None else allow_root
        virtual_path = self._virtual_path(reference.root, parts)
        if not parts and not root_allowed:
            raise PathPolicyError(
                "path_escape",
                "This operation cannot target the session root itself",
                virtual_path=virtual_path,
            )
        if self.denylist.denies(parts):
            raise PathPolicyError(
                "credential_path",
                "Credential-shaped paths are inaccessible",
                virtual_path=virtual_path,
            )

        host_root = self._host_root(reference.root)
        fingerprints, host_path, exists = self._inspect_components(host_root, parts, virtual_path)
        self._validate_existence(existence, exists, virtual_path)
        if exists:
            self._validate_kind(fingerprints[-1].file_type, kind, virtual_path)

        self._assert_contained(host_root, host_path, exists, virtual_path)
        return ResolvedPath(
            reference=PathRef("." if not parts else "/".join(parts), reference.root),
            virtual_path=virtual_path,
            host_path=host_path,
            host_root=host_root,
            access=access,
            existence=existence,
            kind=kind,
            allow_root=root_allowed,
            exists=exists,
            fingerprints=fingerprints,
        )

    def revalidate(self, resolved: ResolvedPath) -> ResolvedPath:
        if not isinstance(resolved, ResolvedPath):
            raise PathPolicyError("invalid_path_policy", "A ResolvedPath is required")
        try:
            fresh = self.resolve(
                resolved.reference,
                access=resolved.access,
                existence=resolved.existence,
                kind=resolved.kind,
                allow_root=resolved.allow_root,
            )
        except PathPolicyError as error:
            raise PathPolicyError(
                "path_changed",
                f"Authorized path is no longer valid: {error}",
                virtual_path=resolved.virtual_path,
            ) from error
        if fresh.host_root != resolved.host_root or fresh.host_path != resolved.host_path:
            raise PathPolicyError(
                "path_changed",
                "Authorized path mapping changed before use",
                virtual_path=resolved.virtual_path,
            )
        if fresh.exists != resolved.exists or fresh.fingerprints != resolved.fingerprints:
            raise PathPolicyError(
                "path_changed",
                "Authorized path changed before use",
                virtual_path=resolved.virtual_path,
            )
        return fresh

    def is_denied(self, reference: PathRef) -> bool:
        parts = self._portable_parts(reference.value)
        return self.denylist.denies(parts)

    def visible_child_names(
        self,
        directory: ResolvedPath,
        names: Iterable[str],
    ) -> tuple[str, ...]:
        if directory.kind is not PathKind.DIRECTORY or not directory.exists:
            raise PathPolicyError(
                "not_directory",
                "Filtering children requires an existing directory authorization",
                virtual_path=directory.virtual_path,
            )
        base_parts = self._portable_parts(directory.reference.value)
        visible: list[str] = []
        for name in names:
            child_parts = self._portable_parts(name)
            if len(child_parts) != 1:
                raise PathPolicyError("invalid_path", "Directory child names must be one segment")
            if not self.denylist.denies(base_parts + child_parts):
                visible.append(name)
        return tuple(visible)

    def _inspect_components(
        self,
        host_root: Path,
        parts: tuple[str, ...],
        virtual_path: str,
    ) -> tuple[tuple[ComponentFingerprint, ...], Path, bool]:
        try:
            root_stat = host_root.lstat()
        except FileNotFoundError as error:
            raise PathPolicyError(
                "session_integrity_error",
                "Physical session root is missing",
                virtual_path=virtual_path,
            ) from error
        except OSError as error:
            raise PathPolicyError(
                "session_integrity_error",
                f"Cannot inspect physical session root: {error}",
                virtual_path=virtual_path,
            ) from error
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise PathPolicyError(
                "path_escape",
                "Physical session root is not a real directory",
                virtual_path=virtual_path,
            )

        fingerprints = [self._fingerprint((), root_stat)]
        current = host_root
        exists = True
        for index, part in enumerate(parts):
            current = current / part
            is_final = index == len(parts) - 1
            try:
                item_stat = current.lstat()
            except FileNotFoundError:
                if not is_final:
                    raise PathPolicyError(
                        "path_not_found",
                        "A parent directory does not exist",
                        virtual_path=virtual_path,
                    )
                exists = False
                break
            except OSError as error:
                raise PathPolicyError(
                    "path_unavailable",
                    f"Cannot inspect path: {error}",
                    virtual_path=virtual_path,
                ) from error
            if stat.S_ISLNK(item_stat.st_mode):
                raise PathPolicyError(
                    "path_escape",
                    "Symbolic links are not allowed in authorized paths",
                    virtual_path=virtual_path,
                )
            if not is_final and not stat.S_ISDIR(item_stat.st_mode):
                raise PathPolicyError(
                    "not_directory",
                    "A path component is not a directory",
                    virtual_path=virtual_path,
                )
            if (
                self.reject_hardlinks
                and stat.S_ISREG(item_stat.st_mode)
                and item_stat.st_nlink > 1
            ):
                raise PathPolicyError(
                    "path_escape",
                    "Hard-linked files are not allowed in authorized paths",
                    virtual_path=virtual_path,
                )
            fingerprints.append(self._fingerprint(parts[: index + 1], item_stat))
        return tuple(fingerprints), current, exists

    @staticmethod
    def _validate_existence(
        expected: PathExistence,
        exists: bool,
        virtual_path: str,
    ) -> None:
        if expected is PathExistence.MUST_EXIST and not exists:
            raise PathPolicyError(
                "path_not_found",
                "Path does not exist",
                virtual_path=virtual_path,
            )
        if expected is PathExistence.MUST_NOT_EXIST and exists:
            raise PathPolicyError(
                "path_exists",
                "Path already exists",
                virtual_path=virtual_path,
            )

    @staticmethod
    def _validate_kind(file_type: int, expected: PathKind, virtual_path: str) -> None:
        if expected is PathKind.FILE and file_type != stat.S_IFREG:
            raise PathPolicyError("not_file", "Path is not a regular file", virtual_path=virtual_path)
        if expected is PathKind.DIRECTORY and file_type != stat.S_IFDIR:
            raise PathPolicyError("not_directory", "Path is not a directory", virtual_path=virtual_path)
        if expected is PathKind.ANY and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
            raise PathPolicyError(
                "unsupported_file_type",
                "Path is not a regular file or directory",
                virtual_path=virtual_path,
            )

    @staticmethod
    def _assert_contained(
        host_root: Path,
        host_path: Path,
        exists: bool,
        virtual_path: str,
    ) -> None:
        try:
            root_real = host_root.resolve(strict=True)
            target_real = host_path.resolve(strict=exists)
            target_real.relative_to(root_real)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
            raise PathPolicyError(
                "path_escape",
                "Path does not remain inside its session root",
                virtual_path=virtual_path,
            ) from error

    def _host_root(self, root: Root) -> Path:
        if root is Root.INPUT:
            return self.session_paths.host_input
        if root is Root.OUTPUT:
            return self.session_paths.host_output
        raise PathPolicyError("invalid_root", "Unknown virtual root")

    def _virtual_path(self, root: Root, parts: tuple[str, ...]) -> str:
        base = f"/{root.value}/{self.session_paths.session_id}"
        return base if not parts else base + "/" + "/".join(parts)

    @staticmethod
    def _fingerprint(
        relative_parts: tuple[str, ...],
        item_stat: os.stat_result,
    ) -> ComponentFingerprint:
        return ComponentFingerprint(
            relative_parts=relative_parts,
            device=item_stat.st_dev,
            inode=item_stat.st_ino,
            file_type=stat.S_IFMT(item_stat.st_mode),
        )

    @staticmethod
    def _portable_parts(value: str) -> tuple[str, ...]:
        if not isinstance(value, str) or not value:
            raise PathPolicyError("invalid_path", "Path must be non-empty text")
        if len(value.encode("utf-8")) > MAX_VIRTUAL_PATH_BYTES:
            raise PathPolicyError("invalid_path", "Path exceeds the portable length limit")
        if value == ".":
            return ()
        if value.startswith(("/", "\\")) or _DRIVE_PREFIX.match(value):
            raise PathPolicyError("path_escape", "Absolute paths are forbidden")
        if "\\" in value or "\x00" in value:
            raise PathPolicyError("path_escape", "Alternate separators and NUL bytes are forbidden")
        if value.endswith("/") or "//" in value:
            raise PathPolicyError("invalid_path", "Empty path segments are forbidden")

        parts = tuple(value.split("/"))
        for part in parts:
            if part in {"", ".", ".."}:
                code = "path_escape" if part == ".." else "invalid_path"
                raise PathPolicyError(code, "Dot and empty path segments are forbidden")
            if len(part.encode("utf-8")) > MAX_SEGMENT_BYTES:
                raise PathPolicyError("invalid_path", "Path segment exceeds 255 bytes")
            if part.endswith((" ", ".")):
                raise PathPolicyError("invalid_path", "Path segments cannot end in a dot or space")
            if any(
                character in _WINDOWS_FORBIDDEN
                or ord(character) == 127
                or unicodedata.category(character).startswith("C")
                for character in part
            ):
                raise PathPolicyError("invalid_path", "Path contains non-portable characters")
            reserved_name = part.split(".", 1)[0].upper()
            if reserved_name in _WINDOWS_RESERVED:
                raise PathPolicyError("invalid_path", "Path uses a reserved device name")
        return parts
