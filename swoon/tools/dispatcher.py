"""Allowlisted dispatcher for Phase 7's read-only capabilities."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypeAlias

from swoon.aeml.models import (
    ProtocolError,
    PathRef,
    Result,
    Root,
    ToolEffect,
    ValidatedAction,
    ValidatedMessage,
)
from swoon.aeml.tool_registry import TOOL_SPECS
from swoon.policy import PathPolicy, PathPolicyError
from swoon.session import Session, SessionError, SessionManager, SessionStatus

from .dependencies import DependencyReadTools
from .errors import ToolExecutionError
from .filesystem import FilesystemReadTools
from .git import GitReadTools
from .models import ReadToolLimits


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


class ReadOnlyToolDispatcher:
    """Execute only validated, explicitly allowlisted read operations."""

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

    def execute(self, action: ValidatedAction, session: Session) -> ToolResponse:
        boundary_error = self._validate_boundary(action, session)
        if boundary_error is not None:
            return boundary_error

        try:
            action_digest = self._action_digest(action)
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

        policy = PathPolicy(session.paths)
        handler = self._handler(action, policy)
        if isinstance(handler, ProtocolError):
            return handler

        last_error: ToolExecutionError | None = None
        for attempt in range(2):
            try:
                result = handler(action)
                self.session_manager.record_action_result(
                    session,
                    action.spec.name,
                    result,
                    action_digest=action_digest,
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
                    f"Read operation failed ({error.__class__.__name__})",
                    retryable=True,
                )
            except Exception as error:
                return ProtocolError(
                    "tool_failed",
                    f"Read tool failed unexpectedly ({error.__class__.__name__})",
                    action.source.id,
                )
            if last_error is not None and (not last_error.retryable or attempt == 1):
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

    def _handler(self, action: ValidatedAction, policy: PathPolicy):
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

    @staticmethod
    def _validate_boundary(
        action: ValidatedAction,
        session: Session,
    ) -> ProtocolError | None:
        if not isinstance(action, ValidatedAction):
            return ProtocolError("invalid_validated_action", "ValidatedAction is required")
        if not isinstance(session, Session):
            return ProtocolError(
                "invalid_session",
                "A managed Session is required",
                action.source.id,
            )
        canonical = TOOL_SPECS.get(action.source.tool)
        if canonical is None or action.spec != canonical or action.spec.name != action.source.tool:
            return ProtocolError(
                "invalid_validated_action",
                "Validated action does not match the canonical tool schema",
                action.source.id,
            )
        if action.spec.effect is not ToolEffect.READ_ONLY:
            return ProtocolError(
                "write_tool_disabled",
                "Mutating and executing tools are disabled in the read-only phase",
                action.source.id,
            )
        if action.spec.name not in IMPLEMENTED_READ_TOOLS:
            return ProtocolError(
                "unsupported_read_tool",
                f"Read tool {action.spec.name!r} is not implemented in this phase",
                action.source.id,
            )
        if session.state.status is not SessionStatus.ACTIVE:
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
        return None

    @staticmethod
    def _action_digest(action: ValidatedAction) -> str:
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
