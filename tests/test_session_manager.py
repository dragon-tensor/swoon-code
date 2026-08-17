from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from swoon.aeml.models import Action, Argument, PathRef, Result, ResultStatus, Root
from swoon.session import (
    ImportLimits,
    ProcessStatus,
    SessionConflictError,
    SessionError,
    SessionImportError,
    SessionManager,
    SessionStatus,
    StepLimitReachedError,
)


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sessions = self.root / "sessions"
        self.manager = SessionManager(self.sessions)

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

    def test_create_exposes_virtual_roots_and_private_layout(self) -> None:
        session = self.manager.create(session_id="sess_alpha")

        self.assertEqual(session.paths.input_root, "/input/sess_alpha")
        self.assertEqual(session.paths.output_root, "/output/sess_alpha")
        self.assertEqual(session.state.step, 0)
        self.assertEqual(session.state.max_steps, 40)
        self.assertTrue(session.paths.host_input.is_dir())
        self.assertTrue(session.paths.host_output.is_dir())
        self.assertTrue(session.paths.state_file.is_file())

        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(session.paths.host_root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(session.paths.host_input.stat().st_mode), 0o500)
            self.assertEqual(stat.S_IMODE(session.paths.host_output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(session.paths.state_file.stat().st_mode), 0o600)

        loaded = self.manager.load("sess_alpha")
        self.assertEqual(loaded.state, session.state)

    def test_project_import_copies_regular_files_and_seals_input(self) -> None:
        source = self.root / "project"
        nested = source / "bin"
        nested.mkdir(parents=True)
        (source / "app.py").write_text("print('hello')\n", encoding="utf-8")
        executable = nested / "run.sh"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

        session = self.manager.create(source, session_id="sess_import")

        self.assertEqual(
            (session.paths.host_input / "app.py").read_text(encoding="utf-8"),
            "print('hello')\n",
        )
        self.assertEqual(
            (session.paths.host_input / "bin" / "run.sh").read_text(encoding="utf-8"),
            "#!/bin/sh\nexit 0\n",
        )
        self.assertEqual(list(session.paths.host_output.iterdir()), [])
        if os.name != "nt":
            self.assertEqual(
                stat.S_IMODE((session.paths.host_input / "app.py").stat().st_mode),
                0o400,
            )
            self.assertEqual(
                stat.S_IMODE((session.paths.host_input / "bin" / "run.sh").stat().st_mode),
                0o500,
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links unavailable")
    def test_project_import_rejects_symlinks_and_removes_partial_session(self) -> None:
        source = self.root / "project"
        source.mkdir()
        target = self.root / "outside.txt"
        target.write_text("secret", encoding="utf-8")
        (source / "escape").symlink_to(target)

        with self.assertRaises(SessionImportError) as raised:
            self.manager.create(source, session_id="sess_symlink")

        self.assertEqual(raised.exception.code, "project_import_failed")
        self.assertFalse((self.sessions / "sess_symlink").exists())

    def test_project_import_limits_file_count(self) -> None:
        source = self.root / "project"
        source.mkdir()
        (source / "one").write_text("1", encoding="utf-8")
        (source / "two").write_text("2", encoding="utf-8")
        manager = SessionManager(
            self.root / "limited-sessions",
            import_limits=ImportLimits(max_files=1, max_total_bytes=100, max_file_bytes=100),
        )

        with self.assertRaisesRegex(SessionImportError, "too many files"):
            manager.create(source, session_id="sess_limited")
        self.assertFalse((manager.base_dir / "sess_limited").exists())

    def test_source_project_must_be_a_directory(self) -> None:
        source = self.root / "file.txt"
        source.write_text("not a project", encoding="utf-8")
        with self.assertRaises(SessionImportError):
            self.manager.create(source, session_id="sess_file")

    def test_step_and_plan_updates_are_atomic_and_persistent(self) -> None:
        session = self.manager.create(max_steps=5, session_id="sess_steps")
        self.manager.set_plan(session, "1. inspect\n2. build")
        self.manager.advance_step(session)
        self.manager.advance_step(session)
        self.manager.advance_step(session)
        self.manager.advance_step(session)

        loaded = self.manager.load(session.id)
        self.assertEqual(loaded.state.plan, "1. inspect\n2. build")
        self.assertEqual(loaded.state.step, 4)
        self.assertEqual(loaded.state.revision, 5)
        self.assertTrue(loaded.state.step_limit_approaching)
        self.assertEqual(list(loaded.paths.host_root.glob(".state-*.tmp")), [])

    def test_step_limit_is_enforced(self) -> None:
        session = self.manager.create(max_steps=1, session_id="sess_limit")
        self.manager.advance_step(session)
        with self.assertRaises(StepLimitReachedError) as raised:
            self.manager.advance_step(session)
        self.assertEqual(raised.exception.code, "step_limit_reached")

    def test_stale_session_update_is_rejected(self) -> None:
        first = self.manager.create(session_id="sess_conflict")
        stale = self.manager.load(first.id)
        self.manager.advance_step(first)

        with self.assertRaises(SessionConflictError) as raised:
            self.manager.set_plan(stale, "stale write")

        self.assertEqual(raised.exception.code, "session_conflict")
        self.assertIsNone(self.manager.load(first.id).state.plan)

    def test_action_results_are_idempotency_records(self) -> None:
        session = self.manager.create(session_id="sess_actions")
        result = Result("a1", ResultStatus.SUCCESS, body="three files")
        self.manager.record_action_result(session, "list-dir", result)

        loaded = self.manager.load(session.id)
        record = loaded.state.action("a1")
        self.assertEqual(record.tool, "list-dir")
        self.assertEqual(record.result.body, "three files")
        self.assertEqual(loaded.state.result_history, ("a1",))
        self.assertEqual(loaded.state.used_action_ids, ("a1",))

        with self.assertRaises(SessionError) as raised:
            self.manager.record_action_result(session, "list-dir", result)
        self.assertEqual(raised.exception.code, "duplicate_action_id")

    def test_version_one_state_is_loaded_and_upgraded_on_update(self) -> None:
        session = self.manager.create(session_id="sess_v1_upgrade")
        self.manager.record_action_result(
            session,
            "list-dir",
            Result("a1", ResultStatus.SUCCESS, body="legacy"),
        )
        raw = json.loads(session.paths.state_file.read_text(encoding="utf-8"))
        raw["version"] = 1
        raw.pop("used_action_ids")
        raw.pop("pending_confirmation")
        for action in raw["action_ledger"]:
            action.pop("action_digest")
        session.paths.state_file.write_text(json.dumps(raw), encoding="utf-8")
        session.paths.state_file.chmod(0o600)

        loaded = self.manager.load(session.id)
        self.assertIsNone(loaded.state.action("a1").action_digest)
        self.manager.set_plan(loaded, "upgrade")

        upgraded = json.loads(session.paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(upgraded["version"], 4)
        self.assertIn("action_digest", upgraded["action_ledger"][0])
        self.assertEqual(upgraded["used_action_ids"], ["a1"])

    def test_version_two_state_is_loaded_and_upgraded_on_update(self) -> None:
        session = self.manager.create(session_id="sess_v2_upgrade")
        self.manager.record_action_result(
            session,
            "list-dir",
            Result("a1", ResultStatus.SUCCESS, body="legacy"),
        )
        raw = json.loads(session.paths.state_file.read_text(encoding="utf-8"))
        raw["version"] = 2
        raw.pop("used_action_ids")
        raw.pop("pending_confirmation")
        session.paths.state_file.write_text(json.dumps(raw), encoding="utf-8")
        session.paths.state_file.chmod(0o600)

        loaded = self.manager.load(session.id)
        self.assertEqual(loaded.state.used_action_ids, ("a1",))
        self.manager.set_plan(loaded, "upgrade")

        upgraded = json.loads(session.paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(upgraded["version"], 4)
        self.assertEqual(upgraded["used_action_ids"], ["a1"])

    def test_version_three_state_is_loaded_and_upgraded_on_update(self) -> None:
        session = self.manager.create(session_id="sess_v3_upgrade")
        raw = json.loads(session.paths.state_file.read_text(encoding="utf-8"))
        raw["version"] = 3
        raw.pop("pending_confirmation")
        session.paths.state_file.write_text(json.dumps(raw), encoding="utf-8")
        session.paths.state_file.chmod(0o600)

        loaded = self.manager.load(session.id)
        self.assertIsNone(loaded.state.pending_confirmation)
        self.manager.set_plan(loaded, "upgrade")

        upgraded = json.loads(session.paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(upgraded["version"], 4)
        self.assertIsNone(upgraded["pending_confirmation"])

    def test_action_ids_can_be_reserved_before_a_failed_attempt(self) -> None:
        session = self.manager.create(session_id="sess_reserved_actions")
        self.manager.reserve_action_ids(session, ("attempt1", "attempt2"))

        loaded = self.manager.load(session.id)
        self.assertEqual(loaded.state.used_action_ids, ("attempt1", "attempt2"))
        self.assertEqual(loaded.state.result_history, ())

        with self.assertRaises(SessionError) as raised:
            self.manager.reserve_action_ids(session, ("attempt1",))
        self.assertEqual(raised.exception.code, "duplicate_action_id")

    def test_pending_confirmation_persists_exact_action_and_requires_resolution(self) -> None:
        session = self.manager.create(session_id="sess_pending_confirmation")
        action = Action(
            id="overwrite1",
            tool="overwrite-file",
            path=PathRef("src/app.py", Root.OUTPUT),
            arguments=(Argument("content", "print('new')\n"),),
            expect_confirm=True,
        )
        self.manager.reserve_action_ids(session, (action.id,))
        self.manager.request_confirmation(
            session,
            action,
            "overwrite a non-empty output file",
            "a" * 64,
        )

        loaded = self.manager.load(session.id)
        self.assertEqual(loaded.state.status, SessionStatus.WAITING_USER)
        self.assertEqual(loaded.state.pending_confirmation.action, action)
        self.assertEqual(loaded.state.result_history, ())

        with self.assertRaises(SessionError) as raised:
            self.manager.set_status(loaded, SessionStatus.ACTIVE)
        self.assertEqual(raised.exception.code, "confirmation_pending")

        denial = Result(
            action.id,
            ResultStatus.FAILURE,
            body="Human denied this action.",
        )
        self.manager.record_action_result(
            loaded,
            action.tool,
            denial,
            resolve_confirmation=True,
        )
        resolved = self.manager.load(session.id)
        self.assertEqual(resolved.state.status, SessionStatus.ACTIVE)
        self.assertIsNone(resolved.state.pending_confirmation)
        self.assertEqual(resolved.state.action(action.id).result, denial)

    def test_aborting_a_pending_confirmation_clears_it(self) -> None:
        session = self.manager.create(session_id="sess_abort_confirmation")
        action = Action(
            "overwrite1",
            "overwrite-file",
            path=PathRef("app.py", Root.OUTPUT),
            arguments=(Argument("content", "new"),),
            expect_confirm=True,
        )
        self.manager.reserve_action_ids(session, (action.id,))
        self.manager.request_confirmation(session, action, "replace app.py", "b" * 64)
        self.manager.set_status(session, SessionStatus.ABORTED)

        loaded = self.manager.load(session.id)
        self.assertEqual(loaded.state.status, SessionStatus.ABORTED)
        self.assertIsNone(loaded.state.pending_confirmation)

    def test_step_limit_extension_requires_waiting_at_the_limit(self) -> None:
        session = self.manager.create(max_steps=2, session_id="sess_extend")

        with self.assertRaises(SessionError) as raised:
            self.manager.extend_step_limit(session, 1)
        self.assertEqual(raised.exception.code, "step_extension_not_allowed")

        self.manager.advance_step(session)
        self.manager.set_status(session, SessionStatus.WAITING_USER)
        with self.assertRaises(SessionError) as raised:
            self.manager.extend_step_limit(session, 1)
        self.assertEqual(raised.exception.code, "step_extension_not_allowed")

        self.manager.set_status(session, SessionStatus.ACTIVE)
        self.manager.advance_step(session)
        self.manager.set_status(session, SessionStatus.WAITING_USER)
        self.manager.extend_step_limit(session, 3)

        self.assertEqual(session.state.max_steps, 5)
        self.assertEqual(session.state.status, SessionStatus.WAITING_USER)

    def test_oversized_result_is_not_persisted(self) -> None:
        manager = SessionManager(self.root / "small-state", max_result_bytes=4)
        session = manager.create(session_id="sess_result_limit")
        with self.assertRaises(SessionError) as raised:
            manager.record_action_result(
                session,
                "read-file",
                Result("a1", ResultStatus.SUCCESS, body="12345"),
            )
        self.assertEqual(raised.exception.code, "result_too_large")
        self.assertIsNone(manager.load(session.id).state.action("a1"))

    def test_chunk_sequence_state_survives_reload(self) -> None:
        session = self.manager.create(session_id="sess_chunks")
        path = PathRef("src/large.py", Root.OUTPUT)
        self.manager.record_chunk(session, path, seq=1, final=False)
        self.manager.record_chunk(session, path, seq=2, final=True)

        loaded = self.manager.load(session.id)
        chunk = loaded.state.chunk(path)
        self.assertEqual(chunk.next_seq, 3)
        self.assertTrue(chunk.finalized)

        with self.assertRaises(SessionError) as raised:
            self.manager.record_chunk(session, path, seq=3, final=True)
        self.assertEqual(raised.exception.code, "chunk_sequence_error")

    def test_chunk_state_rejects_input_root(self) -> None:
        session = self.manager.create(session_id="sess_input_chunk")
        with self.assertRaises(SessionError) as raised:
            self.manager.record_chunk(
                session,
                PathRef("file", Root.INPUT),
                seq=1,
                final=False,
            )
        self.assertEqual(raised.exception.code, "input_readonly")

    def test_process_handles_are_persisted_and_updated(self) -> None:
        session = self.manager.create(session_id="sess_process")
        handle = self.manager.register_process(session, pid=1234, handle="proc_test")
        self.manager.update_process(
            session,
            handle,
            status=ProcessStatus.EXITED,
            output_offset=88,
        )

        process = self.manager.load(session.id).state.process(handle)
        self.assertEqual(process.pid, 1234)
        self.assertEqual(process.status, ProcessStatus.EXITED)
        self.assertEqual(process.output_offset, 88)

        with self.assertRaises(SessionError) as raised:
            self.manager.update_process(session, handle, status=ProcessStatus.RUNNING)
        self.assertEqual(raised.exception.code, "invalid_process_transition")

    def test_terminal_status_cannot_be_reopened(self) -> None:
        session = self.manager.create(session_id="sess_status")
        self.manager.set_status(session, SessionStatus.COMPLETED)
        with self.assertRaises(SessionError) as raised:
            self.manager.set_status(session, SessionStatus.ACTIVE)
        self.assertEqual(raised.exception.code, "invalid_status_transition")

    def test_load_rejects_writable_input_tampering(self) -> None:
        source = self.root / "project"
        source.mkdir()
        (source / "app.py").write_text("pass\n", encoding="utf-8")
        session = self.manager.create(source, session_id="sess_tampered")
        if os.name == "nt":
            self.skipTest("POSIX mode assertion")
        (session.paths.host_input / "app.py").chmod(0o600)

        with self.assertRaises(SessionError) as raised:
            self.manager.load(session.id)
        self.assertEqual(raised.exception.code, "session_integrity_error")

    def test_load_rejects_tampered_structured_state(self) -> None:
        session = self.manager.create(session_id="sess_state_tamper")
        raw = json.loads(session.paths.state_file.read_text(encoding="utf-8"))
        raw["chunks"] = [
            {
                "root": "input",
                "path": "forbidden",
                "next_seq": 2,
                "finalized": False,
                "updated_at": raw["updated_at"],
            }
        ]
        session.paths.state_file.write_text(json.dumps(raw), encoding="utf-8")
        session.paths.state_file.chmod(0o600)

        with self.assertRaises(SessionError) as raised:
            self.manager.load(session.id)
        self.assertEqual(raised.exception.code, "invalid_session_state")

    def test_invalid_session_id_cannot_escape_storage(self) -> None:
        with self.assertRaises(SessionError) as raised:
            self.manager.load("../outside")
        self.assertEqual(raised.exception.code, "invalid_session_id")


if __name__ == "__main__":
    unittest.main()
