"""Allowlisted dispatchers for read, output mutation, and sandbox capabilities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias

from swoon.aeml.models import (
    AEMLMessage,
    NextDirective,
    ProtocolError,
    PathRef,
    Result,
    Root,
    ToolEffect,
    ValidatedAction,
    ValidatedMessage,
)
from swoon.aeml.errors import AEMLValidationError
from swoon.aeml.tool_registry import TOOL_SPECS
from swoon.aeml.validator import AEMLValidator
from swoon.policy import PathPolicy, PathPolicyError
from swoon.session import Session, SessionError, SessionManager, SessionStatus

from .dependencies import DependencyReadTools
from .commands import ForegroundCommandTools
from .errors import ToolExecutionError
from .filesystem import FilesystemReadTools
from .git import GitReadTools
from .models import CommandToolLimits, MutationToolLimits, ReadToolLimits
from .mutations import FilesystemMutationTools


ToolResponse: TypeAlias = Result | ProtocolError
IMPLEMENTED_READ_TOOLS = frozenset(
    {
        "read-file",
        "list-dir",
        "grep",
        "git-status",
        "git-diff",
        "git-log",
        "list-dependencies",
    }
)
IMPLEMENTED_MUTATION_TOOLS = frozenset(
    {
        "create-file",
        "overwrite-file",
        "append-file",
        "edit-file",
        "copy-file",
        "copy-dir",
    }
)
IMPLEMENTED_EXECUTION_TOOLS = frozenset(
    {
        "run-command",
        "run-build",
        "run-tests",
        "run-linter",
    }
)
IMPLEMENTED_AGENT_TOOLS = (
    IMPLEMENTED_READ_TOOLS | IMPLEMENTED_MUTATION_TOOLS | IMPLEMENTED_EXECUTION_TOOLS
)


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    action_id: str
    tool: str
    reason: str
    guard: str


class ReadOnlyToolDispatcher:
    """Execute only validated, explicitly allowlisted read operations."""

    implemented_tools = IMPLEMENTED_READ_TOOLS
    allowed_effects = frozenset({ToolEffect.READ_ONLY})

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        limits: ReadToolLimits | None = None,
        git_binary: str | Path | None = None,
    ) -> None:
        self.session_manager = session_manager
        self.limits = limits or ReadToolLimits()
        self.git_binary = git_binary

    @property
    def tool_specs(self):
        return MappingProxyType(
            {name: TOOL_SPECS[name] for name in TOOL_SPECS if name in self.implemented_tools}
        )

    def execute(
        self,
        action: ValidatedAction,
        session: Session,
        *,
        confirmed: bool = False,
    ) -> ToolResponse:
        if type(confirmed) is not bool:
            return ProtocolError("invalid_confirmation", "confirmed must be boolean")
        boundary_error = self._validate_boundary(action, session, confirmed=confirmed)
        if boundary_error is not None:
            return boundary_error

        try:
            action_digest = self.action_digest(action)
        except (AttributeError, TypeError, ValueError):
            return ProtocolError(
                "invalid_validated_action",
                "Validated action contains non-canonical values",
                action.source.id,
            )
        cached = session.state.action(action.source.id)
        if cached is not None:
            if cached.tool == action.spec.name and cached.action_digest == action_digest:
                return cached.result
            return ProtocolError(
                "duplicate_action_id",
                f"Action ID {action.source.id!r} is already associated with another tool",
                action.source.id,
            )

        incomplete = self._incomplete_dependency(action, session)
        if incomplete is not None:
            return ProtocolError("write_incomplete", incomplete, action.source.id)

        sequence_error = self._chunk_sequence_error(action, session)
        if sequence_error is not None:
            return ProtocolError("chunk_sequence_error", sequence_error, action.source.id)

        confirmation = self._runtime_confirmation(action, session)
        if isinstance(confirmation, ProtocolError):
            return confirmation
        if confirmation is not None:
            if action.source.expect_confirm is not True:
                return ProtocolError(
                    "confirmation_required",
                    (
                        f"{confirmation.reason}; resubmit with "
                        "<expect_confirm>true</expect_confirm>"
                    ),
                    action.source.id,
                )
            if not confirmed:
                return ProtocolError(
                    "confirmation_required",
                    f"Human confirmation is required: {confirmation.reason}",
                    action.source.id,
                )
            pending = session.state.pending_confirmation
            if (
                pending is not None
                and pending.action == action.source
                and pending.guard != confirmation.guard
            ):
                return ProtocolError(
                    "confirmation_stale",
                    "The overwrite target changed after confirmation was requested",
                    action.source.id,
                )

        policy = PathPolicy(session.paths)
        handler = self._handler(action, policy, confirmed=confirmed)
        if isinstance(handler, ProtocolError):
            return handler

        last_error: ToolExecutionError | None = None
        attempts = 2 if action.spec.effect is ToolEffect.READ_ONLY else 1
        for attempt in range(attempts):
            try:
                result = handler(action)
                chunk = action.source.chunk
                pending = session.state.pending_confirmation
                self.session_manager.record_action_result(
                    session,
                    action.spec.name,
                    result,
                    action_digest=action_digest,
                    chunk_path=action.source.path if chunk is not None else None,
                    chunk_seq=chunk.seq if chunk is not None else None,
                    chunk_final=chunk.final if chunk is not None else None,
                    resolve_confirmation=(
                        confirmed
                        and pending is not None
                        and pending.action.id == action.source.id
                    ),
                )
                return result
            except PathPolicyError as error:
                last_error = ToolExecutionError(
                    error.code,
                    str(error),
                    retryable=error.code in {"path_changed", "path_unavailable"},
                )
            except ToolExecutionError as error:
                last_error = error
            except SessionError as error:
                return ProtocolError(error.code, str(error), action.source.id)
            except OSError as error:
                last_error = ToolExecutionError(
                    "tool_failed",
                    f"Tool operation failed ({error.__class__.__name__})",
                    retryable=True,
                )
            except Exception as error:
                return ProtocolError(
                    "tool_failed",
                    f"Tool failed unexpectedly ({error.__class__.__name__})",
                    action.source.id,
                )
            if last_error is not None and (
                not last_error.retryable or attempt == attempts - 1
            ):
                break

        assert last_error is not None
        return ProtocolError(last_error.code, str(last_error), action.source.id)

    def execute_message(
        self,
        message: ValidatedMessage,
        session: Session,
    ) -> tuple[ToolResponse, ...]:
        if not isinstance(message, ValidatedMessage):
            return (ProtocolError("invalid_validated_message", "ValidatedMessage is required"),)
        return tuple(self.execute(action, session) for action in message.actions)

    def confirmation_request(
        self,
        action: ValidatedAction,
        session: Session,
    ) -> ConfirmationRequest | ProtocolError | None:
        """Preflight an active action without executing it."""

        boundary_error = self._validate_boundary(action, session, confirmed=False)
        if boundary_error is not None:
            return boundary_error
        incomplete = self._incomplete_dependency(action, session)
        if incomplete is not None:
            return ProtocolError("write_incomplete", incomplete, action.source.id)
        sequence_error = self._chunk_sequence_error(action, session)
        if sequence_error is not None:
            return ProtocolError("chunk_sequence_error", sequence_error, action.source.id)
        request = self._runtime_confirmation(action, session)
        if request is not None and not isinstance(request, ProtocolError):
            if action.source.expect_confirm is not True:
                return ProtocolError(
                    "confirmation_required",
                    (
                        f"{request.reason}; resubmit with "
                        "<expect_confirm>true</expect_confirm>"
                    ),
                    action.source.id,
                )
        return request

    def _handler(
        self,
        action: ValidatedAction,
        policy: PathPolicy,
        *,
        confirmed: bool,
    ):
        del confirmed
        filesystem = FilesystemReadTools(policy, self.limits)
        if action.spec.name == "read-file":
            return filesystem.read_file
        if action.spec.name == "list-dir":
            return filesystem.list_dir
        if action.spec.name == "grep":
            return filesystem.grep
        if action.spec.name == "list-dependencies":
            return DependencyReadTools(policy, self.limits).list_dependencies
        if action.spec.name in {"git-status", "git-diff", "git-log"}:
            try:
                git = GitReadTools(
                    policy,
                    self.limits,
                    git_binary=self.git_binary,
                )
            except ToolExecutionError as error:
                return ProtocolError(error.code, str(error), action.source.id)
            return {
                "git-status": git.status,
                "git-diff": git.diff,
                "git-log": git.log,
            }[action.spec.name]
        return ProtocolError(
            "unsupported_read_tool",
            f"Read tool {action.spec.name!r} is not implemented in this phase",
            action.source.id,
        )

    def _validate_boundary(
        self,
        action: ValidatedAction,
        session: Session,
        *,
        confirmed: bool,
    ) -> ProtocolError | None:
        if not isinstance(action, ValidatedAction):
            return ProtocolError("invalid_validated_action", "ValidatedAction is required")
        if not isinstance(session, Session):
            return ProtocolError(
                "invalid_session",
                "A managed Session is required",
                action.source.id,
            )
        try:
            expected_paths = self.session_manager.paths(session.id)
        except SessionError as error:
            return ProtocolError(error.code, str(error), action.source.id)
        if session.paths != expected_paths:
            return ProtocolError(
                "session_integrity_error",
                "Session belongs to another SessionManager",
                action.source.id,
            )
        canonical = TOOL_SPECS.get(action.source.tool)
        if canonical is None or action.spec != canonical or action.spec.name != action.source.tool:
            return ProtocolError(
                "invalid_validated_action",
                "Validated action does not match the canonical tool schema",
                action.source.id,
            )
        try:
            reconstructed = AEMLValidator({canonical.name: canonical}).validate(
                AEMLMessage(
                    turn=1,
                    session=session.id,
                    actions=(action.source,),
                    next=NextDirective.AWAIT_RESULT,
                ),
                expected_turn=1,
                expected_session=session.id,
            )
        except AEMLValidationError:
            return ProtocolError(
                "invalid_validated_action",
                "Validated action source no longer passes the canonical schema",
                action.source.id,
            )
        if reconstructed.actions != (action,):
            return ProtocolError(
                "invalid_validated_action",
                "Validated action values do not match their source action",
                action.source.id,
            )
        if action.spec.effect not in self.allowed_effects:
            return ProtocolError(
                "write_tool_disabled",
                "This dispatcher does not enable the requested tool effect",
                action.source.id,
            )
        if action.spec.name not in self.implemented_tools:
            code = (
                "unsupported_read_tool"
                if action.spec.effect is ToolEffect.READ_ONLY
                else "unsupported_tool"
            )
            return ProtocolError(
                code,
                f"Tool {action.spec.name!r} is not implemented by this dispatcher",
                action.source.id,
            )
        pending = session.state.pending_confirmation
        pending_match = (
            confirmed
            and pending is not None
            and pending.action == action.source
            and session.state.status is SessionStatus.WAITING_USER
        )
        if session.state.status is not SessionStatus.ACTIVE and not pending_match:
            return ProtocolError(
                "session_not_active",
                f"Session is {session.state.status.value}, not active",
                action.source.id,
            )
        return None

    @staticmethod
    def _incomplete_dependency(action: ValidatedAction, session: Session) -> str | None:
        pending = [
            record.path
            for record in session.state.chunks
            if not record.finalized and record.path.root is Root.OUTPUT
        ]
        if not pending:
            return None

        if action.spec.name == "read-file":
            path = action.source.path
            if path is not None and path.root is Root.OUTPUT and path in pending:
                return f"{path.value!r} has an unfinished chunk sequence"
            return None
        if action.spec.name == "grep":
            scope = action.source.path
            if scope is None or scope.root is not Root.OUTPUT:
                return None
            scope_parts = () if scope.value == "." else tuple(scope.value.split("/"))
            for path in pending:
                path_parts = tuple(path.value.split("/"))
                if path_parts[: len(scope_parts)] == scope_parts:
                    return "grep scope contains an unfinished chunk sequence"
            return None
        if action.spec.name in {"git-diff", "list-dependencies"}:
            return f"{action.spec.name} is blocked by an unfinished output write"
        if action.spec.name in IMPLEMENTED_EXECUTION_TOOLS:
            return f"{action.spec.name} is blocked by an unfinished output write"
        if action.spec.name in {"create-file", "overwrite-file", "edit-file"}:
            path = action.source.path
            if path is not None and path in pending:
                return f"{path.value!r} has an unfinished chunk sequence"
            return None
        if action.spec.name == "append-file":
            return None
        if action.spec.name == "copy-file":
            source = action.argument("from")
            target = action.argument("to")
            for value in (source, target):
                if isinstance(value, PathRef) and value.root is Root.OUTPUT and value in pending:
                    return f"{value.value!r} has an unfinished chunk sequence"
            return None
        if action.spec.name == "copy-dir":
            source = action.argument("from")
            target = action.argument("to")
            for value in (source, target):
                if not isinstance(value, PathRef) or value.root is not Root.OUTPUT:
                    continue
                scope = () if value.value == "." else tuple(value.value.split("/"))
                for path in pending:
                    parts = tuple(path.value.split("/"))
                    if parts[: len(scope)] == scope:
                        return "copy-dir scope contains an unfinished chunk sequence"
        return None

    @staticmethod
    def _chunk_sequence_error(action: ValidatedAction, session: Session) -> str | None:
        if action.spec.name not in {"create-file", "overwrite-file", "append-file"}:
            return None
        path = action.source.path
        if path is None:
            return "Chunk-capable write is missing its path"
        chunk = action.source.chunk
        existing = session.state.chunk(path)
        if action.spec.name in {"create-file", "overwrite-file"}:
            if chunk is not None and existing is not None:
                return "A chunk sequence for this path already exists"
            return None
        if existing is not None and not existing.finalized:
            if chunk is None:
                return "An unfinished chunk must continue with explicit sequence metadata"
            if chunk.seq != existing.next_seq:
                return f"Expected chunk sequence {existing.next_seq}, received {chunk.seq}"
            return None
        if chunk is not None:
            if existing is None:
                return "append-file chunking requires an existing sequence"
            return "Chunk sequence is already finalized"
        return None

    def _runtime_confirmation(
        self,
        action: ValidatedAction,
        session: Session,
    ) -> ConfirmationRequest | ProtocolError | None:
        del session
        return None

    @staticmethod
    def action_digest(action: ValidatedAction) -> str:
        def value(item):
            if isinstance(item, PathRef):
                return {"type": "path", "root": item.root.value, "value": item.value}
            return item

        source = action.source
        payload = {
            "tool": action.spec.name,
            "path": None
            if source.path is None
            else {"root": source.path.root.value, "value": source.path.value},
            "arguments": [
                {"name": argument.name, "value": value(argument.value)}
                for argument in action.arguments
            ],
            "chunk": None
            if source.chunk is None
            else {"seq": source.chunk.seq, "final": source.chunk.final},
            "expect_confirm": source.expect_confirm,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class AgentToolDispatcher(ReadOnlyToolDispatcher):
    """Execute reads, output mutations, and disposable foreground commands."""

    implemented_tools = IMPLEMENTED_AGENT_TOOLS
    allowed_effects = frozenset(
        {ToolEffect.READ_ONLY, ToolEffect.MUTATING, ToolEffect.EXECUTING}
    )

    def __init__(
        self,
        session_manager: SessionManager,
        *,
        limits: ReadToolLimits | None = None,
        mutation_limits: MutationToolLimits | None = None,
        command_limits: CommandToolLimits | None = None,
        git_binary: str | Path | None = None,
        sandbox_binary: str | Path | None = None,
        resource_limiter_binary: str | Path | None = None,
        sandbox_python_binary: str | Path | None = None,
    ) -> None:
        super().__init__(session_manager, limits=limits, git_binary=git_binary)
        self.mutation_limits = mutation_limits or MutationToolLimits()
        self.command_limits = command_limits or CommandToolLimits()
        self.sandbox_binary = sandbox_binary
        self.resource_limiter_binary = resource_limiter_binary
        self.sandbox_python_binary = sandbox_python_binary

    def _handler(
        self,
        action: ValidatedAction,
        policy: PathPolicy,
        *,
        confirmed: bool,
    ):
        if action.spec.name in IMPLEMENTED_MUTATION_TOOLS:
            filesystem = FilesystemMutationTools(policy, self.mutation_limits)
            return {
                "create-file": filesystem.create_file,
                "overwrite-file": lambda selected: filesystem.overwrite_file(
                    selected,
                    allow_nonempty=confirmed,
                ),
                "append-file": filesystem.append_file,
                "edit-file": filesystem.edit_file,
                "copy-file": filesystem.copy_file,
                "copy-dir": filesystem.copy_directory,
            }[action.spec.name]
        if action.spec.name in IMPLEMENTED_EXECUTION_TOOLS:
            commands = ForegroundCommandTools(
                policy,
                self.command_limits,
                sandbox_binary=self.sandbox_binary,
                resource_limiter_binary=self.resource_limiter_binary,
                python_binary=self.sandbox_python_binary,
            )
            return {
                "run-command": commands.run_command,
                "run-build": commands.run_build,
                "run-tests": commands.run_tests,
                "run-linter": commands.run_linter,
            }[action.spec.name]
        return super()._handler(action, policy, confirmed=confirmed)

    def _runtime_confirmation(
        self,
        action: ValidatedAction,
        session: Session,
    ) -> ConfirmationRequest | ProtocolError | None:
        if action.spec.name != "overwrite-file":
            return None
        try:
            reason, guard = FilesystemMutationTools(
                PathPolicy(session.paths),
                self.mutation_limits,
            ).overwrite_confirmation_details(action)
        except (PathPolicyError, ToolExecutionError) as error:
            code = getattr(error, "code", "tool_failed")
            return ProtocolError(code, str(error), action.source.id)
        pending = session.state.pending_confirmation
        if reason is None and not (
            pending is not None and pending.action == action.source
        ):
            return None
        return ConfirmationRequest(
            action.source.id,
            action.spec.name,
            reason or "overwrite-file target snapshot requires revalidation",
            guard,
        )
