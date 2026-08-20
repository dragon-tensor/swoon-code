"""Bounded construction and safe XML rendering for interpreter AEML context."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass

from swoon.session.models import Session

from .errors import AEMLContextError
from .models import (
    AEMLContext,
    Environment,
    ProtocolError,
    Result,
    ResultStatus,
    ResultSummary,
    SystemNotice,
    Truncation,
)


_SESSION_ID = re.compile(r"sess_[A-Za-z0-9_-]{1,64}\Z")
_ACTION_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_TOOL_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_NOTICE_TYPE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ATTRIBUTE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class ContextLimits:
    """Hard bounds applied before context crosses the chatbot boundary."""

    max_context_bytes: int = 256 * 1024
    max_user_prompt_bytes: int = 96 * 1024
    max_plan_bytes: int = 16 * 1024
    recent_results: int = 4
    max_result_body_bytes: int = 24 * 1024
    max_history_summaries: int = 32
    max_summary_bytes: int = 256
    max_errors: int = 16
    max_error_message_bytes: int = 4 * 1024
    max_external_notices: int = 16
    max_notice_attributes: int = 16
    max_notice_value_bytes: int = 2 * 1024
    max_pending_chunks: int = 32

    def __post_init__(self) -> None:
        positive = (
            "max_context_bytes",
            "max_user_prompt_bytes",
            "max_plan_bytes",
            "max_result_body_bytes",
            "max_summary_bytes",
            "max_error_message_bytes",
            "max_notice_attributes",
            "max_notice_value_bytes",
        )
        nonnegative = (
            "recent_results",
            "max_history_summaries",
            "max_errors",
            "max_external_notices",
            "max_pending_chunks",
        )
        for name in positive:
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in nonnegative:
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_notice_attributes < 3:
            raise ValueError("max_notice_attributes must be at least 3")


class AEMLContextBuilder:
    """Create a compact immutable context from a managed session.

    The builder reads in-memory session records only. It never mutates state, accesses a
    project file, or exposes the session's physical host directories.
    """

    def __init__(self, limits: ContextLimits | None = None) -> None:
        self.limits = limits or ContextLimits()
        self.renderer = AEMLContextRenderer(
            max_context_bytes=self.limits.max_context_bytes,
            max_notice_attributes=self.limits.max_notice_attributes,
        )

    def build(
        self,
        session: Session,
        *,
        turn: int,
        user_prompt: str | None = None,
        errors: Iterable[ProtocolError] = (),
        notices: Iterable[SystemNotice] = (),
    ) -> AEMLContext:
        if not isinstance(session, Session):
            raise AEMLContextError("invalid_context", "A managed Session is required")
        if type(turn) is not int or turn < 1:
            raise AEMLContextError("invalid_context", "Context turn must be a positive integer")
        if user_prompt is not None:
            if not isinstance(user_prompt, str):
                raise AEMLContextError("invalid_context", "User prompt must be text or null")
            self._require_size(
                user_prompt,
                self.limits.max_user_prompt_bytes,
                "user_prompt_too_large",
                "User prompt",
            )

        external_errors = self._collect_limited(
            errors,
            self.limits.max_errors,
            ProtocolError,
            "errors",
        )
        external_notices = self._collect_limited(
            notices,
            self.limits.max_external_notices,
            SystemNotice,
            "notices",
        )
        generated_notices: list[SystemNotice] = []

        compact_errors = tuple(
            self._compact_error(item, generated_notices) for item in external_errors
        )
        compact_notices = tuple(self._compact_notice(item) for item in external_notices)
        plan = self._compact_plan(session.state.plan, generated_notices)
        summaries, results = self._history(session, generated_notices)
        self._chunk_notices(session, generated_notices)
        self._step_notice(session, generated_notices)

        context = AEMLContext(
            turn=turn,
            session=session.id,
            environment=Environment(
                output_root=session.paths.output_root,
                input_root=session.paths.input_root,
                cwd=session.paths.output_root,
                status=session.state.status.value,
            ),
            step=session.state.step,
            max_steps=session.state.max_steps,
            user_prompt=user_prompt,
            results=results,
            errors=compact_errors,
            notices=compact_notices + tuple(generated_notices),
            plan=plan,
            summaries=summaries,
        )
        self.renderer.render(context)
        return context

    def _history(
        self,
        session: Session,
        generated_notices: list[SystemNotice],
    ) -> tuple[tuple[ResultSummary, ...], tuple[Result, ...]]:
        records_by_id = {record.action_id: record for record in session.state.action_ledger}
        try:
            records = [records_by_id[action_id] for action_id in session.state.result_history]
        except KeyError as error:
            raise AEMLContextError(
                "invalid_context_state",
                "Result history references an absent action record",
            ) from error

        split = max(0, len(records) - self.limits.recent_results)
        older = records[:split]
        recent = records[split:]
        if self.limits.max_history_summaries:
            summarized = older[-self.limits.max_history_summaries :]
        else:
            summarized = []
        omitted = len(older) - len(summarized)
        if omitted:
            generated_notices.append(
                SystemNotice("history_omitted", (("count", str(omitted)),))
            )

        summaries = tuple(
            ResultSummary(
                action_id=record.action_id,
                tool=record.tool,
                status=record.result.status,
                preview=self._summary(record.result.body),
            )
            for record in summarized
        )
        results = tuple(
            self._compact_result(record.result, generated_notices) for record in recent
        )
        return summaries, results

    def _compact_result(
        self,
        result: Result,
        generated_notices: list[SystemNotice],
    ) -> Result:
        if not isinstance(result.body, str):
            raise AEMLContextError("invalid_context_state", "Persisted result body is not text")
        try:
            body_bytes = len(result.body.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise AEMLContextError(
                "invalid_context_state",
                "Persisted result body is not valid Unicode",
            ) from error
        if body_bytes <= self.limits.max_result_body_bytes:
            return result
        body = _truncate_utf8(result.body, self.limits.max_result_body_bytes)
        included_bytes = len(body.encode("utf-8"))
        source_total = (
            result.truncation.total_bytes
            if result.truncation is not None
            else body_bytes
        )
        offset = result.truncation.offset if result.truncation is not None else 0
        generated_notices.append(
            SystemNotice(
                "context_result_compacted",
                (
                    ("id", result.action_id),
                    ("total_bytes", str(source_total)),
                    ("included_bytes", str(included_bytes)),
                ),
            )
        )
        status = (
            ResultStatus.PARTIAL
            if result.status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL}
            else result.status
        )
        return Result(
            action_id=result.action_id,
            status=status,
            body=body,
            lines=result.lines,
            truncation=Truncation(total_bytes=max(source_total, body_bytes), offset=offset),
        )

    def _compact_error(
        self,
        error: ProtocolError,
        generated_notices: list[SystemNotice],
    ) -> ProtocolError:
        if not isinstance(error.message, str):
            raise AEMLContextError("invalid_context", "Protocol error message must be text")
        size = _xml_text_size(error.message)
        if size <= self.limits.max_error_message_bytes:
            return error
        message = _truncate_xml_text(error.message, self.limits.max_error_message_bytes)
        attributes = [("code", error.code), ("total_bytes", str(size))]
        if error.action_id is not None:
            attributes.append(("id", error.action_id))
        generated_notices.append(SystemNotice("context_error_compacted", tuple(attributes)))
        return ProtocolError(error.code, message, error.action_id)

    def _compact_notice(self, notice: SystemNotice) -> SystemNotice:
        if not isinstance(notice.attributes, tuple):
            raise AEMLContextError(
                "invalid_context",
                "System notice attributes must be a tuple",
            )
        if len(notice.attributes) > self.limits.max_notice_attributes:
            raise AEMLContextError(
                "context_item_limit",
                "System notice contains too many attributes",
            )
        attributes: list[tuple[str, str]] = []
        for attribute in notice.attributes:
            if not isinstance(attribute, tuple) or len(attribute) != 2:
                raise AEMLContextError(
                    "invalid_context",
                    "System notice attributes must be name/value pairs",
                )
            name, value = attribute
            if not isinstance(name, str) or not isinstance(value, str):
                raise AEMLContextError(
                    "invalid_context",
                    "System notice attributes must contain text",
                )
            attributes.append(
                (name, _truncate_xml_text(value, self.limits.max_notice_value_bytes))
            )
        return SystemNotice(notice.type, tuple(attributes))

    def _compact_plan(
        self,
        plan: str | None,
        generated_notices: list[SystemNotice],
    ) -> str | None:
        if plan is None:
            return None
        if not isinstance(plan, str):
            raise AEMLContextError("invalid_context_state", "Persisted plan is not text")
        total_bytes = _xml_text_size(plan)
        if total_bytes <= self.limits.max_plan_bytes:
            return plan
        compact = _truncate_xml_text(plan, self.limits.max_plan_bytes)
        generated_notices.append(
            SystemNotice(
                "context_plan_compacted",
                (
                    ("total_bytes", str(total_bytes)),
                    ("included_bytes", str(_xml_text_size(compact))),
                ),
            )
        )
        return compact

    def _summary(self, body: str) -> str:
        collapsed = " ".join(body.split()) or "(empty result)"
        return _truncate_xml_text(collapsed, self.limits.max_summary_bytes)

    def _chunk_notices(self, session: Session, generated_notices: list[SystemNotice]) -> None:
        pending = [record for record in session.state.chunks if not record.finalized]
        included = pending[: self.limits.max_pending_chunks]
        for record in included:
            generated_notices.append(
                SystemNotice(
                    "write_incomplete",
                    (
                        ("root", record.path.root.value),
                        ("path", record.path.value),
                        ("next_seq", str(record.next_seq)),
                    ),
                )
            )
        if len(pending) > len(included):
            generated_notices.append(
                SystemNotice(
                    "pending_chunks_omitted",
                    (("count", str(len(pending) - len(included))),),
                )
            )

    @staticmethod
    def _step_notice(session: Session, generated_notices: list[SystemNotice]) -> None:
        state = session.state
        if state.step >= state.max_steps:
            notice_type = "step_limit_reached"
        elif state.step_limit_approaching:
            notice_type = "step_limit_approaching"
        else:
            return
        generated_notices.append(
            SystemNotice(
                notice_type,
                (("step", str(state.step)), ("max_steps", str(state.max_steps))),
            )
        )

    @staticmethod
    def _collect_limited(
        values: Iterable[object],
        maximum: int,
        expected_type: type,
        label: str,
    ) -> tuple:
        if isinstance(values, (str, bytes)):
            raise AEMLContextError("invalid_context", f"Context {label} must be an iterable")
        collected = []
        try:
            iterator = iter(values)
        except TypeError as error:
            raise AEMLContextError(
                "invalid_context",
                f"Context {label} must be an iterable",
            ) from error
        for _ in range(maximum + 1):
            try:
                item = next(iterator)
            except StopIteration:
                break
            if not isinstance(item, expected_type):
                raise AEMLContextError(
                    "invalid_context",
                    f"Context {label} contains an invalid value",
                )
            collected.append(item)
        if len(collected) > maximum:
            raise AEMLContextError(
                "context_item_limit",
                f"Context contains more than {maximum} {label}",
            )
        return tuple(collected)

    @staticmethod
    def _require_size(text: str, maximum: int, code: str, label: str) -> None:
        if _xml_text_size(text) > maximum:
            raise AEMLContextError(code, f"{label} exceeds {maximum} bytes")


class AEMLContextRenderer:
    """Serialize trusted structure and untrusted text into deterministic XML."""

    def __init__(
        self,
        *,
        max_context_bytes: int = 256 * 1024,
        max_items: int = 512,
        max_notice_attributes: int = 32,
    ) -> None:
        if type(max_context_bytes) is not int or max_context_bytes < 1:
            raise ValueError("max_context_bytes must be a positive integer")
        if type(max_items) is not int or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        if type(max_notice_attributes) is not int or max_notice_attributes < 1:
            raise ValueError("max_notice_attributes must be a positive integer")
        self.max_context_bytes = max_context_bytes
        self.max_items = max_items
        self.max_notice_attributes = max_notice_attributes

    def render(self, context: AEMLContext) -> str:
        self._validate_context(context)
        root = ET.Element("aeml_context")
        self._attributes(
            root,
            (
                ("turn", str(context.turn)),
                ("session", context.session),
                ("output_root", context.environment.output_root),
                ("input_root", context.environment.input_root),
                ("step", f"{context.step}/{context.max_steps}"),
            ),
        )

        environment = ET.SubElement(root, "env")
        environment_attributes = [
            ("output_root", context.environment.output_root),
            ("input_root", context.environment.input_root),
            ("cwd", context.environment.cwd),
        ]
        if context.environment.status is not None:
            environment_attributes.append(("status", context.environment.status))
        self._attributes(environment, environment_attributes)

        if context.user_prompt is not None:
            self._text(ET.SubElement(root, "user_prompt"), context.user_prompt)
        if context.plan is not None:
            self._text(ET.SubElement(root, "plan"), context.plan)

        if context.summaries:
            history = ET.SubElement(root, "history")
            for summary in context.summaries:
                element = ET.SubElement(history, "summary")
                self._attributes(
                    element,
                    (
                        ("id", summary.action_id),
                        ("tool", summary.tool),
                        ("status", summary.status.value),
                    ),
                )
                self._text(element, summary.preview)

        for result in context.results:
            element = ET.SubElement(root, "result")
            attributes = [("id", result.action_id)]
            if result.lines is not None:
                attributes.append(("lines", result.lines))
            self._attributes(element, attributes)
            self._text(ET.SubElement(element, "status"), result.status.value)
            self._text(ET.SubElement(element, "output"), result.body)
            if result.truncation is not None:
                truncated = ET.SubElement(element, "truncated")
                self._attributes(
                    truncated,
                    (
                        ("total_bytes", str(result.truncation.total_bytes)),
                        ("offset", str(result.truncation.offset)),
                    ),
                )

        for error in context.errors:
            element = ET.SubElement(root, "error")
            attributes = [("code", error.code)]
            if error.action_id is not None:
                attributes.insert(0, ("id", error.action_id))
            self._attributes(element, attributes)
            self._text(ET.SubElement(element, "status"), ResultStatus.FAILURE.value)
            self._text(ET.SubElement(element, "message"), error.message)

        for notice in context.notices:
            element = ET.SubElement(root, "system_notice")
            self._attributes(element, (("type", notice.type), *notice.attributes))

        ET.indent(root, space="  ")
        rendered = ET.tostring(root, encoding="unicode", short_empty_elements=True)
        size = len(rendered.encode("utf-8"))
        if size > self.max_context_bytes:
            raise AEMLContextError(
                "context_too_large",
                f"Rendered context exceeds {self.max_context_bytes} bytes",
            )
        return rendered

    def _validate_context(self, context: AEMLContext) -> None:
        if not isinstance(context, AEMLContext):
            raise AEMLContextError("invalid_context", "AEMLContext is required")
        if type(context.turn) is not int or context.turn < 1:
            raise AEMLContextError("invalid_context", "Context turn must be positive")
        if not isinstance(context.session, str) or not _SESSION_ID.fullmatch(context.session):
            raise AEMLContextError("invalid_context", "Context session ID is invalid")
        if type(context.step) is not int or context.step < 0:
            raise AEMLContextError("invalid_context", "Context step must be non-negative")
        if type(context.max_steps) is not int or context.max_steps < 1:
            raise AEMLContextError("invalid_context", "Context max_steps must be positive")
        if context.step > context.max_steps:
            raise AEMLContextError("invalid_context", "Context step exceeds max_steps")

        expected_output = f"/output/{context.session}"
        expected_input = f"/input/{context.session}"
        environment = context.environment
        if not isinstance(environment, Environment):
            raise AEMLContextError("invalid_context", "Context environment is invalid")
        if not all(
            isinstance(value, str)
            for value in (
                environment.output_root,
                environment.input_root,
                environment.cwd,
            )
        ):
            raise AEMLContextError("invalid_context", "Context environment paths are invalid")
        if environment.output_root != expected_output or environment.input_root != expected_input:
            raise AEMLContextError(
                "invalid_virtual_root",
                "Context roots must be the session's fixed virtual roots",
            )
        if environment.cwd != expected_output:
            raise AEMLContextError(
                "invalid_virtual_root",
                "Context cwd must equal the virtual output root",
            )
        if environment.status is not None:
            if not isinstance(environment.status, str) or environment.status not in {
                "active",
                "waiting_user",
                "completed",
                "aborted",
            }:
                raise AEMLContextError("invalid_context", "Context session status is invalid")

        item_count = (
            len(context.results)
            + len(context.errors)
            + len(context.notices)
            + len(context.summaries)
        )
        if item_count > self.max_items:
            raise AEMLContextError(
                "context_item_limit",
                f"Context contains more than {self.max_items} result items",
            )
        for value, label in (
            (context.user_prompt, "user_prompt"),
            (context.plan, "plan"),
        ):
            self._optional_text_value(value, label)
        for result in context.results:
            self._validate_result(result)
        for error in context.errors:
            self._validate_error(error)
        for notice in context.notices:
            self._validate_notice(notice)
        for summary in context.summaries:
            self._validate_summary(summary)
        self._validate_aggregate_text_size(context)

    def _validate_aggregate_text_size(self, context: AEMLContext) -> None:
        size = 0
        values: list[str] = []
        if context.user_prompt is not None:
            values.append(context.user_prompt)
        if context.plan is not None:
            values.append(context.plan)
        for result in context.results:
            values.append(result.body)
            if result.lines is not None:
                values.append(result.lines)
        values.extend(error.message for error in context.errors)
        values.extend(summary.preview for summary in context.summaries)
        values.extend(
            value
            for notice in context.notices
            for _, value in notice.attributes
        )
        for value in values:
            size += _xml_text_size(value)
            if size > self.max_context_bytes:
                raise AEMLContextError(
                    "context_too_large",
                    "Context text exceeds the aggregate byte limit",
                )

    def _validate_result(self, result: Result) -> None:
        if (
            not isinstance(result, Result)
            or not isinstance(result.action_id, str)
            or not _ACTION_ID.fullmatch(result.action_id)
        ):
            raise AEMLContextError("invalid_context", "Context result ID is invalid")
        if not isinstance(result.status, ResultStatus):
            raise AEMLContextError("invalid_context", "Context result status is invalid")
        self._text_value(result.body, "result body")
        self._optional_text_value(result.lines, "result lines")
        if result.truncation is not None:
            truncation = result.truncation
            if (
                not isinstance(truncation, Truncation)
                or type(truncation.total_bytes) is not int
                or type(truncation.offset) is not int
                or truncation.total_bytes < 0
                or truncation.offset < 0
                or truncation.offset > truncation.total_bytes
            ):
                raise AEMLContextError("invalid_context", "Context truncation is invalid")

    def _validate_error(self, error: ProtocolError) -> None:
        if not isinstance(error, ProtocolError):
            raise AEMLContextError("invalid_context", "Context error is invalid")
        if not isinstance(error.code, str) or not _NOTICE_TYPE.fullmatch(error.code):
            raise AEMLContextError("invalid_context", "Context error code is invalid")
        if error.action_id is not None:
            if not isinstance(error.action_id, str) or not _ACTION_ID.fullmatch(error.action_id):
                raise AEMLContextError("invalid_context", "Context error action ID is invalid")
        self._text_value(error.message, "error message")

    def _validate_notice(self, notice: SystemNotice) -> None:
        if not isinstance(notice, SystemNotice):
            raise AEMLContextError("invalid_context", "Context notice is invalid")
        if not isinstance(notice.type, str) or not _NOTICE_TYPE.fullmatch(notice.type):
            raise AEMLContextError("invalid_context", "Context notice type is invalid")
        if not isinstance(notice.attributes, tuple):
            raise AEMLContextError("invalid_context", "Context notice attributes are invalid")
        if len(notice.attributes) > self.max_notice_attributes:
            raise AEMLContextError(
                "context_item_limit",
                "Context notice contains too many attributes",
            )
        seen = {"type"}
        for attribute in notice.attributes:
            if not isinstance(attribute, tuple) or len(attribute) != 2:
                raise AEMLContextError("invalid_context", "Context notice attribute is invalid")
            name, value = attribute
            if (
                not isinstance(name, str)
                or not _ATTRIBUTE_NAME.fullmatch(name)
                or name in seen
                or name == "escaped_controls"
                or name.lower().startswith("xml")
            ):
                raise AEMLContextError("invalid_context", "Context notice attribute is invalid")
            seen.add(name)
            self._text_value(value, "notice attribute")

    def _validate_summary(self, summary: ResultSummary) -> None:
        if not isinstance(summary, ResultSummary):
            raise AEMLContextError("invalid_context", "Context summary is invalid")
        if not isinstance(summary.action_id, str) or not _ACTION_ID.fullmatch(summary.action_id):
            raise AEMLContextError("invalid_context", "Context summary ID is invalid")
        if not isinstance(summary.tool, str) or not _TOOL_NAME.fullmatch(summary.tool):
            raise AEMLContextError("invalid_context", "Context summary tool is invalid")
        if not isinstance(summary.status, ResultStatus):
            raise AEMLContextError("invalid_context", "Context summary status is invalid")
        self._text_value(summary.preview, "summary preview")

    def _optional_text_value(self, value: str | None, label: str) -> None:
        if value is not None:
            self._text_value(value, label)

    def _text_value(self, value: str, label: str) -> None:
        if not isinstance(value, str):
            raise AEMLContextError("invalid_context", f"Context {label} must be text")
        if _xml_text_size(value) > self.max_context_bytes:
            raise AEMLContextError(
                "context_too_large",
                f"Context {label} alone exceeds the context byte limit",
            )

    @staticmethod
    def _attributes(element: ET.Element, attributes: Iterable[tuple[str, str]]) -> None:
        replaced = False
        for name, value in sorted(attributes):
            safe, changed = _xml_safe(value)
            element.set(name, safe)
            replaced = replaced or changed
        if replaced:
            element.set("escaped_controls", "true")

    @staticmethod
    def _text(element: ET.Element, value: str) -> None:
        safe, changed = _xml_safe(value)
        element.text = safe
        if changed:
            element.set("escaped_controls", "true")


def _truncate_xml_text(value: str, maximum: int) -> str:
    if _xml_text_size(value) <= maximum:
        return value
    output: list[str] = []
    size = 0
    for character in value:
        safe, _ = _xml_safe(character)
        character_size = len(safe.encode("utf-8"))
        if size + character_size > maximum:
            break
        output.append(character)
        size += character_size
    return "".join(output)


def _truncate_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore")


def _xml_text_size(value: str) -> int:
    safe, _ = _xml_safe(value)
    return len(safe.encode("utf-8"))


def _xml_safe(value: str) -> tuple[str, bool]:
    output: list[str] = []
    changed = False
    for character in value:
        codepoint = ord(character)
        valid = (
            codepoint in {0x09, 0x0A, 0x0D}
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        )
        if valid:
            output.append(character)
            continue
        changed = True
        if codepoint <= 0xFFFF:
            output.append(f"\\u{codepoint:04X}")
        else:
            output.append(f"\\U{codepoint:08X}")
    return "".join(output), changed
