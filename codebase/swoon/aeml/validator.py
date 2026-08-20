"""Semantic validation for parsed AEML messages.

The validator is pure: it performs no filesystem access and executes no tools.
Runtime policies such as resolved-path containment and conditional confirmation
are deliberately left to later policy phases.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping

from .errors import AEMLValidationError
from .models import (
    AEMLMessage,
    Action,
    Argument,
    ArgumentKind,
    ArgumentSpec,
    Confirmation,
    NextDirective,
    PathRef,
    Root,
    ToolEffect,
    ToolSpec,
    TypedArgument,
    TypedValue,
    ValidatedAction,
    ValidatedMessage,
)
from .tool_registry import TOOL_SPECS


_SESSION_ID = re.compile(r"sess_[A-Za-z0-9_-]{1,64}\Z")
_ACTION_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_INTEGER = re.compile(r"-?[0-9]+\Z")
_CHMOD_MODE = re.compile(r"0?(?:600|700)\Z")


class AEMLValidator:
    """Validate protocol invariants and tool-specific argument schemas."""

    def __init__(self, tool_specs: Mapping[str, ToolSpec] = TOOL_SPECS) -> None:
        self.tool_specs = tool_specs

    def validate(
        self,
        message: AEMLMessage,
        *,
        expected_turn: int | None = None,
        expected_session: str | None = None,
        known_action_ids: Collection[str] = (),
    ) -> ValidatedMessage:
        self._validate_envelope(
            message,
            expected_turn=expected_turn,
            expected_session=expected_session,
        )
        self._validate_control_flow(message)

        seen = set(known_action_ids)
        validated_actions: list[ValidatedAction] = []
        for action in message.actions:
            if not _ACTION_ID.fullmatch(action.id):
                raise AEMLValidationError(
                    "invalid_action_id",
                    f"Invalid action id {action.id!r}",
                    action_id=action.id,
                )
            if action.id in seen:
                raise AEMLValidationError(
                    "duplicate_action_id",
                    f"Action id {action.id!r} has already been used in this session",
                    action_id=action.id,
                )
            seen.add(action.id)
            validated_actions.append(self._validate_action(action))

        if len(validated_actions) > 1 and any(
            item.spec.effect is not ToolEffect.READ_ONLY for item in validated_actions
        ):
            raise AEMLValidationError(
                "batch_write_not_allowed",
                "Multiple actions may be batched only when every action is read-only",
            )

        return ValidatedMessage(source=message, actions=tuple(validated_actions))

    @staticmethod
    def _validate_envelope(
        message: AEMLMessage,
        *,
        expected_turn: int | None,
        expected_session: str | None,
    ) -> None:
        if not _SESSION_ID.fullmatch(message.session):
            raise AEMLValidationError(
                "invalid_session",
                "Session must match sess_[A-Za-z0-9_-]{1,64}",
            )
        if expected_turn is not None and message.turn != expected_turn:
            raise AEMLValidationError(
                "turn_mismatch",
                f"Expected turn {expected_turn}, received turn {message.turn}",
            )
        if expected_session is not None and message.session != expected_session:
            raise AEMLValidationError(
                "session_mismatch",
                f"Expected session {expected_session!r}, received {message.session!r}",
            )

    @staticmethod
    def _validate_control_flow(message: AEMLMessage) -> None:
        if message.complete is not None:
            if not message.complete.strip():
                raise AEMLValidationError("invalid_complete", "<complete> cannot be empty")
            if message.actions or message.ask_user is not None or message.next is not None:
                raise AEMLValidationError(
                    "invalid_complete",
                    "A completion turn cannot contain actions, <ask_user>, or <next>",
                )
            if message.say is not None:
                raise AEMLValidationError(
                    "invalid_complete",
                    "Use <complete> as the closing human-facing message instead of <say>",
                )
            return

        if message.next is None:
            raise AEMLValidationError(
                "missing_next",
                "Every non-completion turn must contain <next>",
            )
        if message.ask_user is not None:
            if not message.ask_user.strip():
                raise AEMLValidationError("invalid_ask_user", "<ask_user> cannot be empty")
            if message.actions:
                raise AEMLValidationError(
                    "invalid_control_flow",
                    "<ask_user> cannot be combined with actions",
                )
            if message.next is not NextDirective.AWAIT_USER:
                raise AEMLValidationError(
                    "invalid_next",
                    "A turn containing <ask_user> must use <next>await_user</next>",
                )
        elif message.actions and message.next is not NextDirective.AWAIT_RESULT:
            raise AEMLValidationError(
                "invalid_next",
                "A turn containing actions must use <next>await_result</next>",
            )
        elif message.next is NextDirective.AWAIT_RESULT and not message.actions:
            raise AEMLValidationError(
                "invalid_next",
                "<next>await_result</next> requires at least one action",
            )
        elif message.next is NextDirective.AWAIT_USER:
            raise AEMLValidationError(
                "invalid_next",
                "<next>await_user</next> requires <ask_user>",
            )

        if message.say is not None and not message.say.strip():
            raise AEMLValidationError("invalid_say", "<say> cannot be empty")

    def _validate_action(self, action: Action) -> ValidatedAction:
        spec = self.tool_specs.get(action.tool)
        if spec is None:
            valid = ", ".join(sorted(self.tool_specs))
            raise AEMLValidationError(
                "unknown_tool",
                f"Unknown tool {action.tool!r}; valid tools: {valid}",
                action_id=action.id,
            )

        self._validate_direct_path(action, spec)
        arguments = self._validate_arguments(action, spec)
        self._validate_cross_field_rules(action, spec, arguments)
        self._validate_chunk(action, spec)

        if spec.confirmation is Confirmation.ALWAYS and action.expect_confirm is not True:
            raise AEMLValidationError(
                "confirmation_required",
                f"Tool {spec.name!r} must declare <expect_confirm>true</expect_confirm>",
                action_id=action.id,
            )

        return ValidatedAction(source=action, spec=spec, arguments=arguments)

    @staticmethod
    def _validate_direct_path(action: Action, spec: ToolSpec) -> None:
        if spec.path_required and action.path is None:
            raise AEMLValidationError(
                "missing_path",
                f"Tool {spec.name!r} requires <path>",
                action_id=action.id,
            )
        if action.path is not None and not spec.path_allowed:
            raise AEMLValidationError(
                "unexpected_path",
                f"Tool {spec.name!r} does not accept <path>",
                action_id=action.id,
            )
        if action.path is None:
            return
        if not action.path.value.strip():
            raise AEMLValidationError("invalid_path", "Path cannot be empty", action_id=action.id)
        if spec.path_write_target and action.path.root is Root.INPUT:
            raise AEMLValidationError(
                "input_readonly",
                "The input root is read-only",
                action_id=action.id,
            )
        if action.path.root not in spec.path_roots:
            allowed = ", ".join(root.value for root in sorted(spec.path_roots, key=lambda item: item.value))
            raise AEMLValidationError(
                "invalid_root",
                f"Tool {spec.name!r} path root must be one of: {allowed}",
                action_id=action.id,
            )

    def _validate_arguments(
        self,
        action: Action,
        spec: ToolSpec,
    ) -> tuple[TypedArgument, ...]:
        raw_by_name: dict[str, Argument] = {}
        for argument in action.arguments:
            if argument.name in raw_by_name:
                raise AEMLValidationError(
                    "duplicate_argument",
                    f"Argument {argument.name!r} appears more than once",
                    action_id=action.id,
                )
            raw_by_name[argument.name] = argument

        unknown = set(raw_by_name) - {item.name for item in spec.arguments}
        if unknown:
            names = ", ".join(sorted(unknown))
            raise AEMLValidationError(
                "unknown_argument",
                f"Tool {spec.name!r} received unknown argument(s): {names}",
                action_id=action.id,
            )

        typed: list[TypedArgument] = []
        for argument_spec in spec.arguments:
            argument = raw_by_name.get(argument_spec.name)
            if argument is None:
                if argument_spec.required:
                    raise AEMLValidationError(
                        "missing_argument",
                        f"Tool {spec.name!r} requires <{argument_spec.name}>",
                        action_id=action.id,
                    )
                continue
            typed.append(
                TypedArgument(
                    name=argument.name,
                    value=self._convert_argument(action, argument, argument_spec),
                )
            )
        return tuple(typed)

    def _convert_argument(
        self,
        action: Action,
        argument: Argument,
        spec: ArgumentSpec,
    ) -> TypedValue:
        attributes = dict(argument.attributes)
        if spec.kind is not ArgumentKind.PATH and attributes:
            names = ", ".join(sorted(attributes))
            raise AEMLValidationError(
                "invalid_argument",
                f"Argument {argument.name!r} does not accept attribute(s): {names}",
                action_id=action.id,
            )

        if spec.kind is ArgumentKind.TEXT:
            if not spec.allow_empty and not argument.value.strip():
                raise AEMLValidationError(
                    "invalid_argument",
                    f"Argument {argument.name!r} cannot be empty",
                    action_id=action.id,
                )
            return argument.value

        value = argument.value.strip()
        if spec.kind is ArgumentKind.INTEGER:
            if not _INTEGER.fullmatch(value):
                raise AEMLValidationError(
                    "invalid_argument",
                    f"Argument {argument.name!r} must be an integer",
                    action_id=action.id,
                )
            number = int(value)
            if spec.minimum is not None and number < spec.minimum:
                raise AEMLValidationError(
                    "invalid_argument",
                    f"Argument {argument.name!r} must be at least {spec.minimum}",
                    action_id=action.id,
                )
            if spec.maximum is not None and number > spec.maximum:
                raise AEMLValidationError(
                    "invalid_argument",
                    f"Argument {argument.name!r} must be at most {spec.maximum}",
                    action_id=action.id,
                )
            return number

        if spec.kind is ArgumentKind.BOOLEAN:
            normalized = value.lower()
            if normalized not in {"true", "false"}:
                raise AEMLValidationError(
                    "invalid_argument",
                    f"Argument {argument.name!r} must be true or false",
                    action_id=action.id,
                )
            return normalized == "true"

        if spec.kind is ArgumentKind.ENUM:
            if value not in spec.choices:
                choices = ", ".join(spec.choices)
                raise AEMLValidationError(
                    "invalid_argument",
                    f"Argument {argument.name!r} must be one of: {choices}",
                    action_id=action.id,
                )
            return value

        if spec.kind is ArgumentKind.PATH:
            unknown_attributes = set(attributes) - {"root"}
            if unknown_attributes:
                names = ", ".join(sorted(unknown_attributes))
                raise AEMLValidationError(
                    "invalid_argument",
                    f"Path argument {argument.name!r} has unknown attribute(s): {names}",
                    action_id=action.id,
                )
            try:
                root = Root(attributes.get("root", Root.OUTPUT.value))
            except ValueError as error:
                raise AEMLValidationError(
                    "invalid_root",
                    f"Path argument {argument.name!r} root must be output or input",
                    action_id=action.id,
                ) from error
            if not value:
                raise AEMLValidationError(
                    "invalid_path",
                    f"Path argument {argument.name!r} cannot be empty",
                    action_id=action.id,
                )
            if spec.write_target and root is Root.INPUT:
                raise AEMLValidationError(
                    "input_readonly",
                    "The input root is read-only",
                    action_id=action.id,
                )
            if root not in spec.allowed_roots:
                allowed = ", ".join(
                    item.value for item in sorted(spec.allowed_roots, key=lambda item: item.value)
                )
                raise AEMLValidationError(
                    "invalid_root",
                    f"Path argument {argument.name!r} root must be one of: {allowed}",
                    action_id=action.id,
                )
            return PathRef(value=value, root=root)

        raise AssertionError(f"Unhandled argument kind: {spec.kind}")

    @staticmethod
    def _validate_cross_field_rules(
        action: Action,
        spec: ToolSpec,
        arguments: tuple[TypedArgument, ...],
    ) -> None:
        values = {argument.name: argument.value for argument in arguments}
        if spec.name == "read-file":
            start = values.get("start_line")
            end = values.get("end_line")
            if isinstance(start, int) and isinstance(end, int) and start > end:
                raise AEMLValidationError(
                    "invalid_argument",
                    "start_line cannot be greater than end_line",
                    action_id=action.id,
                )
        if spec.name == "chmod":
            mode = values.get("mode")
            if not isinstance(mode, str) or not _CHMOD_MODE.fullmatch(mode.strip()):
                raise AEMLValidationError(
                    "invalid_argument",
                    "chmod mode must be owner-private 600 or 700",
                    action_id=action.id,
                )

    @staticmethod
    def _validate_chunk(action: Action, spec: ToolSpec) -> None:
        if action.chunk is None:
            return
        if not spec.supports_chunk:
            raise AEMLValidationError(
                "chunk_not_supported",
                f"Tool {spec.name!r} does not support chunking",
                action_id=action.id,
            )
        if spec.name in {"create-file", "overwrite-file"} and action.chunk.seq != 1:
            raise AEMLValidationError(
                "chunk_sequence_error",
                f"{spec.name} must start a chunk sequence at seq=1",
                action_id=action.id,
            )
        if spec.name == "append-file" and action.chunk.seq < 2:
            raise AEMLValidationError(
                "chunk_sequence_error",
                "append-file chunk continuations must use seq>=2",
                action_id=action.id,
            )
