"""Typed, side-effect-free AEML protocol objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias


class Root(str, Enum):
    OUTPUT = "output"
    INPUT = "input"


class NextDirective(str, Enum):
    PROCEED = "proceed"
    AWAIT_RESULT = "await_result"
    AWAIT_USER = "await_user"
    DONE = "done"
    ABORT = "abort"


class ResultStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    TIMEOUT = "timeout"


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    EXECUTING = "executing"


class Confirmation(str, Enum):
    NONE = "none"
    CONDITIONAL = "conditional"
    ALWAYS = "always"


class ArgumentKind(str, Enum):
    TEXT = "text"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ENUM = "enum"
    PATH = "path"


@dataclass(frozen=True, slots=True)
class PathRef:
    value: str
    root: Root = Root.OUTPUT


@dataclass(frozen=True, slots=True)
class Argument:
    name: str
    value: str
    attributes: tuple[tuple[str, str], ...] = ()

    def attribute(self, name: str) -> str | None:
        for key, value in self.attributes:
            if key == name:
                return value
        return None


@dataclass(frozen=True, slots=True)
class Chunk:
    seq: int
    final: bool


@dataclass(frozen=True, slots=True)
class Action:
    id: str
    tool: str
    path: PathRef | None = None
    arguments: tuple[Argument, ...] = ()
    chunk: Chunk | None = None
    expect_confirm: bool | None = None

    def argument(self, name: str) -> Argument | None:
        for argument in self.arguments:
            if argument.name == name:
                return argument
        return None


@dataclass(frozen=True, slots=True)
class AEMLMessage:
    turn: int
    session: str
    plan: str | None = None
    thought: str | None = None
    actions: tuple[Action, ...] = ()
    say: str | None = None
    ask_user: str | None = None
    next: NextDirective | None = None
    complete: str | None = None


@dataclass(frozen=True, slots=True)
class Environment:
    output_root: str
    input_root: str
    cwd: str
    status: str | None = None


@dataclass(frozen=True, slots=True)
class Truncation:
    total_bytes: int
    offset: int


@dataclass(frozen=True, slots=True)
class Result:
    action_id: str
    status: ResultStatus
    body: str = ""
    lines: str | None = None
    truncation: Truncation | None = None


@dataclass(frozen=True, slots=True)
class ProtocolError:
    code: str
    message: str
    action_id: str | None = None


@dataclass(frozen=True, slots=True)
class SystemNotice:
    type: str
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ResultSummary:
    action_id: str
    tool: str
    status: ResultStatus
    preview: str = ""


@dataclass(frozen=True, slots=True)
class AEMLContext:
    turn: int
    session: str
    environment: Environment
    step: int
    max_steps: int
    user_prompt: str | None = None
    results: tuple[Result, ...] = ()
    errors: tuple[ProtocolError, ...] = ()
    notices: tuple[SystemNotice, ...] = ()
    plan: str | None = None
    summaries: tuple[ResultSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    name: str
    kind: ArgumentKind = ArgumentKind.TEXT
    required: bool = False
    allow_empty: bool = False
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    allowed_roots: frozenset[Root] = field(default_factory=frozenset)
    write_target: bool = False
    allow_base64: bool = False


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    effect: ToolEffect
    arguments: tuple[ArgumentSpec, ...] = ()
    path_required: bool = False
    path_allowed: bool = False
    path_roots: frozenset[Root] = field(default_factory=frozenset)
    path_write_target: bool = False
    supports_chunk: bool = False
    confirmation: Confirmation = Confirmation.NONE

    def argument(self, name: str) -> ArgumentSpec | None:
        for argument in self.arguments:
            if argument.name == name:
                return argument
        return None


TypedValue: TypeAlias = str | int | bool | PathRef


@dataclass(frozen=True, slots=True)
class TypedArgument:
    name: str
    value: TypedValue


@dataclass(frozen=True, slots=True)
class ValidatedAction:
    source: Action
    spec: ToolSpec
    arguments: tuple[TypedArgument, ...]

    def argument(self, name: str) -> TypedValue | None:
        for argument in self.arguments:
            if argument.name == name:
                return argument.value
        return None


@dataclass(frozen=True, slots=True)
class ValidatedMessage:
    source: AEMLMessage
    actions: tuple[ValidatedAction, ...]
