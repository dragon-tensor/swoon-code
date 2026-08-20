from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from swoon.aeml import AEMLPromptBuilder
from swoon.aeml.tool_registry import TOOL_SPECS
from swoon.orchestration import (
    AgentOrchestrator,
    OrchestrationError,
    ReadOnlyOrchestrator,
    RunStopReason,
)
from swoon.session import SessionManager, SessionStatus
from swoon.transport import AEMLChatChannel
from swoon.tools import AgentToolDispatcher


class FakeTextTransport:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def send(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("No fake response remains")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ReadOnlyOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = SessionManager(self.root / "sessions")

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

    def test_executes_read_action_then_completes_and_never_publishes_thought(self) -> None:
        session = self.manager.create(session_id="sess_orchestrator")
        transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_orchestrator">'
                    "<plan>1. Inspect the output</plan>"
                    "<thought>private chain of thought</thought>"
                    "<say>Inspecting.</say>"
                    '<action id="list1"><tool>list-dir</tool>'
                    '<path root="output">.</path></action>'
                    "<next>await_result</next></aeml>"
                ),
                (
                    '<aeml turn="2" session="sess_orchestrator">'
                    "<complete>Inspection complete.</complete></aeml>"
                ),
            ]
        )
        published: list[str] = []
        orchestrator = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
            message_sink=published.append,
        )

        outcome = orchestrator.run(session, "Inspect the project")

        self.assertEqual(outcome.reason, RunStopReason.COMPLETED)
        self.assertEqual(outcome.summary, "Inspection complete.")
        self.assertEqual(outcome.updates, ("Inspecting.",))
        self.assertEqual(outcome.last_turn, 2)
        self.assertEqual(outcome.session.state.status, SessionStatus.COMPLETED)
        self.assertEqual(outcome.session.state.step, 2)
        self.assertEqual(outcome.session.state.plan, "1. Inspect the output")
        self.assertEqual(outcome.session.state.used_action_ids, ("list1",))
        self.assertEqual(outcome.session.state.result_history, ("list1",))
        self.assertEqual(published, ["Inspecting.", "Inspection complete."])
        self.assertNotIn("private chain of thought", "\n".join(published))
        self.assertIn('<result id="list1">', transport.prompts[1])

    def test_tool_error_is_returned_in_the_next_context(self) -> None:
        session = self.manager.create(session_id="sess_tool_error")
        transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_tool_error">'
                    '<action id="missing1"><tool>read-file</tool>'
                    '<path root="input">missing.txt</path></action>'
                    "<next>await_result</next></aeml>"
                ),
                (
                    '<aeml turn="2" session="sess_tool_error">'
                    "<complete>The requested file is absent.</complete></aeml>"
                ),
            ]
        )

        outcome = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
        ).run(session, "Read missing.txt")

        self.assertEqual(outcome.reason, RunStopReason.COMPLETED)
        self.assertEqual(outcome.session.state.used_action_ids, ("missing1",))
        self.assertEqual(outcome.session.state.result_history, ())
        self.assertIn('<error code="path_not_found" id="missing1">', transport.prompts[1])

    def test_parse_repair_reuses_the_same_turn_and_step(self) -> None:
        session = self.manager.create(session_id="sess_parse_retry")
        transport = FakeTextTransport(
            [
                "not XML",
                (
                    '<aeml turn="1" session="sess_parse_retry">'
                    "<complete>Recovered.</complete></aeml>"
                ),
            ]
        )

        outcome = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
        ).run(session, "Finish")

        self.assertEqual(outcome.reason, RunStopReason.COMPLETED)
        self.assertEqual(outcome.session.state.step, 1)
        self.assertEqual(len(transport.prompts), 2)
        self.assertIn('type="parse_error"', transport.prompts[1])
        self.assertIn('attempt="1"', transport.prompts[1])
        self.assertIn('remaining="2"', transport.prompts[1])

    def test_truncated_repair_is_distinct_from_generic_parse_feedback(self) -> None:
        session = self.manager.create(session_id="sess_truncated_retry")
        transport = FakeTextTransport(
            [
                '<aeml turn="1" session="sess_truncated_retry"><complete>cut off',
                (
                    '<aeml turn="1" session="sess_truncated_retry">'
                    "<complete>Recovered.</complete></aeml>"
                ),
            ]
        )

        outcome = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
        ).run(session, "Finish")

        self.assertEqual(outcome.reason, RunStopReason.COMPLETED)
        self.assertEqual(outcome.session.state.step, 1)
        self.assertIn(
            'type="likely_truncated_by_message_limit"',
            transport.prompts[1],
        )

    def test_validation_repair_returns_structured_error_on_the_same_turn(self) -> None:
        session = self.manager.create(session_id="sess_validation_retry")
        transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_validation_retry">'
                    '<action id="write1"><tool>create-file</tool>'
                    '<path>unsafe.txt</path><args><content>x</content></args></action>'
                    "<next>await_result</next></aeml>"
                ),
                (
                    '<aeml turn="1" session="sess_validation_retry">'
                    "<complete>Writes are unavailable.</complete></aeml>"
                ),
            ]
        )

        outcome = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
        ).run(session, "Create a file")

        self.assertEqual(outcome.reason, RunStopReason.COMPLETED)
        self.assertEqual(outcome.session.state.step, 1)
        self.assertIn('code="unknown_tool"', transport.prompts[1])
        self.assertIn('id="write1"', transport.prompts[1])

    def test_invalid_action_id_feedback_omits_the_unrenderable_id(self) -> None:
        session = self.manager.create(session_id="sess_invalid_action_id")
        transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_invalid_action_id">'
                    '<action id="bad id"><tool>list-dir</tool>'
                    '<path root="output">.</path></action>'
                    "<next>await_result</next></aeml>"
                ),
                (
                    '<aeml turn="1" session="sess_invalid_action_id">'
                    "<complete>Corrected.</complete></aeml>"
                ),
            ]
        )

        outcome = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
        ).run(session, "Inspect")

        self.assertEqual(outcome.reason, RunStopReason.COMPLETED)
        self.assertIn('<error code="invalid_action_id">', transport.prompts[1])
        self.assertNotIn('id="bad id"', transport.prompts[1])

    def test_retry_exhaustion_aborts_the_session(self) -> None:
        session = self.manager.create(session_id="sess_bad_protocol")
        transport = FakeTextTransport(["bad one", "bad two", "bad three"])

        outcome = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
        ).run(session, "Finish")

        self.assertEqual(outcome.reason, RunStopReason.PROTOCOL_ERROR)
        self.assertEqual(outcome.error.code, "malformed_output")
        self.assertEqual(outcome.session.state.status, SessionStatus.ABORTED)
        self.assertEqual(outcome.session.state.step, 1)
        self.assertEqual(len(transport.prompts), 3)
        self.assertIsNone(outcome.last_turn)

    def test_human_question_pauses_and_a_real_answer_resumes(self) -> None:
        session = self.manager.create(session_id="sess_human_pause")
        transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_human_pause">'
                    "<ask_user>Which color?</ask_user>"
                    "<next>await_user</next></aeml>"
                ),
                (
                    '<aeml turn="2" session="sess_human_pause">'
                    "<complete>Blue selected.</complete></aeml>"
                ),
            ]
        )
        orchestrator = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
        )

        paused = orchestrator.run(session, "Choose a color")
        resumed = orchestrator.run(paused.session, "Blue")

        self.assertEqual(paused.reason, RunStopReason.AWAITING_USER)
        self.assertEqual(paused.question, "Which color?")
        self.assertEqual(resumed.reason, RunStopReason.COMPLETED)
        self.assertEqual(resumed.session.state.step, 2)
        self.assertIn("<user_prompt>Blue</user_prompt>", transport.prompts[1])

    def test_step_limit_requires_explicit_extension_before_resuming(self) -> None:
        session = self.manager.create(max_steps=1, session_id="sess_step_pause")
        transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_step_pause">'
                    "<say>I need one more turn.</say><next>proceed</next></aeml>"
                ),
                (
                    '<aeml turn="2" session="sess_step_pause">'
                    "<complete>Finished after approval.</complete></aeml>"
                ),
            ]
        )
        orchestrator = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
        )

        paused = orchestrator.run(session, "Finish")
        still_paused = orchestrator.run(paused.session, "Continue")
        resumed = orchestrator.run(
            still_paused.session,
            "Continue",
            additional_steps=1,
        )

        self.assertEqual(paused.reason, RunStopReason.STEP_LIMIT)
        self.assertEqual(still_paused.reason, RunStopReason.STEP_LIMIT)
        self.assertEqual(len(transport.prompts), 2)
        self.assertEqual(resumed.reason, RunStopReason.COMPLETED)
        self.assertEqual(resumed.session.state.step, 2)
        self.assertEqual(resumed.session.state.max_steps, 2)

    def test_failed_action_id_remains_reserved_and_cannot_be_reused(self) -> None:
        session = self.manager.create(session_id="sess_failed_id")
        transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_failed_id">'
                    '<action id="read1"><tool>read-file</tool>'
                    '<path root="input">absent</path></action>'
                    "<next>await_result</next></aeml>"
                ),
                (
                    '<aeml turn="2" session="sess_failed_id">'
                    '<action id="read1"><tool>list-dir</tool>'
                    '<path root="output">.</path></action>'
                    "<next>await_result</next></aeml>"
                ),
                (
                    '<aeml turn="2" session="sess_failed_id">'
                    "<complete>Used a fresh response instead.</complete></aeml>"
                ),
            ]
        )

        outcome = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
        ).run(session, "Inspect")

        self.assertEqual(outcome.reason, RunStopReason.COMPLETED)
        self.assertEqual(outcome.session.state.step, 2)
        self.assertEqual(outcome.session.state.used_action_ids, ("read1",))
        self.assertEqual(outcome.session.state.result_history, ())
        self.assertIn('code="duplicate_action_id"', transport.prompts[2])

    def test_done_and_abort_directives_close_with_distinct_outcomes(self) -> None:
        cases = (
            ("done", RunStopReason.DONE, SessionStatus.COMPLETED),
            ("abort", RunStopReason.ABORTED, SessionStatus.ABORTED),
        )
        for index, (directive, reason, status) in enumerate(cases, start=1):
            with self.subTest(directive=directive):
                session_id = f"sess_directive_{index}"
                session = self.manager.create(session_id=session_id)
                response = (
                    f'<aeml turn="1" session="{session_id}">'
                    f"<next>{directive}</next></aeml>"
                )
                outcome = ReadOnlyOrchestrator(
                    self.manager,
                    AEMLChatChannel(FakeTextTransport([response])),
                ).run(session, "Stop")

                self.assertEqual(outcome.reason, reason)
                self.assertEqual(outcome.session.state.status, status)

    def test_transport_failure_is_not_retried_and_leaves_session_active(self) -> None:
        session = self.manager.create(session_id="sess_transport_failure")
        transport = FakeTextTransport([RuntimeError("browser disconnected")])
        orchestrator = ReadOnlyOrchestrator(
            self.manager,
            AEMLChatChannel(transport),
        )

        with self.assertRaises(OrchestrationError) as raised:
            orchestrator.run(session, "Finish")

        self.assertEqual(raised.exception.code, "transport_failed")
        persisted = self.manager.load(session.id)
        self.assertEqual(persisted.state.status, SessionStatus.ACTIVE)
        self.assertEqual(persisted.state.step, 1)
        self.assertEqual(len(transport.prompts), 1)

    def test_constructor_rejects_a_channel_that_advertises_write_tools(self) -> None:
        prompt_builder = AEMLPromptBuilder(
            {"create-file": TOOL_SPECS["create-file"]}
        )
        channel = AEMLChatChannel(
            FakeTextTransport([]),
            prompt_builder=prompt_builder,
        )

        with self.assertRaisesRegex(ValueError, "unsafe tools: create-file"):
            ReadOnlyOrchestrator(self.manager, channel)


class AgentOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = SessionManager(self.root / "sessions")

    def tearDown(self) -> None:
        ReadOnlyOrchestratorTests._make_writable(self.root)
        self.temporary.cleanup()

    def agent(
        self,
        transport: FakeTextTransport,
        *,
        keep_alive: bool = False,
    ) -> AgentOrchestrator:
        dispatcher = AgentToolDispatcher(self.manager)
        channel = AEMLChatChannel(
            transport,
            prompt_builder=AEMLPromptBuilder(dispatcher.tool_specs),
        )
        return AgentOrchestrator(
            self.manager,
            channel,
            dispatcher=dispatcher,
            keep_alive=keep_alive,
        )

    def test_agent_executes_output_create_then_completes(self) -> None:
        session = self.manager.create(session_id="sess_agent_create")
        transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_create">'
                    '<action id="create1"><tool>create-file</tool><path>app.py</path>'
                    "<args><content>print('hello')\n</content></args></action>"
                    "<next>await_result</next></aeml>"
                ),
                (
                    '<aeml turn="2" session="sess_agent_create">'
                    "<complete>Created app.py.</complete></aeml>"
                ),
            ]
        )

        outcome = self.agent(transport).run(session, "Create an app")

        self.assertEqual(outcome.reason, RunStopReason.COMPLETED)
        self.assertEqual(
            (session.paths.host_output / "app.py").read_text(encoding="utf-8"),
            "print('hello')\n",
        )
        self.assertIn('name="create-file"', transport.prompts[0])
        self.assertIn('<result id="create1">', transport.prompts[1])

    def test_interactive_agent_keeps_a_completed_session_open(self) -> None:
        session = self.manager.create(max_steps=3, session_id="sess_agent_console")
        transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_console">'
                    "<complete>Initial task complete.</complete></aeml>"
                ),
                (
                    '<aeml turn="2" session="sess_agent_console">'
                    "<complete>Follow-up complete.</complete></aeml>"
                ),
            ]
        )
        messages: list[str] = []
        orchestrator = self.agent(transport, keep_alive=True)
        orchestrator.message_sink = messages.append

        first = orchestrator.run(session, "Inspect the project")
        second = orchestrator.run(first.session, "Add type hints")

        self.assertEqual(first.reason, RunStopReason.AWAITING_USER)
        self.assertEqual(first.summary, "Initial task complete.")
        self.assertEqual(second.reason, RunStopReason.AWAITING_USER)
        self.assertEqual(second.summary, "Follow-up complete.")
        self.assertEqual(second.session.state.status, SessionStatus.WAITING_USER)
        self.assertEqual(second.session.state.step, 2)
        self.assertIn("<user_prompt>Add type hints</user_prompt>", transport.prompts[1])
        self.assertEqual(
            messages,
            [
                "Initial task complete.",
                "Task complete. Enter the next instruction or /quit.",
                "Follow-up complete.",
                "Task complete. Enter the next instruction or /quit.",
            ],
        )

    def test_overwrite_confirmation_survives_new_orchestrator_and_channel(self) -> None:
        session = self.manager.create(session_id="sess_agent_confirm")
        target = session.paths.host_output / "app.py"
        target.write_text("old\n", encoding="utf-8")
        first_transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_confirm">'
                    '<action id="overwrite1"><tool>overwrite-file</tool><path>app.py</path>'
                    "<args><content>new\n</content></args>"
                    "<expect_confirm>true</expect_confirm></action>"
                    "<next>await_result</next></aeml>"
                )
            ]
        )

        paused = self.agent(first_transport).run(session, "Replace app.py")

        self.assertEqual(paused.reason, RunStopReason.AWAITING_CONFIRMATION)
        self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
        persisted = self.manager.load(session.id)
        self.assertEqual(persisted.state.status, SessionStatus.WAITING_USER)
        self.assertEqual(persisted.state.pending_confirmation.action.id, "overwrite1")

        second_transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_confirm">'
                    "<complete>Replacement approved and complete.</complete></aeml>"
                )
            ]
        )
        completed = self.agent(second_transport).run(
            persisted,
            None,
            confirmation=True,
        )

        self.assertEqual(completed.reason, RunStopReason.COMPLETED)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
        self.assertIn('<result id="overwrite1">', second_transport.prompts[0])
        final = self.manager.load(session.id)
        self.assertIsNone(final.state.pending_confirmation)
        self.assertEqual(final.state.result_history, ("overwrite1",))

    def test_delete_confirmation_survives_new_orchestrator_and_channel(self) -> None:
        session = self.manager.create(session_id="sess_agent_delete_confirm")
        target = session.paths.host_output / "obsolete.txt"
        target.write_text("obsolete", encoding="utf-8")
        first_transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_delete_confirm">'
                    '<action id="delete1"><tool>delete-file</tool>'
                    '<path>obsolete.txt</path><expect_confirm>true</expect_confirm>'
                    "</action><next>await_result</next></aeml>"
                )
            ]
        )

        paused = self.agent(first_transport).run(session, "Delete obsolete.txt")

        self.assertEqual(paused.reason, RunStopReason.AWAITING_CONFIRMATION)
        self.assertTrue(target.is_file())
        persisted = self.manager.load(session.id)
        self.assertEqual(persisted.state.pending_confirmation.action.tool, "delete-file")

        second_transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_delete_confirm">'
                    "<complete>Approved deletion complete.</complete></aeml>"
                )
            ]
        )
        completed = self.agent(second_transport).run(
            persisted,
            None,
            confirmation=True,
        )

        self.assertEqual(completed.reason, RunStopReason.COMPLETED)
        self.assertFalse(target.exists())
        self.assertIn('<result id="delete1">', second_transport.prompts[0])
        final = self.manager.load(session.id)
        self.assertIsNone(final.state.pending_confirmation)
        self.assertEqual(final.state.result_history, ("delete1",))

    def test_denied_overwrite_is_persisted_as_failure_and_never_runs(self) -> None:
        session = self.manager.create(session_id="sess_agent_deny")
        target = session.paths.host_output / "app.py"
        target.write_text("old", encoding="utf-8")
        first = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_deny">'
                    '<action id="overwrite1"><tool>overwrite-file</tool><path>app.py</path>'
                    "<args><content>new</content></args>"
                    "<expect_confirm>true</expect_confirm></action>"
                    "<next>await_result</next></aeml>"
                )
            ]
        )
        paused = self.agent(first).run(session, "Replace app.py")
        second = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_deny">'
                    "<complete>Kept the original.</complete></aeml>"
                )
            ]
        )

        completed = self.agent(second).run(paused.session, None, confirmation=False)

        self.assertEqual(completed.reason, RunStopReason.COMPLETED)
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertIn("<status>failure</status>", second.prompts[0])
        record = self.manager.load(session.id).state.action("overwrite1")
        self.assertEqual(record.result.status.value, "failure")

    def test_approval_fails_closed_when_target_changed_during_pause(self) -> None:
        session = self.manager.create(session_id="sess_agent_stale_confirmation")
        target = session.paths.host_output / "app.py"
        target.write_text("old", encoding="utf-8")
        first = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_stale_confirmation">'
                    '<action id="overwrite1"><tool>overwrite-file</tool><path>app.py</path>'
                    "<args><content>requested</content></args>"
                    "<expect_confirm>true</expect_confirm></action>"
                    "<next>await_result</next></aeml>"
                )
            ]
        )
        paused = self.agent(first).run(session, "Replace app.py")
        target.write_text("changed while waiting", encoding="utf-8")
        second = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_stale_confirmation">'
                    "<complete>Detected the stale approval.</complete></aeml>"
                )
            ]
        )

        completed = self.agent(second).run(paused.session, None, confirmation=True)

        self.assertEqual(completed.reason, RunStopReason.COMPLETED)
        self.assertEqual(target.read_text(encoding="utf-8"), "changed while waiting")
        self.assertIn('code="confirmation_stale"', second.prompts[0])
        final = self.manager.load(session.id)
        self.assertIsNone(final.state.pending_confirmation)
        self.assertIsNone(final.state.action("overwrite1"))

    def test_undeclared_overwrite_returns_error_without_pausing_or_mutating(self) -> None:
        session = self.manager.create(session_id="sess_agent_no_declaration")
        target = session.paths.host_output / "app.py"
        target.write_text("old", encoding="utf-8")
        transport = FakeTextTransport(
            [
                (
                    '<aeml turn="1" session="sess_agent_no_declaration">'
                    '<action id="overwrite1"><tool>overwrite-file</tool><path>app.py</path>'
                    "<args><content>new</content></args></action>"
                    "<next>await_result</next></aeml>"
                ),
                (
                    '<aeml turn="2" session="sess_agent_no_declaration">'
                    "<complete>Did not overwrite.</complete></aeml>"
                ),
            ]
        )

        outcome = self.agent(transport).run(session, "Replace app.py")

        self.assertEqual(outcome.reason, RunStopReason.COMPLETED)
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertIn('code="confirmation_required"', transport.prompts[1])
        self.assertIsNone(self.manager.load(session.id).state.pending_confirmation)


if __name__ == "__main__":
    unittest.main()
