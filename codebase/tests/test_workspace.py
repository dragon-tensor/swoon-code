from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from swoon.cli import EXIT_SUCCESS, main
from swoon.session import (
    SessionError,
    SessionStatus,
    WorkspaceSessionManager,
    session_id_for_workspace,
)

from tests.test_cli import FakeBrowserTransport


class WorkspaceSessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"

    def tearDown(self) -> None:
        for directory, directories, files in os.walk(
            self.root,
            topdown=False,
            followlinks=False,
        ):
            path = Path(directory)
            for name in files:
                try:
                    (path / name).chmod(0o600)
                except OSError:
                    pass
            for name in directories:
                try:
                    (path / name).chmod(0o700)
                except OSError:
                    pass
            try:
                path.chmod(0o700)
            except OSError:
                pass
        self.temporary.cleanup()

    def test_named_session_adopts_matching_input_and_creates_matching_output(self) -> None:
        manager = WorkspaceSessionManager(self.work)
        supplied = self.work / "input" / "demo"
        supplied.mkdir()
        (supplied / "app.py").write_text("print('hello')\n", encoding="utf-8")

        session = manager.create(session_id=session_id_for_workspace("demo"))

        self.assertEqual(session.paths.host_input, self.work / "input" / "demo")
        self.assertEqual(session.paths.host_output, self.work / "output" / "demo")
        self.assertEqual(session.paths.host_root, self.work / ".sessions" / "sess_demo")
        self.assertTrue((session.paths.host_input / "app.py").is_file())
        self.assertTrue(session.paths.host_output.is_dir())
        self.assertEqual(manager.load_name("demo").state, session.state)
        if os.name != "nt":
            self.assertFalse(stat.S_IMODE(supplied.stat().st_mode) & 0o222)

    def test_named_session_rejects_unsafe_names_and_existing_output(self) -> None:
        manager = WorkspaceSessionManager(self.work)
        with self.assertRaisesRegex(SessionError, "Workspace name"):
            session_id_for_workspace("../escape")

        (self.work / "output" / "demo").mkdir()
        with self.assertRaisesRegex(SessionError, "Output folder already exists"):
            manager.create(session_id=session_id_for_workspace("demo"))

    def test_deleting_named_session_removes_matching_visible_folders(self) -> None:
        manager = WorkspaceSessionManager(self.work)
        session = manager.create(session_id=session_id_for_workspace("demo"))
        (session.paths.host_output / "result.py").write_text(
            "print('done')\n",
            encoding="utf-8",
        )
        manager.set_status(session, SessionStatus.ABORTED)

        manager.delete_session(session.id)

        self.assertFalse(session.paths.host_root.exists())
        self.assertFalse(session.paths.host_input.exists())
        self.assertFalse(session.paths.host_output.exists())

    def test_short_cli_name_starts_and_pauses_headless_agent_session(self) -> None:
        browser = FakeBrowserTransport(
            [
                (
                    '<aeml turn="1" session="sess_demo">'
                    "<complete>Created the requested script.</complete></aeml>"
                )
            ]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        environment = {
            "SWOON_WORK_ROOT": str(self.work),
            "SWOON_COOKIE_FILE": str(self.root / "cookies.json"),
        }
        with (
            patch.dict(os.environ, environment, clear=False),
            patch("swoon.cli.ChatGPTWebTransport", return_value=browser) as transport,
            patch("builtins.input", side_effect=["Write a Python script", "/quit"]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(["demo"])

        session = WorkspaceSessionManager(self.work).load_name("demo")
        self.assertEqual(code, EXIT_SUCCESS)
        self.assertEqual(session.state.status, SessionStatus.WAITING_USER)
        self.assertEqual(session.paths.host_output, self.work / "output" / "demo")
        self.assertIn("Swoon Code interactive agent", stdout.getvalue())
        self.assertIn("Created the requested script.", stdout.getvalue())
        self.assertIn("paused", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(transport.call_args.kwargs["headless"])
        self.assertTrue(browser.closed)


if __name__ == "__main__":
    unittest.main()
