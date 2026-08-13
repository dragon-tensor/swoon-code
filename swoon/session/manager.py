"""Creation, import, persistence, and lifecycle updates for Swoon sessions."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from swoon.aeml.models import PathRef, Result, ResultStatus, Root

from .errors import (
    SessionConflictError,
    SessionError,
    SessionImportError,
    SessionNotFoundError,
    StepLimitReachedError,
)
from .models import (
    ActionRecord,
    ChunkRecord,
    ImportLimits,
    ProcessRecord,
    PROCESS_HANDLE_PATTERN,
    ProcessStatus,
    Session,
    SessionPaths,
    SessionState,
    SessionStatus,
    validate_session_id,
)


DEFAULT_MAX_STEPS = 40
DEFAULT_MAX_STATE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_RESULT_BYTES = 512 * 1024


def default_session_directory() -> Path:
    """Return an OS-appropriate private data location for physical sessions."""

    if os.name == "nt":
        parent = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if parent:
            return Path(parent) / "swoon-code" / "sessions"
        return Path.home() / "AppData" / "Local" / "swoon-code" / "sessions"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "swoon-code" / "sessions"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    parent = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return parent / "swoon-code" / "sessions"


class SessionManager:
    """Manage isolated session folders and crash-safe state.

    The paths exposed to AEML are always ``/input/<id>`` and ``/output/<id>``.
    Their physical host locations remain an interpreter implementation detail.
    """

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        import_limits: ImportLimits | None = None,
        max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
    ) -> None:
        if max_state_bytes < 1 or max_result_bytes < 1:
            raise ValueError("State and result limits must be positive")
        self.base_dir = Path(base_dir or default_session_directory()).expanduser().absolute()
        self.import_limits = import_limits or ImportLimits()
        self.max_state_bytes = max_state_bytes
        self.max_result_bytes = max_result_bytes
        self._ensure_private_directory(self.base_dir)

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
        if source is not None:
            try:
                self.base_dir.resolve().relative_to(source)
            except ValueError:
                pass
            else:
                raise SessionImportError("Session storage cannot be located inside the source project")

        identifier = session_id or self._new_session_id()
        validate_session_id(identifier)
        paths = self.paths(identifier)
        try:
            paths.host_root.mkdir(mode=0o700)
        except FileExistsError as error:
            raise SessionError("session_exists", f"Session {identifier!r} already exists") from error
        except OSError as error:
            raise SessionError("session_create_failed", f"Could not create session: {error}") from error

        try:
            paths.host_input.mkdir(mode=0o700)
            paths.host_output.mkdir(mode=0o700)
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
            raise

    def load(self, session_id: str) -> Session:
        validate_session_id(session_id)
        paths = self.paths(session_id)
        if not paths.host_root.exists():
            raise SessionNotFoundError(session_id)
        self._validate_layout(paths)
        state = self._read_state(paths.state_file)
        if state.session_id != session_id:
            raise SessionError(
                "session_integrity_error",
                "Session state ID does not match its directory",
            )
        return Session(paths=paths, state=state)

    def paths(self, session_id: str) -> SessionPaths:
        validate_session_id(session_id)
        host_root = self.base_dir / session_id
        return SessionPaths(
            session_id=session_id,
            host_root=host_root,
            host_input=host_root / "input",
            host_output=host_root / "output",
            state_file=host_root / "state.json",
            lock_file=host_root / ".lock",
        )

    def advance_step(self, session: Session) -> Session:
        def mutate(state: SessionState) -> SessionState:
            self._require_active(state)
            if state.step >= state.max_steps:
                raise StepLimitReachedError(state.session_id, state.max_steps)
            return replace(state, step=state.step + 1)

        return self._update(session, mutate)

    def set_plan(self, session: Session, plan: str | None) -> Session:
        if plan is not None and not isinstance(plan, str):
            raise SessionError("invalid_plan", "Plan must be text or null")
        def mutate(state: SessionState) -> SessionState:
            self._require_not_terminal(state)
            return replace(state, plan=plan)

        return self._update(session, mutate)

    def set_status(self, session: Session, status: SessionStatus) -> Session:
        if not isinstance(status, SessionStatus):
            raise SessionError("invalid_status", "Unknown session status")

        transitions = {
            SessionStatus.ACTIVE: {
                SessionStatus.WAITING_USER,
                SessionStatus.COMPLETED,
                SessionStatus.ABORTED,
            },
            SessionStatus.WAITING_USER: {
                SessionStatus.ACTIVE,
                SessionStatus.COMPLETED,
                SessionStatus.ABORTED,
            },
            SessionStatus.COMPLETED: set(),
            SessionStatus.ABORTED: set(),
        }

        def mutate(state: SessionState) -> SessionState:
            if status == state.status:
                return state
            if status not in transitions[state.status]:
                raise SessionError(
                    "invalid_status_transition",
                    f"Cannot transition from {state.status.value} to {status.value}",
                )
            return replace(state, status=status)

        return self._update(session, mutate)

    def record_action_result(self, session: Session, tool: str, result: Result) -> Session:
        if not isinstance(tool, str) or not tool.strip():
            raise SessionError("invalid_action_record", "Tool name cannot be empty")
        if (
            not isinstance(result, Result)
            or not result.action_id
            or not isinstance(result.status, ResultStatus)
        ):
            raise SessionError("invalid_action_record", "A structured action result is required")
        if len(result.body.encode("utf-8")) > self.max_result_bytes:
            raise SessionError(
                "result_too_large",
                f"Persisted result exceeds {self.max_result_bytes} bytes",
            )

        def mutate(state: SessionState) -> SessionState:
            self._require_active(state)
            if state.action(result.action_id) is not None:
                raise SessionError(
                    "duplicate_action_id",
                    f"Action {result.action_id!r} already has a persisted result",
                )
            record = ActionRecord(
                action_id=result.action_id,
                tool=tool,
                result=result,
                completed_at=self._now(),
            )
            return replace(
                state,
                action_ledger=state.action_ledger + (record,),
                result_history=state.result_history + (result.action_id,),
            )

        return self._update(session, mutate)

    def record_chunk(
        self,
        session: Session,
        path: PathRef,
        *,
        seq: int,
        final: bool,
    ) -> Session:
        if path.root is not Root.OUTPUT:
            raise SessionError("input_readonly", "Chunk writes may only target the output root")
        if not path.value.strip():
            raise SessionError("invalid_path", "Chunk path cannot be empty")
        if type(seq) is not int or seq < 1 or type(final) is not bool:
            raise SessionError("chunk_sequence_error", "Invalid chunk sequence metadata")

        def mutate(state: SessionState) -> SessionState:
            self._require_active(state)
            existing = state.chunk(path)
            if existing is None:
                if seq != 1:
                    raise SessionError("chunk_sequence_error", "A new chunk sequence must start at 1")
                record = ChunkRecord(path, next_seq=2, finalized=final, updated_at=self._now())
                return replace(state, chunks=state.chunks + (record,))
            if existing.finalized:
                raise SessionError("chunk_sequence_error", "Chunk sequence is already finalized")
            if seq != existing.next_seq:
                raise SessionError(
                    "chunk_sequence_error",
                    f"Expected chunk sequence {existing.next_seq}, received {seq}",
                )
            replacement = replace(
                existing,
                next_seq=seq + 1,
                finalized=final,
                updated_at=self._now(),
            )
            chunks = tuple(replacement if item.path == path else item for item in state.chunks)
            return replace(state, chunks=chunks)

        return self._update(session, mutate)

    def register_process(self, session: Session, *, pid: int, handle: str | None = None) -> str:
        if type(pid) is not int or pid < 1:
            raise SessionError("invalid_process", "Process PID must be a positive integer")
        process_handle = handle or f"proc_{secrets.token_hex(12)}"
        if not PROCESS_HANDLE_PATTERN.fullmatch(process_handle):
            raise SessionError("invalid_process", "Invalid process handle")

        def mutate(state: SessionState) -> SessionState:
            self._require_active(state)
            if state.process(process_handle) is not None:
                raise SessionError("duplicate_process_handle", "Process handle already exists")
            process = ProcessRecord(
                handle=process_handle,
                pid=pid,
                status=ProcessStatus.RUNNING,
                output_offset=0,
                started_at=self._now(),
            )
            return replace(state, processes=state.processes + (process,))

        self._update(session, mutate)
        return process_handle

    def update_process(
        self,
        session: Session,
        handle: str,
        *,
        status: ProcessStatus | None = None,
        output_offset: int | None = None,
    ) -> Session:
        if status is not None and not isinstance(status, ProcessStatus):
            raise SessionError("invalid_process", "Unknown process status")
        if output_offset is not None and (type(output_offset) is not int or output_offset < 0):
            raise SessionError("invalid_process", "Output offset must be non-negative")

        def mutate(state: SessionState) -> SessionState:
            existing = state.process(handle)
            if existing is None:
                raise SessionError("unknown_process_handle", f"Unknown process handle {handle!r}")
            if existing.status is not ProcessStatus.RUNNING and status not in {None, existing.status}:
                raise SessionError(
                    "invalid_process_transition",
                    f"Process {handle!r} is already {existing.status.value}",
                )
            if output_offset is not None and output_offset < existing.output_offset:
                raise SessionError(
                    "invalid_process",
                    "Process output offset cannot move backwards",
                )
            replacement = replace(
                existing,
                status=status or existing.status,
                output_offset=existing.output_offset if output_offset is None else output_offset,
            )
            processes = tuple(
                replacement if item.handle == handle else item for item in state.processes
            )
            return replace(state, processes=processes)

        return self._update(session, mutate)

    def _update(
        self,
        session: Session,
        mutation: Callable[[SessionState], SessionState],
    ) -> Session:
        expected_paths = self.paths(session.id)
        if session.paths != expected_paths:
            raise SessionError("session_integrity_error", "Session belongs to another manager")

        with self._exclusive_lock(session.paths.lock_file):
            persisted = self._read_state(session.paths.state_file)
            if persisted.revision != session.state.revision:
                raise SessionConflictError(session.id)
            candidate = mutation(persisted)
            if candidate.session_id != persisted.session_id:
                raise SessionError("session_integrity_error", "Mutation changed the session ID")
            updated = replace(
                candidate,
                revision=persisted.revision + 1,
                updated_at=self._now(),
            )
            self._write_state(session.paths, updated, creating=False)

        session.state = updated
        return session

    def _validate_layout(self, paths: SessionPaths) -> None:
        for path, label in (
            (paths.host_root, "session"),
            (paths.host_input, "input"),
            (paths.host_output, "output"),
        ):
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError as error:
                raise SessionError("session_integrity_error", f"Missing {label} directory") from error
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise SessionError("session_integrity_error", f"Invalid {label} directory")
        try:
            state_mode = paths.state_file.lstat().st_mode
        except FileNotFoundError as error:
            raise SessionError("session_integrity_error", "Missing session state") from error
        if not stat.S_ISREG(state_mode) or stat.S_ISLNK(state_mode):
            raise SessionError("session_integrity_error", "Session state is not a regular file")
        if os.name != "nt":
            if stat.S_IMODE(state_mode) & 0o077:
                raise SessionError("session_integrity_error", "Session state permissions are too broad")
            self._assert_input_tree_readonly(paths.host_input)

    def _read_state(self, state_file: Path) -> SessionState:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(state_file, flags)
        except FileNotFoundError as error:
            raise SessionError("session_integrity_error", "Session state is missing") from error
        except OSError as error:
            raise SessionError("session_integrity_error", f"Cannot open session state: {error}") from error
        with os.fdopen(descriptor, "rb") as state_stream:
            payload = state_stream.read(self.max_state_bytes + 1)
        if len(payload) > self.max_state_bytes:
            raise SessionError("invalid_session_state", "Session state exceeds its size limit")
        try:
            raw: Any = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SessionError("invalid_session_state", f"Invalid session JSON: {error}") from error
        return SessionState.from_dict(raw)

    def _write_state(self, paths: SessionPaths, state: SessionState, *, creating: bool) -> None:
        payload = (
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(payload) > self.max_state_bytes:
            raise SessionError("session_state_too_large", "Session state exceeds its size limit")
        if creating and paths.state_file.exists():
            raise SessionError("session_integrity_error", "Session state already exists")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".state-",
            suffix=".tmp",
            dir=paths.host_root,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as state_stream:
                state_stream.write(payload)
                state_stream.flush()
                os.fsync(state_stream.fileno())
            os.replace(temporary_path, paths.state_file)
            self._fsync_directory(paths.host_root)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary_path.unlink(missing_ok=True)
            raise

    def _copy_project(self, source: Path, destination: Path) -> None:
        counters = {"files": 0, "bytes": 0}
        try:
            self._copy_directory_contents(source, destination, counters)
        except SessionImportError:
            raise
        except OSError as error:
            raise SessionImportError(f"Could not import project: {error}") from error

    def _copy_directory_contents(
        self,
        source: Path,
        destination: Path,
        counters: dict[str, int],
    ) -> None:
        source_mode = source.lstat().st_mode
        if stat.S_ISLNK(source_mode) or not stat.S_ISDIR(source_mode):
            raise SessionImportError(f"Unsafe project directory entry: {source}")

        with os.scandir(source) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                source_path = Path(entry.path)
                destination_path = destination / entry.name
                entry_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise SessionImportError(f"Symbolic links are not imported: {source_path}")
                if stat.S_ISDIR(entry_stat.st_mode):
                    destination_path.mkdir(mode=0o700)
                    self._copy_directory_contents(source_path, destination_path, counters)
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    raise SessionImportError(f"Special files are not imported: {source_path}")
                if self.import_limits.reject_hardlinks and entry_stat.st_nlink > 1:
                    raise SessionImportError(f"Hard-linked files are not imported: {source_path}")
                if entry_stat.st_size > self.import_limits.max_file_bytes:
                    raise SessionImportError(f"File exceeds import limit: {source_path}")

                counters["files"] += 1
                if counters["files"] > self.import_limits.max_files:
                    raise SessionImportError("Project contains too many files")
                self._copy_regular_file(source_path, destination_path, entry_stat, counters)

    def _copy_regular_file(
        self,
        source: Path,
        destination: Path,
        expected_stat: os.stat_result,
        counters: dict[str, int],
    ) -> None:
        source_flags = os.O_RDONLY
        source_flags |= getattr(os, "O_CLOEXEC", 0)
        source_flags |= getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source, source_flags)
        try:
            opened_stat = os.fstat(source_fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise SessionImportError(f"Project file changed type during import: {source}")
            if (opened_stat.st_dev, opened_stat.st_ino) != (
                expected_stat.st_dev,
                expected_stat.st_ino,
            ):
                raise SessionImportError(f"Project file changed during import: {source}")
            if self.import_limits.reject_hardlinks and opened_stat.st_nlink > 1:
                raise SessionImportError(f"Hard-linked files are not imported: {source}")

            destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            destination_flags |= getattr(os, "O_CLOEXEC", 0)
            destination_fd = os.open(destination, destination_flags, 0o600)
            try:
                with os.fdopen(source_fd, "rb", closefd=False) as source_stream:
                    with os.fdopen(destination_fd, "wb", closefd=False) as destination_stream:
                        self._copy_stream(source_stream, destination_stream, source, counters)
                        destination_stream.flush()
                        os.fsync(destination_stream.fileno())
                executable = bool(opened_stat.st_mode & 0o111)
                os.fchmod(destination_fd, 0o700 if executable else 0o600)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)

    def _copy_stream(
        self,
        source_stream: BinaryIO,
        destination_stream: BinaryIO,
        source: Path,
        counters: dict[str, int],
    ) -> None:
        file_bytes = 0
        while block := source_stream.read(1024 * 1024):
            file_bytes += len(block)
            counters["bytes"] += len(block)
            if file_bytes > self.import_limits.max_file_bytes:
                raise SessionImportError(f"File grew beyond import limit: {source}")
            if counters["bytes"] > self.import_limits.max_total_bytes:
                raise SessionImportError("Project exceeds total import size limit")
            destination_stream.write(block)

    @staticmethod
    def _validate_source_project(source_project: str | Path) -> Path:
        candidate = Path(source_project).expanduser()
        try:
            if candidate.is_symlink():
                raise SessionImportError("The source project itself cannot be a symbolic link")
            source = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise SessionImportError(f"Source project does not exist: {candidate}") from error
        if not source.is_dir():
            raise SessionImportError("Source project must be a directory")
        return source

    @staticmethod
    def _seal_input_tree(root: Path) -> None:
        for directory, directories, files in os.walk(root, topdown=False, followlinks=False):
            directory_path = Path(directory)
            for name in files:
                path = directory_path / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise SessionImportError(f"Unsafe input entry while sealing: {path}")
                path.chmod(0o500 if mode & 0o111 else 0o400)
            for name in directories:
                path = directory_path / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise SessionImportError(f"Unsafe input directory while sealing: {path}")
                path.chmod(0o500)
        root.chmod(0o500)

    @staticmethod
    def _assert_input_tree_readonly(root: Path) -> None:
        for directory, directories, files in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            for name in directories + files:
                path = directory_path / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise SessionError("session_integrity_error", f"Input contains a symlink: {path}")
                if stat.S_IMODE(mode) & 0o222:
                    raise SessionError("session_integrity_error", f"Input is writable: {path}")
        if stat.S_IMODE(root.lstat().st_mode) & 0o222:
            raise SessionError("session_integrity_error", "Input root is writable")

    @staticmethod
    def _remove_failed_layout(root: Path) -> None:
        if not root.exists() or root.is_symlink():
            return
        for directory, directories, files in os.walk(root, topdown=False, followlinks=False):
            directory_path = Path(directory)
            for name in files:
                try:
                    (directory_path / name).chmod(0o600)
                except OSError:
                    pass
            for name in directories:
                try:
                    (directory_path / name).chmod(0o700)
                except OSError:
                    pass
            try:
                directory_path.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(root)

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise SessionError("session_integrity_error", f"Invalid session base directory: {path}")
        if os.name != "nt":
            path.chmod(0o700)

    @staticmethod
    def _create_lock_file(path: Path) -> None:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SessionError("session_integrity_error", "Session lock is not a regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    @contextmanager
    def _exclusive_lock(self, path: Path) -> Iterator[None]:
        self._create_lock_file(path)
        flags = os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _require_active(state: SessionState) -> None:
        if state.status is not SessionStatus.ACTIVE:
            raise SessionError(
                "session_not_active",
                f"Session is {state.status.value}, not active",
            )

    @staticmethod
    def _require_not_terminal(state: SessionState) -> None:
        if state.status in {SessionStatus.COMPLETED, SessionStatus.ABORTED}:
            raise SessionError(
                "session_terminal",
                f"Session is already {state.status.value}",
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _new_session_id() -> str:
        return f"sess_{secrets.token_hex(16)}"
