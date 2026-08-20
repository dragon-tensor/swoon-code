"""In-process ownership and bounded log capture for sandboxed background work."""

from __future__ import annotations

import atexit
import codecs
import os
import select
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from swoon.session import ProcessStatus, ProcessTerminationReason

from .commands import ForegroundCommandTools


_XML_ALLOWED_CONTROLS = {"\t", "\n", "\r"}


@dataclass(frozen=True, slots=True)
class BackgroundMetadata:
    status: ProcessStatus
    output_bytes: int
    exit_code: int | None
    termination_reason: ProcessTerminationReason | None


class SupervisedBackgroundProcess:
    """Own one live Bubblewrap process; persisted PIDs are never used for signaling."""

    def __init__(
        self,
        *,
        session_root: Path,
        handle: str,
        process: subprocess.Popen[bytes],
        temporary: tempfile.TemporaryDirectory,
        log_fd: int,
        initial_output: bytes,
        max_output_bytes: int,
        max_output_lines: int,
        max_runtime_seconds: int,
        logical_output_root: str,
        logical_input_root: str,
    ) -> None:
        if process.stdout is None:
            raise ValueError("Background process requires a captured stdout pipe")
        self.session_root = session_root.resolve()
        self.handle = handle
        self.process = process
        self.temporary = temporary
        self.log_fd = log_fd
        self.initial_output = initial_output
        self.max_output_bytes = max_output_bytes
        self.max_output_lines = max_output_lines
        self.max_runtime_seconds = max_runtime_seconds
        self._path_replacements = tuple(
            sorted(
                (
                    (str(self.session_root / "output"), logical_output_root),
                    (str(self.session_root / "input"), logical_input_root),
                    (str(Path(temporary.name).resolve()), "[sandbox]"),
                    (str(self.session_root), "[session]"),
                ),
                key=lambda item: len(item[0]),
                reverse=True,
            )
        )
        self._physical_paths = tuple(
            physical for physical, _ in self._path_replacements
        )
        self._redaction_buffer = ""
        self._max_redaction_prefix = max(
            len(physical) for physical in self._physical_paths
        ) - 1
        self._lock = threading.RLock()
        self._done = threading.Event()
        self._status = ProcessStatus.RUNNING
        self._raw_output_bytes = 0
        self._output_bytes = 0
        self._exit_code: int | None = None
        self._termination_reason: ProcessTerminationReason | None = None
        self._requested_reason: ProcessTerminationReason | None = None
        self._remaining_lines = max_output_lines
        self._started_monotonic = time.monotonic()
        self._thread = threading.Thread(
            target=self._capture,
            name=f"swoon-{handle}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    @property
    def started(self) -> bool:
        return self._thread.ident is not None

    def metadata(self) -> BackgroundMetadata:
        with self._lock:
            return BackgroundMetadata(
                status=self._status,
                output_bytes=self._output_bytes,
                exit_code=self._exit_code,
                termination_reason=self._termination_reason,
            )

    def request_kill(self, reason: ProcessTerminationReason) -> bool:
        if reason not in {
            ProcessTerminationReason.USER,
            ProcessTerminationReason.OUTPUT_LIMIT,
            ProcessTerminationReason.RUNTIME_LIMIT,
            ProcessTerminationReason.SESSION_END,
            ProcessTerminationReason.HOST_EXIT,
            ProcessTerminationReason.SUPERVISOR_ERROR,
        }:
            raise ValueError("Invalid background-process kill reason")
        with self._lock:
            if self._status is not ProcessStatus.RUNNING:
                return False
            if self._requested_reason is None:
                self._requested_reason = reason
        ForegroundCommandTools._kill(self.process)
        return True

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def _capture(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        output_fd = self.process.stdout.fileno()
        terminal_error = False
        try:
            initial_output = self.initial_output
            self.initial_output = b""
            if initial_output and self._consume(decoder, initial_output):
                self.request_kill(ProcessTerminationReason.OUTPUT_LIMIT)

            while self.process.poll() is None:
                if (
                    time.monotonic() - self._started_monotonic
                    >= self.max_runtime_seconds
                ):
                    self.request_kill(ProcessTerminationReason.RUNTIME_LIMIT)
                readable, _, _ = select.select((output_fd,), (), (), 0.05)
                if readable:
                    reached_eof, exceeded = self._drain(decoder, output_fd)
                    if exceeded:
                        self.request_kill(ProcessTerminationReason.OUTPUT_LIMIT)
                    if reached_eof:
                        break

            if self.process.poll() is None:
                ForegroundCommandTools._kill(self.process)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                ForegroundCommandTools._kill(self.process)
                self.process.wait()

            _, exceeded = self._drain(decoder, output_fd)
            if exceeded:
                self._set_requested_reason(ProcessTerminationReason.OUTPUT_LIMIT)
            tail = decoder.decode(b"", final=True)
            if self._consume_text(tail, final=True):
                self._set_requested_reason(ProcessTerminationReason.OUTPUT_LIMIT)
        except Exception:
            terminal_error = True
            self._set_requested_reason(ProcessTerminationReason.SUPERVISOR_ERROR)
            ForegroundCommandTools._kill(self.process)
            try:
                self.process.wait(timeout=2)
            except Exception:
                pass
        finally:
            try:
                os.fsync(self.log_fd)
            except OSError:
                terminal_error = True
                self._set_requested_reason(ProcessTerminationReason.SUPERVISOR_ERROR)
            try:
                os.close(self.log_fd)
            except OSError:
                pass
            try:
                self.process.stdout.close()
            except Exception:
                pass
            try:
                self.temporary.cleanup()
            except Exception:
                terminal_error = True
                self._set_requested_reason(ProcessTerminationReason.SUPERVISOR_ERROR)

            with self._lock:
                reason = self._requested_reason
                if terminal_error:
                    reason = ProcessTerminationReason.SUPERVISOR_ERROR
                self._exit_code = self.process.returncode
                if reason is None:
                    self._status = ProcessStatus.EXITED
                    self._termination_reason = ProcessTerminationReason.EXITED
                else:
                    self._status = ProcessStatus.KILLED
                    self._termination_reason = reason
            self._done.set()

    def _drain(self, decoder, descriptor: int) -> tuple[bool, bool]:
        exceeded = False
        while True:
            try:
                block = os.read(descriptor, 64 * 1024)
            except BlockingIOError:
                return False, exceeded
            if not block:
                return True, exceeded
            if self._consume(decoder, block):
                exceeded = True
                return False, True

    def _consume(self, decoder, payload: bytes) -> bool:
        remaining = self.max_output_bytes - self._raw_output_bytes
        selected = payload[: max(0, remaining)]
        self._raw_output_bytes += len(selected)
        raw_exceeded = len(selected) < len(payload)
        text = decoder.decode(selected, final=False)
        output_exceeded = self._consume_text(text, final=False)
        return raw_exceeded or output_exceeded

    def _consume_text(self, text: str, *, final: bool) -> bool:
        self._redaction_buffer += text
        if final:
            selected = self._redaction_buffer
            self._redaction_buffer = ""
        else:
            retained = self._incomplete_redaction_suffix(self._redaction_buffer)
            if retained:
                selected = self._redaction_buffer[:-retained]
                self._redaction_buffer = self._redaction_buffer[-retained:]
            else:
                selected = self._redaction_buffer
                self._redaction_buffer = ""
        return bool(selected) and self._append_text(selected)

    def _incomplete_redaction_suffix(self, text: str) -> int:
        maximum = min(len(text), self._max_redaction_prefix)
        for length in range(maximum, 0, -1):
            suffix = text[-length:]
            if any(path.startswith(suffix) for path in self._physical_paths):
                return length
        return 0

    def _append_text(self, text: str) -> bool:
        for physical, logical in self._path_replacements:
            text = text.replace(physical, logical)
        sanitized = "".join(
            character
            if character in _XML_ALLOWED_CONTROLS
            or not unicodedata.category(character).startswith("C")
            else "\ufffd"
            for character in text
        )
        line_limited = False
        if self._remaining_lines == 0:
            return bool(sanitized)
        newline_count = 0
        cut: int | None = None
        for index, character in enumerate(sanitized):
            if character != "\n":
                continue
            newline_count += 1
            if newline_count == self._remaining_lines:
                cut = index + 1
                break
        if cut is not None:
            line_limited = cut < len(sanitized)
            sanitized = sanitized[:cut]
            self._remaining_lines = 0
        else:
            self._remaining_lines -= newline_count

        encoded = sanitized.encode("utf-8")
        with self._lock:
            remaining = self.max_output_bytes - self._output_bytes
            if len(encoded) > remaining:
                encoded = self._utf8_prefix(encoded, max(0, remaining))
                line_limited = True
            self._write_all(encoded)
            self._output_bytes += len(encoded)
        return line_limited

    def _write_all(self, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(self.log_fd, view)
            view = view[written:]

    def _set_requested_reason(self, reason: ProcessTerminationReason) -> None:
        with self._lock:
            if self._requested_reason is None:
                self._requested_reason = reason

    @staticmethod
    def _utf8_prefix(payload: bytes, maximum: int) -> bytes:
        selected = payload[:maximum]
        while selected:
            try:
                selected.decode("utf-8")
                return selected
            except UnicodeDecodeError as error:
                if error.reason == "unexpected end of data":
                    selected = selected[: error.start]
                    continue
                return selected.decode("utf-8", errors="replace").encode("utf-8")[:maximum]
        return b""


class BackgroundProcessRegistry:
    """Keep unforgeable live ownership separate from persisted diagnostic PIDs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[tuple[str, str], SupervisedBackgroundProcess] = {}

    def add(self, supervisor: SupervisedBackgroundProcess) -> None:
        key = self._key(supervisor.session_root, supervisor.handle)
        with self._lock:
            if key in self._processes:
                raise ValueError("Duplicate live background process handle")
            self._processes[key] = supervisor

    def get(self, session_root: Path, handle: str) -> SupervisedBackgroundProcess | None:
        with self._lock:
            return self._processes.get(self._key(session_root, handle))

    def remove(self, supervisor: SupervisedBackgroundProcess) -> None:
        """Forget an exact supervisor without disturbing a replacement entry."""

        key = self._key(supervisor.session_root, supervisor.handle)
        with self._lock:
            if self._processes.get(key) is supervisor:
                del self._processes[key]

    def for_session(self, session_root: Path) -> tuple[SupervisedBackgroundProcess, ...]:
        root = self._root(session_root)
        with self._lock:
            return tuple(
                supervisor
                for (candidate, _), supervisor in self._processes.items()
                if candidate == root
            )

    def shutdown_all(self) -> None:
        with self._lock:
            supervisors = tuple(self._processes.values())
        for supervisor in supervisors:
            try:
                supervisor.request_kill(ProcessTerminationReason.HOST_EXIT)
            except Exception:
                pass
        for supervisor in supervisors:
            try:
                supervisor.wait(1)
            except Exception:
                pass

    @staticmethod
    def _key(session_root: Path, handle: str) -> tuple[str, str]:
        return BackgroundProcessRegistry._root(session_root), handle

    @staticmethod
    def _root(session_root: Path) -> str:
        return str(session_root.resolve())


BACKGROUND_PROCESSES = BackgroundProcessRegistry()
atexit.register(BACKGROUND_PROCESSES.shutdown_all)
