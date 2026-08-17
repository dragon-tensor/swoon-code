"""Command-line interfaces for the browser relay and bounded AEML agent."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .aeml import AEMLPromptBuilder
from .orchestration import (
    AgentOrchestrator,
    OrchestrationLimits,
    RunResult,
    RunStopReason,
)
from .session import DEFAULT_MAX_STEPS, Session, SessionManager, SessionStatus
from .transport import AEMLChatChannel, ChatGPTWebTransport
from .tools import AgentToolDispatcher


EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_ABORTED = 3
EXIT_PROTOCOL_ERROR = 4
EXIT_RUNTIME_ERROR = 5
EXIT_INPUT_REQUIRED = 6
EXIT_INTERRUPTED = 130

_COMMANDS = frozenset({"chat", "agent"})
_ABORT_COMMANDS = frozenset({"/abort", "/quit"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swoon",
        description="ChatGPT browser relay and bounded AEML coding agent",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

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
    agent.add_argument(
        "--session-dir",
        help="physical session storage directory (defaults to the private app data directory)",
    )
    agent.add_argument(
        "--session-id",
        help="explicit ID for a new session; normally generated automatically",
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
    agent.add_argument(
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


def main(argv: list[str] | None = None) -> int:
    """Run the Swoon CLI, accepting both subcommands and legacy relay flags."""

    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not raw:
        parser.print_help()
        return 1
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


def _add_browser_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cookies", required=True, help="ChatGPT cookie/storage-state JSON")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=180.0,
        help="response timeout in seconds (default: 180)",
    )


def _run_chat(args: argparse.Namespace) -> int:
    if not args.prompt and not args.interactive:
        print("Error: chat requires --prompt or --interactive.", file=sys.stderr)
        return EXIT_USAGE

    client = None
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
    if args.resume is not None and args.max_steps is not None:
        print("Error: --max-steps applies only to new sessions.", file=sys.stderr)
        return EXIT_USAGE
    if args.resume is None and args.additional_steps is not None:
        print("Error: --additional-steps requires --resume.", file=sys.stderr)
        return EXIT_USAGE
    if args.resume is None and (args.approve_pending or args.deny_pending):
        print(
            "Error: pending-action decisions require --resume.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.prompt is not None and not args.prompt.strip():
        print("Error: --prompt cannot be empty.", file=sys.stderr)
        return EXIT_USAGE

    try:
        manager = SessionManager(args.session_dir)
        session = manager.load(args.resume) if args.resume else None
    except Exception as error:
        _report_error(error)
        return EXIT_RUNTIME_ERROR

    if session is not None:
        print(f"Session: {session.id}")
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

    if session is None:
        try:
            session = manager.create(
                args.project,
                max_steps=args.max_steps or DEFAULT_MAX_STEPS,
                session_id=args.session_id,
            )
        except Exception as error:
            _report_error(error)
            return EXIT_RUNTIME_ERROR
        print(f"Session: {session.id}")

    client = None
    try:
        client = _transport(args)
        client.start()
        dispatcher = AgentToolDispatcher(manager)
        prompt_builder = AEMLPromptBuilder(dispatcher.tool_specs)
        orchestrator = AgentOrchestrator(
            manager,
            AEMLChatChannel(client, prompt_builder=prompt_builder),
            dispatcher=dispatcher,
            limits=OrchestrationLimits(args.protocol_retries),
            message_sink=_print_agent_message,
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
        return _drive_agent_outcome(
            manager,
            orchestrator,
            outcome,
            non_interactive=args.non_interactive,
            pending_step_prompt=prompt if prompt_was_blocked_by_limit else None,
        )
    except Exception as error:
        _report_error(error)
        return EXIT_RUNTIME_ERROR
    finally:
        _close_transport(client)


def _drive_agent_outcome(
    manager: SessionManager,
    orchestrator: AgentOrchestrator,
    outcome: RunResult,
    *,
    non_interactive: bool,
    pending_step_prompt: str | None,
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
            answer = _read_nonempty("Answer (/abort to stop)> ")
            if answer is None:
                _report_input_required(outcome.session, "a human answer")
                return EXIT_INPUT_REQUIRED
            if answer in _ABORT_COMMANDS:
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
    return _read_nonempty("Task> ")


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
    return ChatGPTWebTransport(
        args.cookies,
        verbose=args.verbose,
        headless=not args.headed,
        response_timeout=args.timeout,
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


def _report_input_required(session: Session, requirement: str) -> None:
    print(
        f"Input required: session {session.id} is waiting for {requirement}.",
        file=sys.stderr,
    )


def _report_error(error: Exception) -> None:
    code = getattr(error, "code", error.__class__.__name__)
    print(f"Error [{code}]: {error}", file=sys.stderr)


def _normalize_legacy_args(argv: Sequence[str]) -> list[str]:
    if not argv or argv[0] in _COMMANDS or argv[0] in {"-h", "--help"}:
        return list(argv)
    return ["chat", *argv]


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
