"""Bounded AEML orchestration over a session-bound chat channel."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock

from swoon.aeml import (
    AEMLChannelError,
    AEMLContextBuilder,
    AEMLContextError,
    AEMLParseError,
    AEMLTruncatedError,
    AEMLValidationError,
    AEMLValidator,
)
from swoon.aeml.models import (
    AEMLMessage,
    NextDirective,
    ProtocolError,
    Result,
    ResultStatus,
    SystemNotice,
    ToolEffect,
    ValidatedMessage,
)
from swoon.session import (
    ProcessTerminationReason,
    Session,
    SessionError,
    SessionManager,
    SessionStatus,
)
from swoon.session.models import ACTION_ID_PATTERN
from swoon.tools import (
    AgentToolDispatcher,
    ConfirmationRequest,
    ReadOnlyToolDispatcher,
    ToolExecutionError,
)
from swoon.transport import AEMLChatChannel

from .errors import OrchestrationError
from .models import OrchestrationLimits, RunResult, RunStopReason


class AEMLOrchestrator:
    """Advance validated AEML turns through one explicitly allowlisted dispatcher.

    One session step is consumed for each new AEML turn. Repair attempts reuse that turn
    and are bounded independently by :class:`OrchestrationLimits`.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        channel: AEMLChatChannel,
        *,
        dispatcher: ReadOnlyToolDispatcher | None = None,
        context_builder: AEMLContextBuilder | None = None,
        limits: OrchestrationLimits | None = None,
        message_sink: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(session_manager, SessionManager):
            raise TypeError("session_manager must be a SessionManager")
        if not isinstance(channel, AEMLChatChannel):
            raise TypeError("channel must be an AEMLChatChannel")
        selected_dispatcher = dispatcher or ReadOnlyToolDispatcher(session_manager)
        if not isinstance(selected_dispatcher, ReadOnlyToolDispatcher):
            raise TypeError("dispatcher must be a ReadOnlyToolDispatcher")
        if selected_dispatcher.session_manager is not session_manager:
            raise ValueError("Dispatcher and orchestrator must use the same SessionManager")
        selected_context_builder = context_builder or AEMLContextBuilder()
        if not isinstance(selected_context_builder, AEMLContextBuilder):
            raise TypeError("context_builder must be an AEMLContextBuilder")
        selected_limits = limits or OrchestrationLimits()
        if not isinstance(selected_limits, OrchestrationLimits):
            raise TypeError("limits must be OrchestrationLimits or null")
        if message_sink is not None and not callable(message_sink):
            raise TypeError("message_sink must be callable or null")

        tool_specs = channel.prompt_builder.tool_specs
        unsupported = set(tool_specs) - selected_dispatcher.implemented_tools
        disallowed_effects = {
            name
            for name, spec in tool_specs.items()
            if spec.effect not in selected_dispatcher.allowed_effects
        }
        if unsupported or disallowed_effects:
            names = ", ".join(sorted(unsupported | disallowed_effects))
            raise ValueError(f"Orchestration channel enables unsafe tools: {names}")

        self.session_manager = session_manager
        self.channel = channel
        self.dispatcher = selected_dispatcher
        self.context_builder = selected_context_builder
        self.limits = selected_limits
        self.message_sink = message_sink
        self._run_lock = Lock()

    def run(
        self,
        session: Session,
        user_prompt: str | None,
        *,
        additional_steps: int | None = None,
        confirmation: bool | None = None,
    ) -> RunResult:
        """Run until completion, a human pause, a hard stop, or retry exhaustion."""

        if not self._run_lock.acquire(blocking=False):
            raise OrchestrationError(
                "run_in_progress",
                "Only one orchestration run may execute at a time",
            )
        try:
            return self._run(
                session,
                user_prompt,
                additional_steps=additional_steps,
                confirmation=confirmation,
            )
        finally:
            self._run_lock.release()

    def _run(
        self,
        supplied_session: Session,
        user_prompt: str | None,
        *,
        additional_steps: int | None,
        confirmation: bool | None,
    ) -> RunResult:
        session = self._load_managed_session(supplied_session)
        if session.state.status in {SessionStatus.COMPLETED, SessionStatus.ABORTED}:
            raise OrchestrationError(
                "session_terminal",
                f"Session is already {session.state.status.value}",
            )
        if self.channel.session_id not in {None, session.id}:
            raise OrchestrationError(
                "session_mismatch",
                f"Channel is bound to session {self.channel.session_id!r}",
            )

        updates: list[str] = []
        pending_errors: tuple[ProtocolError, ...] = ()
        pending_notices: tuple[SystemNotice, ...] = ()
        next_user_prompt = user_prompt

        if session.state.pending_confirmation is not None:
            if additional_steps is not None:
                raise OrchestrationError(
                    "confirmation_pending",
                    "Resolve the pending action before extending the step budget",
                )
            if confirmation is None:
                return self._confirmation_result(session)
            if type(confirmation) is not bool:
                raise OrchestrationError(
                    "invalid_confirmation",
                    "confirmation must be boolean or null",
                )
            action = self._validated_pending_action(session)
            pending = session.state.pending_confirmation
            assert pending is not None
            if confirmation:
                response = self.dispatcher.execute(action, session, confirmed=True)
                if isinstance(response, ProtocolError):
                    self.session_manager.clear_pending_confirmation(session)
                    pending_errors = (response,)
                elif not isinstance(response, Result):
                    raise OrchestrationError(
                        "invalid_tool_response",
                        "Dispatcher returned an unsupported confirmation response",
                    )
                next_user_prompt = user_prompt or (
                    f"The human approved pending action {action.source.id!r}. Continue."
                )
            else:
                denial = Result(
                    action.source.id,
                    ResultStatus.FAILURE,
                    body="Human denied this destructive action; no tool operation ran.",
                )
                self.session_manager.record_action_result(
                    session,
                    action.spec.name,
                    denial,
                    action_digest=self.dispatcher.action_digest(action),
                    resolve_confirmation=True,
                )
                next_user_prompt = user_prompt or (
                    f"The human denied pending action {action.source.id!r}. Continue safely."
                )
        else:
            if confirmation is not None:
                raise OrchestrationError(
                    "unexpected_confirmation",
                    "The session has no action awaiting confirmation",
                )
            if (
                session.state.status is SessionStatus.ACTIVE
                and session.state.step >= session.state.max_steps
            ):
                self.session_manager.set_status(session, SessionStatus.WAITING_USER)

            if session.state.status is SessionStatus.WAITING_USER:
                if session.state.step >= session.state.max_steps:
                    if additional_steps is None:
                        return self._step_limit_result(session)
                    self._require_user_prompt(user_prompt)
                    self.session_manager.extend_step_limit(session, additional_steps)
                elif additional_steps is not None:
                    raise OrchestrationError(
                        "unexpected_step_extension",
                        "A non-exhausted user pause cannot extend the step budget",
                    )
                else:
                    self._require_user_prompt(user_prompt)
                self.session_manager.set_status(session, SessionStatus.ACTIVE)
            else:
                if additional_steps is not None:
                    raise OrchestrationError(
                        "unexpected_step_extension",
                        "An active session cannot extend its own step budget",
                    )
                self._require_user_prompt(user_prompt)

        while True:
            if session.state.step >= session.state.max_steps:
                self.session_manager.set_status(session, SessionStatus.WAITING_USER)
                return self._step_limit_result(session, updates=updates)

            self.session_manager.advance_step(session)
            turn = 1 if self.channel.last_turn is None else self.channel.last_turn + 1
            message, protocol_failure = self._exchange_with_retries(
                session,
                turn=turn,
                user_prompt=next_user_prompt,
                errors=pending_errors,
                notices=pending_notices,
            )
            next_user_prompt = None
            if protocol_failure is not None:
                self._set_terminal_status(session, SessionStatus.ABORTED)
                return RunResult(
                    session=session,
                    reason=RunStopReason.PROTOCOL_ERROR,
                    updates=tuple(updates),
                    error=protocol_failure,
                    last_turn=self.channel.last_turn,
                )
            assert message is not None
            source = message.source

            if source.plan is not None and source.plan != session.state.plan:
                self.session_manager.set_plan(session, source.plan)

            if source.say is not None:
                updates.append(source.say)

            if source.complete is not None:
                self._set_terminal_status(session, SessionStatus.COMPLETED)
                self._publish_updates(source.say, source.complete)
                return RunResult(
                    session=session,
                    reason=RunStopReason.COMPLETED,
                    updates=tuple(updates),
                    summary=source.complete,
                    last_turn=self.channel.last_turn,
                )

            if source.ask_user is not None:
                self.session_manager.set_status(session, SessionStatus.WAITING_USER)
                self._publish_updates(source.say, source.ask_user)
                return RunResult(
                    session=session,
                    reason=RunStopReason.AWAITING_USER,
                    updates=tuple(updates),
                    question=source.ask_user,
                    last_turn=self.channel.last_turn,
                )

            if source.next is NextDirective.DONE:
                self._set_terminal_status(session, SessionStatus.COMPLETED)
                self._publish_updates(source.say)
                return RunResult(
                    session=session,
                    reason=RunStopReason.DONE,
                    updates=tuple(updates),
                    last_turn=self.channel.last_turn,
                )

            if source.next is NextDirective.ABORT:
                self._set_terminal_status(session, SessionStatus.ABORTED)
                self._publish_updates(source.say)
                return RunResult(
                    session=session,
                    reason=RunStopReason.ABORTED,
                    updates=tuple(updates),
                    last_turn=self.channel.last_turn,
                )

            if message.actions:
                action_ids = tuple(action.source.id for action in message.actions)
                self.session_manager.reserve_action_ids(session, action_ids)
                responses: list[Result | ProtocolError] = []
                for action in message.actions:
                    confirmation_request = self.dispatcher.confirmation_request(
                        action,
                        session,
                    )
                    if isinstance(confirmation_request, ConfirmationRequest):
                        if len(message.actions) != 1:
                            raise OrchestrationError(
                                "invalid_control_flow",
                                "A confirmation-requiring action must be the only action",
                            )
                        self.session_manager.request_confirmation(
                            session,
                            action.source,
                            confirmation_request.reason,
                            confirmation_request.guard,
                        )
                        self._publish_updates(source.say)
                        return self._confirmation_result(
                            session,
                            updates=updates,
                        )
                    if isinstance(confirmation_request, ProtocolError):
                        responses.append(confirmation_request)
                    else:
                        responses.append(self.dispatcher.execute(action, session))
                pending_errors = tuple(
                    response
                    for response in responses
                    if isinstance(response, ProtocolError)
                )
                if not all(isinstance(response, (Result, ProtocolError)) for response in responses):
                    raise OrchestrationError(
                        "invalid_tool_response",
                        "Dispatcher returned an unsupported response",
                    )
            else:
                pending_errors = ()
            pending_notices = ()
            self._publish_updates(source.say)

    def _set_terminal_status(
        self,
        session: Session,
        status: SessionStatus,
    ) -> None:
        self._before_terminal(session)
        self.session_manager.set_status(session, status)

    def _before_terminal(self, session: Session) -> None:
        del session

    def _exchange_with_retries(
        self,
        session: Session,
        *,
        turn: int,
        user_prompt: str | None,
        errors: tuple[ProtocolError, ...],
        notices: tuple[SystemNotice, ...],
    ) -> tuple[ValidatedMessage | None, ProtocolError | None]:
        retry_errors = errors
        retry_notices = notices
        final_action_id: str | None = None

        for attempt in range(self.limits.max_protocol_retries + 1):
            try:
                context = self.context_builder.build(
                    session,
                    turn=turn,
                    user_prompt=user_prompt,
                    errors=retry_errors,
                    notices=retry_notices,
                )
            except AEMLContextError as error:
                raise OrchestrationError(error.code, str(error)) from error
            except Exception as error:
                raise OrchestrationError(
                    "context_failed",
                    f"AEML context construction failed ({error.__class__.__name__})",
                ) from error

            try:
                message = self.channel.exchange(
                    context,
                    known_action_ids=session.state.used_action_ids,
                )
                return message, None
            except (AEMLTruncatedError, AEMLParseError) as error:
                failure_type = error.code
                final_action_id = error.action_id
                retry_errors = errors
                retry_notices = notices + (
                    self._repair_notice(failure_type, attempt),
                )
            except AEMLValidationError as error:
                final_action_id = self._safe_action_id(error.action_id)
                retry_errors = errors + (
                    ProtocolError(error.code, str(error), final_action_id),
                )
                retry_notices = notices
            except (AEMLChannelError, AEMLContextError) as error:
                raise OrchestrationError(error.code, str(error)) from error
            except Exception as error:
                raise OrchestrationError(
                    "transport_failed",
                    f"AEML transport failed ({error.__class__.__name__})",
                ) from error

        attempts = self.limits.max_protocol_retries + 1
        return None, ProtocolError(
            "malformed_output",
            f"Assistant output remained invalid after {attempts} attempt(s)",
            final_action_id,
        )

    def _repair_notice(self, failure_type: str, attempt: int) -> SystemNotice:
        return SystemNotice(
            failure_type,
            (
                ("attempt", str(attempt + 1)),
                ("remaining", str(self.limits.max_protocol_retries - attempt)),
            ),
        )

    @staticmethod
    def _safe_action_id(action_id: str | None) -> str | None:
        if isinstance(action_id, str) and ACTION_ID_PATTERN.fullmatch(action_id):
            return action_id
        return None

    def _validated_pending_action(self, session: Session):
        pending = session.state.pending_confirmation
        if pending is None:
            raise OrchestrationError(
                "confirmation_not_pending",
                "The session has no pending confirmation",
            )
        if pending.action.tool not in self.channel.prompt_builder.tool_specs:
            raise OrchestrationError(
                "confirmation_tool_unavailable",
                "The current channel does not enable the pending action's tool",
            )
        known_ids = set(session.state.used_action_ids)
        known_ids.discard(pending.action.id)
        message = AEMLMessage(
            turn=1,
            session=session.id,
            actions=(pending.action,),
            next=NextDirective.AWAIT_RESULT,
        )
        try:
            validated = AEMLValidator(self.dispatcher.tool_specs).validate(
                message,
                expected_turn=1,
                expected_session=session.id,
                known_action_ids=known_ids,
            )
        except AEMLValidationError as error:
            raise OrchestrationError(
                "invalid_pending_confirmation",
                f"Persisted pending action is no longer valid: {error}",
            ) from error
        if len(validated.actions) != 1:
            raise OrchestrationError(
                "invalid_pending_confirmation",
                "Persisted confirmation does not contain exactly one action",
            )
        return validated.actions[0]

    def _confirmation_result(
        self,
        session: Session,
        *,
        updates: list[str] | None = None,
    ) -> RunResult:
        pending = session.state.pending_confirmation
        if pending is None:
            raise OrchestrationError(
                "confirmation_not_pending",
                "The session has no pending confirmation",
            )
        question = (
            f"Approve action {pending.action.id!r} ({pending.action.tool})? "
            f"{pending.reason}. Denial leaves the target unchanged."
        )
        self._publish_updates(question)
        return RunResult(
            session=session,
            reason=RunStopReason.AWAITING_CONFIRMATION,
            updates=tuple(updates or ()),
            question=question,
            last_turn=self.channel.last_turn,
        )

    def _step_limit_result(
        self,
        session: Session,
        *,
        updates: list[str] | None = None,
    ) -> RunResult:
        question = (
            f"The session reached its {session.state.max_steps}-step limit. "
            "Approve additional steps explicitly to continue, or abort the session."
        )
        self._publish_updates(question)
        return RunResult(
            session=session,
            reason=RunStopReason.STEP_LIMIT,
            updates=tuple(updates or ()),
            question=question,
            last_turn=self.channel.last_turn,
        )

    def _load_managed_session(self, session: Session) -> Session:
        if not isinstance(session, Session):
            raise OrchestrationError("invalid_session", "A managed Session is required")
        expected_paths = self.session_manager.paths(session.id)
        if session.paths != expected_paths:
            raise OrchestrationError(
                "session_integrity_error",
                "Session belongs to another SessionManager",
            )
        return self.session_manager.load(session.id)

    @staticmethod
    def _require_user_prompt(user_prompt: str | None) -> None:
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise OrchestrationError(
                "invalid_user_prompt",
                "A non-empty human prompt is required to start or resume a run",
            )

    def _publish_updates(self, *messages: str | None) -> None:
        if self.message_sink is None:
            return
        for message in messages:
            if message is None:
                continue
            try:
                self.message_sink(message)
            except Exception as error:
                raise OrchestrationError(
                    "message_sink_failed",
                    f"Human message sink failed ({error.__class__.__name__})",
                ) from error


class ReadOnlyOrchestrator(AEMLOrchestrator):
    """Compatibility facade that permits only the seven read capabilities."""

    def __init__(
        self,
        session_manager: SessionManager,
        channel: AEMLChatChannel,
        *,
        dispatcher: ReadOnlyToolDispatcher | None = None,
        context_builder: AEMLContextBuilder | None = None,
        limits: OrchestrationLimits | None = None,
        message_sink: Callable[[str], None] | None = None,
    ) -> None:
        selected = dispatcher or ReadOnlyToolDispatcher(session_manager)
        if isinstance(selected, AgentToolDispatcher):
            raise TypeError("dispatcher must be a read-only dispatcher")
        super().__init__(
            session_manager,
            channel,
            dispatcher=selected,
            context_builder=context_builder,
            limits=limits,
            message_sink=message_sink,
        )


class AgentOrchestrator(AEMLOrchestrator):
    """Bounded agent loop with reads, output mutations, and sandboxed verification."""

    def __init__(
        self,
        session_manager: SessionManager,
        channel: AEMLChatChannel,
        *,
        dispatcher: AgentToolDispatcher | None = None,
        context_builder: AEMLContextBuilder | None = None,
        limits: OrchestrationLimits | None = None,
        message_sink: Callable[[str], None] | None = None,
    ) -> None:
        selected = dispatcher or AgentToolDispatcher(session_manager)
        if not isinstance(selected, AgentToolDispatcher):
            raise TypeError("dispatcher must be an AgentToolDispatcher")
        super().__init__(
            session_manager,
            channel,
            dispatcher=selected,
            context_builder=context_builder,
            limits=limits,
            message_sink=message_sink,
        )

    def shutdown_background(
        self,
        session: Session,
        *,
        reason: ProcessTerminationReason = ProcessTerminationReason.HOST_EXIT,
    ) -> None:
        dispatcher = self.dispatcher
        assert isinstance(dispatcher, AgentToolDispatcher)
        dispatcher.shutdown_background(session, reason=reason)

    def _before_terminal(self, session: Session) -> None:
        try:
            self.shutdown_background(
                session,
                reason=ProcessTerminationReason.SESSION_END,
            )
        except (SessionError, ToolExecutionError) as error:
            raise OrchestrationError(
                "background_shutdown_failed",
                f"Could not stop background work ({error.code})",
            ) from error
