from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swoon.aeml import AEMLParser, AEMLValidator
from swoon.aeml.errors import AEMLValidationError
from swoon.aeml.models import PathRef, ProtocolError, Result, Root
from swoon.session import SessionManager
from swoon.tools import (
    AgentToolDispatcher,
    ConfirmationRequest,
    IMPLEMENTED_MUTATION_TOOLS,
    MutationToolLimits,
)


class LifecycleToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = SessionManager(self.root / "sessions")
        self.session = self.manager.create(session_id="sess_lifecycle")
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

    @property
    def output(self) -> Path:
        return self.session.paths.host_output

    def test_phase_fourteen_tools_are_enabled(self) -> None:
        expected = {"delete-file", "delete-dir", "move", "rename", "chmod"}
        self.assertTrue(expected.issubset(IMPLEMENTED_MUTATION_TOOLS))
        self.assertTrue(expected.issubset(self.dispatcher.tool_specs))
        self.assertEqual(len(self.dispatcher.tool_specs), 27)

    def test_delete_file_requires_host_confirmation_and_persists_success(self) -> None:
        target = self.output / "obsolete.txt"
        target.write_text("remove me", encoding="utf-8")
        action = self.action(
            "delete_file",
            "<tool>delete-file</tool><path>obsolete.txt</path>"
            "<expect_confirm>true</expect_confirm>",
        )

        request = self.dispatcher.confirmation_request(action, self.session)
        blocked = self.dispatcher.execute(action, self.session)

        self.assertIsInstance(request, ConfirmationRequest)
        self.assertIn("9 bytes", request.reason)
        self.assertEqual(blocked.code, "confirmation_required")
        self.assertTrue(target.is_file())

        approved = self.dispatcher.execute(action, self.session, confirmed=True)

        self.assertIsInstance(approved, Result)
        self.assertFalse(target.exists())
        record = self.manager.load(self.session.id).state.action("delete_file")
        self.assertEqual(record.tool, "delete-file")

    def test_delete_confirmation_guard_detects_file_change(self) -> None:
        target = self.output / "guarded.txt"
        target.write_text("original", encoding="utf-8")
        action = self.action(
            "guarded_delete",
            "<tool>delete-file</tool><path>guarded.txt</path>"
            "<expect_confirm>true</expect_confirm>",
        )
        request = self.dispatcher.confirmation_request(action, self.session)
        self.assertIsInstance(request, ConfirmationRequest)
        self.manager.reserve_action_ids(self.session, (action.source.id,))
        self.manager.request_confirmation(
            self.session,
            action.source,
            request.reason,
            request.guard,
        )
        target.write_text("changed", encoding="utf-8")

        response = self.dispatcher.execute(action, self.session, confirmed=True)

        self.assertEqual(response.code, "confirmation_stale")
        self.assertEqual(target.read_text(encoding="utf-8"), "changed")

    def test_delete_directory_is_recursive_bounded_and_guarded(self) -> None:
        tree = self.output / "old"
        (tree / "nested").mkdir(parents=True)
        (tree / "one.txt").write_text("one", encoding="utf-8")
        (tree / "nested" / "two.txt").write_text("two", encoding="utf-8")
        action = self.action(
            "delete_tree",
            "<tool>delete-dir</tool><path>old</path>"
            "<expect_confirm>true</expect_confirm>",
        )
        request = self.dispatcher.confirmation_request(action, self.session)

        self.assertIsInstance(request, ConfirmationRequest)
        self.assertIn("3 entries, 2 files, 6 bytes", request.reason)
        response = self.dispatcher.execute(action, self.session, confirmed=True)

        self.assertIsInstance(response, Result)
        self.assertFalse(tree.exists())

    def test_delete_directory_guard_detects_added_entry(self) -> None:
        tree = self.output / "guarded-tree"
        tree.mkdir()
        action = self.action(
            "guarded_tree_delete",
            "<tool>delete-dir</tool><path>guarded-tree</path>"
            "<expect_confirm>true</expect_confirm>",
        )
        request = self.dispatcher.confirmation_request(action, self.session)
        self.assertIsInstance(request, ConfirmationRequest)
        self.manager.reserve_action_ids(self.session, (action.source.id,))
        self.manager.request_confirmation(
            self.session,
            action.source,
            request.reason,
            request.guard,
        )
        (tree / "new.txt").write_text("new", encoding="utf-8")

        response = self.dispatcher.execute(action, self.session, confirmed=True)

        self.assertEqual(response.code, "confirmation_stale")
        self.assertTrue((tree / "new.txt").is_file())

    def test_delete_directory_rejects_links_and_protected_descendants(self) -> None:
        linked_tree = self.output / "linked-tree"
        linked_tree.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            os.symlink(outside, linked_tree / "link")
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest("Symbolic links unavailable")
        link_action = self.action(
            "delete_linked_tree",
            "<tool>delete-dir</tool><path>linked-tree</path>"
            "<expect_confirm>true</expect_confirm>",
        )

        linked = self.dispatcher.confirmation_request(link_action, self.session)

        self.assertEqual(linked.code, "path_escape")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

        protected_tree = self.output / "protected-tree"
        protected_tree.mkdir()
        (protected_tree / ".env").write_text("SECRET=x", encoding="utf-8")
        protected_action = self.action(
            "delete_protected_tree",
            "<tool>delete-dir</tool><path>protected-tree</path>"
            "<expect_confirm>true</expect_confirm>",
        )
        protected = self.dispatcher.confirmation_request(protected_action, self.session)

        self.assertEqual(protected.code, "credential_path")
        self.assertTrue((protected_tree / ".env").is_file())

    def test_delete_file_rejects_symlinks_and_hardlinks(self) -> None:
        outside = self.root / "outside-delete.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            os.symlink(outside, self.output / "symbolic.txt")
            os.link(outside, self.output / "hard.txt")
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest("Link creation unavailable")

        symbolic = self.execute(
            "delete_symbolic",
            "<tool>delete-file</tool><path>symbolic.txt</path>"
            "<expect_confirm>true</expect_confirm>",
            confirmed=True,
        )
        hard = self.execute(
            "delete_hard",
            "<tool>delete-file</tool><path>hard.txt</path>"
            "<expect_confirm>true</expect_confirm>",
            confirmed=True,
        )

        self.assertEqual(symbolic.code, "path_escape")
        self.assertEqual(hard.code, "path_escape")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_move_relocates_files_and_directories_without_overwrite(self) -> None:
        source = self.output / "source.txt"
        source.write_text("source", encoding="utf-8")
        moved = self.execute(
            "move_file",
            "<tool>move</tool><args>"
            '<from root="output">source.txt</from>'
            '<to root="output">nested.txt</to></args>',
        )
        self.assertIsInstance(moved, Result)
        self.assertFalse(source.exists())
        self.assertEqual((self.output / "nested.txt").read_text(encoding="utf-8"), "source")

        directory = self.output / "tree"
        directory.mkdir()
        (directory / "child.txt").write_text("child", encoding="utf-8")
        (self.output / "container").mkdir()
        moved_directory = self.execute(
            "move_directory",
            "<tool>move</tool><args>"
            '<from root="output">tree</from>'
            '<to root="output">container/tree</to></args>',
        )
        self.assertIsInstance(moved_directory, Result)
        self.assertTrue((self.output / "container" / "tree" / "child.txt").is_file())

        existing_source = self.output / "another.txt"
        existing_source.write_text("another", encoding="utf-8")
        collision = self.execute(
            "move_collision",
            "<tool>move</tool><args>"
            '<from root="output">another.txt</from>'
            '<to root="output">nested.txt</to></args>',
        )
        self.assertEqual(collision.code, "path_exists")
        self.assertEqual(existing_source.read_text(encoding="utf-8"), "another")
        self.assertEqual((self.output / "nested.txt").read_text(encoding="utf-8"), "source")

    def test_move_destination_race_never_replaces_new_entry(self) -> None:
        source = self.output / "racing-source.txt"
        target = self.output / "racing-target.txt"
        source.write_text("source", encoding="utf-8")

        def insert_destination(_parent_fd: int, _name: str) -> bool:
            target.write_text("racer", encoding="utf-8")
            return False

        with patch(
            "swoon.tools.lifecycle.FilesystemLifecycleTools._entry_exists",
            side_effect=insert_destination,
        ):
            response = self.execute(
                "move_race",
                "<tool>move</tool><args>"
                '<from root="output">racing-source.txt</from>'
                '<to root="output">racing-target.txt</to></args>',
            )

        self.assertEqual(response.code, "path_exists")
        self.assertEqual(source.read_text(encoding="utf-8"), "source")
        self.assertEqual(target.read_text(encoding="utf-8"), "racer")

    def test_rename_is_same_parent_only_and_move_rejects_descendant(self) -> None:
        folder = self.output / "folder"
        folder.mkdir()
        (folder / "old.txt").write_text("value", encoding="utf-8")
        renamed = self.execute(
            "rename_file",
            "<tool>rename</tool><args>"
            '<from root="output">folder/old.txt</from>'
            '<to root="output">folder/new.txt</to></args>',
        )
        self.assertIsInstance(renamed, Result)
        self.assertTrue((folder / "new.txt").is_file())

        (self.output / "other").mkdir()
        mismatch = self.execute(
            "rename_parent",
            "<tool>rename</tool><args>"
            '<from root="output">folder/new.txt</from>'
            '<to root="output">other/new.txt</to></args>',
        )
        self.assertEqual(mismatch.code, "rename_parent_mismatch")
        self.assertTrue((folder / "new.txt").is_file())

        recursive = self.execute(
            "move_recursive",
            "<tool>move</tool><args>"
            '<from root="output">folder</from>'
            '<to root="output">folder/nested</to></args>',
        )
        self.assertEqual(recursive.code, "recursive_move")
        self.assertTrue(folder.is_dir())

    def test_move_rejects_unsafe_directory_contents(self) -> None:
        tree = self.output / "unsafe-tree"
        tree.mkdir()
        outside = self.root / "outside-move.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            os.symlink(outside, tree / "link")
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest("Symbolic links unavailable")

        response = self.execute(
            "move_unsafe",
            "<tool>move</tool><args>"
            '<from root="output">unsafe-tree</from>'
            '<to root="output">moved-tree</to></args>',
        )

        self.assertEqual(response.code, "path_escape")
        self.assertTrue(tree.is_dir())
        self.assertFalse((self.output / "moved-tree").exists())

    def test_chmod_only_toggles_owner_private_file_executability(self) -> None:
        target = self.output / "script.sh"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o600)

        executable = self.execute(
            "chmod_exec",
            "<tool>chmod</tool><path>script.sh</path><args><mode>700</mode></args>",
        )
        private = self.execute(
            "chmod_private",
            "<tool>chmod</tool><path>script.sh</path><args><mode>0600</mode></args>",
        )

        self.assertIsInstance(executable, Result)
        self.assertIsInstance(private, Result)
        if os.name != "nt":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

        with self.assertRaises(AEMLValidationError) as raised:
            self.action(
                "chmod_unsafe",
                "<tool>chmod</tool><path>script.sh</path><args><mode>644</mode></args>",
            )
        self.assertEqual(raised.exception.code, "invalid_argument")

        directory = self.output / "directory"
        directory.mkdir()
        rejected_directory = self.execute(
            "chmod_directory",
            "<tool>chmod</tool><path>directory</path><args><mode>700</mode></args>",
        )
        self.assertEqual(rejected_directory.code, "not_file")

    def test_unfinished_chunks_block_lifecycle_actions(self) -> None:
        created = self.execute(
            "unfinished_create",
            "<tool>create-file</tool><path>unfinished.txt</path>"
            "<args><content>part</content></args>"
            '<chunk seq="1" final="false"/>',
        )
        self.assertIsInstance(created, Result)

        delete_action = self.action(
            "delete_unfinished",
            "<tool>delete-file</tool><path>unfinished.txt</path>"
            "<expect_confirm>true</expect_confirm>",
        )
        delete = self.dispatcher.confirmation_request(delete_action, self.session)
        move = self.execute(
            "move_unfinished",
            "<tool>move</tool><args>"
            '<from root="output">unfinished.txt</from>'
            '<to root="output">moved.txt</to></args>',
        )
        chmod = self.execute(
            "chmod_unfinished",
            "<tool>chmod</tool><path>unfinished.txt</path><args><mode>700</mode></args>",
        )

        self.assertEqual(delete.code, "write_incomplete")
        self.assertEqual(move.code, "write_incomplete")
        self.assertEqual(chmod.code, "write_incomplete")
        self.assertTrue((self.output / "unfinished.txt").is_file())

    def test_move_and_delete_remap_and_clear_finalized_chunk_state(self) -> None:
        (self.output / "tree").mkdir()
        created = self.execute(
            "final_chunk",
            "<tool>create-file</tool><path>tree/file.txt</path>"
            "<args><content>complete</content></args>"
            '<chunk seq="1" final="true"/>',
        )
        self.assertIsInstance(created, Result)

        moved = self.execute(
            "move_chunk_tree",
            "<tool>move</tool><args>"
            '<from root="output">tree</from>'
            '<to root="output">relocated</to></args>',
        )
        self.assertIsInstance(moved, Result)
        state = self.manager.load(self.session.id).state
        self.assertIsNone(state.chunk(PathRef("tree/file.txt", Root.OUTPUT)))
        self.assertIsNotNone(state.chunk(PathRef("relocated/file.txt", Root.OUTPUT)))

        deleted = self.execute(
            "delete_chunk_tree",
            "<tool>delete-dir</tool><path>relocated</path>"
            "<expect_confirm>true</expect_confirm>",
            confirmed=True,
        )
        self.assertIsInstance(deleted, Result)
        state = self.manager.load(self.session.id).state
        self.assertIsNone(state.chunk(PathRef("relocated/file.txt", Root.OUTPUT)))

    def test_move_chunk_metadata_collision_fails_before_filesystem_change(self) -> None:
        for action_id, path in (("chunk_source", "source.txt"), ("chunk_target", "target.txt")):
            created = self.execute(
                action_id,
                f"<tool>create-file</tool><path>{path}</path>"
                "<args><content>complete</content></args>"
                '<chunk seq="1" final="true"/>',
            )
            self.assertIsInstance(created, Result)
        (self.output / "target.txt").unlink()

        response = self.execute(
            "move_chunk_collision",
            "<tool>move</tool><args>"
            '<from root="output">source.txt</from>'
            '<to root="output">target.txt</to></args>',
        )

        self.assertEqual(response.code, "chunk_state_conflict")
        self.assertTrue((self.output / "source.txt").is_file())
        self.assertFalse((self.output / "target.txt").exists())

    def test_lifecycle_entry_limit_fails_before_deletion(self) -> None:
        tree = self.output / "large-tree"
        tree.mkdir()
        (tree / "one").write_text("1", encoding="utf-8")
        (tree / "two").write_text("2", encoding="utf-8")
        limited = AgentToolDispatcher(
            self.manager,
            mutation_limits=MutationToolLimits(
                max_content_bytes=8,
                max_file_bytes=8,
                max_copy_entries=1,
                max_copy_bytes=8,
            ),
        )
        action = self.action(
            "delete_limited",
            "<tool>delete-dir</tool><path>large-tree</path>"
            "<expect_confirm>true</expect_confirm>",
        )

        response = limited.confirmation_request(action, self.session)

        self.assertEqual(response.code, "lifecycle_too_large")
        self.assertTrue((tree / "one").is_file())
        self.assertTrue((tree / "two").is_file())

    def test_lifecycle_depth_limit_fails_before_move(self) -> None:
        tree = self.output / "deep-tree"
        (tree / "nested").mkdir(parents=True)
        (tree / "nested" / "file").write_text("x", encoding="utf-8")
        limited = AgentToolDispatcher(
            self.manager,
            mutation_limits=MutationToolLimits(max_lifecycle_depth=1),
        )
        action = self.action(
            "move_deep_tree",
            "<tool>move</tool><args>"
            '<from root="output">deep-tree</from>'
            '<to root="output">moved-deep-tree</to></args>',
        )

        response = limited.execute(action, self.session)

        self.assertEqual(response.code, "lifecycle_too_large")
        self.assertTrue(tree.is_dir())
        self.assertFalse((self.output / "moved-deep-tree").exists())


if __name__ == "__main__":
    unittest.main()
