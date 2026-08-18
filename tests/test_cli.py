from __future__ import annotations

import io
import platform
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from swoon.cli import (
    EXIT_ABORTED,
    EXIT_INPUT_REQUIRED,
    EXIT_PROTOCOL_ERROR,
    EXIT_RUNTIME_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE,
    legacy_main,
    main,
)
from swoon.session import (
    ProcessStatus,
    ProcessTerminationReason,
    SessionManager,
    SessionStatus,
)


class FakeBrowserTransport:
    def __init__(
        self,
        responses: list[str | Exception],
        *,
        start_error: Exception | None = None,
    ) -> None:
        self.responses = list(responses)
        self.start_error = start_error
        self.prompts: list[str] = []
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True
        if self.start_error is not None:
            raise self.start_error

    def send(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("No fake browser response remains")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sessions = self.root / "sessions"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(
        self,
        argv: list[str],
        browser: FakeBrowserTransport,
        *,
        inputs: list[str] | None = None,
        legacy: bool = False,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        entrypoint = legacy_main if legacy else main
        with (
            patch("swoon.cli.ChatGPTWebTransport", return_value=browser),
            patch("builtins.input", side_effect=inputs or []),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = entrypoint(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def agent_args(self, *extra: str) -> list[str]:
        return [
            "agent",
            "--cookies",
            str(self.root / "cookies.json"),
            "--session-dir",
            str(self.sessions),
            *extra,
        ]

    @staticmethod
    def background_runtime_available() -> bool:
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

    def test_legacy_flags_and_compatibility_entrypoint_still_run_direct_chat(self) -> None:
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                browser = FakeBrowserTransport(["relay response"])
                argv = [
                    "--cookies",
                    str(self.root / "cookies.json"),
                    "--prompt",
                    "hello",
                ]

                code, stdout, stderr = self.invoke(argv, browser, legacy=legacy)

                self.assertEqual(code, EXIT_SUCCESS)
                self.assertEqual(browser.prompts, ["hello"])
                self.assertTrue(browser.started)
                self.assertTrue(browser.closed)
                self.assertIn("relay response", stdout)
                self.assertEqual(stderr, "")

    def test_version_flag_reports_packaged_version(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            main(["--version"])

        self.assertEqual(raised.exception.code, EXIT_SUCCESS)
        self.assertEqual(stdout.getvalue().strip(), "swoon 0.1.0")

    def test_doctor_reports_consumer_and_optional_sandbox_readiness(self) -> None:
        browser = FakeBrowserTransport([])
        with (
            patch(
                "swoon.cli._browser_runtime_status",
                return_value=(True, "Chromium test launched successfully"),
            ),
            patch(
                "swoon.cli._command_sandbox_status",
                return_value=(False, "missing bwrap"),
            ),
        ):
            code, stdout, stderr = self.invoke(["doctor"], browser)

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn("Swoon Code 0.1.0", stdout)
        self.assertIn("[ok] Browser runtime", stdout)
        self.assertIn("[skip] Cookie file", stdout)
        self.assertIn("[optional] Command sandbox: missing bwrap", stdout)
        self.assertIn("Consumer CLI is ready.", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_fails_for_an_invalid_supplied_cookie_file(self) -> None:
        browser = FakeBrowserTransport([])
        with (
            patch(
                "swoon.cli._browser_runtime_status",
                return_value=(True, "Chromium test launched successfully"),
            ),
            patch(
                "swoon.cli._cookie_status",
                return_value=(False, "invalid or unreadable (ValueError)"),
            ),
            patch(
                "swoon.cli._command_sandbox_status",
                return_value=(True, "ready on x86_64"),
            ),
        ):
            code, stdout, stderr = self.invoke(
                ["doctor", "--cookies", str(self.root / "cookies.json")],
                browser,
            )

        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertIn("[fail] Cookie file", stdout)
        self.assertIn("Consumer check failed", stderr)

    def test_session_commands_list_show_export_and_delete_completed_output(self) -> None:
        manager = SessionManager(self.sessions)
        session = manager.create(session_id="sess_cli_management")
        (session.paths.host_output / "result.txt").write_text("ready\n", encoding="utf-8")
        manager.set_status(session, SessionStatus.COMPLETED)
        browser = FakeBrowserTransport([])

        code, stdout, stderr = self.invoke(
            ["session", "list", "--session-dir", str(self.sessions)],
            browser,
        )
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn("sess_cli_management\tcompleted", stdout)
        self.assertEqual(stderr, "")

        code, stdout, stderr = self.invoke(
            [
                "session",
                "show",
                session.id,
                "--session-dir",
                str(self.sessions),
            ],
            browser,
        )
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn(f"Output: {session.paths.host_output}", stdout)
        self.assertIn("Status: completed", stdout)
        self.assertEqual(stderr, "")

        destination = self.root / "consumer-export"
        code, stdout, stderr = self.invoke(
            [
                "session",
                "export",
                session.id,
                str(destination),
                "--session-dir",
                str(self.sessions),
            ],
            browser,
        )
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual((destination / "result.txt").read_text(encoding="utf-8"), "ready\n")
        self.assertIn("Exported sess_cli_management", stdout)
        self.assertEqual(stderr, "")

        code, stdout, stderr = self.invoke(
            [
                "session",
                "delete",
                session.id,
                "--session-dir",
                str(self.sessions),
                "--yes",
            ],
            browser,
        )
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn("Deleted session sess_cli_management", stdout)
        self.assertEqual(stderr, "")
        self.assertFalse(session.paths.host_root.exists())

    def test_session_delete_is_guarded_and_active_state_requires_force(self) -> None:
        session = SessionManager(self.sessions).create(session_id="sess_cli_active_delete")
        browser = FakeBrowserTransport([])

        code, stdout, stderr = self.invoke(
            [
                "session",
                "delete",
                session.id,
                "--session-dir",
                str(self.sessions),
            ],
            browser,
            inputs=["no"],
        )
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn("cancelled", stdout)
        self.assertEqual(stderr, "")
        self.assertTrue(session.paths.host_root.exists())

        code, _, stderr = self.invoke(
            [
                "session",
                "delete",
                session.id,
                "--session-dir",
                str(self.sessions),
                "--yes",
            ],
            browser,
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertIn("session_not_terminal", stderr)

        code, stdout, stderr = self.invoke(
            [
                "session",
                "delete",
                session.id,
                "--session-dir",
                str(self.sessions),
                "--yes",
                "--force-active",
            ],
            browser,
        )
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertIn("Deleted session", stdout)
        self.assertEqual(stderr, "")

    def test_agent_creates_a_session_and_completes(self) -> None:
        session_id = "sess_cli_complete"
        browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<complete>Inspection complete.</complete></aeml>"
                )
            ]
        )

        code, stdout, stderr = self.invoke(
            self.agent_args(
                "--session-id",
                session_id,
                "--prompt",
                "Inspect the project",
            ),
            browser,
        )

        session = SessionManager(self.sessions).load(session_id)
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(session.state.status, SessionStatus.COMPLETED)
        self.assertEqual(session.state.step, 1)
        self.assertIn(f"Session: {session_id}", stdout)
        self.assertIn(f"Output: {session.paths.host_output}", stdout)
        self.assertIn("Inspection complete.", stdout)
        self.assertEqual(stderr, "")
        self.assertTrue(browser.closed)

    def test_agent_answers_a_human_question_in_the_same_browser_session(self) -> None:
        session_id = "sess_cli_question"
        browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<ask_user>Which color?</ask_user>"
                    "<next>await_user</next></aeml>"
                ),
                (
                    f'<aeml turn="2" session="{session_id}">'
                    "<complete>Blue selected.</complete></aeml>"
                ),
            ]
        )

        code, stdout, stderr = self.invoke(
            self.agent_args(
                "--session-id",
                session_id,
                "--prompt",
                "Choose a color",
            ),
            browser,
            inputs=["Blue"],
        )

        session = SessionManager(self.sessions).load(session_id)
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(session.state.status, SessionStatus.COMPLETED)
        self.assertEqual(session.state.step, 2)
        self.assertIn("Which color?", stdout)
        self.assertIn("Blue selected.", stdout)
        self.assertIn("<user_prompt>Blue</user_prompt>", browser.prompts[1])
        self.assertEqual(stderr, "")

    def test_noninteractive_exit_stops_live_background_process(self) -> None:
        if not self.background_runtime_available():
            self.skipTest("Bubblewrap background-command runtime is unavailable")
        session_id = "sess_cli_background"
        browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    '<action id="launch1"><tool>run-command-background</tool>'
                    '<args><cmd><![CDATA[python3 -u -c "import time; '
                    'print(\'live\'); time.sleep(60)"]]></cmd></args></action>'
                    '<next>await_result</next></aeml>'
                ),
                (
                    f'<aeml turn="2" session="{session_id}">'
                    '<ask_user>Continue?</ask_user><next>await_user</next></aeml>'
                ),
            ]
        )

        code, _, stderr = self.invoke(
            self.agent_args(
                "--session-id",
                session_id,
                "--prompt",
                "Start a background check",
                "--non-interactive",
            ),
            browser,
        )

        session = SessionManager(self.sessions).load(session_id)
        process = session.state.processes[0]
        self.assertEqual(code, EXIT_INPUT_REQUIRED)
        self.assertEqual(session.state.status, SessionStatus.WAITING_USER)
        self.assertEqual(process.status, ProcessStatus.KILLED)
        self.assertEqual(
            process.termination_reason,
            ProcessTerminationReason.HOST_EXIT,
        )
        self.assertIn("human answer", stderr)
        self.assertTrue(browser.closed)

    def test_agent_obtains_explicit_step_approval(self) -> None:
        session_id = "sess_cli_steps"
        browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<say>One more turn is needed.</say><next>proceed</next></aeml>"
                ),
                (
                    f'<aeml turn="2" session="{session_id}">'
                    "<complete>Finished with approval.</complete></aeml>"
                ),
            ]
        )

        code, stdout, stderr = self.invoke(
            self.agent_args(
                "--session-id",
                session_id,
                "--max-steps",
                "1",
                "--prompt",
                "Inspect",
            ),
            browser,
            inputs=["2"],
        )

        session = SessionManager(self.sessions).load(session_id)
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(session.state.status, SessionStatus.COMPLETED)
        self.assertEqual(session.state.max_steps, 3)
        self.assertEqual(session.state.step, 2)
        self.assertIn("reached its 1-step limit", stdout)
        self.assertIn("human approved 2 additional steps", browser.prompts[1])
        self.assertEqual(stderr, "")

    def test_answer_on_the_final_step_survives_the_required_extension(self) -> None:
        session_id = "sess_cli_final_step_answer"
        browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<ask_user>Which color?</ask_user>"
                    "<next>await_user</next></aeml>"
                ),
                (
                    f'<aeml turn="2" session="{session_id}">'
                    "<complete>Blue received.</complete></aeml>"
                ),
            ]
        )

        code, _, stderr = self.invoke(
            self.agent_args(
                "--session-id",
                session_id,
                "--max-steps",
                "1",
                "--prompt",
                "Choose",
            ),
            browser,
            inputs=["Blue", "1"],
        )

        session = SessionManager(self.sessions).load(session_id)
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(session.state.max_steps, 2)
        self.assertEqual(session.state.step, 2)
        self.assertIn("<user_prompt>Blue</user_prompt>", browser.prompts[1])
        self.assertNotIn("human approved", browser.prompts[1])
        self.assertEqual(stderr, "")

    def test_non_interactive_question_is_resumable_in_a_new_process(self) -> None:
        session_id = "sess_cli_resume"
        first_browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<ask_user>Which color?</ask_user>"
                    "<next>await_user</next></aeml>"
                )
            ]
        )
        first_code, _, first_stderr = self.invoke(
            self.agent_args(
                "--session-id",
                session_id,
                "--prompt",
                "Choose",
                "--non-interactive",
            ),
            first_browser,
        )

        waiting = SessionManager(self.sessions).load(session_id)
        self.assertEqual(first_code, EXIT_INPUT_REQUIRED)
        self.assertEqual(waiting.state.status, SessionStatus.WAITING_USER)
        self.assertIn("waiting for a human answer", first_stderr)

        second_browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<complete>Blue selected after resume.</complete></aeml>"
                )
            ]
        )
        second_code, second_stdout, second_stderr = self.invoke(
            self.agent_args(
                "--resume",
                session_id,
                "--prompt",
                "Blue",
                "--non-interactive",
            ),
            second_browser,
        )

        completed = SessionManager(self.sessions).load(session_id)
        self.assertEqual(second_code, EXIT_SUCCESS)
        self.assertEqual(completed.state.status, SessionStatus.COMPLETED)
        self.assertEqual(completed.state.step, 2)
        self.assertIn("Blue selected after resume.", second_stdout)
        self.assertIn("<user_prompt>Blue</user_prompt>", second_browser.prompts[0])
        self.assertEqual(second_stderr, "")

    def test_non_interactive_step_pause_accepts_explicit_resume_approval(self) -> None:
        session_id = "sess_cli_noninteractive_steps"
        first_browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<next>proceed</next></aeml>"
                )
            ]
        )
        first_code, _, first_stderr = self.invoke(
            self.agent_args(
                "--session-id",
                session_id,
                "--max-steps",
                "1",
                "--prompt",
                "Inspect",
                "--non-interactive",
            ),
            first_browser,
        )

        waiting = SessionManager(self.sessions).load(session_id)
        self.assertEqual(first_code, EXIT_INPUT_REQUIRED)
        self.assertEqual(waiting.state.status, SessionStatus.WAITING_USER)
        self.assertIn("step-budget approval", first_stderr)

        second_browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<complete>Finished after explicit approval.</complete></aeml>"
                )
            ]
        )
        second_code, second_stdout, second_stderr = self.invoke(
            self.agent_args(
                "--resume",
                session_id,
                "--additional-steps",
                "1",
                "--prompt",
                "Continue",
                "--non-interactive",
            ),
            second_browser,
        )

        completed = SessionManager(self.sessions).load(session_id)
        self.assertEqual(second_code, EXIT_SUCCESS)
        self.assertEqual(completed.state.status, SessionStatus.COMPLETED)
        self.assertEqual(completed.state.max_steps, 2)
        self.assertEqual(completed.state.step, 2)
        self.assertIn("Finished after explicit approval.", second_stdout)
        self.assertIn("<user_prompt>Continue</user_prompt>", second_browser.prompts[0])
        self.assertEqual(second_stderr, "")

    def test_non_interactive_overwrite_confirmation_resumes_with_exact_approval(self) -> None:
        session_id = "sess_cli_confirmation"
        manager = SessionManager(self.sessions)
        session = manager.create(session_id=session_id)
        target = session.paths.host_output / "app.py"
        target.write_text("old", encoding="utf-8")
        first_browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    '<action id="overwrite1"><tool>overwrite-file</tool><path>app.py</path>'
                    "<args><content>new</content></args>"
                    "<expect_confirm>true</expect_confirm></action>"
                    "<next>await_result</next></aeml>"
                )
            ]
        )

        first_code, _, first_stderr = self.invoke(
            self.agent_args(
                "--resume",
                session_id,
                "--prompt",
                "Replace app.py",
                "--non-interactive",
            ),
            first_browser,
        )

        waiting = manager.load(session_id)
        self.assertEqual(first_code, EXIT_INPUT_REQUIRED)
        self.assertIsNotNone(waiting.state.pending_confirmation)
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertIn("approval or denial", first_stderr)

        second_browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<complete>Approved replacement complete.</complete></aeml>"
                )
            ]
        )
        second_code, second_stdout, second_stderr = self.invoke(
            self.agent_args(
                "--resume",
                session_id,
                "--approve-pending",
                "--non-interactive",
            ),
            second_browser,
        )

        completed = manager.load(session_id)
        self.assertEqual(second_code, EXIT_SUCCESS)
        self.assertEqual(completed.state.status, SessionStatus.COMPLETED)
        self.assertIsNone(completed.state.pending_confirmation)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertIn("Approved replacement complete.", second_stdout)
        self.assertIn('<result id="overwrite1">', second_browser.prompts[0])
        self.assertEqual(second_stderr, "")

    def test_non_interactive_delete_confirmation_resumes_exact_action(self) -> None:
        session_id = "sess_cli_delete_confirmation"
        manager = SessionManager(self.sessions)
        session = manager.create(session_id=session_id)
        target = session.paths.host_output / "obsolete.txt"
        target.write_text("obsolete", encoding="utf-8")
        first_browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    '<action id="delete1"><tool>delete-file</tool>'
                    '<path>obsolete.txt</path><expect_confirm>true</expect_confirm>'
                    "</action><next>await_result</next></aeml>"
                )
            ]
        )

        first_code, _, first_stderr = self.invoke(
            self.agent_args(
                "--resume",
                session_id,
                "--prompt",
                "Delete obsolete.txt",
                "--non-interactive",
            ),
            first_browser,
        )

        waiting = manager.load(session_id)
        self.assertEqual(first_code, EXIT_INPUT_REQUIRED)
        self.assertEqual(waiting.state.pending_confirmation.action.tool, "delete-file")
        self.assertTrue(target.is_file())
        self.assertIn("approval or denial", first_stderr)

        second_browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<complete>Approved deletion complete.</complete></aeml>"
                )
            ]
        )
        second_code, second_stdout, second_stderr = self.invoke(
            self.agent_args(
                "--resume",
                session_id,
                "--approve-pending",
                "--non-interactive",
            ),
            second_browser,
        )

        completed = manager.load(session_id)
        self.assertEqual(second_code, EXIT_SUCCESS)
        self.assertIsNone(completed.state.pending_confirmation)
        self.assertFalse(target.exists())
        self.assertIn("Approved deletion complete.", second_stdout)
        self.assertIn('<result id="delete1">', second_browser.prompts[0])
        self.assertEqual(second_stderr, "")

    def test_interactive_denial_keeps_original_file(self) -> None:
        session_id = "sess_cli_denial"
        manager = SessionManager(self.sessions)
        session = manager.create(session_id=session_id)
        target = session.paths.host_output / "app.py"
        target.write_text("old", encoding="utf-8")
        browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    '<action id="overwrite1"><tool>overwrite-file</tool><path>app.py</path>'
                    "<args><content>new</content></args>"
                    "<expect_confirm>true</expect_confirm></action>"
                    "<next>await_result</next></aeml>"
                ),
                (
                    f'<aeml turn="2" session="{session_id}">'
                    "<complete>Kept the original file.</complete></aeml>"
                ),
            ]
        )

        code, stdout, stderr = self.invoke(
            self.agent_args("--resume", session_id, "--prompt", "Replace app.py"),
            browser,
            inputs=["n"],
        )

        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertIn("Pending overwrite-file", stdout)
        self.assertIn("Kept the original file.", stdout)
        self.assertIn("<status>failure</status>", browser.prompts[1])
        self.assertEqual(stderr, "")

    def test_pending_decision_flag_rejects_session_without_pending_action(self) -> None:
        session_id = "sess_cli_no_pending"
        SessionManager(self.sessions).create(session_id=session_id)
        browser = FakeBrowserTransport([])

        code, _, stderr = self.invoke(
            self.agent_args(
                "--resume",
                session_id,
                "--approve-pending",
                "--non-interactive",
            ),
            browser,
        )

        self.assertEqual(code, EXIT_USAGE)
        self.assertFalse(browser.started)
        self.assertIn("no pending action", stderr)

    def test_resume_at_step_limit_preserves_the_supplied_human_answer(self) -> None:
        session_id = "sess_cli_limited_resume"
        manager = SessionManager(self.sessions)
        session = manager.create(max_steps=1, session_id=session_id)
        manager.advance_step(session)
        manager.set_status(session, SessionStatus.WAITING_USER)
        browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<complete>Answer received.</complete></aeml>"
                )
            ]
        )

        code, _, stderr = self.invoke(
            self.agent_args(
                "--resume",
                session_id,
                "--prompt",
                "Blue",
            ),
            browser,
            inputs=["1"],
        )

        completed = manager.load(session_id)
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(completed.state.max_steps, 2)
        self.assertEqual(completed.state.step, 2)
        self.assertIn("<user_prompt>Blue</user_prompt>", browser.prompts[0])
        self.assertNotIn("human approved", browser.prompts[0])
        self.assertEqual(stderr, "")

    def test_user_can_abort_while_the_agent_is_waiting(self) -> None:
        session_id = "sess_cli_abort"
        browser = FakeBrowserTransport(
            [
                (
                    f'<aeml turn="1" session="{session_id}">'
                    "<ask_user>Continue?</ask_user>"
                    "<next>await_user</next></aeml>"
                )
            ]
        )

        code, _, stderr = self.invoke(
            self.agent_args(
                "--session-id",
                session_id,
                "--prompt",
                "Inspect",
            ),
            browser,
            inputs=["/abort"],
        )

        session = SessionManager(self.sessions).load(session_id)
        self.assertEqual(code, EXIT_ABORTED)
        self.assertEqual(session.state.status, SessionStatus.ABORTED)
        self.assertIn("aborted by the user", stderr)

    def test_protocol_exhaustion_has_a_distinct_exit_code(self) -> None:
        session_id = "sess_cli_protocol"
        browser = FakeBrowserTransport(["bad one", "bad two", "bad three"])

        code, _, stderr = self.invoke(
            self.agent_args(
                "--session-id",
                session_id,
                "--prompt",
                "Inspect",
                "--non-interactive",
            ),
            browser,
        )

        session = SessionManager(self.sessions).load(session_id)
        self.assertEqual(code, EXIT_PROTOCOL_ERROR)
        self.assertEqual(session.state.status, SessionStatus.ABORTED)
        self.assertIn("malformed_output", stderr)
        self.assertEqual(len(browser.prompts), 3)

    def test_browser_start_failure_is_reported_and_closed(self) -> None:
        session_id = "sess_cli_browser_failure"
        browser = FakeBrowserTransport([], start_error=RuntimeError("browser unavailable"))

        code, _, stderr = self.invoke(
            self.agent_args(
                "--session-id",
                session_id,
                "--prompt",
                "Inspect",
            ),
            browser,
        )

        session = SessionManager(self.sessions).load(session_id)
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertEqual(session.state.status, SessionStatus.ACTIVE)
        self.assertEqual(session.state.step, 0)
        self.assertTrue(browser.closed)
        self.assertIn("browser unavailable", stderr)

    def test_resume_rejects_new_session_only_options_before_browser_start(self) -> None:
        session_id = "sess_cli_usage"
        SessionManager(self.sessions).create(session_id=session_id)
        browser = FakeBrowserTransport([])

        code, _, stderr = self.invoke(
            self.agent_args(
                "--resume",
                session_id,
                "--max-steps",
                "5",
                "--prompt",
                "Inspect",
            ),
            browser,
        )

        self.assertEqual(code, EXIT_USAGE)
        self.assertFalse(browser.started)
        self.assertIn("applies only to new sessions", stderr)


if __name__ == "__main__":
    unittest.main()
