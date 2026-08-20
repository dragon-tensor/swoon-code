from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swoon.aeml import AEMLParser, AEMLValidator
from swoon.aeml.models import ProtocolError, Result, ResultStatus, TypedArgument
from swoon.session import SessionManager
from swoon.tools import ReadOnlyToolDispatcher, ReadToolLimits


class ReadOnlyToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        (source / "src").mkdir(parents=True)
        (source / "src" / "input.py").write_text(
            "alpha\nbeta needle\ngamma\n",
            encoding="utf-8",
        )
        (source / ".env").write_text("INPUT_SECRET=hidden\n", encoding="utf-8")
        self.manager = SessionManager(self.root / "sessions")
        self.session = self.manager.create(source, session_id="sess_tools")
        self.dispatcher = ReadOnlyToolDispatcher(self.manager)
        self.parser = AEMLParser()
        self.validator = AEMLValidator()

    def tearDown(self) -> None:
        self._make_writable(self.root)
        self.temporary.cleanup()

    @staticmethod
    def _make_writable(root: Path) -> None:
        if not root.exists():
            return
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

    def action(self, inner: str):
        message = self.parser.parse(
            f'<aeml turn="1" session="{self.session.id}">{inner}'
            "<next>await_result</next></aeml>"
        )
        return self.validator.validate(
            message,
            expected_turn=1,
            expected_session=self.session.id,
        ).actions[0]

    def execute(self, inner: str):
        return self.dispatcher.execute(self.action(inner), self.session)

    def test_read_file_range_is_structured_and_persisted(self) -> None:
        response = self.execute(
            '<action id="read1"><tool>read-file</tool>'
            '<path root="input">src/input.py</path>'
            '<args><start_line>2</start_line><end_line>3</end_line></args></action>'
        )

        self.assertIsInstance(response, Result)
        self.assertEqual(response.status, ResultStatus.SUCCESS)
        self.assertEqual(response.body, "beta needle\ngamma\n")
        self.assertEqual(response.lines, "2-3")
        record = self.manager.load(self.session.id).state.action("read1")
        self.assertEqual(record.tool, "read-file")
        self.assertEqual(record.result, response)

    def test_read_file_output_is_utf8_safely_truncated(self) -> None:
        target = self.session.paths.host_output / "unicode.txt"
        target.write_text("🙂🙂🙂\n", encoding="utf-8")
        dispatcher = ReadOnlyToolDispatcher(
            self.manager,
            limits=ReadToolLimits(max_output_bytes=5),
        )
        response = dispatcher.execute(
            self.action(
                '<action id="read_truncated"><tool>read-file</tool>'
                '<path root="output">unicode.txt</path></action>'
            ),
            self.session,
        )

        self.assertIsInstance(response, Result)
        self.assertEqual(response.status, ResultStatus.PARTIAL)
        self.assertEqual(response.body, "🙂")
        self.assertEqual(response.truncation.total_bytes, 13)
        self.assertEqual(response.truncation.offset, 0)

    def test_binary_file_returns_protocol_error_and_is_not_persisted(self) -> None:
        (self.session.paths.host_output / "binary.dat").write_bytes(b"hello\x00world")
        response = self.execute(
            '<action id="binary1"><tool>read-file</tool>'
            '<path root="output">binary.dat</path></action>'
        )

        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "binary_unsupported")
        self.assertIsNone(self.manager.load(self.session.id).state.action("binary1"))

    def test_ranged_read_does_not_skip_binary_validation_before_start(self) -> None:
        payload = b"a" * 9000 + b"\x00\ntext line\n"
        (self.session.paths.host_output / "late-binary.dat").write_bytes(payload)
        response = self.execute(
            '<action id="binary_range"><tool>read-file</tool>'
            '<path root="output">late-binary.dat</path>'
            '<args><start_line>2</start_line><end_line>2</end_line></args></action>'
        )
        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "binary_unsupported")

    def test_list_dir_is_recursive_deterministic_and_filters_credentials(self) -> None:
        output = self.session.paths.host_output
        (output / "src").mkdir()
        (output / "src" / "b.py").write_text("b", encoding="utf-8")
        (output / "src" / "a.py").write_text("a", encoding="utf-8")
        (output / ".env").write_text("SECRET=never", encoding="utf-8")
        (output / "server.pem").write_text("private", encoding="utf-8")
        (output / "escape\x1b[31m.txt").write_text("terminal", encoding="utf-8")

        response = self.execute(
            '<action id="list1"><tool>list-dir</tool><path root="output">.</path>'
            '<args><recursive>true</recursive><pattern>*.py</pattern></args></action>'
        )

        self.assertIsInstance(response, Result)
        self.assertEqual(response.body, "f src/a.py 1\nf src/b.py 1\n")
        self.assertNotIn(".env", response.body)
        self.assertNotIn("pem", response.body)
        self.assertNotIn("escape", response.body)

    def test_grep_is_literal_scoped_and_skips_binary_and_denied_files(self) -> None:
        output = self.session.paths.host_output
        (output / "src").mkdir()
        (output / "src" / "one.txt").write_text(
            "before\nneedle [literal]\nafter\nsecond needle [literal]\n",
            encoding="utf-8",
        )
        (output / "src" / "binary").write_bytes(b"needle\x00secret")
        (output / ".env").write_text("needle DO_NOT_LEAK", encoding="utf-8")

        response = self.execute(
            '<action id="grep1"><tool>grep</tool><path root="output">.</path><args>'
            '<pattern>needle [literal]</pattern><max_results>1</max_results>'
            '<context_lines>1</context_lines></args></action>'
        )

        self.assertIsInstance(response, Result)
        self.assertEqual(
            response.body,
            "src/one.txt-1-before\n"
            "src/one.txt:2:needle [literal]\n"
            "src/one.txt-3-after\n",
        )
        self.assertNotIn("DO_NOT_LEAK", response.body)

    def test_grep_total_scan_limit_is_enforced(self) -> None:
        output = self.session.paths.host_output
        (output / "one.txt").write_text("a" * 20, encoding="utf-8")
        (output / "two.txt").write_text("b" * 20, encoding="utf-8")
        dispatcher = ReadOnlyToolDispatcher(
            self.manager,
            limits=ReadToolLimits(max_file_bytes=100, max_scan_bytes=25),
        )
        response = dispatcher.execute(
            self.action(
                '<action id="grep_limit"><tool>grep</tool><path root="output">.</path>'
                '<args><pattern>missing</pattern></args></action>'
            ),
            self.session,
        )
        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "tool_failed")

    def test_read_of_unfinished_output_chunk_is_blocked(self) -> None:
        (self.session.paths.host_output / "partial.py").write_text("partial", encoding="utf-8")
        self.manager.record_chunk(
            self.session,
            self.action(
                '<action id="placeholder"><tool>read-file</tool>'
                '<path root="output">partial.py</path></action>'
            ).source.path,
            seq=1,
            final=False,
        )
        response = self.execute(
            '<action id="read_partial"><tool>read-file</tool>'
            '<path root="output">partial.py</path></action>'
        )
        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "write_incomplete")

    def test_cached_action_result_is_returned_without_second_revision(self) -> None:
        action = self.action(
            '<action id="cache1"><tool>list-dir</tool><path root="input">.</path></action>'
        )
        first = self.dispatcher.execute(action, self.session)
        revision = self.session.state.revision
        second = self.dispatcher.execute(action, self.session)

        self.assertEqual(second, first)
        self.assertEqual(self.session.state.revision, revision)

        changed = self.action(
            '<action id="cache1"><tool>list-dir</tool>'
            '<path root="input">src</path></action>'
        )
        rejected = self.dispatcher.execute(changed, self.session)
        self.assertIsInstance(rejected, ProtocolError)
        self.assertEqual(rejected.code, "duplicate_action_id")
        self.assertEqual(self.session.state.revision, revision)

    def test_write_and_unimplemented_read_tools_fail_closed(self) -> None:
        write = self.action(
            '<action id="write1"><tool>create-file</tool><path>x</path>'
            '<args><content>x</content></args></action>'
        )
        response = self.dispatcher.execute(write, self.session)
        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "write_tool_disabled")

        unsupported = self.action(
            '<action id="env1"><tool>get-env</tool><args><name>PATH</name></args></action>'
        )
        response = self.dispatcher.execute(unsupported, self.session)
        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "unsupported_read_tool")

    def test_forged_validated_action_fails_closed(self) -> None:
        legitimate = self.action(
            '<action id="forged1"><tool>list-dir</tool><path root="input">.</path></action>'
        )
        forged = replace(
            legitimate,
            arguments=(TypedArgument("recursive", object()),),
        )
        response = self.dispatcher.execute(forged, self.session)
        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "invalid_validated_action")

    def test_execute_message_runs_a_validated_read_batch(self) -> None:
        message = self.parser.parse(
            f'<aeml turn="1" session="{self.session.id}">'
            '<action id="batch1"><tool>list-dir</tool><path root="input">.</path></action>'
            '<action id="batch2"><tool>read-file</tool>'
            '<path root="input">src/input.py</path></action>'
            '<next>await_result</next></aeml>'
        )
        validated = self.validator.validate(message)
        responses = self.dispatcher.execute_message(validated, self.session)
        self.assertEqual(len(responses), 2)
        self.assertTrue(all(isinstance(response, Result) for response in responses))

    def test_dependency_listing_parses_manifests_and_redacts_urls(self) -> None:
        output = self.session.paths.host_output
        (output / "pyproject.toml").write_text(
            '[project]\ndependencies = ["flask>=3", '
            '"private @ https://user:password@example.test/pkg.whl?token=abc"]\n'
            '[project.optional-dependencies]\ndev = ["pytest>=8"]\n',
            encoding="utf-8",
        )
        (output / "package.json").write_text(
            '{"dependencies":{"react":"^19"},"devDependencies":{"vite":"^7"}}',
            encoding="utf-8",
        )
        (output / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")

        response = self.execute(
            '<action id="deps1"><tool>list-dependencies</tool></action>'
        )

        self.assertIsInstance(response, Result)
        self.assertIn("pip runtime flask>=3", response.body)
        self.assertIn("pip optional:dev pytest>=8", response.body)
        self.assertIn("pnpm runtime react ^19", response.body)
        self.assertIn("pnpm dev vite ^7", response.body)
        self.assertIn("https://[redacted]@example.test/pkg.whl?token=[redacted]", response.body)
        self.assertNotIn("password", response.body)
        self.assertNotIn("token=abc", response.body)

    def test_invalid_dependency_manifest_returns_tool_error(self) -> None:
        (self.session.paths.host_output / "package.json").write_text("{broken", encoding="utf-8")
        response = self.execute(
            '<action id="deps_bad"><tool>list-dependencies</tool>'
            '<args><manager>npm</manager></args></action>'
        )
        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "tool_failed")


@unittest.skipUnless(shutil.which("git"), "Git executable unavailable")
class GitReadToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = SessionManager(self.root / "sessions")
        self.session = self.manager.create(session_id="sess_git_tools")
        self.output = self.session.paths.host_output
        self.git("init", "-q")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.invalid")
        (self.output / "safe.txt").write_text("old safe\n", encoding="utf-8")
        (self.output / ".env").write_text("TOKEN=old-secret\n", encoding="utf-8")
        self.git("add", "safe.txt", ".env")
        self.git("commit", "-q", "-m", "initial commit")
        (self.output / "safe.txt").write_text("new safe\n", encoding="utf-8")
        (self.output / ".env").write_text("TOKEN=new-secret\n", encoding="utf-8")
        self.dispatcher = ReadOnlyToolDispatcher(self.manager)
        self.parser = AEMLParser()
        self.validator = AEMLValidator()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> None:
        subprocess.run(
            [shutil.which("git"), "-C", str(self.output), *arguments],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def action(self, action_id: str, tool: str, extra: str = ""):
        message = self.parser.parse(
            f'<aeml turn="1" session="{self.session.id}"><action id="{action_id}">'
            f"<tool>{tool}</tool>{extra}</action><next>await_result</next></aeml>"
        )
        return self.validator.validate(message).actions[0]

    def test_git_status_and_diff_filter_credential_paths_and_content(self) -> None:
        status = self.dispatcher.execute(self.action("status1", "git-status"), self.session)
        diff = self.dispatcher.execute(self.action("diff1", "git-diff"), self.session)

        self.assertIsInstance(status, Result)
        self.assertIn("safe.txt", status.body)
        self.assertNotIn(".env", status.body)
        self.assertIsInstance(diff, Result)
        self.assertIn("new safe", diff.body)
        self.assertNotIn(".env", diff.body)
        self.assertNotIn("secret", diff.body)

    def test_git_log_is_bounded_and_does_not_expose_email(self) -> None:
        response = self.dispatcher.execute(
            self.action(
                "log1",
                "git-log",
                "<args><max_count>1</max_count></args>",
            ),
            self.session,
        )
        self.assertIsInstance(response, Result)
        self.assertIn("initial commit", response.body)
        self.assertIn("Test User", response.body)
        self.assertNotIn("test@example.invalid", response.body)
        self.assertEqual(len(response.body.splitlines()), 1)

    def test_repository_configured_helpers_are_not_executed(self) -> None:
        marker = self.root / "helper-ran"
        command = f"sh -c 'touch {marker}'"
        self.git("config", "core.fsmonitor", command)
        self.git("config", "diff.evil.command", command)
        self.git("config", "filter.evil.clean", command)
        (self.output / ".gitattributes").write_text(
            "*.txt diff=evil filter=evil\n",
            encoding="utf-8",
        )

        status = self.dispatcher.execute(self.action("status_safe", "git-status"), self.session)
        diff = self.dispatcher.execute(self.action("diff_safe", "git-diff"), self.session)

        self.assertIsInstance(status, Result)
        self.assertIsInstance(diff, Result)
        self.assertFalse(marker.exists())

    def test_external_git_object_store_is_rejected(self) -> None:
        alternates = self.output / ".git" / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(str(self.root / "outside-objects"), encoding="utf-8")

        response = self.dispatcher.execute(self.action("status_alt", "git-status"), self.session)
        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "path_escape")

    def test_git_inspection_requires_repository(self) -> None:
        manager = SessionManager(self.root / "other-sessions")
        session = manager.create(session_id="sess_no_repo")
        dispatcher = ReadOnlyToolDispatcher(manager)
        message = self.parser.parse(
            '<aeml turn="1" session="sess_no_repo"><action id="no_repo">'
            '<tool>git-status</tool></action><next>await_result</next></aeml>'
        )
        action = self.validator.validate(message).actions[0]
        response = dispatcher.execute(action, session)
        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "not_repository")


if __name__ == "__main__":
    unittest.main()
