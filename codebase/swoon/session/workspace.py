"""Consumer-visible workspace layout for named Swoon sessions."""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

from .errors import SessionError, SessionImportError, SessionNotFoundError
from .manager import (
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_RESULT_BYTES,
    DEFAULT_MAX_STATE_BYTES,
    SessionManager,
)
from .models import ImportLimits, Session, SessionPaths, SessionState, SessionStatus, validate_session_id


WORKSPACE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def validate_workspace_name(name: str) -> None:
    if not isinstance(name, str) or not WORKSPACE_NAME_PATTERN.fullmatch(name):
        raise SessionError(
            "invalid_workspace_name",
            "Workspace name must contain 1-64 letters, numbers, underscores, or hyphens",
        )


def session_id_for_workspace(name: str) -> str:
    validate_workspace_name(name)
    return f"sess_{name}"


def workspace_name_for_session(session_id: str) -> str:
    validate_session_id(session_id)
    return session_id.removeprefix("sess_")


def default_work_directory() -> Path:
    """Return the configured consumer work root without exposing implementation metadata."""

    configured = os.environ.get("SWOON_WORK_ROOT")
    if configured:
        return Path(configured).expanduser().absolute()
    if os.name == "nt":
        parent = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if parent:
            return Path(parent) / "Swoon Code" / "work"
        return Path.home() / "AppData" / "Local" / "Swoon Code" / "work"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Swoon Code" / "work"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    parent = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return parent / "swoon-code" / "work"


class WorkspaceSessionManager(SessionManager):
    """Store named input/output trees visibly and keep state in ``work/.sessions``."""

    def __init__(
        self,
        work_dir: str | Path | None = None,
        *,
        import_limits: ImportLimits | None = None,
        max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
    ) -> None:
        self.work_dir = Path(work_dir or default_work_directory()).expanduser().absolute()
        self.input_dir = self.work_dir / "input"
        self.output_dir = self.work_dir / "output"
        self._ensure_private_directory(self.work_dir)
        self._ensure_private_directory(self.input_dir)
        self._ensure_private_directory(self.output_dir)
        super().__init__(
            self.work_dir / ".sessions",
            import_limits=import_limits,
            max_state_bytes=max_state_bytes,
            max_result_bytes=max_result_bytes,
        )

    def paths(self, session_id: str) -> SessionPaths:
        validate_session_id(session_id)
        name = workspace_name_for_session(session_id)
        host_root = self.base_dir / session_id
        return SessionPaths(
            session_id=session_id,
            host_root=host_root,
            host_input=self.input_dir / name,
            host_output=self.output_dir / name,
            state_file=host_root / "state.json",
            lock_file=host_root / ".lock",
        )

    def create(
        self,
        source_project: str | Path | None = None,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        session_id: str | None = None,
    ) -> Session:
        if type(max_steps) is not int or not 1 <= max_steps <= 10_000:
            raise SessionError("invalid_max_steps", "max_steps must be between 1 and 10000")
        source = self._validate_source_project(source_project) if source_project else None
        identifier = session_id or self._new_session_id()
        validate_session_id(identifier)
        paths = self.paths(identifier)

        adopted_input = paths.host_input.exists() or paths.host_input.is_symlink()
        if adopted_input and source is not None:
            raise SessionImportError(
                f"Input folder already exists for {workspace_name_for_session(identifier)!r}; "
                "remove --project and use the files already in work/input"
            )
        if paths.host_output.exists() or paths.host_output.is_symlink():
            raise SessionError(
                "workspace_output_exists",
                f"Output folder already exists for {workspace_name_for_session(identifier)!r}",
            )
        if adopted_input:
            self._validate_adopted_input(paths.host_input)

        created_input = False
        created_output = False
        try:
            paths.host_root.mkdir(mode=0o700)
            if not adopted_input:
                paths.host_input.mkdir(mode=0o700)
                created_input = True
            paths.host_output.mkdir(mode=0o700)
            created_output = True
            if source is not None:
                self._copy_project(source, paths.host_input)
            self._seal_input_tree(paths.host_input)

            now = self._now()
            state = SessionState(
                session_id=identifier,
                status=SessionStatus.ACTIVE,
                created_at=now,
                updated_at=now,
                max_steps=max_steps,
            )
            self._write_state(paths, state, creating=True)
            self._create_lock_file(paths.lock_file)
            return Session(paths=paths, state=state)
        except Exception:
            self._remove_failed_layout(paths.host_root)
            if created_output:
                self._remove_failed_layout(paths.host_output)
            if created_input:
                self._remove_failed_layout(paths.host_input)
            raise

    def load_name(self, name: str) -> Session:
        return self.load(session_id_for_workspace(name))

    def load_name_if_present(self, name: str) -> Session | None:
        try:
            return self.load_name(name)
        except SessionNotFoundError:
            return None

    def delete_session(self, session_id: str, *, force_active: bool = False) -> None:
        """Delete hidden state and the matching visible input/output folders."""

        paths = self.paths(session_id)
        self.load(session_id)
        super().delete_session(session_id, force_active=force_active)
        try:
            for visible_root in (paths.host_input, paths.host_output):
                self._remove_failed_layout(visible_root)
                if visible_root.exists() or visible_root.is_symlink():
                    raise OSError(f"Could not remove {visible_root}")
            self._fsync_directory(self.input_dir)
            self._fsync_directory(self.output_dir)
        except OSError as error:
            raise SessionError(
                "session_delete_failed",
                "Session state was deleted but visible workspace cleanup failed",
            ) from error

    def _validate_export_destination(self, paths: SessionPaths, target: Path) -> None:
        super()._validate_export_destination(paths, target)
        for protected in (self.work_dir, self.input_dir, self.output_dir):
            try:
                target.relative_to(protected.resolve(strict=True))
            except ValueError:
                continue
            raise SessionError(
                "invalid_export_destination",
                "Session output cannot be exported inside the active work directory",
            )

    def _validate_adopted_input(self, root: Path) -> None:
        try:
            root_mode = root.lstat().st_mode
        except OSError as error:
            raise SessionImportError(f"Cannot inspect input folder: {error}") from error
        if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
            raise SessionImportError("Named input must be a regular directory")

        files = 0
        total_bytes = 0
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            for name in directories:
                mode = (directory_path / name).lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise SessionImportError("Input folders cannot contain links or special entries")
            for name in filenames:
                path = directory_path / name
                item = path.lstat()
                if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
                    raise SessionImportError("Input folders cannot contain links or special files")
                if self.import_limits.reject_hardlinks and item.st_nlink > 1:
                    raise SessionImportError("Input folders cannot contain hard-linked files")
                if item.st_size > self.import_limits.max_file_bytes:
                    raise SessionImportError(f"File exceeds import limit: {path}")
                files += 1
                total_bytes += item.st_size
                if files > self.import_limits.max_files:
                    raise SessionImportError("Input folder contains too many files")
                if total_bytes > self.import_limits.max_total_bytes:
                    raise SessionImportError("Input folder exceeds total import size limit")
