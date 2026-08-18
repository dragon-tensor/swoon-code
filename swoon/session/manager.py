"""Creation, import, persistence, and lifecycle updates for Swoon sessions."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator

from swoon.aeml.models import Action, PathRef, Result, ResultStatus, Root

from .errors import (
    SessionConflictError,
    SessionError,
    SessionImportError,
    SessionNotFoundError,
    StepLimitReachedError,
)
from .models import (
    ACTION_ID_PATTERN,
    ActionRecord,
    ChunkRecord,
    ImportLimits,
    PendingConfirmation,
    ProcessRecord,
    PROCESS_HANDLE_PATTERN,
    ProcessStatus,
    ProcessTerminationReason,
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

    def list_session_ids(self) -> tuple[str, ...]:
        """Return deterministic candidate session IDs without trusting their contents."""

        identifiers: list[str] = []
        try:
            entries = os.scandir(self.base_dir)
        except OSError as error:
            raise SessionError("session_list_failed", "Could not list session storage") from error
        with entries:
            for entry in entries:
                try:
                    validate_session_id(entry.name)
                except SessionError:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        identifiers.append(entry.name)
                except OSError:
                    identifiers.append(entry.name)
        return tuple(sorted(identifiers))

    def export_output(self, session_id: str, destination: str | Path) -> Path:
        """Copy one terminal session's output to a new user-selected directory."""

        paths = self.paths(session_id)
        self.load(session_id)
        target = self._resolved_missing_destination(destination)
        self._validate_export_destination(paths, target)
        with self._exclusive_lock(paths.lock_file):
            self._validate_layout(paths)
            state = self._read_state(paths.state_file)
            if state.session_id != session_id:
                raise SessionError(
                    "session_integrity_error",
                    "Session state ID does not match its directory",
                )
            if state.status not in {SessionStatus.COMPLETED, SessionStatus.ABORTED}:
                raise SessionError(
                    "session_not_terminal",
                    "Only completed or aborted sessions can be exported",
                )
            target.mkdir(mode=0o700)
            try:
                self._copy_project(paths.host_output, target)
            except Exception:
                self._remove_failed_layout(target)
                raise
        self._fsync_directory(target.parent)
        return target

    def delete_session(self, session_id: str, *, force_active: bool = False) -> None:
        """Delete one exact session after validation, refusing live work by default."""

        if type(force_active) is not bool:
            raise TypeError("force_active must be boolean")
        paths = self.paths(session_id)
        self.load(session_id)
        tombstone = self.base_dir / f".delete-{session_id}-{secrets.token_hex(8)}"
        moved = False
        with self._exclusive_lock(paths.lock_file):
            self._validate_layout(paths)
            state = self._read_state(paths.state_file)
            if state.session_id != session_id:
                raise SessionError(
                    "session_integrity_error",
                    "Session state ID does not match its directory",
                )
            if any(process.status is ProcessStatus.RUNNING for process in state.processes):
                raise SessionError(
                    "session_process_running",
                    "A session with recorded running work cannot be deleted",
                )
            if (
                state.status not in {SessionStatus.COMPLETED, SessionStatus.ABORTED}
                and not force_active
            ):
                raise SessionError(
                    "session_not_terminal",
                    "Abort or complete the session before deletion, or explicitly force it",
                )
            if os.name != "nt":
                os.replace(paths.host_root, tombstone)
                moved = True
        if not moved:
            os.replace(paths.host_root, tombstone)
        try:
            self._remove_failed_layout(tombstone)
            self._fsync_directory(self.base_dir)
        except OSError as error:
            raise SessionError(
                "session_delete_failed",
                f"Session was isolated at {tombstone.name!r} but cleanup failed",
            ) from error

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

    def _validate_export_destination(
        self,
        paths: SessionPaths,
        target: Path,
    ) -> None:
        for protected in (paths.host_root, self.base_dir):
            try:
                target.relative_to(protected.resolve(strict=True))
            except ValueError:
                continue
            raise SessionError(
                "invalid_export_destination",
                "Session output cannot be exported inside session storage",
            )

    @staticmethod
    def _resolved_missing_destination(destination: str | Path) -> Path:
        candidate = Path(destination).expanduser()
        if not candidate.name:
            raise SessionError("invalid_export_destination", "Export destination is invalid")
        if candidate.exists() or candidate.is_symlink():
            raise SessionError(
                "export_destination_exists",
                "Export destination must not already exist",
            )
        try:
            parent = candidate.parent.resolve(strict=True)
        except OSError as error:
            raise SessionError(
                "invalid_export_destination",
                "Export destination parent does not exist",
            ) from error
        if not parent.is_dir():
            raise SessionError(
                "invalid_export_destination",
                "Export destination parent is not a directory",
            )
        return parent / candidate.name

    def advance_step(self, session: Session) -> Session:
        def mutate(state: SessionState) -> SessionState:
            self._require_active(state)
            if state.step >= state.max_steps:
                raise StepLimitReachedError(state.session_id, state.max_steps)
            return replace(state, step=state.step + 1)

        return self._update(session, mutate)

    def extend_step_limit(self, session: Session, additional_steps: int) -> Session:
        """Extend an exhausted budget after an explicit human-side approval."""

        if type(additional_steps) is not int or additional_steps < 1:
            raise SessionError(
                "invalid_step_extension",
                "additional_steps must be a positive integer",
            )

        def mutate(state: SessionState) -> SessionState:
            if state.status is not SessionStatus.WAITING_USER or state.step < state.max_steps:
                raise SessionError(
                    "step_extension_not_allowed",
                    "Step limits may only be extended while waiting at an exhausted limit",
                )
            new_limit = state.max_steps + additional_steps
            if new_limit > 10_000:
                raise SessionError(
                    "invalid_step_extension",
                    "Extended max_steps cannot exceed 10000",
                )
            return replace(state, max_steps=new_limit)

        return self._update(session, mutate)

    def reserve_action_ids(self, session: Session, action_ids: Iterable[str]) -> Session:
        """Persist action IDs before dispatch so failed attempts cannot be reused."""

        if isinstance(action_ids, (str, bytes)):
            raise SessionError("invalid_action_id", "action_ids must be an iterable of IDs")
        try:
            identifiers = tuple(action_ids)
        except TypeError as error:
            raise SessionError(
                "invalid_action_id",
                "action_ids must be an iterable of IDs",
            ) from error
        if not identifiers:
            return session
        for identifier in identifiers:
            if not isinstance(identifier, str) or not ACTION_ID_PATTERN.fullmatch(identifier):
                raise SessionError("invalid_action_id", "Invalid action ID")
        if len(identifiers) != len(set(identifiers)):
            raise SessionError("duplicate_action_id", "Action IDs contain a duplicate")

        def mutate(state: SessionState) -> SessionState:
            self._require_active(state)
            already_used = set(state.used_action_ids).intersection(identifiers)
            if already_used:
                duplicate = sorted(already_used)[0]
                raise SessionError(
                    "duplicate_action_id",
                    f"Action {duplicate!r} has already been used",
                )
            return replace(
                state,
                used_action_ids=state.used_action_ids + identifiers,
            )

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
            if (
                state.pending_confirmation is not None
                and status is SessionStatus.ACTIVE
            ):
                raise SessionError(
                    "confirmation_pending",
                    "A pending confirmation must be approved or denied explicitly",
                )
            if status not in transitions[state.status]:
                raise SessionError(
                    "invalid_status_transition",
                    f"Cannot transition from {state.status.value} to {status.value}",
                )
            return replace(
                state,
                status=status,
                pending_confirmation=(
                    None
                    if status in {SessionStatus.COMPLETED, SessionStatus.ABORTED}
                    else state.pending_confirmation
                ),
            )

        return self._update(session, mutate)

    def record_action_result(
        self,
        session: Session,
        tool: str,
        result: Result,
        *,
        action_digest: str | None = None,
        chunk_path: PathRef | None = None,
        chunk_seq: int | None = None,
        chunk_final: bool | None = None,
        chunk_remove_scope: PathRef | None = None,
        chunk_move_from: PathRef | None = None,
        chunk_move_to: PathRef | None = None,
        resolve_confirmation: bool = False,
        process_handle: str | None = None,
        process_output_offset: int | None = None,
        process_output_bytes: int | None = None,
    ) -> Session:
        if not isinstance(tool, str) or not tool.strip():
            raise SessionError("invalid_action_record", "Tool name cannot be empty")
        if (
            not isinstance(result, Result)
            or not result.action_id
            or not ACTION_ID_PATTERN.fullmatch(result.action_id)
            or not isinstance(result.status, ResultStatus)
        ):
            raise SessionError("invalid_action_record", "A structured action result is required")
        if len(result.body.encode("utf-8")) > self.max_result_bytes:
            raise SessionError(
                "result_too_large",
                f"Persisted result exceeds {self.max_result_bytes} bytes",
            )
        if action_digest is not None and (
            not isinstance(action_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", action_digest)
        ):
            raise SessionError("invalid_action_record", "Invalid action digest")
        chunk_values = (chunk_path, chunk_seq, chunk_final)
        if any(value is not None for value in chunk_values):
            if (
                not isinstance(chunk_path, PathRef)
                or type(chunk_seq) is not int
                or chunk_seq < 1
                or type(chunk_final) is not bool
            ):
                raise SessionError(
                    "chunk_sequence_error",
                    "Chunk result metadata must be supplied together",
                )
        lifecycle_values = (chunk_remove_scope, chunk_move_from, chunk_move_to)
        if chunk_remove_scope is not None and any(
            value is not None for value in (chunk_move_from, chunk_move_to)
        ):
            raise SessionError(
                "invalid_action_record",
                "Chunk removal and relocation metadata are mutually exclusive",
            )
        if (chunk_move_from is None) != (chunk_move_to is None):
            raise SessionError(
                "invalid_action_record",
                "Chunk relocation metadata must be supplied together",
            )
        for value in lifecycle_values:
            if value is not None and (
                not isinstance(value, PathRef)
                or value.root is not Root.OUTPUT
                or not value.value.strip()
            ):
                raise SessionError(
                    "invalid_action_record",
                    "Lifecycle chunk metadata requires output paths",
                )
        if type(resolve_confirmation) is not bool:
            raise SessionError(
                "invalid_action_record",
                "resolve_confirmation must be boolean",
            )
        process_values = (
            process_handle,
            process_output_offset,
            process_output_bytes,
        )
        if any(value is not None for value in process_values):
            if (
                not isinstance(process_handle, str)
                or not PROCESS_HANDLE_PATTERN.fullmatch(process_handle)
                or type(process_output_offset) is not int
                or process_output_offset < 0
                or type(process_output_bytes) is not int
                or process_output_bytes < process_output_offset
            ):
                raise SessionError(
                    "invalid_process",
                    "Process result metadata must contain a valid handle and byte range",
                )

        def mutate(state: SessionState) -> SessionState:
            if resolve_confirmation:
                pending = state.pending_confirmation
                if (
                    state.status is not SessionStatus.WAITING_USER
                    or pending is None
                    or pending.action.id != result.action_id
                    or pending.action.tool != tool
                ):
                    raise SessionError(
                        "confirmation_mismatch",
                        "Result does not match the pending confirmation",
                    )
                state = replace(
                    state,
                    status=SessionStatus.ACTIVE,
                    pending_confirmation=None,
                )
            else:
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
                action_digest=action_digest,
            )
            if chunk_path is not None:
                assert chunk_seq is not None and chunk_final is not None
                state = self._state_with_chunk(
                    state,
                    chunk_path,
                    seq=chunk_seq,
                    final=chunk_final,
                )
            if chunk_remove_scope is not None:
                state = self._state_without_chunk_scope(state, chunk_remove_scope)
            elif chunk_move_from is not None:
                assert chunk_move_to is not None
                state = self._state_with_moved_chunk_scope(
                    state,
                    chunk_move_from,
                    chunk_move_to,
                )
            if process_handle is not None:
                assert process_output_offset is not None
                assert process_output_bytes is not None
                process = state.process(process_handle)
                if process is None:
                    raise SessionError(
                        "unknown_process_handle",
                        f"Unknown process handle {process_handle!r}",
                    )
                if process_output_bytes < process.output_bytes:
                    raise SessionError(
                        "invalid_process",
                        "Process output byte count cannot move backwards",
                    )
                replacement = replace(
                    process,
                    output_offset=max(process.output_offset, process_output_offset),
                    output_bytes=process_output_bytes,
                )
                state = replace(
                    state,
                    processes=tuple(
                        replacement if item.handle == process_handle else item
                        for item in state.processes
                    ),
                )
            return replace(
                state,
                action_ledger=state.action_ledger + (record,),
                result_history=state.result_history + (result.action_id,),
                used_action_ids=(
                    state.used_action_ids
                    if result.action_id in state.used_action_ids
                    else state.used_action_ids + (result.action_id,)
                ),
            )

        return self._update(session, mutate)

    def request_confirmation(
        self,
        session: Session,
        action: Action,
        reason: str,
        guard: str,
    ) -> Session:
        """Persist one exact action before returning control to a human."""

        if (
            not isinstance(action, Action)
            or not ACTION_ID_PATTERN.fullmatch(action.id)
            or action.expect_confirm is not True
        ):
            raise SessionError(
                "invalid_confirmation",
                "A confirmation-marked structured action is required",
            )
        if not isinstance(reason, str) or not reason.strip():
            raise SessionError("invalid_confirmation", "Confirmation reason cannot be empty")
        if len(reason.encode("utf-8")) > 16 * 1024:
            raise SessionError("invalid_confirmation", "Confirmation reason is too large")
        if not isinstance(guard, str) or not re.fullmatch(r"[0-9a-f]{64}", guard):
            raise SessionError("invalid_confirmation", "Invalid confirmation guard")

        def mutate(state: SessionState) -> SessionState:
            self._require_active(state)
            if state.pending_confirmation is not None:
                raise SessionError(
                    "confirmation_pending",
                    "Another action is already awaiting confirmation",
                )
            if action.id not in state.used_action_ids:
                raise SessionError(
                    "invalid_confirmation",
                    "Pending action ID must be reserved before confirmation",
                )
            if state.action(action.id) is not None:
                raise SessionError(
                    "duplicate_action_id",
                    f"Action {action.id!r} already has a persisted result",
                )
            return replace(
                state,
                status=SessionStatus.WAITING_USER,
                pending_confirmation=PendingConfirmation(
                    action=action,
                    reason=reason.strip(),
                    guard=guard,
                    requested_at=self._now(),
                ),
            )

        return self._update(session, mutate)

    def clear_pending_confirmation(self, session: Session) -> Session:
        """Clear a failed approved action without treating it as a denial result."""

        def mutate(state: SessionState) -> SessionState:
            if (
                state.status is not SessionStatus.WAITING_USER
                or state.pending_confirmation is None
            ):
                raise SessionError(
                    "confirmation_not_pending",
                    "The session has no pending confirmation",
                )
            return replace(
                state,
                status=SessionStatus.ACTIVE,
                pending_confirmation=None,
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
            return self._state_with_chunk(state, path, seq=seq, final=final)

        return self._update(session, mutate)

    def _state_with_chunk(
        self,
        state: SessionState,
        path: PathRef,
        *,
        seq: int,
        final: bool,
    ) -> SessionState:
        if path.root is not Root.OUTPUT:
            raise SessionError("input_readonly", "Chunk writes may only target the output root")
        existing = state.chunk(path)
        if existing is None:
            if seq != 1:
                raise SessionError(
                    "chunk_sequence_error",
                    "A new chunk sequence must start at 1",
                )
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

    @staticmethod
    def _state_without_chunk_scope(
        state: SessionState,
        scope: PathRef,
    ) -> SessionState:
        scope_parts = SessionManager._chunk_path_parts(scope)
        chunks = tuple(
            record
            for record in state.chunks
            if not (
                record.path.root is scope.root
                and SessionManager._chunk_path_parts(record.path)[: len(scope_parts)]
                == scope_parts
            )
        )
        return replace(state, chunks=chunks)

    def _state_with_moved_chunk_scope(
        self,
        state: SessionState,
        source: PathRef,
        target: PathRef,
    ) -> SessionState:
        source_parts = self._chunk_path_parts(source)
        target_parts = self._chunk_path_parts(target)
        chunks: list[ChunkRecord] = []
        for record in state.chunks:
            record_parts = self._chunk_path_parts(record.path)
            if (
                record.path.root is source.root
                and record_parts[: len(source_parts)] == source_parts
            ):
                suffix = record_parts[len(source_parts) :]
                moved_value = "/".join(target_parts + suffix) or "."
                chunks.append(
                    replace(
                        record,
                        path=PathRef(moved_value, target.root),
                        updated_at=self._now(),
                    )
                )
            else:
                chunks.append(record)
        keys = tuple((record.path.root, record.path.value) for record in chunks)
        if len(keys) != len(set(keys)):
            raise SessionError(
                "chunk_state_conflict",
                "Move destination conflicts with existing chunk metadata",
            )
        return replace(state, chunks=tuple(chunks))

    @staticmethod
    def _chunk_path_parts(path: PathRef) -> tuple[str, ...]:
        return () if path.value == "." else tuple(path.value.split("/"))

    def register_process(
        self,
        session: Session,
        *,
        pid: int,
        handle: str | None = None,
        max_output_lines: int = 100_000,
    ) -> str:
        if type(pid) is not int or pid < 1:
            raise SessionError("invalid_process", "Process PID must be a positive integer")
        if (
            type(max_output_lines) is not int
            or not 1 <= max_output_lines <= 100_000
        ):
            raise SessionError(
                "invalid_process",
                "Process max_output_lines must be between 1 and 100000",
            )
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
                max_output_lines=max_output_lines,
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
        output_bytes: int | None = None,
        exit_code: int | None = None,
        termination_reason: ProcessTerminationReason | None = None,
    ) -> Session:
        if status is not None and not isinstance(status, ProcessStatus):
            raise SessionError("invalid_process", "Unknown process status")
        if output_offset is not None and (type(output_offset) is not int or output_offset < 0):
            raise SessionError("invalid_process", "Output offset must be non-negative")
        if output_bytes is not None and (type(output_bytes) is not int or output_bytes < 0):
            raise SessionError("invalid_process", "Output bytes must be non-negative")
        if exit_code is not None and (
            type(exit_code) is not int or not -(2**31) <= exit_code < 2**31
        ):
            raise SessionError("invalid_process", "Invalid process exit code")
        if termination_reason is not None and not isinstance(
            termination_reason,
            ProcessTerminationReason,
        ):
            raise SessionError("invalid_process", "Unknown process termination reason")

        def mutate(state: SessionState) -> SessionState:
            existing = state.process(handle)
            if existing is None:
                raise SessionError("unknown_process_handle", f"Unknown process handle {handle!r}")
            if existing.status is not ProcessStatus.RUNNING and status not in {None, existing.status}:
                raise SessionError(
                    "invalid_process_transition",
                    f"Process {handle!r} is already {existing.status.value}",
                )
            if existing.status is not ProcessStatus.RUNNING:
                if exit_code is not None and exit_code != existing.exit_code:
                    raise SessionError(
                        "invalid_process_transition",
                        "Terminal process exit metadata cannot change",
                    )
                if (
                    termination_reason is not None
                    and termination_reason is not existing.termination_reason
                ):
                    raise SessionError(
                        "invalid_process_transition",
                        "Terminal process reason cannot change",
                    )
            selected_status = status or existing.status
            if output_offset is not None and output_offset < existing.output_offset:
                raise SessionError(
                    "invalid_process",
                    "Process output offset cannot move backwards",
                )
            selected_output_bytes = (
                existing.output_bytes if output_bytes is None else output_bytes
            )
            if selected_output_bytes < existing.output_bytes:
                raise SessionError(
                    "invalid_process",
                    "Process output byte count cannot move backwards",
                )
            selected_offset = (
                existing.output_offset if output_offset is None else output_offset
            )
            selected_output_bytes = max(selected_output_bytes, selected_offset)

            selected_exit_code = (
                existing.exit_code if exit_code is None else exit_code
            )
            selected_reason = termination_reason or existing.termination_reason
            ended_at = existing.ended_at
            if selected_status is ProcessStatus.RUNNING:
                if selected_exit_code is not None or selected_reason is not None:
                    raise SessionError(
                        "invalid_process",
                        "Running process cannot contain terminal metadata",
                    )
            else:
                if ended_at is None:
                    ended_at = self._now()
                if selected_status is ProcessStatus.EXITED:
                    selected_exit_code = 0 if selected_exit_code is None else selected_exit_code
                    selected_reason = selected_reason or ProcessTerminationReason.EXITED
                    if selected_reason is not ProcessTerminationReason.EXITED:
                        raise SessionError(
                            "invalid_process",
                            "Exited process has an invalid termination reason",
                        )
                elif selected_status is ProcessStatus.KILLED:
                    selected_exit_code = -9 if selected_exit_code is None else selected_exit_code
                    selected_reason = selected_reason or ProcessTerminationReason.USER
                    if selected_reason not in {
                        ProcessTerminationReason.USER,
                        ProcessTerminationReason.OUTPUT_LIMIT,
                        ProcessTerminationReason.RUNTIME_LIMIT,
                        ProcessTerminationReason.SESSION_END,
                        ProcessTerminationReason.HOST_EXIT,
                        ProcessTerminationReason.SUPERVISOR_ERROR,
                    }:
                        raise SessionError(
                            "invalid_process",
                            "Killed process has an invalid termination reason",
                        )
                elif selected_status is ProcessStatus.LOST:
                    if selected_exit_code is not None:
                        raise SessionError(
                            "invalid_process",
                            "Lost process cannot have an exit code",
                        )
                    selected_reason = (
                        selected_reason or ProcessTerminationReason.SUPERVISOR_LOST
                    )
                    if selected_reason is not ProcessTerminationReason.SUPERVISOR_LOST:
                        raise SessionError(
                            "invalid_process",
                            "Lost process has an invalid termination reason",
                        )
            replacement = replace(
                existing,
                status=selected_status,
                output_offset=selected_offset,
                output_bytes=selected_output_bytes,
                exit_code=selected_exit_code,
                termination_reason=selected_reason,
                ended_at=ended_at,
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
