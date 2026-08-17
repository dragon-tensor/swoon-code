# Read-only AEML orchestration

Phase 9 composes the session manager, bounded context builder, session-bound chat channel, and
read-only dispatcher into an autonomous but bounded loop. It enables no new machine capability:
only the seven Phase 7 read tools can execute, and construction fails if the channel advertises
a mutating, executing, or unimplemented schema.

```text
human prompt
    │
    ▼
advance one session step ──► build context ──► exchange + validate
                                                    │
                         malformed response ────────┤ retry same turn
                                                    │
                         read actions ◄─────────────┘
                              │
                              ▼
                    reserve IDs, dispatch reads
                              │
                              └──────── results/errors into next context
```

The loop returns only at a defined stop: `<complete>`, `<next>done</next>`, `<ask_user>`, the
step limit, `<next>abort</next>`, or protocol-retry exhaustion. Transport and local invariant
failures raise `OrchestrationError` instead of being confused with a normal stop.

## API

```python
from swoon import ReadOnlyOrchestrator, SessionManager
from swoon.transport import AEMLChatChannel, ChatGPTWebTransport

sessions = SessionManager("/private/session/storage")
session = sessions.create("/path/to/reference/project", max_steps=40)

browser = ChatGPTWebTransport("cookies.json")
browser.start()
try:
    orchestrator = ReadOnlyOrchestrator(
        sessions,
        AEMLChatChannel(browser),
        message_sink=print,  # optional: receives <say>, questions, and completion text
    )
    outcome = orchestrator.run(session, "Inspect the project architecture.")
finally:
    browser.close()
```

`RunResult` contains the updated managed session, `reason`, non-final `updates`, an optional
human `question`, optional completion `summary`, optional terminal protocol `error`, and the
last successfully validated conversation turn. `<thought>` is discarded: it is never sent to
the message sink or persisted in session state.

| Reason | Persisted status | Meaning |
|---|---|---|
| `completed` | `completed` | The chatbot returned `<complete>` |
| `done` | `completed` | The chatbot returned `<next>done</next>` |
| `awaiting_user` | `waiting_user` | A real human must answer `<ask_user>` |
| `step_limit` | `waiting_user` | A human must explicitly approve more steps |
| `aborted` | `aborted` | The chatbot returned `<next>abort</next>` |
| `protocol_error` | `aborted` | Valid AEML was not produced within the repair budget |

## Protocol turns, retries, and actions

One new AEML turn consumes one persisted session step. A malformed, truncated, or semantically
invalid response is repaired on the same AEML turn and does not consume another step. The
default is the original attempt plus two retries. Parse/truncation feedback is returned as a
`system_notice`; validation feedback is returned as a structured `error`. If all attempts fail,
the session is aborted with `malformed_output`.

Validated action IDs are reserved atomically before any read executes. Successful results are
stored in the action ledger. Tool failures enter the next context as structured errors, while
their IDs remain reserved across reloads. A batch is reserved as one unit before its tools run.

The conversation turn counter belongs to the channel, while the step counter belongs to the
persisted session. A newly attached channel therefore starts at AEML turn 1 even when a resumed
session already consumed earlier steps.

## Human pauses and budget approval

Resume a normal `<ask_user>` pause by passing the real answer:

```python
if outcome.reason.value == "awaiting_user":
    outcome = orchestrator.run(outcome.session, input(outcome.question + " "))
```

At the step limit, a prompt alone cannot resume execution. The embedding application must make
the approval explicit:

```python
if outcome.reason.value == "step_limit":
    outcome = orchestrator.run(
        outcome.session,
        "Continue with the current task.",
        additional_steps=5,
    )
```

The manager accepts an extension only while status is `waiting_user` and the old budget is fully
exhausted. The total maximum remains 10,000 steps. An active agent cannot grant itself more
steps through AEML or user-prompt text.

## Failure behavior

- Read tools retain their one silent retry for errors classified as transient.
- Chat protocol repair is separately bounded and never reruns a validated tool action.
- A transport exception is not silently retried. The started session step remains consumed and
  the session remains active so the embedding application can decide whether to reconnect.
- Only one `run()` call may be active on an orchestrator, and each channel permits one exchange.
- A channel already bound to another session is rejected.
- `message_sink` failures raise `message_sink_failed`; lifecycle changes already made remain
  durable.

This Phase 9 compatibility API remains intentionally read-only. Phase 11 adds a separate
`AgentOrchestrator`/`AgentToolDispatcher` opt-in for six output filesystem mutations. Phase 12
adds four offline foreground verification tools, and Phase 13 adds three supervised background
tools to that broader agent dispatcher. Package installation, networked services, and Git writes
still require later sandbox phases.

Phase 10 exposes this API through `swoon agent`, including session creation/resume, terminal
answers, explicit step approval, and deterministic exit codes. See `agent-cli.md`.
