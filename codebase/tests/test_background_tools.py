from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from swoon.aeml import AEMLParser, AEMLPromptBuilder, AEMLValidator
from swoon.aeml.models import ProtocolError, Result, ResultStatus
from swoon.orchestration import AgentOrchestrator, RunStopReason
from swoon.session import (
    ProcessStatus,
    ProcessTerminationReason,
    Session,
    SessionManager,
)
from swoon.tools import AgentToolDispatcher, CommandToolLimits
from swoon.transport import AEMLChatChannel


class _FakeTransport:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def send(self, prompt: str) -> str:
        del prompt
        return self.responses.pop(0)


class BackgroundCommandToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        source.mkdir()
        (source / "seed.txt").write_text("seed\n", encoding="utf-8")
        self.manager = SessionManager(self.root / "sessions")
        self.session = self.manager.create(source, session_id="sess_background")
        self.dispatcher = AgentToolDispatcher(self.manager)
        self.parser = AEMLParser()
        self._action_number = 0

    def tearDown(self) -> None:
        try:
            current = self.manager.load(self.session.id)
            self.dispatcher.shutdown_background(
                current,
                reason=ProcessTerminationReason.HOST_EXIT,
            )
        except Exception:
            pass
        self._make_writable(self.root)
        self.temporary.cleanup()

    @staticmethod
    def _runtime_available() -> bool:
        return (
            platform.system() == "Linux"
            and platform.machine().lower()
            in {"x86_64", "amd64", "aarch64", "arm64"}
            and shutil.which("bwrap") is not None
            and shutil.which("prlimit") is not None
            and (
                Path("/usr/bin/python3").exists()
                or Path("/usr/local/bin/python3").exists()
            )
        )

    def require_runtime(self) -> None:
        if not self._runtime_available():
            self.skipTest("Bubblewrap background-command runtime is unavailable")

    @staticmethod
    def _make_writable(root: Path) -> None:
        for directory, directories, files in os.walk(
            root,
            topdown=False,
            followlinks=False,
        ):
            directory_path = Path(directory)
            for name in files:
                path = directory_path / name
                if not path.is_symlink():
                    try:
                        path.chmod(0o600)
                    except OSError:
                        pass
            for name in directories:
                path = directory_path / name
                if not path.is_symlink():
                    try:
                        path.chmod(0o700)
                    except OSError:
                        pass
            try:
                directory_path.chmod(0o700)
            except OSError:
                pass

    def action(self, session: Session, body: str, *, prefix: str = "background"):
        self._action_number += 1
        action_id = f"{prefix}_{self._action_number}"
        message = self.parser.parse(
            f'<aeml turn="1" session="{session.id}">'
            f'<action id="{action_id}">{body}</action>'
            "<next>await_result</next></aeml>"
        )
        validator = AEMLValidator(self.dispatcher.tool_specs)
        return validator.validate(message).actions[0]

    def execute(
        self,
        body: str,
        *,
        session: Session | None = None,
        dispatcher: AgentToolDispatcher | None = None,
        prefix: str = "background",
    ):
        selected_session = session or self.session
        selected_dispatcher = dispatcher or self.dispatcher
        return selected_dispatcher.execute(
            self.action(selected_session, body, prefix=prefix),
            selected_session,
        )

    @staticmethod
    def _field(body: str, name: str) -> str:
        matched = re.search(rf"(?m)^{re.escape(name)}=(.*)$", body)
        if matched is None:
            raise AssertionError(f"Missing {name!r} in result body: {body!r}")
        return matched.group(1)

    def _launch(
        self,
        command: str,
        *,
        max_output_lines: int | None = None,
        dispatcher: AgentToolDispatcher | None = None,
    ) -> tuple[str, Result]:
        lines = (
            ""
            if max_output_lines is None
            else f"<max_output_lines>{max_output_lines}</max_output_lines>"
        )
        result = self.execute(
            "<tool>run-command-background</tool><args>"
            f"<cmd><![CDATA[{command}]]></cmd>{lines}</args>",
            dispatcher=dispatcher,
            prefix="launch",
        )
        self.assertIsInstance(result, Result)
        self.assertEqual(result.status, ResultStatus.SUCCESS)
        handle = self._field(result.body, "handle")
        self.assertRegex(handle, r"\Aproc_[A-Za-z0-9_-]+\Z")
        return handle, result

    def _stream(
        self,
        handle: str,
        *,
        offset: int | None = 0,
        max_output_lines: int | None = None,
    ):
        offset_xml = "" if offset is None else f"<offset>{offset}</offset>"
        lines_xml = (
            ""
            if max_output_lines is None
            else f"<max_output_lines>{max_output_lines}</max_output_lines>"
        )
        return self.execute(
            "<tool>stream-output</tool><args>"
            f"<handle>{handle}</handle>{offset_xml}{lines_xml}</args>",
            prefix="stream",
        )

    def _wait_for_terminal(self, handle: str, timeout: float = 5) -> Result:
        deadline = time.monotonic() + timeout
        last: Result | ProtocolError | None = None
        while time.monotonic() < deadline:
            last = self._stream(handle)
            self.assertIsInstance(last, Result)
            if self._field(last.body, "status") != ProcessStatus.RUNNING.value:
                return last
            time.sleep(0.05)
        self.fail(f"Background process did not terminate; last response={last!r}")

    def test_natural_exit_streams_output_and_discards_workspace_changes(self) -> None:
        self.require_runtime()
        (self.session.paths.host_output / "seed.txt").write_text(
            "seed\n",
            encoding="utf-8",
        )
        handle, launched = self._launch(
            "python3 -u -c \"from pathlib import Path; import time; "
            "print(Path('seed.txt').read_text(), end=''); "
            "Path('temporary.txt').write_text('discarded'); "
            "time.sleep(0.15); print('finished')\""
        )

        terminal = self._wait_for_terminal(handle)
        self.assertEqual(self._field(terminal.body, "status"), "exited")
        self.assertEqual(self._field(terminal.body, "exit_code"), "0")
        self.assertEqual(self._field(terminal.body, "termination_reason"), "exited")
        self.assertIn("seed\nfinished\n", terminal.body)
        self.assertIn("workspace_changes=discarded", launched.body)
        self.assertFalse((self.session.paths.host_output / "temporary.txt").exists())

        record = self.manager.load(self.session.id).state.process(handle)
        self.assertEqual(record.status, ProcessStatus.EXITED)
        self.assertEqual(record.exit_code, 0)
        self.assertIsNotNone(record.ended_at)
        log = self.session.paths.host_root / f".process-{handle}.log"
        self.assertTrue(log.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_streaming_uses_utf8_offsets_and_bounded_line_windows(self) -> None:
        self.require_runtime()
        handle, _ = self._launch(
            "python3 -u -c \"print('é'); print('two'); print('three')\""
        )
        time.sleep(0.2)

        first = self._stream(handle, max_output_lines=2)
        self.assertIsInstance(first, Result)
        self.assertEqual(first.status, ResultStatus.PARTIAL)
        self.assertIn("é\ntwo\n", first.body)
        self.assertNotIn("three\n", first.body)
        next_offset = int(self._field(first.body, "next_offset"))

        second = self._stream(handle, offset=next_offset, max_output_lines=2)
        self.assertIsInstance(second, Result)
        self.assertEqual(second.status, ResultStatus.SUCCESS)
        self.assertIn("three\n", second.body)

        invalid = self._stream(handle, offset=1)
        self.assertIsInstance(invalid, ProtocolError)
        self.assertEqual(invalid.code, "invalid_offset")

    def test_kill_is_handle_scoped_and_persists_user_reason(self) -> None:
        self.require_runtime()
        handle, _ = self._launch(
            "python3 -u -c \"import time; print('ready'); time.sleep(60)\""
        )
        killed = self.execute(
            "<tool>kill-process</tool><args>"
            f"<handle>{handle}</handle></args>",
            prefix="kill",
        )

        self.assertIsInstance(killed, Result)
        self.assertEqual(killed.status, ResultStatus.SUCCESS)
        self.assertEqual(self._field(killed.body, "status"), "killed")
        self.assertEqual(self._field(killed.body, "termination_reason"), "user")
        record = self.manager.load(self.session.id).state.process(handle)
        self.assertEqual(record.status, ProcessStatus.KILLED)
        self.assertEqual(record.termination_reason, ProcessTerminationReason.USER)

        repeated = self.execute(
            "<tool>kill-process</tool><args>"
            f"<handle>{handle}</handle></args>",
            prefix="kill",
        )
        self.assertIsInstance(repeated, Result)
        self.assertEqual(repeated.status, ResultStatus.FAILURE)

    def test_output_and_runtime_limits_terminate_background_work(self) -> None:
        self.require_runtime()
        output_handle, _ = self._launch(
            "python3 -u -c \"[print(i) for i in range(1000)]\"",
            max_output_lines=2,
        )
        output_terminal = self._wait_for_terminal(output_handle)
        self.assertEqual(
            self._field(output_terminal.body, "termination_reason"),
            "output_limit",
        )
        output_text = output_terminal.body.split("output:\n", 1)[1]
        self.assertEqual(len(output_text.splitlines()), 2)

        limited = AgentToolDispatcher(
            self.manager,
            command_limits=CommandToolLimits(
                background_max_runtime_seconds=1,
            ),
        )
        runtime_handle, _ = self._launch(
            "python3 -u -c \"import time; print('waiting'); time.sleep(30)\"",
            dispatcher=limited,
        )
        runtime_terminal = self._wait_for_terminal(runtime_handle, timeout=4)
        self.assertEqual(
            self._field(runtime_terminal.body, "termination_reason"),
            "runtime_limit",
        )

    def test_background_sandbox_denies_network_socket_creation(self) -> None:
        self.require_runtime()
        handle, _ = self._launch(
            "python3 -u -c \"import socket; socket.socket()\""
        )

        terminal = self._wait_for_terminal(handle)

        self.assertEqual(self._field(terminal.body, "status"), "exited")
        self.assertEqual(self._field(terminal.body, "exit_code"), "1")
        self.assertIn("PermissionError", terminal.body)

    def test_failed_launch_cleans_process_record_and_private_log(self) -> None:
        self.require_runtime()
        failed_dispatcher = AgentToolDispatcher(
            self.manager,
            sandbox_binary="/usr/bin/false",
        )

        response = self.execute(
            "<tool>run-command-background</tool><args>"
            "<cmd>python3 -c &quot;print('never')&quot;</cmd></args>",
            dispatcher=failed_dispatcher,
            prefix="failed",
        )

        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "sandbox_failed")
        self.assertEqual(self.manager.load(self.session.id).state.processes, ())
        self.assertEqual(
            list(self.session.paths.host_root.glob(".process-proc_*.log")),
            [],
        )

    def test_launch_is_terminated_when_its_handle_result_cannot_persist(self) -> None:
        self.require_runtime()
        manager = SessionManager(
            self.root / "small-result-sessions",
            max_result_bytes=1,
        )
        session = manager.create(session_id="sess_unreported_background")
        dispatcher = AgentToolDispatcher(manager)

        response = dispatcher.execute(
            self.action(
                session,
                "<tool>run-command-background</tool><args>"
                "<cmd>python3 -c &quot;import time; time.sleep(60)&quot;</cmd>"
                "</args>",
                prefix="unreported",
            ),
            session,
        )

        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "result_too_large")
        process = manager.load(session.id).state.processes[0]
        self.assertEqual(process.status, ProcessStatus.KILLED)
        self.assertEqual(
            process.termination_reason,
            ProcessTerminationReason.SUPERVISOR_ERROR,
        )

    def test_failed_stream_result_does_not_advance_persisted_cursor(self) -> None:
        self.require_runtime()
        handle, _ = self._launch("python3 -u -c \"print('retained')\"")
        time.sleep(0.2)
        original_limit = self.manager.max_result_bytes
        self.manager.max_result_bytes = 1
        try:
            failed = self._stream(handle, offset=0)
        finally:
            self.manager.max_result_bytes = original_limit

        self.assertIsInstance(failed, ProtocolError)
        self.assertEqual(failed.code, "result_too_large")
        process = self.manager.load(self.session.id).state.process(handle)
        self.assertEqual(process.output_offset, 0)
        self.assertGreater(process.output_bytes, 0)

        retried = self._stream(handle, offset=None)
        self.assertIsInstance(retried, Result)
        self.assertIn("retained\n", retried.body)

    def test_stream_rejects_a_log_with_broadened_permissions(self) -> None:
        self.require_runtime()
        handle, _ = self._launch("python3 -u -c \"print('safe')\"")
        self._wait_for_terminal(handle)
        log = self.session.paths.host_root / f".process-{handle}.log"
        log.chmod(0o644)

        response = self._stream(handle)

        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "process_log_invalid")

    def test_physical_session_path_is_redacted_across_output_chunks(self) -> None:
        self.require_runtime()
        physical = str(self.session.paths.host_root)
        handle, _ = self._launch(
            "python3 -u -c \"import sys,time; "
            f"p={physical!r}; "
            "[(sys.stdout.write(c),sys.stdout.flush(),time.sleep(.002)) for c in p]; "
            "print()\""
        )

        terminal = self._wait_for_terminal(handle)

        self.assertNotIn(physical, terminal.body)
        self.assertIn("[session]", terminal.body)

    def test_persisted_pid_is_never_signaled_without_live_ownership(self) -> None:
        handle = self.manager.register_process(
            self.session,
            pid=os.getpid(),
            handle="proc_stale_pid",
        )
        with patch("swoon.tools.background.ForegroundCommandTools._kill") as kill:
            response = self.execute(
                "<tool>kill-process</tool><args>"
                f"<handle>{handle}</handle></args>",
                prefix="stale",
            )

        kill.assert_not_called()
        self.assertIsInstance(response, Result)
        self.assertEqual(response.status, ResultStatus.FAILURE)
        record = self.manager.load(self.session.id).state.process(handle)
        self.assertEqual(record.status, ProcessStatus.LOST)
        self.assertIsNone(record.exit_code)
        self.assertEqual(
            record.termination_reason,
            ProcessTerminationReason.SUPERVISOR_LOST,
        )

    def test_handles_are_session_scoped_and_shutdown_is_persisted(self) -> None:
        self.require_runtime()
        handle, _ = self._launch(
            "python3 -u -c \"import time; print('live'); time.sleep(60)\""
        )
        other = self.manager.create(session_id="sess_background_other")
        isolated = self.execute(
            "<tool>kill-process</tool><args>"
            f"<handle>{handle}</handle></args>",
            session=other,
            prefix="other",
        )
        self.assertIsInstance(isolated, ProtocolError)
        self.assertEqual(isolated.code, "unknown_process_handle")

        self.dispatcher.shutdown_background(
            self.session,
            reason=ProcessTerminationReason.HOST_EXIT,
        )
        record = self.manager.load(self.session.id).state.process(handle)
        self.assertEqual(record.status, ProcessStatus.KILLED)
        self.assertEqual(
            record.termination_reason,
            ProcessTerminationReason.HOST_EXIT,
        )

    def test_agent_completion_stops_live_process_before_terminal_status(self) -> None:
        self.require_runtime()
        transport = _FakeTransport(
            [
                (
                    '<aeml turn="1" session="sess_background">'
                    '<action id="launch_orchestrated">'
                    '<tool>run-command-background</tool><args>'
                    '<cmd><![CDATA[python3 -u -c "import time; '
                    'print(\'live\'); time.sleep(60)"]]></cmd>'
                    '</args></action><next>await_result</next></aeml>'
                ),
                (
                    '<aeml turn="2" session="sess_background">'
                    '<complete>Background check complete.</complete></aeml>'
                ),
            ]
        )
        orchestrator = AgentOrchestrator(
            self.manager,
            AEMLChatChannel(
                transport,
                prompt_builder=AEMLPromptBuilder(self.dispatcher.tool_specs),
            ),
            dispatcher=self.dispatcher,
        )

        outcome = orchestrator.run(self.session, "Start then finish")

        self.assertEqual(outcome.reason, RunStopReason.COMPLETED)
        process = self.manager.load(self.session.id).state.processes[0]
        self.assertEqual(process.status, ProcessStatus.KILLED)
        self.assertEqual(
            process.termination_reason,
            ProcessTerminationReason.SESSION_END,
        )


if __name__ == "__main__":
    unittest.main()
