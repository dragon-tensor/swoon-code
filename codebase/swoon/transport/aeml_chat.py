"""One validated AEML exchange over a synchronous text transport."""

from __future__ import annotations

from collections.abc import Collection
from threading import Lock
from typing import Protocol

from swoon.aeml.errors import AEMLChannelError
from swoon.aeml.models import AEMLContext, ValidatedMessage
from swoon.aeml.parser import AEMLParser
from swoon.aeml.prompts import AEMLPromptBuilder
from swoon.aeml.validator import AEMLValidator


class TextTransport(Protocol):
    """Minimal transport contract used by the AEML channel."""

    def send(self, prompt: str) -> str:
        """Send one text prompt and return one assistant response."""


class AEMLChatChannel:
    """Generate, send, parse, and validate exactly one turn per call.

    This class intentionally does not execute actions, update a session, retry responses, or
    run an autonomous loop. A channel instance is bound to one hosted conversation/session.
    """

    def __init__(
        self,
        transport: TextTransport,
        *,
        prompt_builder: AEMLPromptBuilder | None = None,
        parser: AEMLParser | None = None,
        validator: AEMLValidator | None = None,
    ) -> None:
        if not callable(getattr(transport, "send", None)):
            raise TypeError("transport must provide send(prompt)")
        self.transport = transport
        self.prompt_builder = prompt_builder or AEMLPromptBuilder()
        self.parser = parser or AEMLParser()
        self.validator = validator or AEMLValidator(self.prompt_builder.tool_specs)
        if set(self.validator.tool_specs) != set(self.prompt_builder.tool_specs):
            raise ValueError("Prompt and validator must expose the same tool schemas")
        for name, spec in self.prompt_builder.tool_specs.items():
            if self.validator.tool_specs.get(name) != spec:
                raise ValueError("Prompt and validator tool schemas differ")

        self._session_id: str | None = None
        self._last_turn: int | None = None
        self._bootstrap_sent = False
        self._exchange_lock = Lock()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def last_turn(self) -> int | None:
        return self._last_turn

    @property
    def bootstrap_sent(self) -> bool:
        return self._bootstrap_sent

    def exchange(
        self,
        context: AEMLContext,
        *,
        known_action_ids: Collection[str] = (),
    ) -> ValidatedMessage:
        """Perform one synchronous exchange and return its validated AEML message."""

        if not self._exchange_lock.acquire(blocking=False):
            raise AEMLChannelError(
                "exchange_in_progress",
                "Only one AEML exchange may run on a channel at a time",
            )
        try:
            self._validate_sequence(context)
            known_ids = self._known_action_ids(context, known_action_ids)
            initial = not self._bootstrap_sent
            prompt = (
                self.prompt_builder.initial(context)
                if initial
                else self.prompt_builder.continuation(context)
            )
            if self._session_id is None:
                self._session_id = context.session

            response = self.transport.send(prompt)
            self._bootstrap_sent = True
            message = self.parser.parse(response)
            validated = self.validator.validate(
                message,
                expected_turn=context.turn,
                expected_session=context.session,
                known_action_ids=known_ids,
            )
            self._last_turn = context.turn
            return validated
        finally:
            self._exchange_lock.release()

    def _validate_sequence(self, context: AEMLContext) -> None:
        if not isinstance(context, AEMLContext):
            raise AEMLChannelError("invalid_context", "AEMLContext is required")
        expected_turn = 1 if self._last_turn is None else self._last_turn + 1
        if context.turn != expected_turn:
            raise AEMLChannelError(
                "turn_sequence_error",
                f"Expected context turn {expected_turn}, received {context.turn}",
            )
        if self._session_id is not None and context.session != self._session_id:
            raise AEMLChannelError(
                "session_mismatch",
                f"Channel is bound to session {self._session_id!r}",
            )

    @staticmethod
    def _known_action_ids(
        context: AEMLContext,
        supplied: Collection[str],
    ) -> frozenset[str]:
        if isinstance(supplied, (str, bytes)):
            raise AEMLChannelError(
                "invalid_action_history",
                "known_action_ids must be a collection of IDs",
            )
        known: set[str] = set()
        for action_id in supplied:
            if not isinstance(action_id, str):
                raise AEMLChannelError(
                    "invalid_action_history",
                    "known_action_ids must contain text IDs",
                )
            known.add(action_id)
        known.update(result.action_id for result in context.results)
        known.update(summary.action_id for summary in context.summaries)
        return frozenset(known)
