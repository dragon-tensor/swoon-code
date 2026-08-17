from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swoon.aeml import AEMLParser, AEMLValidator
from swoon.aeml.models import ProtocolError, Result, ResultStatus, TypedArgument
from swoon.session import SessionManager
from swoon.tools import AgentToolDispatcher, MutationToolLimits


class MutationToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        (source / "src").mkdir(parents=True)
        (source / "src" / "app.py").write_text("print('input')\n", encoding="utf-8")
        (source / "asset.bin").write_bytes(b"\x00\x01\xff")
        (source / ".env").write_text("SECRET=hidden\n", encoding="utf-8")
        self.manager = SessionManager(self.root / "sessions")
        self.session = self.manager.create(source, session_id="sess_mutations")
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

    def action(self, action_id: str, body: str):
        message = self.parser.parse(
            f'<aeml turn="1" session="{self.session.id}">'
            f'<action id="{action_id}">{body}</action>'
            "<next>await_result</next></aeml>"
        )
        return self.validator.validate(message).actions[0]

    def execute(self, action_id: str, body: str, *, confirmed: bool = False):
        return self.dispatcher.execute(
            self.action(action_id, body),
            self.session,
            confirmed=confirmed,
        )

    def test_create_file_is_private_atomic_and_persisted(self) -> None:
        response = self.execute(
            "create1",
            "<tool>create-file</tool><path>src/new.py</path>"
            "<args><content>print('new')\n</content></args>",
        )

        target = self.session.paths.host_output / "src" / "new.py"
        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "path_not_found")
        (self.session.paths.host_output / "src").mkdir()
        response = self.execute(
            "create2",
            "<tool>create-file</tool><path>src/new.py</path>"
            "<args><content>print('new')\n</content></args>",
        )

        self.assertIsInstance(response, Result)
        self.assertEqual(response.status, ResultStatus.SUCCESS)
        self.assertEqual(target.read_text(encoding="utf-8"), "print('new')\n")
        if os.name != "nt":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        record = self.manager.load(self.session.id).state.action("create2")
        self.assertEqual(record.tool, "create-file")
        self.assertFalse(any(item.name.startswith(".swoon-tmp-") for item in target.parent.iterdir()))

    def test_forged_typed_content_cannot_diverge_from_validated_source(self) -> None:
        legitimate = self.action(
            "forged_create",
            "<tool>create-file</tool><path>forged.txt</path>"
            "<args><content>safe</content></args>",
        )
        forged = replace(
            legitimate,
            arguments=(TypedArgument("content", "different"),),
        )

        response = self.dispatcher.execute(forged, self.session)

        self.assertEqual(response.code, "invalid_validated_action")
        self.assertFalse((self.session.paths.host_output / "forged.txt").exists())

    def test_dispatcher_rejects_session_owned_by_another_manager(self) -> None:
        other_manager = SessionManager(self.root / "other-sessions")
        other = other_manager.create(session_id=self.session.id)
        action = self.action(
            "foreign_create",
            "<tool>create-file</tool><path>foreign.txt</path>"
            "<args><content>no</content></args>",
        )

        response = self.dispatcher.execute(action, other)

        self.assertEqual(response.code, "session_integrity_error")
        self.assertFalse((other.paths.host_output / "foreign.txt").exists())

    def test_create_refuses_existing_or_credential_shaped_targets(self) -> None:
        output = self.session.paths.host_output
        (output / "same.txt").write_text("original", encoding="utf-8")

        existing = self.execute(
            "existing1",
            "<tool>create-file</tool><path>same.txt</path>"
            "<args><content>replacement</content></args>",
        )
        denied = self.execute(
            "denied1",
            "<tool>create-file</tool><path>.env</path>"
            "<args><content>secret</content></args>",
        )

        self.assertEqual(existing.code, "path_exists")
        self.assertEqual(denied.code, "credential_path")
        self.assertEqual((output / "same.txt").read_text(encoding="utf-8"), "original")

    def test_overwrite_never_follows_symlink_or_hardlink_targets(self) -> None:
        output = self.session.paths.host_output
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            os.symlink(outside, output / "linked.txt")
            os.link(outside, output / "hard-linked.txt")
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest("Link creation unavailable")

        symlink_action = self.action(
            "overwrite_symlink",
            "<tool>overwrite-file</tool><path>linked.txt</path>"
            "<args><content>changed</content></args>"
            "<expect_confirm>true</expect_confirm>",
        )
        hardlink_action = self.action(
            "overwrite_hardlink",
            "<tool>overwrite-file</tool><path>hard-linked.txt</path>"
            "<args><content>changed</content></args>"
            "<expect_confirm>true</expect_confirm>",
        )

        symlink_result = self.dispatcher.execute(
            symlink_action,
            self.session,
            confirmed=True,
        )
        hardlink_result = self.dispatcher.execute(
            hardlink_action,
            self.session,
            confirmed=True,
        )

        self.assertEqual(symlink_result.code, "path_escape")
        self.assertEqual(hardlink_result.code, "path_escape")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_nonempty_overwrite_requires_declared_and_host_confirmation(self) -> None:
        target = self.session.paths.host_output / "config.txt"
        target.write_text("old", encoding="utf-8")
        undeclared = self.action(
            "overwrite1",
            "<tool>overwrite-file</tool><path>config.txt</path>"
            "<args><content>new</content></args>",
        )
        request = self.dispatcher.confirmation_request(undeclared, self.session)
        self.assertIsInstance(request, ProtocolError)
        self.assertEqual(request.code, "confirmation_required")

        declared = self.action(
            "overwrite2",
            "<tool>overwrite-file</tool><path>config.txt</path>"
            "<args><content>new</content></args>"
            "<expect_confirm>true</expect_confirm>",
        )
        blocked = self.dispatcher.execute(declared, self.session)
        self.assertEqual(blocked.code, "confirmation_required")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")

        approved = self.dispatcher.execute(declared, self.session, confirmed=True)
        self.assertIsInstance(approved, Result)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_empty_overwrite_needs_no_human_confirmation(self) -> None:
        target = self.session.paths.host_output / "empty.txt"
        target.write_text("", encoding="utf-8")
        response = self.execute(
            "overwrite_empty",
            "<tool>overwrite-file</tool><path>empty.txt</path>"
            "<args><content>filled</content></args>",
        )
        self.assertIsInstance(response, Result)
        self.assertEqual(target.read_text(encoding="utf-8"), "filled")

    def test_append_and_exact_edit_preserve_executable_mode(self) -> None:
        target = self.session.paths.host_output / "script.sh"
        target.write_text("#!/bin/sh\necho one\n", encoding="utf-8")
        target.chmod(0o700)

        appended = self.execute(
            "append1",
            "<tool>append-file</tool><path>script.sh</path>"
            "<args><content>echo two\n</content></args>",
        )
        edited = self.execute(
            "edit1",
            "<tool>edit-file</tool><path>script.sh</path>"
            "<args><old_str>echo one</old_str><new_str>echo first</new_str></args>",
        )

        self.assertIsInstance(appended, Result)
        self.assertIsInstance(edited, Result)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "#!/bin/sh\necho first\necho two\n",
        )
        if os.name != "nt":
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_edit_rejects_missing_ambiguous_and_binary_matches_without_change(self) -> None:
        output = self.session.paths.host_output
        repeated = output / "repeated.txt"
        repeated.write_text("same same", encoding="utf-8")
        binary = output / "binary.dat"
        binary.write_bytes(b"same\x00value")

        ambiguous = self.execute(
            "edit_ambiguous",
            "<tool>edit-file</tool><path>repeated.txt</path>"
            "<args><old_str>same</old_str><new_str>new</new_str></args>",
        )
        missing = self.execute(
            "edit_missing",
            "<tool>edit-file</tool><path>repeated.txt</path>"
            "<args><old_str>absent</old_str><new_str>new</new_str></args>",
        )
        binary_result = self.execute(
            "edit_binary",
            "<tool>edit-file</tool><path>binary.dat</path>"
            "<args><old_str>same</old_str><new_str>new</new_str></args>",
        )

        self.assertEqual(ambiguous.code, "ambiguous_edit")
        self.assertEqual(missing.code, "old_str_not_found")
        self.assertEqual(binary_result.code, "binary_unsupported")
        self.assertEqual(repeated.read_text(encoding="utf-8"), "same same")
        self.assertEqual(binary.read_bytes(), b"same\x00value")

    def test_chunk_sequence_blocks_dependencies_until_final_append(self) -> None:
        created = self.execute(
            "chunk1",
            "<tool>create-file</tool><path>large.txt</path>"
            "<args><content>first</content></args><chunk seq=\"1\" final=\"false\"/>",
        )
        self.assertIsInstance(created, Result)

        blocked_read = self.execute(
            "chunk_read",
            "<tool>read-file</tool><path>large.txt</path>",
        )
        wrong = self.execute(
            "chunk_wrong",
            "<tool>append-file</tool><path>large.txt</path>"
            "<args><content>third</content></args><chunk seq=\"3\" final=\"true\"/>",
        )
        self.assertEqual(blocked_read.code, "write_incomplete")
        self.assertEqual(wrong.code, "chunk_sequence_error")
        self.assertEqual(
            (self.session.paths.host_output / "large.txt").read_text(encoding="utf-8"),
            "first",
        )

        final = self.execute(
            "chunk2",
            "<tool>append-file</tool><path>large.txt</path>"
            "<args><content> second</content></args><chunk seq=\"2\" final=\"true\"/>",
        )
        readable = self.execute(
            "chunk_read2",
            "<tool>read-file</tool><path>large.txt</path>",
        )
        self.assertIsInstance(final, Result)
        self.assertEqual(readable.body, "first second")
        state = self.manager.load(self.session.id).state
        self.assertTrue(state.chunk(self.action("placeholder", "<tool>read-file</tool><path>large.txt</path>").source.path).finalized)

    def test_copy_file_moves_binary_bytes_without_llm_roundtrip(self) -> None:
        response = self.execute(
            "copy_file1",
            "<tool>copy-file</tool><args>"
            '<from root="input">asset.bin</from>'
            '<to root="output">asset.bin</to></args>',
        )
        self.assertIsInstance(response, Result)
        self.assertEqual(
            (self.session.paths.host_output / "asset.bin").read_bytes(),
            b"\x00\x01\xff",
        )

    def test_copy_directory_to_empty_root_filters_credentials(self) -> None:
        response = self.execute(
            "copy_dir1",
            "<tool>copy-dir</tool><args>"
            '<from root="input">.</from><to root="output">.</to></args>',
        )

        output = self.session.paths.host_output
        self.assertIsInstance(response, Result)
        self.assertTrue((output / "src" / "app.py").is_file())
        self.assertTrue((output / "asset.bin").is_file())
        self.assertFalse((output / ".env").exists())
        self.assertIn("skipped 1 denied entry", response.body)

    def test_copy_directory_failure_removes_partial_destination(self) -> None:
        source = self.session.paths.host_output / "source"
        source.mkdir()
        (source / "a").write_bytes(b"aa")
        (source / "b").write_bytes(b"bb")
        limited = AgentToolDispatcher(
            self.manager,
            mutation_limits=MutationToolLimits(
                max_content_bytes=4,
                max_file_bytes=4,
                max_copy_entries=10,
                max_copy_bytes=3,
            ),
        )
        response = limited.execute(
            self.action(
                "copy_limited",
                "<tool>copy-dir</tool><args>"
                '<from root="output">source</from>'
                '<to root="output">destination</to></args>',
            ),
            self.session,
        )

        self.assertIsInstance(response, ProtocolError)
        self.assertEqual(response.code, "copy_too_large")
        self.assertFalse((self.session.paths.host_output / "destination").exists())

    def test_copy_directory_rejects_symlinks_and_recursive_destinations(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("Symbolic links unavailable")
        output = self.session.paths.host_output
        source = output / "tree"
        source.mkdir()
        (source / "file").write_text("safe", encoding="utf-8")
        os.symlink("file", source / "link")

        unsafe = self.execute(
            "copy_symlink",
            "<tool>copy-dir</tool><args>"
            '<from root="output">tree</from><to root="output">copy</to></args>',
        )
        recursive = self.execute(
            "copy_recursive",
            "<tool>copy-dir</tool><args>"
            '<from root="output">tree</from><to root="output">tree/nested</to></args>',
        )

        self.assertEqual(unsafe.code, "path_escape")
        self.assertFalse((output / "copy").exists())
        self.assertEqual(recursive.code, "recursive_copy")
        self.assertFalse((source / "nested").exists())


if __name__ == "__main__":
    unittest.main()
