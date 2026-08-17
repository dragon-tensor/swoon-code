# Swoon Code

Swoon Code combines a hosted ChatGPT conversation with a deterministic local AEML
interpreter. ChatGPT supplies structured instructions; the interpreter validates policy and
performs only explicitly implemented capabilities.

The web transport uses an authenticated ChatGPT browser session rather than an OpenAI API key.

**Experimental / educational.** Automated access may violate OpenAI's terms of service.

## Current status

Implemented foundations:

1. Browser-backed ChatGPT transport
2. Typed AEML models and tool schemas
3. Strict, resource-limited AEML parser
4. Protocol and argument validation
5. Isolated, resumable session management
6. Virtual-root path authorization
7. Read-only tool execution
8. Bounded AEML context, generated prompts, and validated single exchanges
9. Bounded read-only orchestration, protocol repair, and human pauses

The currently executable AEML tools are:

- `read-file`
- `list-dir`
- `grep`
- `git-status`
- `git-diff`
- `git-log`
- `list-dependencies`

Mutating tools and arbitrary command execution remain disabled. The Phase 9 library loop can
now drive the seven read capabilities until completion, a human question, a step-limit pause,
an explicit abort, or bounded protocol-repair exhaustion. The legacy browser-relay CLI remains
a direct chatbot relay; orchestration is currently exposed as a Python API.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m playwright install chromium
```

Export cookies from an authenticated ChatGPT session and save them as `cookies.json`. Both a
Cookie-Editor list and a Playwright storage-state object are accepted.

## Browser relay

```bash
# Interactive relay
./chatgpt.sh --cookies cookies.json -i

# Single prompt
./chatgpt.sh --cookies cookies.json -p "What is Rust?"

# Visible browser for debugging
./chatgpt.sh --cookies cookies.json --headed -v -p "Hello"
```

The legacy `chatgpt_agent.py` entrypoint remains compatible. The browser implementation is
`swoon.transport.ChatGPTWebTransport`.

## AEML foundation API

```python
from swoon import AEMLParser, AEMLValidator, ReadOnlyToolDispatcher, SessionManager

sessions = SessionManager("/private/session/storage")
session = sessions.create("/path/to/reference/project")

message = AEMLParser().parse(raw_assistant_response)
validated = AEMLValidator().validate(
    message,
    expected_turn=1,
    expected_session=session.id,
)

responses = ReadOnlyToolDispatcher(sessions).execute_message(validated, session)
```

Only validated read actions on the explicit Phase 7 allowlist can execute. Successes are
persisted for idempotent replay; failures are returned as structured protocol errors.

To send one protocol turn through the browser transport:

    from swoon import AEMLContextBuilder
    from swoon.transport import AEMLChatChannel, ChatGPTWebTransport

    browser = ChatGPTWebTransport("cookies.json")
    browser.start()
    try:
        channel = AEMLChatChannel(browser)
        context = AEMLContextBuilder().build(
            session,
            turn=1,
            user_prompt="Inspect this project.",
        )
        validated = channel.exchange(
            context,
            known_action_ids=session.state.used_action_ids,
        )
    finally:
        browser.close()

This returns an inert ValidatedMessage; it does not execute tools or continue automatically.

To let the interpreter own the validated read-only loop:

    from swoon import ReadOnlyOrchestrator, SessionManager
    from swoon.transport import AEMLChatChannel, ChatGPTWebTransport

    sessions = SessionManager("/private/session/storage")
    session = sessions.create("/path/to/reference/project")
    browser = ChatGPTWebTransport("cookies.json")
    browser.start()
    try:
        agent = ReadOnlyOrchestrator(sessions, AEMLChatChannel(browser))
        outcome = agent.run(session, "Inspect this project and explain its entry points.")
    finally:
        browser.close()

`outcome.reason` reports completion, a user pause, the step limit, abort, or protocol failure.
If a step limit is reached, only a later human-side call with `additional_steps=N` can extend
it. See the orchestration guide for pause/resume examples and exact failure semantics.

## Documentation

- `aeml_protocol_spec.md` — protocol contract
- `docs/session-management.md` — persistent session boundary
- `docs/path-policy.md` — virtual path policy
- `docs/read-only-tools.md` — Phase 7 execution behavior
- `docs/context-and-prompts.md` — Phase 8 context and transport bridge
- `docs/read-only-orchestration.md` — Phase 9 autonomous read-only loop
- `MIGRATION.md` — original relay history

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
