"""Strict, non-executing parser for assistant-to-interpreter AEML."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .errors import AEMLError, AEMLParseError, AEMLTruncatedError
from .models import AEMLMessage, Action, Argument, Chunk, NextDirective, PathRef, Root


_CDATA = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
_FORBIDDEN_XML = re.compile(r"<!DOCTYPE|<!ENTITY|<!--|<\?", re.IGNORECASE)


class AEMLParser:
    """Parse one complete AEML envelope into immutable protocol models.

    Parsing is intentionally separate from semantic validation and execution.
    A parser instance is reusable and contains no session state.
    """

    def __init__(
        self,
        *,
        max_message_bytes: int = 512 * 1024,
        max_elements: int = 256,
        max_attributes: int = 512,
        max_depth: int = 4,
    ) -> None:
        self.max_message_bytes = max_message_bytes
        self.max_elements = max_elements
        self.max_attributes = max_attributes
        self.max_depth = max_depth

    def parse(self, response: str) -> AEMLMessage:
        if not isinstance(response, str):
            raise AEMLParseError("AEML response must be text")
        if len(response.encode("utf-8")) > self.max_message_bytes:
            raise AEMLParseError(
                f"AEML response exceeds the {self.max_message_bytes}-byte parser limit"
            )

        source = response.strip()
        if not source:
            raise AEMLParseError("AEML response is empty")
        if source.startswith("<aeml") and "</aeml>" not in source:
            raise AEMLTruncatedError()

        scan_source = _CDATA.sub("", source)
        if _FORBIDDEN_XML.search(scan_source):
            raise AEMLParseError("DTD, entity declarations, comments, and processing instructions are forbidden")

        try:
            envelope = ET.fromstring(source)
            self._check_resource_limits(envelope)
            return self._parse_envelope(envelope)
        except AEMLError:
            raise
        except ET.ParseError as error:
            raise AEMLParseError(f"Malformed XML: {error}") from error

    def _check_resource_limits(self, root: ET.Element) -> None:
        elements = 0
        attributes = 0

        def visit(element: ET.Element, depth: int) -> None:
            nonlocal elements, attributes
            if depth > self.max_depth:
                raise AEMLParseError(f"AEML nesting exceeds maximum depth {self.max_depth}")
            if not isinstance(element.tag, str) or "{" in element.tag or "}" in element.tag:
                raise AEMLParseError("XML namespaces and non-text tags are forbidden")
            elements += 1
            attributes += len(element.attrib)
            if elements > self.max_elements:
                raise AEMLParseError(f"AEML contains more than {self.max_elements} elements")
            if attributes > self.max_attributes:
                raise AEMLParseError(f"AEML contains more than {self.max_attributes} attributes")
            for child in element:
                visit(child, depth + 1)

        visit(root, 0)

    def _parse_envelope(self, envelope: ET.Element) -> AEMLMessage:
        if envelope.tag != "aeml":
            raise AEMLParseError("Root element must be <aeml>")
        self._require_attributes(envelope, allowed={"turn", "session"}, required={"turn", "session"})
        self._require_container_whitespace(envelope)

        turn = self._positive_integer(envelope.attrib["turn"], "aeml.turn")
        session = envelope.attrib["session"].strip()
        if not session:
            raise AEMLParseError("aeml.session cannot be empty")

        singletons: dict[str, ET.Element] = {}
        actions: list[Action] = []
        allowed = {"plan", "thought", "action", "say", "ask_user", "next", "complete"}
        for child in envelope:
            if child.tag not in allowed:
                raise AEMLParseError(f"Unknown <aeml> child <{child.tag}>")
            if child.tag == "action":
                actions.append(self._parse_action(child))
                continue
            if child.tag in singletons:
                raise AEMLParseError(f"<{child.tag}> may appear at most once")
            singletons[child.tag] = child

        plan = self._optional_text(singletons, "plan")
        thought = self._optional_text(singletons, "thought")
        say = self._optional_text(singletons, "say")
        ask_user = self._optional_text(singletons, "ask_user")
        complete = self._optional_text(singletons, "complete")

        next_directive = None
        next_element = singletons.get("next")
        if next_element is not None:
            value = self._text_element(next_element, strip=True)
            try:
                next_directive = NextDirective(value)
            except ValueError as error:
                choices = ", ".join(item.value for item in NextDirective)
                raise AEMLParseError(f"<next> must be one of: {choices}") from error

        return AEMLMessage(
            turn=turn,
            session=session,
            plan=plan,
            thought=thought,
            actions=tuple(actions),
            say=say,
            ask_user=ask_user,
            next=next_directive,
            complete=complete,
        )

    def _parse_action(self, element: ET.Element) -> Action:
        self._require_attributes(element, allowed={"id"}, required={"id"})
        self._require_container_whitespace(element)
        action_id = element.attrib["id"].strip()
        if not action_id:
            raise AEMLParseError("action.id cannot be empty")

        allowed = {"tool", "path", "args", "chunk", "expect_confirm"}
        children: dict[str, ET.Element] = {}
        for child in element:
            if child.tag not in allowed:
                raise AEMLParseError(f"Unknown <action> child <{child.tag}>")
            if child.tag in children:
                raise AEMLParseError(f"Action {action_id!r} contains duplicate <{child.tag}>")
            children[child.tag] = child

        tool_element = children.get("tool")
        if tool_element is None:
            raise AEMLParseError(f"Action {action_id!r} is missing <tool>")
        tool = self._text_element(tool_element, strip=True)
        if not tool:
            raise AEMLParseError(f"Action {action_id!r} has an empty <tool>")

        path = None
        path_element = children.get("path")
        if path_element is not None:
            value = self._text_element(path_element, strip=True, allowed_attributes={"root"})
            root = self._root(path_element.attrib.get("root", Root.OUTPUT.value), "path.root")
            path = PathRef(value=value, root=root)

        arguments: list[Argument] = []
        args_element = children.get("args")
        if args_element is not None:
            self._require_attributes(args_element, allowed=set())
            self._require_container_whitespace(args_element)
            for argument in args_element:
                arguments.append(
                    Argument(
                        name=argument.tag,
                        value=self._text_element(
                            argument,
                            strip=False,
                            allowed_attributes={"root"},
                        ),
                        attributes=tuple(sorted(argument.attrib.items())),
                    )
                )

        chunk = None
        chunk_element = children.get("chunk")
        if chunk_element is not None:
            self._require_attributes(
                chunk_element,
                allowed={"seq", "final"},
                required={"seq", "final"},
            )
            if list(chunk_element) or (chunk_element.text or "").strip():
                raise AEMLParseError("<chunk> must be empty")
            chunk = Chunk(
                seq=self._positive_integer(chunk_element.attrib["seq"], "chunk.seq"),
                final=self._boolean(chunk_element.attrib["final"], "chunk.final"),
            )

        expect_confirm = None
        confirm_element = children.get("expect_confirm")
        if confirm_element is not None:
            expect_confirm = self._boolean(
                self._text_element(confirm_element, strip=True),
                "expect_confirm",
            )

        return Action(
            id=action_id,
            tool=tool,
            path=path,
            arguments=tuple(arguments),
            chunk=chunk,
            expect_confirm=expect_confirm,
        )

    def _optional_text(self, elements: dict[str, ET.Element], name: str) -> str | None:
        element = elements.get(name)
        if element is None:
            return None
        return self._text_element(element, strip=True)

    def _text_element(
        self,
        element: ET.Element,
        *,
        strip: bool,
        allowed_attributes: set[str] | None = None,
    ) -> str:
        self._require_attributes(element, allowed=allowed_attributes or set())
        if list(element):
            raise AEMLParseError(f"<{element.tag}> may contain text or CDATA only")
        value = element.text or ""
        return value.strip() if strip else value

    @staticmethod
    def _require_attributes(
        element: ET.Element,
        *,
        allowed: set[str],
        required: set[str] | None = None,
    ) -> None:
        required = required or set()
        unknown = set(element.attrib) - allowed
        missing = required - set(element.attrib)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise AEMLParseError(f"<{element.tag}> has unknown attribute(s): {names}")
        if missing:
            names = ", ".join(sorted(missing))
            raise AEMLParseError(f"<{element.tag}> is missing attribute(s): {names}")

    @staticmethod
    def _require_container_whitespace(element: ET.Element) -> None:
        if (element.text or "").strip():
            raise AEMLParseError(f"Unexpected text directly inside <{element.tag}>")
        for child in element:
            if (child.tail or "").strip():
                raise AEMLParseError(f"Unexpected text after <{child.tag}> in <{element.tag}>")

    @staticmethod
    def _positive_integer(value: str, field: str) -> int:
        if not re.fullmatch(r"[1-9][0-9]*", value.strip()):
            raise AEMLParseError(f"{field} must be a positive integer")
        return int(value)

    @staticmethod
    def _boolean(value: str, field: str) -> bool:
        normalized = value.strip().lower()
        if normalized not in {"true", "false"}:
            raise AEMLParseError(f"{field} must be true or false")
        return normalized == "true"

    @staticmethod
    def _root(value: str, field: str) -> Root:
        try:
            return Root(value.strip())
        except ValueError as error:
            raise AEMLParseError(f"{field} must be output or input") from error
