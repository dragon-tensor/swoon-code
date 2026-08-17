"""Persistent session records and their strict JSON representation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from swoon.aeml.models import (
    Action,
    Argument,
    Chunk,
    PathRef,
    Result,
    ResultStatus,
    Root,
    Truncation,
)

from .errors import SessionError


STATE_VERSION = 4
SUPPORTED_STATE_VERSIONS = frozenset({1, 2, 3, STATE_VERSION})
SESSION_ID_PATTERN = re.compile(r"sess_[A-Za-z0-9_-]{1,64}\Z")
ACTION_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
PROCESS_HANDLE_PATTERN = re.compile(r"proc_[A-Za-z0-9_-]{1,64}\Z")
ACTION_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
ARGUMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")


class SessionStatus(str, Enum):
    ACTIVE = "active"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    ABORTED = "aborted"


class ProcessStatus(str, Enum):
    RUNNING = "running"
    EXITED = "exited"
    KILLED = "killed"


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_files: int = 100_000
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_file_bytes: int = 512 * 1024 * 1024
    reject_hardlinks: bool = True

    def __post_init__(self) -> None:
        if self.max_files < 1 or self.max_total_bytes < 1 or self.max_file_bytes < 1:
            raise ValueError("Import limits must be positive")


@dataclass(frozen=True, slots=True)
class ActionRecord:
    action_id: str
    tool: str
    result: Result
    completed_at: datetime
    action_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    path: PathRef
    next_seq: int
    finalized: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    handle: str
    pid: int
    status: ProcessStatus
    output_offset: int
    started_at: datetime


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """One exact, validated action awaiting a real human decision."""

    action: Action
    reason: str
    guard: str
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class SessionState:
    session_id: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    revision: int = 0
    step: int = 0
    max_steps: int = 40
    plan: str | None = None
    action_ledger: tuple[ActionRecord, ...] = ()
    result_history: tuple[str, ...] = ()
    chunks: tuple[ChunkRecord, ...] = ()
    processes: tuple[ProcessRecord, ...] = ()
    used_action_ids: tuple[str, ...] = ()
    pending_confirmation: PendingConfirmation | None = None

    @property
    def step_limit_approaching(self) -> bool:
        return self.step >= max(1, (self.max_steps * 4 + 4) // 5)

    def action(self, action_id: str) -> ActionRecord | None:
        for record in self.action_ledger:
            if record.action_id == action_id:
                return record
        return None

    def chunk(self, path: PathRef) -> ChunkRecord | None:
        for record in self.chunks:
            if record.path == path:
                return record
        return None

    def process(self, handle: str) -> ProcessRecord | None:
        for record in self.processes:
            if record.handle == handle:
                return record
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "session_id": self.session_id,
            "status": self.status.value,
            "created_at": _timestamp(self.created_at),
            "updated_at": _timestamp(self.updated_at),
            "revision": self.revision,
            "step": self.step,
            "max_steps": self.max_steps,
            "plan": self.plan,
            "action_ledger": [_action_to_dict(item) for item in self.action_ledger],
            "result_history": list(self.result_history),
            "used_action_ids": list(self.used_action_ids),
            "chunks": [_chunk_to_dict(item) for item in self.chunks],
            "processes": [_process_to_dict(item) for item in self.processes],
            "pending_confirmation": (
                None
                if self.pending_confirmation is None
                else _pending_confirmation_to_dict(self.pending_confirmation)
            ),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SessionState:
        if not isinstance(raw, dict):
            raise SessionError("invalid_session_state", "Session state must be a JSON object")
        version = raw.get("version")
        if type(version) is not int or version not in SUPPORTED_STATE_VERSIONS:
            raise SessionError(
                "unsupported_session_version",
                f"Unsupported session state version {version!r}",
            )
        expected_keys = {
            "version",
            "session_id",
            "status",
            "created_at",
            "updated_at",
            "revision",
            "step",
            "max_steps",
            "plan",
            "action_ledger",
            "result_history",
            "chunks",
            "processes",
        }
        if version >= 3:
            expected_keys.add("used_action_ids")
        if version >= 4:
            expected_keys.add("pending_confirmation")
        if set(raw) != expected_keys:
            missing = sorted(expected_keys - set(raw))
            unknown = sorted(set(raw) - expected_keys)
            raise SessionError(
                "invalid_session_state",
                f"Session state keys differ (missing={missing}, unknown={unknown})",
            )
        session_id = _required_string(raw, "session_id")
        validate_session_id(session_id)
        revision = _nonnegative_integer(raw, "revision")
        step = _nonnegative_integer(raw, "step")
        max_steps = _positive_integer(raw, "max_steps")
        if max_steps > 10_000:
            raise SessionError("invalid_session_state", "max_steps exceeds 10000")
        if step > max_steps:
            raise SessionError("invalid_session_state", "Session step exceeds max_steps")
        plan = raw["plan"]
        if plan is not None and not isinstance(plan, str):
            raise SessionError("invalid_session_state", "Session plan must be text or null")

        try:
            status = SessionStatus(_required_string(raw, "status"))
        except ValueError as error:
            raise SessionError("invalid_session_state", "Unknown session status") from error

        action_values = _required_list(raw, "action_ledger")
        actions = tuple(_action_from_dict(item, version=version) for item in action_values)
        action_ids = [item.action_id for item in actions]
        if len(action_ids) != len(set(action_ids)):
            raise SessionError("invalid_session_state", "Action ledger contains duplicate IDs")

        history_values = _required_list(raw, "result_history")
        if not all(isinstance(item, str) for item in history_values):
            raise SessionError("invalid_session_state", "Result history must contain action IDs")
        if len(history_values) != len(set(history_values)):
            raise SessionError("invalid_session_state", "Result history contains duplicate IDs")
        if not set(history_values).issubset(action_ids):
            raise SessionError("invalid_session_state", "Result history references an unknown action")

        used_values = (
            _required_list(raw, "used_action_ids")
            if version >= 3
            else list(history_values)
        )
        if not all(
            isinstance(item, str) and ACTION_ID_PATTERN.fullmatch(item)
            for item in used_values
        ):
            raise SessionError("invalid_session_state", "Used action IDs are invalid")
        if len(used_values) != len(set(used_values)):
            raise SessionError("invalid_session_state", "Used action IDs contain duplicates")
        if not set(action_ids).issubset(used_values):
            raise SessionError(
                "invalid_session_state",
                "Action ledger references an unreserved action ID",
            )

        pending = (
            _pending_confirmation_from_dict(raw["pending_confirmation"])
            if version >= 4 and raw["pending_confirmation"] is not None
            else None
        )
        if pending is not None:
            pending_id = pending.action.id
            if pending_id not in used_values:
                raise SessionError(
                    "invalid_session_state",
                    "Pending confirmation references an unreserved action ID",
                )
            if pending_id in action_ids:
                raise SessionError(
                    "invalid_session_state",
                    "Pending confirmation already has a persisted result",
                )
            if status is not SessionStatus.WAITING_USER:
                raise SessionError(
                    "invalid_session_state",
                    "Pending confirmation requires waiting_user status",
                )

        chunks = tuple(_chunk_from_dict(item) for item in _required_list(raw, "chunks"))
        chunk_keys = [(item.path.root, item.path.value) for item in chunks]
        if len(chunk_keys) != len(set(chunk_keys)):
            raise SessionError("invalid_session_state", "Chunk state contains duplicate paths")

        processes = tuple(_process_from_dict(item) for item in _required_list(raw, "processes"))
        handles = [item.handle for item in processes]
        if len(handles) != len(set(handles)):
            raise SessionError("invalid_session_state", "Process state contains duplicate handles")

        created_at = _datetime(raw, "created_at")
        updated_at = _datetime(raw, "updated_at")
        if updated_at < created_at:
            raise SessionError("invalid_session_state", "updated_at precedes created_at")

        return cls(
            session_id=session_id,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            revision=revision,
            step=step,
            max_steps=max_steps,
            plan=plan,
            action_ledger=actions,
            result_history=tuple(history_values),
            used_action_ids=tuple(used_values),
            chunks=chunks,
            processes=processes,
            pending_confirmation=pending,
        )


@dataclass(frozen=True, slots=True)
class SessionPaths:
    session_id: str
    host_root: Path
    host_input: Path
    host_output: Path
    state_file: Path
    lock_file: Path

    @property
    def input_root(self) -> str:
        return f"/input/{self.session_id}"

    @property
    def output_root(self) -> str:
        return f"/output/{self.session_id}"


@dataclass(slots=True)
class Session:
    paths: SessionPaths
    state: SessionState

    @property
    def id(self) -> str:
        return self.state.session_id


def validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise SessionError(
            "invalid_session_id",
            "Session ID must match sess_[A-Za-z0-9_-]{1,64}",
        )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SessionError("invalid_session_state", "Session timestamps must include a timezone")
    return value.isoformat()


def _datetime(raw: dict[str, Any], key: str) -> datetime:
    value = _required_string(raw, key)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SessionError("invalid_session_state", f"{key} is not an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SessionError("invalid_session_state", f"{key} must include a timezone")
    return parsed


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise SessionError("invalid_session_state", f"{key} must be non-empty text")
    return value


def _required_list(raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise SessionError("invalid_session_state", f"{key} must be a list")
    return value


def _nonnegative_integer(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if type(value) is not int or value < 0:
        raise SessionError("invalid_session_state", f"{key} must be a non-negative integer")
    return value


def _positive_integer(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if type(value) is not int or value < 1:
        raise SessionError("invalid_session_state", f"{key} must be a positive integer")
    return value


def _result_to_dict(result: Result) -> dict[str, Any]:
    return {
        "action_id": result.action_id,
        "status": result.status.value,
        "body": result.body,
        "lines": result.lines,
        "truncation": None
        if result.truncation is None
        else {
            "total_bytes": result.truncation.total_bytes,
            "offset": result.truncation.offset,
        },
    }


def _result_from_dict(raw: Any) -> Result:
    if not isinstance(raw, dict):
        raise SessionError("invalid_session_state", "Action result must be an object")
    if set(raw) != {"action_id", "status", "body", "lines", "truncation"}:
        raise SessionError("invalid_session_state", "Action result has unexpected keys")
    action_id = _required_string(raw, "action_id")
    try:
        status = ResultStatus(_required_string(raw, "status"))
    except ValueError as error:
        raise SessionError("invalid_session_state", "Unknown result status") from error
    body = raw["body"]
    lines = raw["lines"]
    if not isinstance(body, str) or (lines is not None and not isinstance(lines, str)):
        raise SessionError("invalid_session_state", "Result body/lines have invalid types")
    truncation_raw = raw["truncation"]
    truncation = None
    if truncation_raw is not None:
        if not isinstance(truncation_raw, dict) or set(truncation_raw) != {"total_bytes", "offset"}:
            raise SessionError("invalid_session_state", "Invalid result truncation")
        total_bytes = _nonnegative_integer(truncation_raw, "total_bytes")
        offset = _nonnegative_integer(truncation_raw, "offset")
        if offset > total_bytes:
            raise SessionError("invalid_session_state", "Truncation offset exceeds total bytes")
        truncation = Truncation(total_bytes=total_bytes, offset=offset)
    return Result(
        action_id=action_id,
        status=status,
        body=body,
        lines=lines,
        truncation=truncation,
    )


def _action_to_dict(record: ActionRecord) -> dict[str, Any]:
    return {
        "action_id": record.action_id,
        "tool": record.tool,
        "action_digest": record.action_digest,
        "result": _result_to_dict(record.result),
        "completed_at": _timestamp(record.completed_at),
    }


def _action_from_dict(raw: Any, *, version: int) -> ActionRecord:
    expected = {"action_id", "tool", "result", "completed_at"}
    if version >= 2:
        expected.add("action_digest")
    if not isinstance(raw, dict) or set(raw) != expected:
        raise SessionError("invalid_session_state", "Invalid action ledger record")
    action_id = _required_string(raw, "action_id")
    tool = _required_string(raw, "tool")
    if not ACTION_ID_PATTERN.fullmatch(action_id) or not TOOL_NAME_PATTERN.fullmatch(tool):
        raise SessionError("invalid_session_state", "Invalid action ID or tool name")
    action_digest = raw.get("action_digest")
    if action_digest is not None and (
        not isinstance(action_digest, str)
        or not ACTION_DIGEST_PATTERN.fullmatch(action_digest)
    ):
        raise SessionError("invalid_session_state", "Invalid action digest")
    result = _result_from_dict(raw["result"])
    if result.action_id != action_id:
        raise SessionError("invalid_session_state", "Action and result IDs do not match")
    return ActionRecord(
        action_id=action_id,
        tool=tool,
        result=result,
        completed_at=_datetime(raw, "completed_at"),
        action_digest=action_digest,
    )


def _pending_confirmation_to_dict(record: PendingConfirmation) -> dict[str, Any]:
    action = record.action
    return {
        "action": {
            "id": action.id,
            "tool": action.tool,
            "path": (
                None
                if action.path is None
                else {"root": action.path.root.value, "value": action.path.value}
            ),
            "arguments": [
                {
                    "name": argument.name,
                    "value": argument.value,
                    "attributes": [
                        {"name": name, "value": value}
                        for name, value in argument.attributes
                    ],
                }
                for argument in action.arguments
            ],
            "chunk": (
                None
                if action.chunk is None
                else {"seq": action.chunk.seq, "final": action.chunk.final}
            ),
            "expect_confirm": action.expect_confirm,
        },
        "reason": record.reason,
        "guard": record.guard,
        "requested_at": _timestamp(record.requested_at),
    }


def _pending_confirmation_from_dict(raw: Any) -> PendingConfirmation:
    if not isinstance(raw, dict) or set(raw) != {
        "action",
        "reason",
        "guard",
        "requested_at",
    }:
        raise SessionError("invalid_session_state", "Invalid pending confirmation")
    reason = _required_string(raw, "reason")
    guard = _required_string(raw, "guard")
    if not ACTION_DIGEST_PATTERN.fullmatch(guard):
        raise SessionError("invalid_session_state", "Invalid pending confirmation guard")
    action_raw = raw["action"]
    if not isinstance(action_raw, dict) or set(action_raw) != {
        "id",
        "tool",
        "path",
        "arguments",
        "chunk",
        "expect_confirm",
    }:
        raise SessionError("invalid_session_state", "Invalid pending confirmation action")

    action_id = _required_string(action_raw, "id")
    tool = _required_string(action_raw, "tool")
    if not ACTION_ID_PATTERN.fullmatch(action_id) or not TOOL_NAME_PATTERN.fullmatch(tool):
        raise SessionError("invalid_session_state", "Invalid pending action ID or tool")

    path = None
    path_raw = action_raw["path"]
    if path_raw is not None:
        if not isinstance(path_raw, dict) or set(path_raw) != {"root", "value"}:
            raise SessionError("invalid_session_state", "Invalid pending action path")
        try:
            root = Root(_required_string(path_raw, "root"))
        except ValueError as error:
            raise SessionError("invalid_session_state", "Invalid pending action root") from error
        path = PathRef(_required_string(path_raw, "value"), root)

    arguments_raw = _required_list(action_raw, "arguments")
    arguments: list[Argument] = []
    argument_names: set[str] = set()
    for item in arguments_raw:
        if not isinstance(item, dict) or set(item) != {"name", "value", "attributes"}:
            raise SessionError("invalid_session_state", "Invalid pending action argument")
        name = _required_string(item, "name")
        value = item["value"]
        if not ARGUMENT_NAME_PATTERN.fullmatch(name) or not isinstance(value, str):
            raise SessionError("invalid_session_state", "Invalid pending action argument")
        if name in argument_names:
            raise SessionError("invalid_session_state", "Duplicate pending action argument")
        argument_names.add(name)
        attributes_raw = _required_list(item, "attributes")
        attributes: list[tuple[str, str]] = []
        attribute_names: set[str] = set()
        for attribute in attributes_raw:
            if not isinstance(attribute, dict) or set(attribute) != {"name", "value"}:
                raise SessionError(
                    "invalid_session_state",
                    "Invalid pending action argument attribute",
                )
            attribute_name = _required_string(attribute, "name")
            attribute_value = attribute["value"]
            if (
                not ARGUMENT_NAME_PATTERN.fullmatch(attribute_name)
                or not isinstance(attribute_value, str)
                or attribute_name in attribute_names
            ):
                raise SessionError(
                    "invalid_session_state",
                    "Invalid pending action argument attribute",
                )
            attribute_names.add(attribute_name)
            attributes.append((attribute_name, attribute_value))
        arguments.append(Argument(name, value, tuple(attributes)))

    chunk = None
    chunk_raw = action_raw["chunk"]
    if chunk_raw is not None:
        if not isinstance(chunk_raw, dict) or set(chunk_raw) != {"seq", "final"}:
            raise SessionError("invalid_session_state", "Invalid pending action chunk")
        seq = chunk_raw["seq"]
        final = chunk_raw["final"]
        if type(seq) is not int or seq < 1 or type(final) is not bool:
            raise SessionError("invalid_session_state", "Invalid pending action chunk")
        chunk = Chunk(seq, final)

    expect_confirm = action_raw["expect_confirm"]
    if expect_confirm is not True:
        raise SessionError(
            "invalid_session_state",
            "A pending action must declare confirmation",
        )
    return PendingConfirmation(
        action=Action(
            id=action_id,
            tool=tool,
            path=path,
            arguments=tuple(arguments),
            chunk=chunk,
            expect_confirm=True,
        ),
        reason=reason,
        guard=guard,
        requested_at=_datetime(raw, "requested_at"),
    )


def _chunk_to_dict(record: ChunkRecord) -> dict[str, Any]:
    return {
        "root": record.path.root.value,
        "path": record.path.value,
        "next_seq": record.next_seq,
        "finalized": record.finalized,
        "updated_at": _timestamp(record.updated_at),
    }


def _chunk_from_dict(raw: Any) -> ChunkRecord:
    if not isinstance(raw, dict) or set(raw) != {
        "root",
        "path",
        "next_seq",
        "finalized",
        "updated_at",
    }:
        raise SessionError("invalid_session_state", "Invalid chunk record")
    try:
        root = Root(_required_string(raw, "root"))
    except ValueError as error:
        raise SessionError("invalid_session_state", "Invalid chunk root") from error
    path = _required_string(raw, "path")
    if root is not Root.OUTPUT:
        raise SessionError("invalid_session_state", "Chunk records must target output")
    next_seq = _positive_integer(raw, "next_seq")
    finalized = raw["finalized"]
    if type(finalized) is not bool:
        raise SessionError("invalid_session_state", "Chunk finalized must be boolean")
    return ChunkRecord(
        path=PathRef(path, root),
        next_seq=next_seq,
        finalized=finalized,
        updated_at=_datetime(raw, "updated_at"),
    )


def _process_to_dict(record: ProcessRecord) -> dict[str, Any]:
    return {
        "handle": record.handle,
        "pid": record.pid,
        "status": record.status.value,
        "output_offset": record.output_offset,
        "started_at": _timestamp(record.started_at),
    }


def _process_from_dict(raw: Any) -> ProcessRecord:
    if not isinstance(raw, dict) or set(raw) != {
        "handle",
        "pid",
        "status",
        "output_offset",
        "started_at",
    }:
        raise SessionError("invalid_session_state", "Invalid process record")
    try:
        status = ProcessStatus(_required_string(raw, "status"))
    except ValueError as error:
        raise SessionError("invalid_session_state", "Invalid process status") from error
    handle = _required_string(raw, "handle")
    if not PROCESS_HANDLE_PATTERN.fullmatch(handle):
        raise SessionError("invalid_session_state", "Invalid process handle")
    return ProcessRecord(
        handle=handle,
        pid=_positive_integer(raw, "pid"),
        status=status,
        output_offset=_nonnegative_integer(raw, "output_offset"),
        started_at=_datetime(raw, "started_at"),
    )
