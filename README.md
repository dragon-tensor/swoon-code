# Swoon Code

Swoon Code combines a hosted ChatGPT conversation with a deterministic local AEML
interpreter. ChatGPT supplies structured instructions; the interpreter validates policy and
performs only explicitly implemented capabilities.

The web transport uses an authenticated ChatGPT browser session rather than an OpenAI API key.

> [!IMPORTANT]
> Swoon Code is an independent educational and research demonstration of AEML. It is not
> affiliated with, sponsored by, or endorsed by OpenAI. Each user is responsible for ensuring
> that their use complies with applicable law, account rules, and the terms of every connected
> service. If a provider does not permit a particular automation method, do not use that method.

The project is not intended to cause harm, facilitate violence or abuse, bypass safeguards or
access controls, or evade provider restrictions. Educational intent does not override third-party
terms. Read [Responsible use and project status](RESPONSIBLE_USE.md) before using the browser
transport, and treat exported browser cookies as account credentials.

Version 0.1 is an alpha release candidate. Its automated gates are offline; a build is not
live-verified until a release owner separately completes the authorized live check on the target
host.

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
13. Supervised background commands with bounded streaming and handle-scoped termination
14. Guarded output deletion, atomic relocation, and owner-private mode changes
15. Offline wheel packaging, installed-entrypoint smoke testing, and consumer diagnostics
16. Confirmed exact dependency declaration changes with atomic manifests and lockfile refusal
17. Apache-2.0 licensing, responsible-use guidance, and legal metadata in release artifacts
18. Owner-private browser credentials, opt-in debug artifacts, and an explicit supported scope
19. Human-side session listing, output export, guarded cleanup, and consumer retention guidance
20. Adversarial AEML corpus, documented threat model, installed-session smoke, and opt-in live gate
21. Reproducible wheel/source releases, SPDX SBOM, checksums, CI, governance, and draft publishing

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
- `run-command-background` (offline, disposable, and bounded)
- `stream-output` (opaque handle plus UTF-8 byte offset)
- `kill-process` (live session-owned handles only)
- `delete-file` (always requires real-human confirmation)
- `delete-dir` (bounded recursive deletion; always requires real-human confirmation)
- `move` (atomic output-only relocation to a missing destination)
- `rename` (atomic same-parent rename to a missing destination)
- `chmod` (regular files; owner-private `0600` or `0700` only)
- `install-dependency` (adds one exact registry declaration; never downloads package code)
- `remove-dependency` (removes one declaration; never invokes a package manager)

Writes are confined to the session output root; input stays read-only. Foreground and background
execution are offline and run against filtered disposable snapshots, so command-side filesystem
changes never persist. Background work is addressed only by an opaque session handle; it cannot
survive interpreter shutdown. Persistent deletion and relocation stay inside output and pass the
same no-follow policy boundary; deletion is guarded by a separate human decision. Git mutations,
package downloads, networked services, and persistent environment changes remain disabled.
Dependency tools can make one exact, human-confirmed manifest change only when no related lockfile
would become stale. The `swoon agent` command can drive these twenty-seven capabilities until
completion, a human question, guarded confirmation, a step-limit pause, an explicit abort, or
bounded protocol-repair exhaustion. The separate `swoon chat` command and legacy `chatgpt.sh`
wrapper remain direct chatbot relays.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m playwright install chromium
```

Until the editable install is run, the same source CLI is available as
`.venv/bin/python -m swoon`. Check the local consumer runtime with:

```bash
.venv/bin/python -m swoon doctor
```

Command tools additionally require compatible `bwrap` and `prlimit` executables on 64-bit Linux
(x86-64 or AArch64). They fail closed rather than falling back to unsandboxed
execution when those primitives are unavailable. Atomic `move`/`rename` additionally require a
Linux filesystem exposing `renameat2(RENAME_NOREPLACE)`; an unsupported host fails closed instead
of risking destination replacement.

While signed in and viewing `https://chatgpt.com/`, export that site's cookies and save them as
`cookies.json`. Do not export only from `auth.openai.com`: authentication-page cookies alone do
not establish a ChatGPT application session. Both a Cookie-Editor list and a Playwright
storage-state object are accepted, and the export must contain at least one `chatgpt.com` cookie.
On POSIX systems the file must be owner-only:

```bash
chmod 600 cookies.json
```

Choose an unencrypted JSON cookie-list export. Hotcleaner Cookie Editor encrypted-backup files
(`url`/`version`/encrypted `data`) are intentionally not decrypted or accepted. Do not provide an
export password to Swoon.

Refreshed storage state is not written automatically. Use `--save-storage-state` with a path in
an existing owner-private directory to opt in. Debug screenshots are also disabled by default;
`--debug-artifacts PRIVATE_DIRECTORY` enables uniquely named, owner-private captures.

When `--cookies` is supplied, `doctor` validates credential structure and rejects authentication-
site-only exports before Chromium starts. It never prints cookie values.

## Consumer artifacts

Build and test both consumer artifacts without downloading build tooling:

```bash
python3 scripts/build_wheel.py
python3 scripts/smoke_wheel.py dist/swoon_code-0.1.0-py3-none-any.whl
python3 scripts/build_sdist.py
python3 scripts/smoke_sdist.py dist/swoon_code-0.1.0.tar.gz
```

The wheel exposes the `swoon` console entry point; the source archive can rebuild that wheel. Both
are exercised in fresh, networkless environments. A complete clean-tree release rehearsal also
creates a direct-dependency SPDX document, artifact manifest, and checksums:

```bash
python3 scripts/release.py --out-dir /tmp/swoon-release
```

A real installation resolves Playwright and installs Chromium separately; the browser is not
bundled into these artifacts. See [Consumer build and test](docs/consumer-testing.md) for exact
source, package, browser, cookie, relay, and agent acceptance steps.

## Agent CLI

Create a session by importing an existing project as read-only input:

```bash
.venv/bin/swoon agent \
  --cookies cookies.json \
  --project /path/to/project \
  --prompt "Copy this project to output and add a health endpoint."
```

The command prints the session ID and private output path before starting the browser. If the agent
asks a question or reaches its step limit, the default interactive mode reads the human answer or
additional-step approval from the terminal. Resume a saved session with the same session storage
directory:

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

If a non-empty overwrite, deletion, or dependency declaration is waiting for guarded approval,
resume the exact stored action with `--approve-pending` or `--deny-pending`. Neither flag approves
future actions:

```bash
.venv/bin/swoon agent \
  --cookies cookies.json \
  --resume sess_EXAMPLE \
  --approve-pending \
  --non-interactive
```

The agent can copy, modify, move, rename, chmod, and—after a real-human decision—delete output
files, run offline foreground verification, and supervise bounded offline background jobs.
Command workspaces are disposable: builds, formatter edits, and other command-side changes are
discarded. It can add or remove confirmed exact dependency declarations, but it still cannot
download package artifacts, refresh lockfiles, mutate Git, expose a network service, or access
the network.

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

# Explicitly save refreshed credentials in an owner-private directory
mkdir -p -m 700 "$HOME/.local/share/swoon-code"
./chatgpt.sh --cookies cookies.json \
  --save-storage-state "$HOME/.local/share/swoon-code/refreshed-state.json" \
  -p "Hello"
```

The legacy `chatgpt_agent.py` entrypoint remains compatible. The browser implementation is
`swoon.transport.ChatGPTWebTransport`.

Swoon does not treat the first visible text as a completed reply. It waits until ChatGPT is no
longer showing a generation control and the assistant message has remained unchanged for five
seconds. `--timeout` is the maximum wait for the whole response; `--response-settle-time` adjusts
the unchanged-text window. If the maximum expires, Swoon stops the exchange and never submits a
repair prompt based on a partial response. Multiline AEML is inserted into the composer as one
atomic value and submitted once; embedded newlines are never emitted as Enter key events.

## Session results and cleanup

The agent prints both its session ID and private physical output path. It never changes the
original imported project. After a session is completed or aborted, inspect and export its output:

```bash
.venv/bin/swoon session list
.venv/bin/swoon session show sess_EXAMPLE
.venv/bin/swoon session export sess_EXAMPLE ./swoon-result
```

The export destination must not exist. Review the exported tree before applying it elsewhere.
Remove retained session data with an exact, separately confirmed command:

```bash
.venv/bin/swoon session delete sess_EXAMPLE
```

See [Consumer session management](docs/session-cli.md) for custom storage, automation, active-state
refusal, export validation, and retention behavior.

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
one capability-derived prompt/validator allowlist for the seven reads, eleven filesystem
mutations, two guarded dependency mutations, four disposable foreground tools, and three
supervised background tools. See the filesystem, lifecycle, dependency, foreground-command, and
background-command guides for the embedding API and safety boundaries.

## License

Swoon Code is licensed under the [Apache License 2.0](LICENSE). The source distribution and wheel
carry the SPDX identifier `Apache-2.0`, the complete license, and the project [NOTICE](NOTICE).
Third-party products and dependencies remain subject to their own licenses and terms.
See [Third-party notices](THIRD_PARTY_NOTICES.md) for the direct dependency and separately
installed runtime boundary.

The [responsible-use statement](RESPONSIBLE_USE.md) explains the project's educational purpose,
independence, credential precautions, and user responsibility. It is guidance rather than an
additional restriction on the Apache-2.0 license.

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
- `docs/background-commands.md` — Phase 13 supervised background lifecycle
- `docs/filesystem-lifecycle.md` — Phase 14 guarded delete/move/rename/chmod boundary
- `docs/consumer-testing.md` — Phase 15 wheel build, installation, doctor, and acceptance flow
- `docs/dependency-changes.md` — Phase 16 exact declaration and lockfile safety boundary
- `docs/supported-scope.md` — Phase 18 product, platform, and transport support boundary
- `docs/session-cli.md` — Phase 19 human-side output export and retained-session management
- `docs/security-model.md` — Phase 20 trust boundaries and adversarial/live release gates
- `docs/release-checklist.md` — Phase 21 offline, live, tag, and publication gates
- `RESPONSIBLE_USE.md` — educational purpose, non-affiliation, and user responsibility
- `SECURITY.md` — private vulnerability reporting and supported security scope
- `SUPPORT.md` — issue-reporting and redaction expectations
- `CONTRIBUTING.md` — development, testing, and contribution policy
- `CHANGELOG.md` — release-candidate history
- `THIRD_PARTY_NOTICES.md` — dependency and external-runtime notices
- `MIGRATION.md` — original relay history

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
python3 scripts/aeml_eval.py
python3 scripts/smoke_wheel.py
python3 scripts/smoke_sdist.py
python3 scripts/release.py --out-dir /tmp/swoon-release
```

These gates do not contact ChatGPT. A release owner can separately run the credentialed,
provider-authorized live gate described in `docs/security-model.md`; credentials are never put in
CI. Follow the complete [release checklist](docs/release-checklist.md) before publishing.
