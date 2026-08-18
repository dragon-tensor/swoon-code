from __future__ import annotations

import unittest

from swoon.aeml import AEMLParser, AEMLValidationError, AEMLValidator, PathRef, Root
from swoon.aeml.models import ToolEffect
from swoon.aeml.tool_registry import TOOL_SPECS


class AEMLValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AEMLParser()
        self.validator = AEMLValidator()

    def validate(self, inner: str, *, turn: int = 1, session: str = "sess_test"):
        source = f'<aeml turn="{turn}" session="{session}">{inner}</aeml>'
        return self.validator.validate(
            self.parser.parse(source),
            expected_turn=turn,
            expected_session=session,
        )

    def assert_code(self, code: str, inner: str) -> AEMLValidationError:
        with self.assertRaises(AEMLValidationError) as raised:
            self.validate(inner)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_batches_multiple_read_only_actions(self) -> None:
        validated = self.validate(
            '<action id="a1"><tool>list-dir</tool><path root="input">.</path></action>'
            '<action id="a2"><tool>read-file</tool><path root="input">app.py</path>'
            '<args><start_line>1</start_line><end_line>10</end_line></args></action>'
            '<next>await_result</next>'
        )

        self.assertEqual(len(validated.actions), 2)
        self.assertTrue(
            all(action.spec.effect is ToolEffect.READ_ONLY for action in validated.actions)
        )
        self.assertEqual(validated.actions[1].argument("start_line"), 1)

    def test_multiple_write_actions_are_rejected(self) -> None:
        self.assert_code(
            "batch_write_not_allowed",
            '<action id="a1"><tool>create-file</tool><path>a</path>'
            '<args><content>one</content></args></action>'
            '<action id="a2"><tool>create-file</tool><path>b</path>'
            '<args><content>two</content></args></action>'
            '<next>await_result</next>',
        )

    def test_write_to_input_is_rejected_before_execution(self) -> None:
        self.assert_code(
            "input_readonly",
            '<action id="a1"><tool>create-file</tool><path root="input">x</path>'
            '<args><content>unsafe</content></args></action><next>await_result</next>',
        )

    def test_copy_destination_cannot_be_input(self) -> None:
        self.assert_code(
            "input_readonly",
            '<action id="a1"><tool>copy-dir</tool><args>'
            '<from root="input">.</from><to root="input">.</to>'
            '</args></action><next>await_result</next>',
        )

    def test_copy_paths_become_typed_path_refs(self) -> None:
        validated = self.validate(
            '<action id="a1"><tool>copy-dir</tool><args>'
            '<from root="input">.</from><to root="output">project</to>'
            '</args></action><next>await_result</next>'
        )
        source = validated.actions[0].argument("from")
        destination = validated.actions[0].argument("to")
        self.assertEqual(source, PathRef(".", Root.INPUT))
        self.assertEqual(destination, PathRef("project", Root.OUTPUT))

    def test_unknown_tool_is_rejected(self) -> None:
        self.assert_code(
            "unknown_tool",
            '<action id="a1"><tool>do-whatever</tool></action><next>await_result</next>',
        )

    def test_missing_required_argument_is_rejected(self) -> None:
        self.assert_code(
            "missing_argument",
            '<action id="a1"><tool>create-file</tool><path>x</path></action>'
            '<next>await_result</next>',
        )

    def test_read_line_range_is_ordered(self) -> None:
        self.assert_code(
            "invalid_argument",
            '<action id="a1"><tool>read-file</tool><path>x</path><args>'
            '<start_line>20</start_line><end_line>10</end_line>'
            '</args></action><next>await_result</next>',
        )

    def test_chunk_initial_write_must_start_at_one(self) -> None:
        self.assert_code(
            "chunk_sequence_error",
            '<action id="a1"><tool>create-file</tool><path>x</path>'
            '<args><content>part</content></args><chunk seq="2" final="false"/>'
            '</action><next>await_result</next>',
        )

    def test_append_chunk_can_continue_at_two(self) -> None:
        validated = self.validate(
            '<action id="a1"><tool>append-file</tool><path>x</path>'
            '<args><content>part</content></args><chunk seq="2" final="true"/>'
            '</action><next>await_result</next>'
        )
        self.assertEqual(validated.actions[0].source.chunk.seq, 2)

    def test_edit_file_does_not_support_chunking(self) -> None:
        self.assert_code(
            "chunk_not_supported",
            '<action id="a1"><tool>edit-file</tool><path>x</path><args>'
            '<old_str>a</old_str><new_str>b</new_str></args>'
            '<chunk seq="1" final="true"/></action><next>await_result</next>',
        )

    def test_delete_requires_declared_confirmation(self) -> None:
        self.assert_code(
            "confirmation_required",
            '<action id="a1"><tool>delete-file</tool><path>x</path></action>'
            '<next>await_result</next>',
        )

        validated = self.validate(
            '<action id="a1"><tool>delete-file</tool><path>x</path>'
            '<expect_confirm>true</expect_confirm></action><next>await_result</next>'
        )
        self.assertTrue(validated.actions[0].source.expect_confirm)

    def test_chmod_accepts_only_owner_private_file_modes(self) -> None:
        for mode in ("600", "0600", "700", "0700"):
            with self.subTest(mode=mode):
                validated = self.validate(
                    '<action id="a1"><tool>chmod</tool><path>x</path>'
                    f"<args><mode>{mode}</mode></args></action>"
                    "<next>await_result</next>"
                )
                self.assertEqual(validated.actions[0].argument("mode"), mode)
        for mode in ("644", "777", "400", "00600"):
            with self.subTest(mode=mode):
                self.assert_code(
                    "invalid_argument",
                    '<action id="a1"><tool>chmod</tool><path>x</path>'
                    f"<args><mode>{mode}</mode></args></action>"
                    "<next>await_result</next>",
                )

    def test_non_complete_turn_requires_next(self) -> None:
        self.assert_code("missing_next", '<say>Working</say>')

    def test_complete_turn_has_no_next_or_action(self) -> None:
        validated = self.validate('<complete>Finished safely.</complete>')
        self.assertEqual(validated.source.complete, "Finished safely.")

        self.assert_code(
            "invalid_complete",
            '<complete>Finished.</complete><next>done</next>',
        )

    def test_ask_user_requires_await_user(self) -> None:
        self.assert_code(
            "invalid_next",
            '<ask_user>Which framework?</ask_user><next>proceed</next>',
        )
        validated = self.validate(
            '<ask_user>Which framework?</ask_user><next>await_user</next>'
        )
        self.assertEqual(validated.source.ask_user, "Which framework?")

    def test_expected_turn_and_session_are_enforced(self) -> None:
        message = self.parser.parse(
            '<aeml turn="2" session="sess_actual"><say>x</say><next>proceed</next></aeml>'
        )
        with self.assertRaises(AEMLValidationError) as turn_error:
            self.validator.validate(message, expected_turn=1)
        self.assertEqual(turn_error.exception.code, "turn_mismatch")

        with self.assertRaises(AEMLValidationError) as session_error:
            self.validator.validate(message, expected_session="sess_other")
        self.assertEqual(session_error.exception.code, "session_mismatch")

    def test_action_ids_are_unique_across_the_session(self) -> None:
        message = self.parser.parse(
            '<aeml turn="1" session="sess_test"><action id="used">'
            '<tool>git-status</tool></action><next>await_result</next></aeml>'
        )
        with self.assertRaises(AEMLValidationError) as raised:
            self.validator.validate(message, known_action_ids={"used"})
        self.assertEqual(raised.exception.code, "duplicate_action_id")

    def test_registry_covers_the_protocol_capability_tree(self) -> None:
        expected = {
            "run-command", "run-command-background", "kill-process", "stream-output",
            "get-env", "set-env", "read-file", "list-dir", "grep", "create-file",
            "overwrite-file", "append-file", "edit-file", "copy-file", "copy-dir",
            "delete-file", "delete-dir", "move", "rename", "chmod",
            "install-dependency", "remove-dependency", "list-dependencies", "git-init",
            "git-status", "git-diff", "git-log", "git-add", "git-commit", "git-branch",
            "git-checkout", "git-push", "git-pull", "git-merge", "git-rebase",
            "run-build", "run-tests", "run-linter",
        }
        self.assertEqual(set(TOOL_SPECS), expected)


if __name__ == "__main__":
    unittest.main()
