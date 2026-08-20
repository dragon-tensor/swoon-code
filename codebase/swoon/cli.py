"""Command-line interfaces for the browser relay and bounded AEML agent."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .aeml import AEMLPromptBuilder
from .orchestration import (
    AgentOrchestrator,
    OrchestrationLimits,
    RunResult,
    RunStopReason,
)
from .session import (
    DEFAULT_MAX_STEPS,
    ProcessTerminationReason,
    Session,
    SessionManager,
    SessionStatus,
    WorkspaceSessionManager,
    session_id_for_workspace,
    workspace_name_for_session,
)
from .transport import AEMLChatChannel, ChatGPTWebTransport
from .tools import AgentToolDispatcher


EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_ABORTED = 3
EXIT_PROTOCOL_ERROR = 4
EXIT_RUNTIME_ERROR = 5
EXIT_INPUT_REQUIRED = 6
EXIT_INTERRUPTED = 130

_COMMANDS = frozenset({"chat", "agent", "doctor", "session"})
_ABORT_COMMANDS = frozenset({"/abort", "/quit"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swoon",
        description="ChatGPT browser relay and bounded AEML coding agent",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    doctor = commands.add_parser(
        "doctor",
        help="check consumer browser and command-sandbox prerequisites",
    )
    doctor.add_argument(
        "--cookies",
        help="optionally validate a ChatGPT cookie/storage-state JSON file",
    )
    doctor.add_argument(
        "--launch-browser",
        action="store_true",
        help="also launch and close Chromium instead of checking installation only",
    )
    doctor.set_defaults(handler=_run_doctor)

    session_command = commands.add_parser(
        "session",
        help="inspect, export, and remove persisted sessions",
    )
    session_actions = session_command.add_subparsers(
        dest="session_action",
        metavar="ACTION",
        required=True,
    )
    session_list = session_actions.add_parser("list", help="list persisted sessions")
    _add_session_directory_argument(session_list)
    session_list.set_defaults(handler=_run_session_list)

    session_show = session_actions.add_parser("show", help="show one session")
    session_show.add_argument("session_id", metavar="SESSION_ID")
    _add_session_directory_argument(session_show)
    session_show.set_defaults(handler=_run_session_show)

    session_export = session_actions.add_parser(
        "export",
        help="copy terminal-session output to a new directory",
    )
    session_export.add_argument("session_id", metavar="SESSION_ID")
    session_export.add_argument("destination", metavar="DESTINATION")
    _add_session_directory_argument(session_export)
    session_export.set_defaults(handler=_run_session_export)

    session_delete = session_actions.add_parser("delete", help="delete one persisted session")
    session_delete.add_argument("session_id", metavar="SESSION_ID")
    session_delete.add_argument(
        "--yes",
        action="store_true",
        help="confirm deletion without an interactive prompt",
    )
    session_delete.add_argument(
        "--force-active",
        action="store_true",
        help="allow deletion of non-terminal state when no process is recorded running",
    )
    _add_session_directory_argument(session_delete)
    session_delete.set_defaults(handler=_run_session_delete)

    chat = commands.add_parser("chat", help="run the direct ChatGPT terminal relay")
    _add_browser_arguments(chat)
    chat.add_argument("--prompt", "-p")
    chat.add_argument("--interactive", "-i", action="store_true")
    chat.set_defaults(handler=_run_chat)

    agent = commands.add_parser("agent", help="run a bounded AEML coding session")
    _add_browser_arguments(agent)
    agent.add_argument("--prompt", "-p", help="initial task or answer for a resumed session")
    source = agent.add_mutually_exclusive_group()
    source.add_argument("--project", help="copy a project into a new session's input root")
    source.add_argument("--resume", metavar="SESSION_ID", help="resume a persisted session")
    _add_session_directory_argument(agent)
    identity = agent.add_mutually_exclusive_group()
    identity.add_argument(
        "--session-id",
        help="explicit ID for a new session; normally generated automatically",
    )
    identity.add_argument(
        "--name",
        help="named work/input and work/output folder (creates or resumes automatically)",
    )
    agent.add_argument(
        "--max-steps",
        type=_max_steps,
        default=None,
        help=f"new-session turn budget (default: {DEFAULT_MAX_STEPS})",
    )
    agent.add_argument(
        "--additional-steps",
        type=_additional_steps,
        default=None,
        help="explicitly extend an exhausted resumed session",
    )
    agent.add_argument(
        "--protocol-retries",
        type=_protocol_retries,
        default=2,
        help="repairs after an invalid AEML response (default: 2)",
    )
    interaction = agent.add_mutually_exclusive_group()
    interaction.add_argument(
        "--interactive",
        action="store_true",
        help="keep one coding session open at a swoon> terminal prompt",
    )
    interaction.add_argument(
        "--non-interactive",
        action="store_true",
        help="return exit 6 instead of reading human input",
    )
    pending = agent.add_mutually_exclusive_group()
    pending.add_argument(
        "--approve-pending",
        action="store_true",
        help="approve the exact destructive action stored by a resumed session",
    )
    pending.add_argument(
        "--deny-pending",
        action="store_true",
        help="deny the exact destructive action stored by a resumed session",
    )
    agent.set_defaults(handler=_run_agent)
    return parser


def _add_session_directory_argument(parser: argparse.ArgumentParser) -> None:
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument(
        "--session-dir",
        help="legacy nested session storage directory",
    )
    storage.add_argument(
        "--work-dir",
        help="work root containing input and output folders",
    )


def main(argv: list[str] | None = None) -> int:
    """Run the Swoon CLI, accepting both subcommands and legacy relay flags."""

    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not raw:
        raw = ["agent", "--interactive", "--name", "default"]
    normalized = _normalize_legacy_args(raw)
    args = parser.parse_args(normalized)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 1
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


def legacy_main(argv: list[str] | None = None) -> int:
    """Run only the historical relay interface used by chatgpt_agent.py."""

    raw = list(sys.argv[1:] if argv is None else argv)
    return main(["chat", *raw])


def _run_doctor(args: argparse.Namespace) -> int:
    """Report whether this installation can run consumer-facing capabilities."""

    print(f"Swoon Code {__version__}")
    print(f"[ok] Python: {platform.python_version()}")

    browser_ready, browser_detail = _browser_runtime_status(
        launch=args.launch_browser,
    )
    _print_diagnostic("Browser runtime", browser_ready, browser_detail)

    cookies_ready: bool | None = None
    if args.cookies:
        cookies_ready, cookie_detail = _cookie_status(Path(args.cookies))
        _print_diagnostic("Cookie file", cookies_ready, cookie_detail)
    else:
        print("[skip] Cookie file: not supplied")

    sandbox_ready, sandbox_detail = _command_sandbox_status()
    _print_diagnostic("Command sandbox", sandbox_ready, sandbox_detail, optional=True)

    if not browser_ready or cookies_ready is False:
        remedies: list[str] = []
        if not browser_ready:
            remedies.append(
                "Install Chromium with `python -m playwright install chromium`."
            )
        if cookies_ready is False:
            remedies.append("Fix the reported cookie error.")
        print(f"Consumer check failed. {' '.join(remedies)}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    print("Consumer CLI is ready.")
    if not sandbox_ready:
        print("File/read agent tools work, but command execution will fail closed.")
    return EXIT_SUCCESS


def _run_session_list(args: argparse.Namespace) -> int:
    try:
        manager = _session_manager(args)
        identifiers = manager.list_session_ids()
    except Exception as error:
        _report_error(error)
        return EXIT_RUNTIME_ERROR
    if not identifiers:
        print("No sessions found.")
        return EXIT_SUCCESS

    failures = False
    print("SESSION\tSTATUS\tSTEPS\tUPDATED")
    for identifier in identifiers:
        try:
            session = manager.load(identifier)
        except Exception as error:
            failures = True
            code = getattr(error, "code", error.__class__.__name__)
            print(f"{identifier}\tERROR:{code}\t-\t-", file=sys.stderr)
            continue
        label = (
            workspace_name_for_session(session.id)
            if isinstance(manager, WorkspaceSessionManager)
            else session.id
        )
        print(
            f"{label}\t{session.state.status.value}\t"
            f"{session.state.step}/{session.state.max_steps}\t"
            f"{session.state.updated_at.isoformat()}"
        )
    return EXIT_RUNTIME_ERROR if failures else EXIT_SUCCESS


def _run_session_show(args: argparse.Namespace) -> int:
    try:
        manager = _session_manager(args)
        session = manager.load(_session_reference(manager, args.session_id))
    except Exception as error:
        _report_error(error)
        return EXIT_RUNTIME_ERROR
    pending = session.state.pending_confirmation
    print(f"Session: {session.id}")
    print(f"Status: {session.state.status.value}")
    print(f"Steps: {session.state.step}/{session.state.max_steps}")
    print(f"Created: {session.state.created_at.isoformat()}")
    print(f"Updated: {session.state.updated_at.isoformat()}")
    print(f"Input: {session.paths.host_input}")
    print(f"Output: {session.paths.host_output}")
    print(f"Actions: {len(session.state.action_ledger)}")
    print(f"Processes: {len(session.state.processes)}")
    print(f"Pending confirmation: {pending.action.tool if pending is not None else 'none'}")
    return EXIT_SUCCESS


def _run_session_export(args: argparse.Namespace) -> int:
    try:
        manager = _session_manager(args)
        identifier = _session_reference(manager, args.session_id)
        destination = manager.export_output(
            identifier,
            args.destination,
        )
    except Exception as error:
        _report_error(error)
        return EXIT_RUNTIME_ERROR
    print(f"Exported {args.session_id} output to {destination}")
    return EXIT_SUCCESS


def _run_session_delete(args: argparse.Namespace) -> int:
    manager = _session_manager(args)
    identifier = _session_reference(manager, args.session_id)
    if not args.yes:
        try:
            decision = input(
                f"Delete session {args.session_id!r} and all of its private data? [y/N]> "
            ).strip().casefold()
        except EOFError:
            print("Input required: session deletion was not confirmed.", file=sys.stderr)
            return EXIT_INPUT_REQUIRED
        if decision not in {"y", "yes"}:
            print("Session deletion cancelled.")
            return EXIT_SUCCESS
    try:
        manager.delete_session(
            identifier,
            force_active=args.force_active,
        )
    except Exception as error:
        _report_error(error)
        return EXIT_RUNTIME_ERROR
    print(f"Deleted session {args.session_id}.")
    return EXIT_SUCCESS


def _add_browser_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cookies",
        help="ChatGPT cookie JSON (defaults to the configured setup location)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument(
        "--save-storage-state",
        metavar="PATH",
        help="opt in to saving refreshed credentials to an owner-private JSON file",
    )
    parser.add_argument(
        "--debug-artifacts",
        metavar="DIRECTORY",
        help="opt in to private screenshots when browser startup cannot find the chat input",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=180.0,
        help="maximum response wait in seconds (default: 180)",
    )
    parser.add_argument(
        "--response-settle-time",
        type=_positive_timeout,
        default=5.0,
        help="unchanged seconds required before a response is complete (default: 5)",
    )


def _run_chat(args: argparse.Namespace) -> int:
    if not args.prompt and not args.interactive:
        print("Error: chat requires --prompt or --interactive.", file=sys.stderr)
        return EXIT_USAGE

    client = None
    exit_code = EXIT_RUNTIME_ERROR
    try:
        client = _transport(args)
        client.start()
        if args.interactive:
            print("ChatGPT — type messages, /quit to exit")
            while True:
                try:
                    message = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not message:
                    continue
                if message == "/quit":
                    break
                print()
                print(client.send(message))
                print()
        else:
            print(client.send(args.prompt))
        return EXIT_SUCCESS
    except Exception as error:
        _report_error(error)
        return EXIT_RUNTIME_ERROR
    finally:
        _close_transport(client)


def _run_agent(args: argparse.Namespace) -> int:
    if args.resume is not None and args.session_id is not None:
        print("Error: --session-id cannot be combined with --resume.", file=sys.stderr)
        return EXIT_USAGE
    if args.resume is not None and args.name is not None:
        print("Error: --name cannot be combined with --resume.", file=sys.stderr)
        return EXIT_USAGE
    if args.name is not None and args.session_dir is not None:
        print("Error: --name uses --work-dir, not legacy --session-dir.", file=sys.stderr)
        return EXIT_USAGE
    if args.resume is not None and args.max_steps is not None:
        print("Error: --max-steps applies only to new sessions.", file=sys.stderr)
        return EXIT_USAGE
    if args.prompt is not None and not args.prompt.strip():
        print("Error: --prompt cannot be empty.", file=sys.stderr)
        return EXIT_USAGE

    try:
        manager = _session_manager(args)
        if args.resume:
            session = manager.load(_session_reference(manager, args.resume))
        elif args.name:
            assert isinstance(manager, WorkspaceSessionManager)
            session = manager.load_name_if_present(args.name)
        else:
            session = None
        dispatcher = AgentToolDispatcher(manager)
        if session is not None:
            dispatcher.reconcile_background(session)
    except Exception as error:
        _report_error(error)
        return EXIT_RUNTIME_ERROR

    resuming = session is not None
    if resuming and args.project is not None:
        print("Error: --project only applies when creating a named session.", file=sys.stderr)
        return EXIT_USAGE
    if not resuming and args.additional_steps is not None:
        print("Error: --additional-steps requires an existing session.", file=sys.stderr)
        return EXIT_USAGE
    if not resuming and (args.approve_pending or args.deny_pending):
        print("Error: pending-action decisions require an existing session.", file=sys.stderr)
        return EXIT_USAGE
    if resuming and args.max_steps is not None:
        print("Error: --max-steps applies only to new sessions.", file=sys.stderr)
        return EXIT_USAGE

    if args.interactive:
        print("Swoon Code interactive agent")
        print("Enter a coding task at swoon>. Type /quit to pause this session.")

    if session is not None:
        print(f"Session: {session.id}")
        print(f"Output: {session.paths.host_output}")
        terminal = _terminal_session_exit(session)
        if terminal is not None:
            return terminal
        if args.additional_steps is not None:
            if session.state.pending_confirmation is not None:
                print(
                    "Error: resolve the pending action before extending steps.",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            if session.state.step < session.state.max_steps:
                print(
                    "Error: --additional-steps requires an exhausted session.",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            if session.state.max_steps + args.additional_steps > 10_000:
                print(
                    "Error: the extended step budget cannot exceed 10000.",
                    file=sys.stderr,
                )
                return EXIT_USAGE

    initial_confirmation: bool | None = None
    if session is not None and session.state.pending_confirmation is not None:
        if args.approve_pending:
            initial_confirmation = True
        elif args.deny_pending:
            initial_confirmation = False
        elif args.non_interactive:
            _report_input_required(session, "destructive-action approval or denial")
            return EXIT_INPUT_REQUIRED
        else:
            initial_confirmation, abort = _read_pending_confirmation(session)
            if abort:
                manager.set_status(session, SessionStatus.ABORTED)
                print("Session aborted by the user.", file=sys.stderr)
                return EXIT_ABORTED
            if initial_confirmation is None:
                _report_input_required(session, "destructive-action approval or denial")
                return EXIT_INPUT_REQUIRED
        prompt = (
            args.prompt.strip()
            if args.prompt is not None
            else (
                "The human approved the pending action. Continue."
                if initial_confirmation
                else "The human denied the pending action. Continue safely."
            )
        )
    else:
        if args.approve_pending or args.deny_pending:
            print("Error: the resumed session has no pending action.", file=sys.stderr)
            return EXIT_USAGE
        prompt = _initial_prompt(args)
    if prompt is None:
        return EXIT_INPUT_REQUIRED
    if prompt in _ABORT_COMMANDS:
        if session is not None:
            manager.set_status(session, SessionStatus.ABORTED)
        print("Session aborted by the user.", file=sys.stderr)
        return EXIT_ABORTED

    client: ChatGPTWebTransport | None = None
    if session is None:
        try:
            client = _transport(args)
        except Exception as error:
            _report_error(error)
            return EXIT_RUNTIME_ERROR
        try:
            session = manager.create(
                args.project,
                max_steps=args.max_steps or DEFAULT_MAX_STEPS,
                session_id=(
                    session_id_for_workspace(args.name)
                    if args.name is not None
                    else args.session_id
                ),
            )
        except Exception as error:
            _report_error(error)
            return EXIT_RUNTIME_ERROR
        print(f"Session: {session.id}")
        print(f"Output: {session.paths.host_output}")

    try:
        if client is None:
            client = _transport(args)
        client.start()
        prompt_builder = AEMLPromptBuilder(dispatcher.tool_specs)
        orchestrator = AgentOrchestrator(
            manager,
            AEMLChatChannel(client, prompt_builder=prompt_builder),
            dispatcher=dispatcher,
            limits=OrchestrationLimits(args.protocol_retries),
            message_sink=_print_agent_message,
            keep_alive=args.interactive,
        )
        prompt_was_blocked_by_limit = (
            args.additional_steps is None
            and session.state.step >= session.state.max_steps
        )
        outcome = orchestrator.run(
            session,
            prompt,
            additional_steps=args.additional_steps,
            confirmation=initial_confirmation,
        )
        exit_code = _drive_agent_outcome(
            manager,
            orchestrator,
            outcome,
            non_interactive=args.non_interactive,
            pending_step_prompt=prompt if prompt_was_blocked_by_limit else None,
            console=args.interactive,
        )
    except Exception as error:
        _report_error(error)
        exit_code = EXIT_RUNTIME_ERROR
    finally:
        try:
            dispatcher.shutdown_background(
                manager.load(session.id),
                reason=ProcessTerminationReason.HOST_EXIT,
            )
        except Exception as error:
            _report_error(error)
            exit_code = EXIT_RUNTIME_ERROR
        _close_transport(client)
    return exit_code


def _drive_agent_outcome(
    manager: SessionManager,
    orchestrator: AgentOrchestrator,
    outcome: RunResult,
    *,
    non_interactive: bool,
    pending_step_prompt: str | None,
    console: bool = False,
) -> int:
    while True:
        if outcome.reason in {RunStopReason.COMPLETED, RunStopReason.DONE}:
            if outcome.reason is RunStopReason.DONE:
                print("Session completed.")
            return EXIT_SUCCESS

        if outcome.reason is RunStopReason.ABORTED:
            print("Session aborted by the agent.", file=sys.stderr)
            return EXIT_ABORTED

        if outcome.reason is RunStopReason.PROTOCOL_ERROR:
            error = outcome.error
            if error is None:
                print("Protocol failed without a structured error.", file=sys.stderr)
            else:
                print(f"Protocol error [{error.code}]: {error.message}", file=sys.stderr)
            return EXIT_PROTOCOL_ERROR

        if outcome.reason is RunStopReason.AWAITING_USER:
            if non_interactive:
                _report_input_required(outcome.session, "a human answer")
                return EXIT_INPUT_REQUIRED
            answer = _read_nonempty(
                "swoon> " if console else "Answer (/abort to stop)> "
            )
            if answer is None:
                _report_input_required(outcome.session, "a human answer")
                return EXIT_INPUT_REQUIRED
            if answer in _ABORT_COMMANDS:
                orchestrator.shutdown_background(
                    outcome.session,
                    reason=ProcessTerminationReason.SESSION_END,
                )
                if console and answer == "/quit":
                    print(f"Session {outcome.session.id} paused. Resume it with the same name.")
                    return EXIT_SUCCESS
                manager.set_status(outcome.session, SessionStatus.ABORTED)
                print("Session aborted by the user.", file=sys.stderr)
                return EXIT_ABORTED
            answer_was_blocked_by_limit = (
                outcome.session.state.step >= outcome.session.state.max_steps
            )
            outcome = orchestrator.run(outcome.session, answer)
            pending_step_prompt = (
                answer
                if answer_was_blocked_by_limit
                and outcome.reason is RunStopReason.STEP_LIMIT
                else None
            )
            continue

        if outcome.reason is RunStopReason.AWAITING_CONFIRMATION:
            if non_interactive:
                _report_input_required(
                    outcome.session,
                    "destructive-action approval or denial",
                )
                return EXIT_INPUT_REQUIRED
            decision, abort = _read_pending_confirmation(outcome.session)
            if abort:
                orchestrator.shutdown_background(
                    outcome.session,
                    reason=ProcessTerminationReason.SESSION_END,
                )
                manager.set_status(outcome.session, SessionStatus.ABORTED)
                print("Session aborted by the user.", file=sys.stderr)
                return EXIT_ABORTED
            if decision is None:
                _report_input_required(
                    outcome.session,
                    "destructive-action approval or denial",
                )
                return EXIT_INPUT_REQUIRED
            decision_prompt = (
                "The human approved the pending action. Continue."
                if decision
                else "The human denied the pending action. Continue safely."
            )
            outcome = orchestrator.run(
                outcome.session,
                decision_prompt,
                confirmation=decision,
            )
            continue

        if outcome.reason is RunStopReason.STEP_LIMIT:
            if non_interactive:
                _report_input_required(outcome.session, "step-budget approval")
                return EXIT_INPUT_REQUIRED
            additional_steps, abort = _read_step_extension(outcome.session)
            if abort:
                orchestrator.shutdown_background(
                    outcome.session,
                    reason=ProcessTerminationReason.SESSION_END,
                )
                manager.set_status(outcome.session, SessionStatus.ABORTED)
                print("Session aborted by the user.", file=sys.stderr)
                return EXIT_ABORTED
            if additional_steps is None:
                _report_input_required(outcome.session, "step-budget approval")
                return EXIT_INPUT_REQUIRED
            approval = pending_step_prompt or (
                f"The human approved {additional_steps} additional steps. Continue."
            )
            pending_step_prompt = None
            outcome = orchestrator.run(
                outcome.session,
                approval,
                additional_steps=additional_steps,
            )
            continue

        raise RuntimeError(f"Unknown orchestration outcome {outcome.reason!r}")


def _terminal_session_exit(session: Session) -> int | None:
    if session.state.status is SessionStatus.COMPLETED:
        print("Session is already completed.")
        return EXIT_SUCCESS
    if session.state.status is SessionStatus.ABORTED:
        print("Session is already aborted.", file=sys.stderr)
        return EXIT_ABORTED
    return None


def _initial_prompt(args: argparse.Namespace) -> str | None:
    if args.prompt is not None:
        return args.prompt.strip()
    if args.non_interactive:
        print("Input required: agent needs --prompt in non-interactive mode.", file=sys.stderr)
        return None
    return _read_nonempty("swoon> " if args.interactive else "Task> ")


def _read_nonempty(prompt: str) -> str | None:
    while True:
        try:
            value = input(prompt).strip()
        except EOFError:
            return None
        if value:
            return value
        print("Input cannot be empty.", file=sys.stderr)


def _read_step_extension(session: Session) -> tuple[int | None, bool]:
    maximum = 10_000 - session.state.max_steps
    if maximum < 1:
        print("The session is already at the hard 10000-step maximum.", file=sys.stderr)
        return None, False
    while True:
        try:
            raw = input(f"Additional steps (1-{maximum}, /abort to stop)> ").strip()
        except EOFError:
            return None, False
        if raw in _ABORT_COMMANDS:
            return None, True
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if 1 <= value <= maximum:
            return value, False
        print(f"Enter a whole number from 1 to {maximum}, or /abort.", file=sys.stderr)


def _read_pending_confirmation(session: Session) -> tuple[bool | None, bool]:
    pending = session.state.pending_confirmation
    if pending is None:
        raise RuntimeError("Session has no pending confirmation")
    print(
        f"Pending {pending.action.tool} action {pending.action.id!r}: {pending.reason}"
    )
    while True:
        try:
            raw = input("Approve this exact action? [y/N] (/abort to stop)> ").strip().lower()
        except EOFError:
            return None, False
        if raw in _ABORT_COMMANDS:
            return None, True
        if raw in {"y", "yes"}:
            return True, False
        if raw in {"", "n", "no"}:
            return False, False
        print("Enter y, n, or /abort.", file=sys.stderr)


def _transport(args: argparse.Namespace) -> ChatGPTWebTransport:
    cookie_path = Path(args.cookies).expanduser() if args.cookies else _default_cookie_path()
    return ChatGPTWebTransport(
        cookie_path,
        verbose=args.verbose,
        headless=not args.headed,
        response_timeout=args.timeout,
        response_settle_time=args.response_settle_time,
        storage_state_path=args.save_storage_state,
        debug_directory=args.debug_artifacts,
    )


def _close_transport(client: ChatGPTWebTransport | None) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception as error:
        print(
            f"Warning: browser cleanup failed ({error.__class__.__name__}).",
            file=sys.stderr,
        )


def _print_agent_message(message: str) -> None:
    print(message, flush=True)


def _browser_runtime_status(*, launch: bool = False) -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.is_file():
                return False, "Chromium executable is not installed"
            if not launch:
                return True, "Playwright and Chromium are installed"
            browser = playwright.chromium.launch(headless=True)
            try:
                version = browser.version
            finally:
                browser.close()
            return True, f"Chromium {version} launched successfully"
    except Exception as error:
        return False, f"unavailable ({error.__class__.__name__})"
    raise AssertionError("Browser diagnostic reached an impossible state")


def _cookie_status(path: Path) -> tuple[bool, str]:
    try:
        transport = ChatGPTWebTransport(path)
    except Exception as error:
        return False, f"invalid or unreadable: {error}"
    count = len(transport.raw_cookies)
    if count < 1:
        return False, "contains no cookies"
    return True, f"valid storage state ({count} cookies)"


def _command_sandbox_status() -> tuple[bool, str]:
    architecture = platform.machine().lower()
    supported_host = platform.system() == "Linux" and architecture in {
        "x86_64",
        "amd64",
        "aarch64",
        "arm64",
    }
    checks = {
        "supported 64-bit Linux host": supported_host,
        "bwrap": shutil.which("bwrap") is not None,
        "prlimit": shutil.which("prlimit") is not None,
        "system python3": any(
            path.is_file()
            for path in (Path("/usr/bin/python3"), Path("/usr/local/bin/python3"))
        ),
    }
    missing = [name for name, available in checks.items() if not available]
    if missing:
        return False, "missing " + ", ".join(missing)
    return True, f"ready on {architecture}"


def _print_diagnostic(
    label: str,
    ready: bool,
    detail: str,
    *,
    optional: bool = False,
) -> None:
    status = "ok" if ready else "optional" if optional else "fail"
    print(f"[{status}] {label}: {detail}")


def _report_input_required(session: Session, requirement: str) -> None:
    print(
        f"Input required: session {session.id} is waiting for {requirement}.",
        file=sys.stderr,
    )


def _report_error(error: Exception) -> None:
    code = getattr(error, "code", error.__class__.__name__)
    print(f"Error [{code}]: {error}", file=sys.stderr)


def _normalize_legacy_args(argv: Sequence[str]) -> list[str]:
    if not argv or argv[0] in _COMMANDS or argv[0] in {"-h", "--help", "--version"}:
        return list(argv)
    if argv[0].startswith("-"):
        return ["chat", *argv]
    return ["agent", "--interactive", "--name", argv[0], *argv[1:]]


def _session_manager(args: argparse.Namespace) -> SessionManager:
    session_dir = getattr(args, "session_dir", None)
    if session_dir is not None:
        return SessionManager(session_dir)
    return WorkspaceSessionManager(getattr(args, "work_dir", None))


def _session_reference(manager: SessionManager, value: str) -> str:
    if isinstance(manager, WorkspaceSessionManager) and not value.startswith("sess_"):
        return session_id_for_workspace(value)
    return value


def _default_cookie_path() -> Path:
    configured = os.environ.get("SWOON_COOKIE_FILE")
    if configured:
        return Path(configured).expanduser().absolute()
    if os.name == "nt":
        parent = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if parent:
            return Path(parent) / "Swoon Code" / "cookies.json"
        return Path.home() / "AppData" / "Roaming" / "Swoon Code" / "cookies.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Swoon Code" / "cookies.json"
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    parent = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return parent / "swoon-code" / "cookies.json"


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from error
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number")
    return timeout


def _max_steps(value: str) -> int:
    return _bounded_integer(value, minimum=1, maximum=10_000, label="max-steps")


def _additional_steps(value: str) -> int:
    return _bounded_integer(value, minimum=1, maximum=10_000, label="additional-steps")


def _protocol_retries(value: str) -> int:
    return _bounded_integer(value, minimum=0, maximum=10, label="protocol-retries")


def _bounded_integer(value: str, *, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
