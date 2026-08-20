# Agent CLI

Phase 10 exposed the bounded engine as `swoon agent` while preserving the original raw relay as
`swoon chat`. Phase 11 keeps that lifecycle and adds six output-only filesystem mutations:
`create-file`, `overwrite-file`, `append-file`, `edit-file`, `copy-file`, and `copy-dir`, in
addition to the seven read tools. Phase 12 adds four offline foreground tools: `run-command`,
`run-build`, `run-tests`, and `run-linter`. Their filesystems are disposable, so only explicit
filesystem tools persist output changes. Phase 13 adds `run-command-background`, `stream-output`,
and `kill-process` with the same disposable, offline boundary. Package operations, Git mutations,
and networked services stay disabled. Phase 14 adds persistent `delete-file`, `delete-dir`,
`move`, `rename`, and restricted `chmod`; deletion reuses the exact-action decision flow below.
Phase 16 adds guarded `install-dependency` and `remove-dependency` declaration changes. Despite
their protocol names, they do not download artifacts, execute package managers, or refresh locks.

On supported 64-bit Linux hosts, foreground execution requires `bwrap`, `prlimit`, and a system
`python3`. Missing sandbox primitives return a tool error to the agent; the CLI never falls back
to running a command directly on the host. See `foreground-commands.md` and
`background-commands.md` for exact isolation and lifecycle semantics.

## Installation diagnostic

Phase 15 adds a consumer check that does not contact ChatGPT:

```bash
swoon doctor
swoon doctor --cookies cookies.json --launch-browser
```

The default check verifies the Python package, installed Playwright/Chromium files, and optional
command-sandbox executables. Supplying `--cookies` validates the local JSON without printing its
contents. On POSIX systems, validation rejects symbolic links and group/other-readable credential
files; use `chmod 600 cookies.json`. Export while signed in and viewing `https://chatgpt.com/`.
An export containing only `auth.openai.com` cookies is rejected because it cannot establish the
ChatGPT application session. `--launch-browser` additionally opens and closes headless Chromium;
run that stronger probe from the same host terminal that will run the agent, since containers can
deny browser launch even when the browser is correctly installed.

The actual relay performs a second, live check after navigation. If ChatGPT displays its logged-
out controls, Swoon stops before sending the prompt and reports that fresh `chatgpt.com` cookies
are required. Use `--debug-artifacts` only when needed; screenshots may contain account or
conversation information.

## Create a session

For consumers, create `work/input/NAME/`, place source files there, and launch or resume the same
named interactive agent with:

```bash
swoon NAME
```

With no name, `swoon` uses the `default` workspace. Both commands keep a visible Chromium window
alive for the interactive session so a Cloudflare human-verification check can be completed. The
matching output is always visible at `work/output/NAME/`; internal state remains hidden under
`work/.sessions/`. Use `--headless` only when the environment can load ChatGPT without a human
check.

The expanded developer interface remains available:

```bash
swoon agent \
  --cookies cookies.json \
  --project /path/to/project \
  --prompt "Inspect the architecture and identify its entry points."
```

`--project` is optional. When supplied, the manager copies it into the session's sealed input
root. A new session receives a generated ID and a 40-step budget by default. Useful creation
options are:

- `--session-dir PATH` — override private persistent session storage;
- `--work-dir PATH` — override the named consumer `work` root;
- `--name NAME` — create or automatically resume matching input/output folders;
- `--session-id sess_NAME` — assign a valid ID instead of generating one;
- `--max-steps N` — set the initial budget from 1 through 10,000;
- `--protocol-retries N` — allow 0 through 10 AEML repair attempts after the original response;
- `--interactive` — keep one session open and accept successive coding tasks at
  `[user@swoon-code]`;
- `--headed` — explicitly select the agent's default visible-browser mode;
- `--headless` — hide the browser, with no ability to complete a human-verification check;
- `--verbose` — expose transport diagnostics;
- `--save-storage-state PATH` — explicitly persist refreshed credentials with owner-only mode;
- `--debug-artifacts DIRECTORY` — explicitly allow uniquely named private startup screenshots;
- `--timeout SECONDS` — set the positive maximum wait for a complete response;
- `--response-settle-time SECONDS` — require this many unchanged seconds after generation stops
  before accepting a response (default: 5, and it must be shorter than `--timeout`).

Storage-state persistence and screenshots are disabled by default because both can contain account
or conversation information. Their parent directories must already be owner-private. Cleanup or
storage failures are reported rather than silently ignored.

The browser transport refuses to submit a new AEML turn while ChatGPT exposes a visible generation
control. It then waits for the new assistant message to remain unchanged for the configured settle
window. A response timeout is a hard failure: partial output is not parsed and cannot trigger an
automatic repair prompt. Each multiline AEML prompt is filled atomically and submitted with one
explicit Send click, so its line endings cannot become accidental submissions.

Without `--interactive`, omitting `--prompt` asks once for the initial task before creating a new
session. `--interactive` starts a persistent terminal coding-agent console instead: each completed
task returns to `[user@swoon-code]` in the same session, retaining its output, browser conversation,
and validated action history. Direct messages use `[swoon-code]`; `-->> [plan]` shows the explicit
AEML plan, and `>>` shows live tool activity and results. ANSI contrast and red/green/yellow severity
are enabled on capable terminals; `NO_COLOR=1` keeps the semantic prefixes but disables color. The
private AEML `<thought>` field is never displayed. Enter `/quit` to pause the session for
`swoon NAME` to resume later, or `/abort` to make it terminal. The
`--non-interactive` mode instead requires `--prompt` and returns exit 6 if it is absent.

The CLI prints `Session: sess_...` and the human-side physical output path before browser startup.
Keep the ID: browser or transport failures leave durable state that can be inspected or resumed.
Use `swoon session show`, `swoon session export`, and `swoon session delete` as documented in
`session-cli.md`; those commands do not start a browser.

## Human questions and step limits

When AEML returns `<ask_user>`, interactive mode prints the question and waits for a non-empty
answer. Enter `/abort` or `/quit` to persist an aborted session. If the question occurred on the
last available step, the CLI retains the answer, asks for step approval, and sends the retained
answer only after the budget is extended.

At the step limit, interactive mode asks for a positive number of additional steps. The total
budget cannot exceed 10,000. This terminal input becomes the explicit human-side approval
required by `SessionManager.extend_step_limit`; neither AEML nor prompt text can extend the
budget.

## Destructive confirmation

An `overwrite-file` action targeting a non-empty file, every `delete-file` or `delete-dir`, and
every dependency declaration change pauses independently of `<ask_user>` and the step limit. The
exact action and target guard are persisted before the CLI asks:

```text
Pending overwrite-file action 'overwrite1': ...
Approve this exact action? [y/N] (/abort to stop)>
```

Empty input means denial. Denial leaves the target untouched and returns a failure result to the
agent; `/abort` aborts the whole session. Approval fails closed with `confirmation_stale` if the
target or guarded directory tree changed while the prompt was waiting.

In non-interactive mode, an unavailable decision returns exit 6. A later process decides only
the stored action:

```bash
swoon agent \
  --cookies cookies.json \
  --resume sess_EXAMPLE \
  --approve-pending \
  --non-interactive

# Or leave the target unchanged:
swoon agent \
  --cookies cookies.json \
  --resume sess_EXAMPLE \
  --deny-pending \
  --non-interactive
```

The flags are mutually exclusive, require `--resume`, and fail if no confirmation is pending.
They do not approve later destructive actions.

## Resume

Use the same physical session directory when it was customized:

```bash
swoon agent \
  --cookies cookies.json \
  --session-dir /private/session/storage \
  --resume sess_EXAMPLE \
  --prompt "The answer is blue."
```

A new CLI process creates a new browser channel whose conversation-local AEML counter starts at
turn 1. Persisted steps, plans, results, and used action IDs remain attached to the session and
are injected into the new context.

`--max-steps` and `--session-id` apply only to new sessions. To resume an exhausted session from
a script, approval must be explicit:

```bash
swoon agent \
  --cookies cookies.json \
  --resume sess_EXAMPLE \
  --additional-steps 5 \
  --prompt "Continue the current task." \
  --non-interactive
```

`--additional-steps` is valid only with `--resume` and only when the persisted budget is fully
exhausted. A non-exhausted `<ask_user>` pause does not permit a budget extension.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | `<complete>` or `<next>done</next>` |
| 2 | Invalid command-line arguments or option combinations |
| 3 | User or AEML abort |
| 4 | AEML remained invalid after the configured repair attempts |
| 5 | Session, browser, transport, or local orchestration failure |
| 6 | Human answer, destructive decision, or step approval is required but unavailable |
| 130 | Interrupted while browser/orchestration work was active |

On exit 6, the session remains `waiting_user` and can be resumed. Protocol exhaustion persists
`aborted`; a transport error leaves the session active and retains any step already started by
the orchestrator. Before any CLI exit, live background work is terminated and its final status is
persisted with reason `host_exit`. Agent completion or abort uses `session_end`. A later CLI
process never signals a PID loaded from disk; a stale running record becomes `lost` instead.

## Direct chat compatibility

The unstructured relay is separate from the AEML agent:

```bash
swoon chat --cookies cookies.json --prompt "Hello"
swoon chat --cookies cookies.json --interactive
```

Historical invocations without a subcommand still route to `chat`:

```bash
swoon --cookies cookies.json -p "Hello"
./chatgpt.sh --cookies cookies.json -i
python chatgpt_agent.py --cookies cookies.json -p "Hello"
```

Raw chat responses are printed directly and never parsed or executed.
