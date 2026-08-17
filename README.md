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
10. Agent CLI with session create/resume and interactive lifecycle handling
11. Output-only filesystem mutation with atomic file writes and resumable confirmation
12. Offline foreground command, build, test, and linter sandboxing

The currently executable AEML tools are:

- `read-file`
- `list-dir`
- `grep`
- `git-status`
- `git-diff`
- `git-log`
- `list-dependencies`
- `create-file`
- `overwrite-file` (real-human confirmation for a non-empty target)
- `append-file`
- `edit-file`
- `copy-file`
- `copy-dir`
- `run-command` (shell-free argv execution in a disposable sandbox)
- `run-build`
- `run-tests`
- `run-linter`

Writes are confined to the session output root; input stays read-only. Foreground execution is
offline and runs against filtered disposable snapshots, so command-side filesystem changes never
persist. Delete/move/chmod, Git mutations, package changes, background processes, and persistent
environment changes remain disabled. The `swoon agent` command can drive these seventeen
capabilities until completion, a human question,
destructive confirmation, a step-limit pause, an explicit abort, or bounded protocol-repair
exhaustion. The separate `swoon chat` command and legacy `chatgpt.sh` wrapper remain direct
chatbot relays.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m playwright install chromium
```

Foreground command tools additionally require compatible `bwrap` and `prlimit` executables on
64-bit Linux (x86-64 or AArch64). They fail closed rather than falling back to unsandboxed
execution when those primitives are unavailable.

Export cookies from an authenticated ChatGPT session and save them as `cookies.json`. Both a
Cookie-Editor list and a Playwright storage-state object are accepted.

## Agent CLI

Create a session by importing an existing project as read-only input:

```bash
.venv/bin/swoon agent \
  --cookies cookies.json \
  --project /path/to/project \
  --prompt "Copy this project to output and add a health endpoint."
```

The command prints the session ID before starting the browser. If the agent asks a question or
reaches its step limit, the default interactive mode reads the human answer or additional-step
approval from the terminal. Resume a saved session with the same session storage directory:

```bash
.venv/bin/swoon agent \
  --cookies cookies.json \
  --resume sess_EXAMPLE \
  --prompt "Continue the inspection."
```

For scripted use, `--non-interactive` returns exit code 6 when human input is required. An
exhausted session can be resumed with explicit approval:

```bash
.venv/bin/swoon agent \
  --cookies cookies.json \
  --resume sess_EXAMPLE \
  --additional-steps 5 \
  --prompt "Continue." \
  --non-interactive
```

If a non-empty overwrite is waiting for destructive approval, resume the exact stored action
with `--approve-pending` or `--deny-pending`. Neither flag approves future actions:

```bash
.venv/bin/swoon agent \
  --cookies cookies.json \
  --resume sess_EXAMPLE \
  --approve-pending \
  --non-interactive
```

The agent can copy and modify output files and can run offline foreground verification commands.
Command workspaces are disposable: builds, formatter edits, and other command-side changes are
discarded. It still cannot delete persistent files, install packages, mutate Git, start background
processes, or access the network.

## Browser relay

```bash
# Explicit relay command
.venv/bin/swoon chat --cookies cookies.json -i

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

This compatibility example executes only validated reads on the Phase 7 allowlist. Successes are
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

To let the interpreter own a read-only compatibility loop:

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

The `swoon agent` command instead opts into `AgentToolDispatcher` and `AgentOrchestrator`, using
one capability-derived prompt/validator allowlist for the seven reads, six filesystem mutations,
and four disposable foreground execution tools. See the filesystem-mutation and foreground-command
guides for the embedding API and safety boundaries.

## Documentation

- `aeml_protocol_spec.md` — protocol contract
- `docs/session-management.md` — persistent session boundary
- `docs/path-policy.md` — virtual path policy
- `docs/read-only-tools.md` — Phase 7 execution behavior
- `docs/context-and-prompts.md` — Phase 8 context and transport bridge
- `docs/read-only-orchestration.md` — Phase 9 autonomous read-only loop
- `docs/agent-cli.md` — Phase 10 command-line lifecycle and exit codes
- `docs/filesystem-mutations.md` — Phase 11 write boundary and confirmation lifecycle
- `docs/foreground-commands.md` — Phase 12 offline foreground execution boundary
- `MIGRATION.md` — original relay history

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
