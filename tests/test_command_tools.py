from __future__ import annotations

import os
import platform
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from swoon.aeml import AEMLContextBuilder, AEMLParser, AEMLPromptBuilder, AEMLValidator
from swoon.aeml.models import PathRef, ProtocolError, Result, ResultStatus, Root
from swoon.session import SessionManager
from swoon.tools import (
    IMPLEMENTED_BACKGROUND_TOOLS,
    IMPLEMENTED_EXECUTION_TOOLS,
    AgentToolDispatcher,
    CommandToolLimits,
)


class ForegroundCommandToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        source.mkdir()
        (source / "safe-input.txt").write_text("safe input\n", encoding="utf-8")
        (source / ".env").write_text("INPUT_SECRET=hidden\n", encoding="utf-8")
        self.manager = SessionManager(self.root / "sessions")
        self.session = self.manager.create(source, session_id="sess_commands")
        self.dispatcher = AgentToolDispatcher(self.manager)
        self.parser = AEMLParser()
        self.validator = AEMLValidator(self.dispatcher.tool_specs)

    def tearDown(self) -> None:
        self._make_writable(self.root)
        self.temporary.cleanup()

    @staticmethod
    def _make_writable(root: Path) -> None:
        for directory, directories, files in os.walk(root, topdown=False, followlinks=False):
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

    @staticmethod
    def _runtime_available() -> bool:
        return (
            platform.system() == "Linux"
            and platform.machine().lower() in {"x86_64", "amd64", "aarch64", "arm64"}
            and shutil.which("bwrap") is not None
            and shutil.which("prlimit") is not None
            and (Path("/usr/bin/python3").exists() or Path("/usr/local/bin/python3").exists())
        )

    def require_runtime(self) -> None:
        if not self._runtime_available():
            self.skipTest("Bubblewrap foreground-command runtime is unavailable")

    def action(self, action_id: str, body: str):
        message = self.parser.parse(
            f'<aeml turn="1" session="{self.session.id}">'
            f'<action id="{action_id}">{body}</action>'
            "<next>await_result</next></aeml>"
        )
        return self.validator.validate(message).actions[0]

    def execute(self, action_id: str, body: str):
        return self.dispatcher.execute(self.action(action_id, body), self.session)

    def test_agent_allowlist_enables_foreground_and_background_execution(self) -> None:
        self.assertEqual(
            IMPLEMENTED_EXECUTION_TOOLS,
            {"run-command", "run-build", "run-tests", "run-linter"},
        )
        for name in IMPLEMENTED_EXECUTION_TOOLS:
            self.assertIn(name, self.dispatcher.tool_specs)
        self.assertEqual(
            IMPLEMENTED_BACKGROUND_TOOLS,
            {"run-command-background", "kill-process", "stream-output"},
        )
        for name in IMPLEMENTED_BACKGROUND_TOOLS:
            self.assertIn(name, self.dispatcher.tool_specs)
        for name in (
            "install-dependency",
            "remove-dependency",
            "set-env",
        ):
            self.assertNotIn(name, self.dispatcher.tool_specs)
        prompt = AEMLPromptBuilder(self.dispatcher.tool_specs).initial(
            AEMLContextBuilder().build(self.session, turn=1, user_prompt="Verify output")
        )
        self.assertIn('<available_tools count="20">', prompt)
        self.assertIn('name="run-command" effect="executing"', prompt)
        self.assertIn('name="run-command-background" effect="executing"', prompt)
        self.assertIn('name="stream-output" effect="read_only"', prompt)
        self.assertIn("filesystem changes are discarded", prompt.lower())
        self.assertIn("opaque handle", prompt.lower())

    def test_command_reads_seed_but_all_workspace_changes_are_discarded(self) -> None:
        self.require_runtime()
        output = self.session.paths.host_output
        (output / "seed.txt").write_text("original\n", encoding="utf-8")
        response = self.execute(
            "command_disposable",
            "<tool>run-command</tool><args><cmd><![CDATA["
            "python3 -c \"from pathlib import Path; "
            "print(Path('seed.txt').read_text(), end=''); "
            "Path('seed.txt').write_text('changed'); "
            "Path('created.txt').write_text('created')\""
            "]]></cmd><timeout>5</timeout><max_output_lines>20</max_output_lines></args>",
        )

        self.assertIsInstance(response, Result)
        self.assertEqual(response.status, ResultStatus.SUCCESS)
        self.assertIn("original", response.body)
        self.assertIn("workspace_changes=discarded", response.body)
        self.assertEqual((output / "seed.txt").read_text(encoding="utf-8"), "original\n")
        self.assertFalse((output / "created.txt").exists())

    def test_filtered_roots_hide_credentials_and_input_is_read_only(self) -> None:
        self.require_runtime()
        output = self.session.paths.host_output
        (output / ".env").write_text("OUTPUT_SECRET=hidden\n", encoding="utf-8")
        input_root = self.session.paths.input_root
        output_root = self.session.paths.output_root
        response = self.execute(
            "command_credentials",
            "<tool>run-command</tool><args><cmd><![CDATA["
            "python3 -c \"from pathlib import Path; "
            f"print(Path('{input_root}/safe-input.txt').read_text(), end=''); "
            f"print(Path('{input_root}/.env').exists()); "
            f"print(Path('{output_root}/.env').exists()); "
            f"p=Path('{input_root}/new.txt'); "
            "\ntry: p.write_text('no')\nexcept OSError: print('input-readonly')\""
            "]]></cmd><timeout>5</timeout></args>",
        )

        self.assertIsInstance(response, Result)
        self.assertEqual(response.status, ResultStatus.SUCCESS)
        self.assertIn("safe input", response.body)
        self.assertIn("False\nFalse\ninput-readonly", response.body)
        self.assertIn("denied_paths_omitted=2", response.body)
        self.assertFalse((self.session.paths.host_input / "new.txt").exists())

    def test_network_socket_creation_is_denied_by_inherited_seccomp(self) -> None:
        self.require_runtime()
        response = self.execute(
            "command_network",
            "<tool>run-command</tool><args><cmd><![CDATA["
            "python3 -c \"import socket; socket.socket()\""
            "]]></cmd><timeout>5</timeout></args>",
        )

        self.assertIsInstance(response, Result)
        self.assertEqual(response.status, ResultStatus.FAILURE)
        self.assertIn("PermissionError", response.body)
        self.assertIn("exit_code=1", response.body)

    def test_environment_is_cleared_before_command_execution(self) -> None:
        self.require_runtime()
        os.environ["SWOON_TEST_SECRET"] = "must-not-cross"
        try:
            response = self.execute(
                "command_environment",
                "<tool>run-command</tool><args><cmd><![CDATA["
                "python3 -c \"import os; print(os.getenv('SWOON_TEST_SECRET')); "
                "print(os.getenv('HOME')); print(os.getcwd())\""
                "]]></cmd><timeout>5</timeout></args>",
            )
        finally:
            os.environ.pop("SWOON_TEST_SECRET", None)

        self.assertEqual(response.status, ResultStatus.SUCCESS)
        self.assertIn("None\n/tmp/home\n", response.body)
        self.assertIn(self.session.paths.output_root, response.body)
        self.assertNotIn(str(self.session.paths.host_root), response.body)

    def test_timeout_is_a_persisted_structured_result(self) -> None:
        self.require_runtime()
        started = time.monotonic()
        response = self.execute(
            "command_timeout",
            "<tool>run-command</tool><args><cmd>"
            "python3 -c &quot;import time; time.sleep(10)&quot;"
            "</cmd><timeout>1</timeout></args>",
        )

        self.assertLess(time.monotonic() - started, 5)
        self.assertIsInstance(response, Result)
        self.assertEqual(response.status, ResultStatus.TIMEOUT)
        self.assertIn("exit_code=timeout", response.body)
        persisted = self.manager.load(self.session.id).state.action("command_timeout")
        self.assertEqual(persisted.result.status, ResultStatus.TIMEOUT)

    def test_line_limit_returns_partial_output_with_truncation_metadata(self) -> None:
        self.require_runtime()
        response = self.execute(
            "command_lines",
            "<tool>run-command</tool><args><cmd>"
            "python3 -c &quot;print('one'); print('two'); print('three')&quot;"
            "</cmd><timeout>5</timeout><max_output_lines>2</max_output_lines></args>",
        )

        self.assertIsInstance(response, Result)
        self.assertEqual(response.status, ResultStatus.PARTIAL)
        self.assertIn("one\ntwo\n", response.body)
        self.assertNotIn("three", response.body)
        self.assertEqual(response.lines, "1-2")
        self.assertIsNotNone(response.truncation)
        self.assertGreater(response.truncation.total_bytes, len(response.body.encode("utf-8")))

    def test_nonzero_exit_is_failure_and_is_persisted(self) -> None:
        self.require_runtime()
        response = self.execute(
            "command_failure",
            "<tool>run-command</tool><args><cmd>"
            "python3 -c &quot;raise SystemExit(7)&quot;"
            "</cmd><timeout>5</timeout></args>",
        )

        self.assertEqual(response.status, ResultStatus.FAILURE)
        self.assertIn("exit_code=7", response.body)
        record = self.manager.load(self.session.id).state.action("command_failure")
        self.assertEqual(record.result, response)

    def test_absolute_traversal_and_shell_operator_paths_fail_before_launch(self) -> None:
        absolute = self.execute(
            "command_absolute",
            "<tool>run-command</tool><args><cmd>python3 /etc/passwd</cmd></args>",
        )
        traversal = self.execute(
            "command_traversal",
            "<tool>run-command</tool><args><cmd>python3 ../escape.py</cmd></args>",
        )
        operator = self.execute(
            "command_operator",
            "<tool>run-command</tool><args><cmd>python3 app.py | cat</cmd></args>",
        )

        self.assertEqual(absolute.code, "path_escape")
        self.assertEqual(traversal.code, "path_escape")
        self.assertEqual(operator.code, "shell_syntax_disabled")

    def test_capture_safety_limit_kills_unbounded_output(self) -> None:
        self.require_runtime()
        limited = AgentToolDispatcher(
            self.manager,
            command_limits=CommandToolLimits(
                max_capture_bytes=512,
                max_result_bytes=256,
            ),
        )
        response = limited.execute(
            self.action(
                "command_capture",
                "<tool>run-command</tool><args><cmd>"
                "python3 -c &quot;print('x' * 5000)&quot;"
                "</cmd><timeout>5</timeout></args>",
            ),
            self.session,
        )

        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "output_limit_exceeded")
        self.assertIsNone(self.manager.load(self.session.id).state.action("command_capture"))

    def test_missing_or_failed_sandbox_never_falls_back_to_host_execution(self) -> None:
        marker = self.root / "must-not-exist"
        command = self.action(
            "command_no_fallback",
            "<tool>run-command</tool><args><cmd>"
            f"python3 -c &quot;open('{marker}', 'w').write('unsafe')&quot;"
            "</cmd><timeout>5</timeout></args>",
        )
        unavailable = AgentToolDispatcher(
            self.manager,
            sandbox_binary=self.root / "missing-bwrap",
        ).execute(command, self.session)

        self.assertEqual(unavailable.code, "tool_unavailable")
        self.assertFalse(marker.exists())

        self.require_runtime()
        failed = AgentToolDispatcher(
            self.manager,
            sandbox_binary="/usr/bin/false",
        ).execute(command, self.session)
        self.assertEqual(failed.code, "sandbox_failed")
        self.assertFalse(marker.exists())

    def test_unfinished_output_chunk_blocks_every_execution_tool(self) -> None:
        partial = self.session.paths.host_output / "partial.py"
        partial.write_text("print('partial')", encoding="utf-8")
        self.manager.record_chunk(
            self.session,
            PathRef("partial.py", Root.OUTPUT),
            seq=1,
            final=False,
        )

        for index, (tool, args) in enumerate(
            (
                ("run-command", "<cmd>python3 partial.py</cmd>"),
                ("run-build", ""),
                ("run-tests", ""),
                ("run-linter", ""),
                (
                    "run-command-background",
                    "<cmd>python3 partial.py</cmd>",
                ),
            ),
            start=1,
        ):
            response = self.execute(
                f"blocked_command_{index}",
                f"<tool>{tool}</tool>" + (f"<args>{args}</args>" if args else ""),
            )
            self.assertEqual(response.code, "write_incomplete")

    def test_manager_detection_rejects_ambiguous_polyglot_output(self) -> None:
        output = self.session.paths.host_output
        (output / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (output / "package.json").write_text("{}", encoding="utf-8")
        response = self.execute("command_ambiguous", "<tool>run-tests</tool>")

        self.assertEqual(response.code, "manager_ambiguous")

    def test_go_tests_run_through_detected_managed_command(self) -> None:
        self.require_runtime()
        if shutil.which("go") is None:
            self.skipTest("Go is unavailable")
        output = self.session.paths.host_output
        (output / "go.mod").write_text("module example.test/sandbox\n\ngo 1.22\n", encoding="utf-8")
        (output / "value.go").write_text(
            "package sandbox\n\nfunc Value() int { return 42 }\n",
            encoding="utf-8",
        )
        (output / "value_test.go").write_text(
            "package sandbox\n\nimport \"testing\"\n\n"
            "func TestValue(t *testing.T) { if Value() != 42 { t.Fatal(\"bad\") } }\n",
            encoding="utf-8",
        )
        response = self.execute(
            "command_go_tests",
            "<tool>run-tests</tool><args><timeout>30</timeout>"
            "<max_output_lines>50</max_output_lines></args>",
        )

        self.assertIsInstance(response, Result)
        self.assertEqual(response.status, ResultStatus.SUCCESS)
        self.assertIn("exit_code=0", response.body)
        self.assertIn("example.test/sandbox", response.body)


if __name__ == "__main__":
    unittest.main()
