"""Sandboxed background launch, bounded streaming, and handle-scoped termination."""

from __future__ import annotations

import os
import secrets
import select
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from swoon.aeml.models import Result, ResultStatus, Truncation, ValidatedAction
from swoon.policy import PathPolicy
from swoon.session import (
    ProcessRecord,
    ProcessStatus,
    ProcessTerminationReason,
    Session,
    SessionError,
    SessionManager,
)
from swoon.session.models import PROCESS_HANDLE_PATTERN

from .background_supervisor import (
    BACKGROUND_PROCESSES,
    SupervisedBackgroundProcess,
)
from .commands import ForegroundCommandTools, _SANDBOX_READY_MARKER
from .errors import ToolExecutionError
from .models import CommandToolLimits


class BackgroundProcessTools:
    """Manage only processes owned by this interpreter's live supervisor registry."""

    def __init__(
        self,
        policy: PathPolicy,
        limits: CommandToolLimits,
        session_manager: SessionManager,
        *,
        sandbox_binary: str | Path | None = None,
        resource_limiter_binary: str | Path | None = None,
        python_binary: str | Path | None = None,
    ) -> None:
        self.policy = policy
        self.limits = limits
        self.session_manager = session_manager
        self.foreground = ForegroundCommandTools(
            policy,
            limits,
            sandbox_binary=sandbox_binary,
            resource_limiter_binary=resource_limiter_binary,
            python_binary=python_binary,
        )

    def launch(self, action: ValidatedAction, session: Session) -> Result:
        command = action.argument("cmd")
        if not isinstance(command, str):
            raise ToolExecutionError(
                "invalid_command",
                "run-command-background requires text cmd",
            )
        argv = self.foreground._parse_command(command)
        self.foreground._validate_command_paths(argv)
        self.foreground._require_runtime()
        self._reconcile_all(session)
        records = session.state.processes
        if len(records) >= self.limits.max_background_records:
            raise ToolExecutionError(
                "process_limit_exceeded",
                f"Session already contains {self.limits.max_background_records} process records",
            )
        running = sum(record.status is ProcessStatus.RUNNING for record in records)
        if running >= self.limits.max_background_processes:
            raise ToolExecutionError(
                "process_limit_exceeded",
                f"Session already has {self.limits.max_background_processes} running processes",
            )

        max_lines_value = action.argument("max_output_lines")
        max_output_lines = (
            max_lines_value
            if isinstance(max_lines_value, int)
            else self.limits.background_default_output_lines
        )
        handle = self._new_handle(session)
        launch_body = (
            f"handle={handle}\n"
            "status=running\n"
            "offset=0\n"
            "workspace_changes=discarded\n"
            f"max_output_lines={max_output_lines}\n"
            f"max_runtime_seconds={self.limits.background_max_runtime_seconds}\n"
        )
        self._require_result_capacity(launch_body)
        log_path = self._log_path(session, handle)
        log_fd = self._create_log(log_path)
        try:
            temporary = tempfile.TemporaryDirectory(prefix="swoon-background-")
        except Exception as error:
            try:
                os.close(log_fd)
            except OSError:
                pass
            try:
                log_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ToolExecutionError(
                "snapshot_unavailable",
                f"Background snapshot could not be created ({error.__class__.__name__})",
            ) from error
        snapshot_root = Path(temporary.name)
        output_snapshot = snapshot_root / "output"
        input_snapshot = snapshot_root / "input"
        process: subprocess.Popen[bytes] | None = None
        supervisor: SupervisedBackgroundProcess | None = None
        registered = False
        registry_added = False
        supervisor_started = False
        try:
            output_snapshot.mkdir(mode=0o700)
            input_snapshot.mkdir(mode=0o700)
            self.foreground.snapshot_builder.build(output_snapshot, input_snapshot)
            with self.foreground._network_filter() as filter_fd:
                sandbox_argv = self.foreground._sandbox_argv(
                    argv,
                    output_snapshot=output_snapshot,
                    input_snapshot=input_snapshot,
                    filter_fd=filter_fd,
                    timeout=self.limits.background_max_runtime_seconds,
                )
                try:
                    process = subprocess.Popen(
                        sandbox_argv,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                        close_fds=True,
                        pass_fds=(filter_fd,),
                        start_new_session=True,
                    )
                except OSError as error:
                    raise ToolExecutionError(
                        "tool_unavailable",
                        f"Background sandbox could not start ({error.__class__.__name__})",
                    ) from error
                initial_output = self._wait_until_ready(process, snapshot_root)

            supervisor = SupervisedBackgroundProcess(
                session_root=session.paths.host_root,
                handle=handle,
                process=process,
                temporary=temporary,
                log_fd=log_fd,
                initial_output=initial_output,
                max_output_bytes=self.limits.max_capture_bytes,
                max_output_lines=max_output_lines,
                max_runtime_seconds=self.limits.background_max_runtime_seconds,
                logical_output_root=session.paths.output_root,
                logical_input_root=session.paths.input_root,
            )
            self.session_manager.register_process(
                session,
                pid=process.pid,
                handle=handle,
                max_output_lines=max_output_lines,
            )
            registered = True
            supervisor.start()
            supervisor_started = True
            BACKGROUND_PROCESSES.add(supervisor)
            registry_added = True
        except Exception:
            if supervisor is not None and (supervisor_started or supervisor.started):
                supervisor.request_kill(ProcessTerminationReason.SUPERVISOR_ERROR)
                supervisor.wait(2)
            else:
                if supervisor is not None and registry_added:
                    BACKGROUND_PROCESSES.remove(supervisor)
                if process is not None:
                    ForegroundCommandTools._kill(process)
                    try:
                        process.wait(timeout=2)
                    except Exception:
                        pass
                    if process.stdout is not None:
                        try:
                            process.stdout.close()
                        except Exception:
                            pass
                try:
                    os.close(log_fd)
                except OSError:
                    pass
                try:
                    temporary.cleanup()
                except Exception:
                    pass
            if supervisor is not None and registry_added:
                BACKGROUND_PROCESSES.remove(supervisor)
            if registered:
                try:
                    self.session_manager.update_process(
                        session,
                        handle,
                        status=ProcessStatus.KILLED,
                        exit_code=(
                            process.returncode
                            if process is not None
                            and isinstance(process.returncode, int)
                            else -9
                        ),
                        termination_reason=ProcessTerminationReason.SUPERVISOR_ERROR,
                    )
                except SessionError:
                    pass
            else:
                try:
                    log_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        return Result(
            action_id=action.source.id,
            status=ResultStatus.SUCCESS,
            body=launch_body,
        )

    def stream_output(self, action: ValidatedAction, session: Session) -> Result:
        handle = self._handle(action)
        record = self._record(session, handle)
        record = self._reconcile_one(session, record)
        requested_offset = action.argument("offset")
        offset = (
            requested_offset
            if isinstance(requested_offset, int)
            else record.output_offset
        )
        max_lines_value = action.argument("max_output_lines")
        max_lines = (
            max_lines_value
            if isinstance(max_lines_value, int)
            else self.limits.default_output_lines
        )

        payload = self._read_log(
            session,
            handle,
            maximum_bytes=record.output_bytes,
        )
        total_bytes = len(payload)
        if offset > total_bytes:
            raise ToolExecutionError(
                "invalid_offset",
                f"Requested offset {offset} exceeds process output size {total_bytes}",
            )
        candidate = payload[offset:]
        try:
            candidate.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError(
                "invalid_offset",
                "Requested offset does not begin at a UTF-8 boundary",
            ) from error
        candidate = self._line_prefix(candidate, max_lines)

        selected = self._fit_stream_body(record, offset, total_bytes, candidate)
        next_offset = offset + len(selected)
        body = self._stream_body(record, offset, next_offset, total_bytes, selected)
        if len(body.encode("utf-8")) > self.limits.max_result_bytes:
            raise ToolExecutionError(
                "result_limit_too_small",
                "Configured command result limit cannot hold stream metadata",
            )
        partial = next_offset < total_bytes
        return Result(
            action_id=action.source.id,
            status=ResultStatus.PARTIAL if partial else ResultStatus.SUCCESS,
            body=body,
            truncation=(
                Truncation(total_bytes=total_bytes, offset=offset) if partial else None
            ),
        )

    def kill_process(self, action: ValidatedAction, session: Session) -> Result:
        handle = self._handle(action)
        record = self._reconcile_one(session, self._record(session, handle))
        if record.status is not ProcessStatus.RUNNING:
            body = self._terminal_body(record)
            self._require_result_capacity(body)
            return Result(
                action_id=action.source.id,
                status=ResultStatus.FAILURE,
                body=body,
            )
        supervisor = BACKGROUND_PROCESSES.get(session.paths.host_root, handle)
        if supervisor is None:
            record = self._mark_lost(session, record)
            body = self._terminal_body(record)
            self._require_result_capacity(body)
            return Result(
                action_id=action.source.id,
                status=ResultStatus.FAILURE,
                body=body,
            )
        requested = supervisor.request_kill(ProcessTerminationReason.USER)
        if not supervisor.wait(5):
            raise ToolExecutionError(
                "process_kill_failed",
                f"Background process {handle!r} did not terminate",
            )
        record = self._sync_supervisor(session, record, supervisor)
        body = self._terminal_body(record)
        self._require_result_capacity(body)
        return Result(
            action_id=action.source.id,
            status=ResultStatus.SUCCESS if requested else ResultStatus.FAILURE,
            body=body,
        )

    def shutdown_session(
        self,
        session: Session,
        *,
        reason: ProcessTerminationReason = ProcessTerminationReason.SESSION_END,
    ) -> None:
        self._reconcile_all(session)
        for record in tuple(session.state.processes):
            if record.status is not ProcessStatus.RUNNING:
                continue
            supervisor = BACKGROUND_PROCESSES.get(session.paths.host_root, record.handle)
            if supervisor is None:
                self._mark_lost(session, record)
                continue
            supervisor.request_kill(reason)
            if not supervisor.wait(5):
                raise ToolExecutionError(
                    "process_kill_failed",
                    f"Background process {record.handle!r} did not terminate",
                )
            self._sync_supervisor(session, record, supervisor)

    def reconcile_session(self, session: Session) -> None:
        """Refresh all process records against live, interpreter-owned supervisors."""

        self._reconcile_all(session)

    def terminate_unreported(self, session: Session, handle: str) -> None:
        """Stop a launch whose handle result could not be persisted."""

        record = session.state.process(handle)
        supervisor = BACKGROUND_PROCESSES.get(session.paths.host_root, handle)
        if supervisor is None:
            if record is not None and record.status is ProcessStatus.RUNNING:
                self._mark_lost(session, record)
            return
        supervisor.request_kill(ProcessTerminationReason.SUPERVISOR_ERROR)
        if not supervisor.wait(5):
            raise ToolExecutionError(
                "process_kill_failed",
                f"Unreported background process {handle!r} did not terminate",
            )
        try:
            if record is not None and record.status is ProcessStatus.RUNNING:
                self._sync_supervisor(session, record, supervisor)
        finally:
            BACKGROUND_PROCESSES.remove(supervisor)

    def _wait_until_ready(
        self,
        process: subprocess.Popen[bytes],
        snapshot_root: Path,
    ) -> bytes:
        if process.stdout is None:
            raise ToolExecutionError("sandbox_failed", "Background sandbox has no output pipe")
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        captured = bytearray()
        deadline = time.monotonic() + self.limits.background_startup_timeout_seconds
        while True:
            marker_at = captured.find(_SANDBOX_READY_MARKER)
            if marker_at >= 0:
                return bytes(captured[marker_at + len(_SANDBOX_READY_MARKER) :])
            if len(captured) > self.limits.max_capture_bytes:
                ForegroundCommandTools._kill(process)
                process.wait()
                raise ToolExecutionError(
                    "output_limit_exceeded",
                    "Background sandbox output exceeded its limit before startup",
                )
            if time.monotonic() >= deadline:
                ForegroundCommandTools._kill(process)
                process.wait()
                raise ToolExecutionError(
                    "sandbox_start_timeout",
                    "Background sandbox did not reach program launch before its timeout",
                )
            readable, _, _ = select.select((descriptor,), (), (), 0.05)
            if readable:
                while True:
                    try:
                        block = os.read(descriptor, 64 * 1024)
                    except BlockingIOError:
                        break
                    if not block:
                        break
                    remaining = self.limits.max_capture_bytes + 1 - len(captured)
                    if remaining > 0:
                        captured.extend(block[:remaining])
                    if len(block) > remaining:
                        break
            if process.poll() is not None:
                marker_at = captured.find(_SANDBOX_READY_MARKER)
                if marker_at >= 0:
                    return bytes(captured[marker_at + len(_SANDBOX_READY_MARKER) :])
                detail = self.foreground._sanitize_output(
                    bytes(captured),
                    snapshot_root,
                ).strip()
                message = "Background sandbox failed before program launch"
                if detail:
                    message += f": {detail[:500]}"
                raise ToolExecutionError("sandbox_failed", message)

    def _reconcile_all(self, session: Session) -> None:
        records = tuple(session.state.processes)
        for record in records:
            self._reconcile_one(session, record)
        known_handles = {record.handle for record in records}
        for supervisor in BACKGROUND_PROCESSES.for_session(session.paths.host_root):
            if supervisor.handle in known_handles:
                continue
            supervisor.request_kill(ProcessTerminationReason.SUPERVISOR_ERROR)
            if not supervisor.wait(5):
                raise ToolExecutionError(
                    "process_kill_failed",
                    f"Orphan background process {supervisor.handle!r} did not terminate",
                )
            BACKGROUND_PROCESSES.remove(supervisor)
            try:
                self._log_path(session, supervisor.handle).unlink(missing_ok=True)
            except OSError:
                pass

    def _reconcile_one(self, session: Session, record: ProcessRecord) -> ProcessRecord:
        supervisor = BACKGROUND_PROCESSES.get(session.paths.host_root, record.handle)
        if supervisor is None:
            if record.status is ProcessStatus.RUNNING:
                return self._mark_lost(session, record)
            return record
        if record.status is not ProcessStatus.RUNNING:
            metadata = supervisor.metadata()
            if metadata.status is ProcessStatus.RUNNING:
                supervisor.request_kill(ProcessTerminationReason.SESSION_END)
                if not supervisor.wait(5):
                    raise ToolExecutionError(
                        "process_kill_failed",
                        f"Background process {record.handle!r} did not terminate",
                    )
            BACKGROUND_PROCESSES.remove(supervisor)
            return record
        return self._sync_supervisor(session, record, supervisor)

    def _sync_supervisor(
        self,
        session: Session,
        record: ProcessRecord,
        supervisor: SupervisedBackgroundProcess,
    ) -> ProcessRecord:
        metadata = supervisor.metadata()
        if (
            metadata.status is record.status
            and metadata.output_bytes == record.output_bytes
            and metadata.exit_code == record.exit_code
            and metadata.termination_reason is record.termination_reason
        ):
            if metadata.status is not ProcessStatus.RUNNING:
                BACKGROUND_PROCESSES.remove(supervisor)
            return record
        self.session_manager.update_process(
            session,
            record.handle,
            status=metadata.status,
            output_bytes=metadata.output_bytes,
            exit_code=metadata.exit_code,
            termination_reason=metadata.termination_reason,
        )
        refreshed = session.state.process(record.handle)
        assert refreshed is not None
        if metadata.status is not ProcessStatus.RUNNING:
            BACKGROUND_PROCESSES.remove(supervisor)
        return refreshed

    def _mark_lost(self, session: Session, record: ProcessRecord) -> ProcessRecord:
        output_bytes = record.output_bytes
        try:
            output_bytes = max(
                output_bytes,
                self._log_size(self._log_path(session, record.handle)),
            )
        except ToolExecutionError:
            pass
        self.session_manager.update_process(
            session,
            record.handle,
            status=ProcessStatus.LOST,
            output_bytes=output_bytes,
            termination_reason=ProcessTerminationReason.SUPERVISOR_LOST,
        )
        refreshed = session.state.process(record.handle)
        assert refreshed is not None
        return refreshed

    def _fit_stream_body(
        self,
        record: ProcessRecord,
        offset: int,
        total_bytes: int,
        candidate: bytes,
    ) -> bytes:
        selected = candidate
        for _ in range(3):
            next_offset = offset + len(selected)
            metadata = self._stream_body(
                record,
                offset,
                next_offset,
                total_bytes,
                b"",
            ).encode("utf-8")
            available = self.limits.max_result_bytes - len(metadata)
            if available < 0:
                raise ToolExecutionError(
                    "result_limit_too_small",
                    "Configured command result limit cannot hold stream metadata",
                )
            reduced = ForegroundCommandTools._truncate_utf8(
                selected.decode("utf-8"),
                available,
            ).encode("utf-8")
            if len(reduced) == len(selected):
                return selected
            selected = reduced
        if candidate and not selected:
            raise ToolExecutionError(
                "result_limit_too_small",
                "Configured command result limit cannot hold one output character",
            )
        return selected

    @staticmethod
    def _line_prefix(payload: bytes, max_lines: int) -> bytes:
        position = -1
        for _ in range(max_lines):
            position = payload.find(b"\n", position + 1)
            if position < 0:
                return payload
        return payload[: position + 1]

    @staticmethod
    def _stream_body(
        record: ProcessRecord,
        offset: int,
        next_offset: int,
        total_bytes: int,
        selected: bytes,
    ) -> str:
        exit_code = "" if record.exit_code is None else str(record.exit_code)
        reason = (
            ""
            if record.termination_reason is None
            else record.termination_reason.value
        )
        return (
            f"handle={record.handle}\n"
            f"status={record.status.value}\n"
            f"exit_code={exit_code}\n"
            f"termination_reason={reason}\n"
            f"offset={offset}\n"
            f"next_offset={next_offset}\n"
            f"output_bytes={total_bytes}\n"
            "workspace_changes=discarded\n"
            "output:\n"
            + selected.decode("utf-8")
        )

    @staticmethod
    def _terminal_body(record: ProcessRecord) -> str:
        exit_code = "" if record.exit_code is None else str(record.exit_code)
        reason = (
            ""
            if record.termination_reason is None
            else record.termination_reason.value
        )
        return (
            f"handle={record.handle}\n"
            f"status={record.status.value}\n"
            f"exit_code={exit_code}\n"
            f"termination_reason={reason}\n"
            f"output_bytes={record.output_bytes}\n"
            "workspace_changes=discarded\n"
        )

    def _require_result_capacity(self, body: str) -> None:
        if len(body.encode("utf-8")) > self.limits.max_result_bytes:
            raise ToolExecutionError(
                "result_limit_too_small",
                "Configured command result limit cannot hold process metadata",
            )

    def _read_log(
        self,
        session: Session,
        handle: str,
        *,
        maximum_bytes: int,
    ) -> bytes:
        path = self._log_path(session, handle)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as error:
            raise ToolExecutionError(
                "process_log_unavailable",
                f"Background process {handle!r} has no output log",
            ) from error
        except OSError as error:
            raise ToolExecutionError(
                "process_log_unavailable",
                f"Background process log could not be opened ({error.__class__.__name__})",
            ) from error
        try:
            item_stat = os.fstat(descriptor)
            self._validate_log_stat(item_stat)
            if item_stat.st_size < maximum_bytes:
                raise ToolExecutionError(
                    "process_log_invalid",
                    "Background process log is shorter than its recorded output",
                )
            payload = bytearray()
            while len(payload) < maximum_bytes:
                block = os.read(
                    descriptor,
                    min(1024 * 1024, maximum_bytes - len(payload)),
                )
                if not block:
                    raise ToolExecutionError(
                        "process_log_invalid",
                        "Background process log changed while it was read",
                    )
                payload.extend(block)
                if len(payload) > self.limits.max_capture_bytes:
                    raise ToolExecutionError(
                        "process_log_invalid",
                        "Background process log exceeds its safety limit",
                    )
        finally:
            os.close(descriptor)
        try:
            bytes(payload).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError(
                "process_log_invalid",
                "Background process log is not valid UTF-8",
            ) from error
        return bytes(payload)

    def _log_size(self, path: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return 0
        except OSError as error:
            raise ToolExecutionError(
                "process_log_unavailable",
                f"Background process log could not be opened ({error.__class__.__name__})",
            ) from error
        try:
            item_stat = os.fstat(descriptor)
            self._validate_log_stat(item_stat)
            return item_stat.st_size
        finally:
            os.close(descriptor)

    def _validate_log_stat(self, item_stat: os.stat_result) -> None:
        if not stat.S_ISREG(item_stat.st_mode) or item_stat.st_nlink != 1:
            raise ToolExecutionError(
                "process_log_invalid",
                "Background process log is not a private regular file",
            )
        if os.name != "nt" and stat.S_IMODE(item_stat.st_mode) & 0o077:
            raise ToolExecutionError(
                "process_log_invalid",
                "Background process log permissions are too broad",
            )
        if item_stat.st_size > self.limits.max_capture_bytes:
            raise ToolExecutionError(
                "process_log_invalid",
                "Background process log exceeds its safety limit",
            )

    @staticmethod
    def _handle(action: ValidatedAction) -> str:
        handle = action.argument("handle")
        if not isinstance(handle, str) or not PROCESS_HANDLE_PATTERN.fullmatch(handle):
            raise ToolExecutionError("invalid_process_handle", "Invalid process handle")
        return handle

    @staticmethod
    def _record(session: Session, handle: str) -> ProcessRecord:
        record = session.state.process(handle)
        if record is None:
            raise ToolExecutionError(
                "unknown_process_handle",
                f"Unknown process handle {handle!r} for this session",
            )
        return record

    @staticmethod
    def _log_path(session: Session, handle: str) -> Path:
        if not PROCESS_HANDLE_PATTERN.fullmatch(handle):
            raise ToolExecutionError("invalid_process_handle", "Invalid process handle")
        return session.paths.host_root / f".process-{handle}.log"

    def _new_handle(self, session: Session) -> str:
        for _ in range(32):
            handle = f"proc_{secrets.token_hex(12)}"
            if (
                session.state.process(handle) is None
                and BACKGROUND_PROCESSES.get(session.paths.host_root, handle) is None
                and not self._log_path(session, handle).exists()
            ):
                return handle
        raise ToolExecutionError("process_handle_failed", "Could not allocate process handle")

    @staticmethod
    def _create_log(path: Path) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            return descriptor
        except OSError as error:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ToolExecutionError(
                "process_log_unavailable",
                f"Background process log could not be created ({error.__class__.__name__})",
            ) from error
