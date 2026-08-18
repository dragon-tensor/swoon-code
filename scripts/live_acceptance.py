#!/usr/bin/env python3
"""Run the opt-in, owner-authorized live browser and disposable-agent release gate."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from swoon.aeml import ResultStatus  # noqa: E402
from swoon.cli import EXIT_SUCCESS, main as swoon_main  # noqa: E402
from swoon.session import SessionManager, SessionStatus  # noqa: E402
from swoon.transport import ChatGPTWebTransport  # noqa: E402


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        if path.is_file():
            payload = path.read_bytes()
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return digest.hexdigest()


def _write_fixture(project: Path) -> None:
    project.mkdir(mode=0o700)
    (project / "app.py").write_text(
        "def add(left: int, right: int) -> int:\n    return left + right\n",
        encoding="utf-8",
    )
    (project / "test_app.py").write_text(
        "import unittest\n\n"
        "from app import add\n\n\n"
        "class AppTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )


def _relay_gate(args: argparse.Namespace, marker: str) -> None:
    transport = ChatGPTWebTransport(
        args.cookies,
        verbose=args.verbose,
        headless=not args.headed,
        response_timeout=args.timeout,
    )
    try:
        transport.start()
        response = transport.send(
            f"Reply with this exact release-gate marker and no other text: {marker}"
        )
    finally:
        transport.close()
    if marker not in response:
        raise RuntimeError("Live relay response did not contain the requested marker")


def _agent_gate(args: argparse.Namespace, workspace: Path, marker: str) -> None:
    project = workspace / "project"
    sessions = workspace / "sessions"
    exported = workspace / "exported"
    _write_fixture(project)
    original_digest = _tree_digest(project)
    session_id = f"sess_live_{secrets.token_hex(8)}"
    task = (
        "This is a disposable release-acceptance fixture. Copy every input file to output, "
        f"create acceptance.txt containing exactly {marker} followed by one newline, run "
        "`python3 -m unittest -v` with the offline run-command tool, and complete only after "
        "the test succeeds. Do not request package installation or network access."
    )
    arguments = [
        "agent",
        "--cookies",
        str(args.cookies),
        "--project",
        str(project),
        "--session-dir",
        str(sessions),
        "--session-id",
        session_id,
        "--max-steps",
        "12",
        "--protocol-retries",
        "3",
        "--timeout",
        str(args.timeout),
        "--prompt",
        task,
        "--non-interactive",
    ]
    if args.headed:
        arguments.append("--headed")
    if args.verbose:
        arguments.append("--verbose")
    code = swoon_main(arguments)
    if code != EXIT_SUCCESS:
        raise RuntimeError(f"Live agent exited with status {code}")

    manager = SessionManager(sessions)
    session = manager.load(session_id)
    if session.state.status is not SessionStatus.COMPLETED:
        raise RuntimeError(f"Live agent ended in state {session.state.status.value!r}")
    marker_file = session.paths.host_output / "acceptance.txt"
    if marker_file.read_text(encoding="utf-8") != marker + "\n":
        raise RuntimeError("Live agent did not create the exact acceptance marker")
    if not (session.paths.host_output / "app.py").is_file():
        raise RuntimeError("Live agent did not copy the fixture into output")
    command_success = any(
        record.tool in {"run-command", "run-tests"}
        and record.result is not None
        and record.result.status is ResultStatus.SUCCESS
        for record in session.state.action_ledger
    )
    if not command_success:
        raise RuntimeError("Live agent did not record a successful verification command")
    if _tree_digest(project) != original_digest:
        raise RuntimeError("Live agent changed the original fixture")

    manager.export_output(session_id, exported)
    if (exported / "acceptance.txt").read_text(encoding="utf-8") != marker + "\n":
        raise RuntimeError("Exported output does not contain the acceptance marker")
    manager.delete_session(session_id)
    if session.paths.host_root.exists():
        raise RuntimeError("Live acceptance session cleanup did not complete")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", type=Path, required=True)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--acknowledge-provider-terms",
        action="store_true",
        required=True,
        help="confirm that this live browser automation is authorized for the supplied account",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="retain the private disposable workspace for manual inspection",
    )
    args = parser.parse_args(argv)
    if not args.cookies.is_file():
        parser.error("--cookies must identify an existing private storage-state file")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    workspace = Path(tempfile.mkdtemp(prefix="swoon-live-acceptance-"))
    if os.name == "posix":
        workspace.chmod(0o700)
    marker = f"SWOON_LIVE_OK_{secrets.token_hex(8)}"
    print("Live gate: this will send a relay prompt and a bounded coding task.")
    print(f"Private workspace: {workspace}")
    try:
        _relay_gate(args, marker)
        _agent_gate(args, workspace, marker)
    except Exception as error:
        print(f"Live acceptance failed: {error}", file=sys.stderr)
        print(f"Inspect the private workspace before removing it: {workspace}", file=sys.stderr)
        return 1
    print("Live browser and disposable-agent acceptance passed.")
    if args.keep_workspace:
        print(f"Retained private workspace: {workspace}")
    else:
        shutil.rmtree(workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
