# Agent CLI

Phase 10 exposed the bounded engine as `swoon agent` while preserving the original raw relay as
`swoon chat`. Phase 11 keeps that lifecycle and adds six output-only filesystem mutations:
`create-file`, `overwrite-file`, `append-file`, `edit-file`, `copy-file`, and `copy-dir`, in
addition to the seven read tools. Arbitrary commands and all other mutating schemas remain
disabled.

## Create a session

```bash
.venv/bin/swoon agent \
  --cookies cookies.json \
  --project /path/to/project \
  --prompt "Inspect the architecture and identify its entry points."
```

`--project` is optional. When supplied, the manager copies it into the session's sealed input
root. A new session receives a generated ID and a 40-step budget by default. Useful creation
options are:

- `--session-dir PATH` — override private persistent session storage;
- `--session-id sess_NAME` — assign a valid ID instead of generating one;
- `--max-steps N` — set the initial budget from 1 through 10,000;
- `--protocol-retries N` — allow 0 through 10 AEML repair attempts after the original response;
- `--headed` and `--verbose` — expose the browser and transport diagnostics;
- `--timeout SECONDS` — set the positive response timeout.

If `--prompt` is omitted, interactive mode asks for the initial task before creating a new
session. `--non-interactive` instead requires `--prompt` and returns exit 6 if it is absent.

The CLI prints `Session: sess_...` before browser startup. Keep that ID: browser or transport
failures leave durable state that can be inspected or resumed.

## Human questions and step limits

When AEML returns `<ask_user>`, interactive mode prints the question and waits for a non-empty
answer. Enter `/abort` or `/quit` to persist an aborted session. If the question occurred on the
last available step, the CLI retains the answer, asks for step approval, and sends the retained
answer only after the budget is extended.

At the step limit, interactive mode asks for a positive number of additional steps. The total
budget cannot exceed 10,000. This terminal input becomes the explicit human-side approval
required by `SessionManager.extend_step_limit`; neither AEML nor prompt text can extend the
budget.

## Destructive overwrite confirmation

An `overwrite-file` action targeting a non-empty file pauses independently of `<ask_user>` and
the step limit. The exact action and target guard are persisted before the CLI asks:

```text
Pending overwrite-file action 'overwrite1': ...
Approve this exact action? [y/N] (/abort to stop)>
```

Empty input means denial. Denial leaves the target untouched and returns a failure result to the
agent; `/abort` aborts the whole session. Approval fails closed with `confirmation_stale` if the
target changed while the prompt was waiting.

In non-interactive mode, an unavailable decision returns exit 6. A later process decides only
the stored action:

```bash
.venv/bin/swoon agent \
  --cookies cookies.json \
  --resume sess_EXAMPLE \
  --approve-pending \
  --non-interactive

# Or leave the target unchanged:
.venv/bin/swoon agent \
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
.venv/bin/swoon agent \
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
.venv/bin/swoon agent \
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
the orchestrator.

## Direct chat compatibility

The unstructured relay is separate from the AEML agent:

```bash
.venv/bin/swoon chat --cookies cookies.json --prompt "Hello"
.venv/bin/swoon chat --cookies cookies.json --interactive
```

Historical invocations without a subcommand still route to `chat`:

```bash
.venv/bin/swoon --cookies cookies.json -p "Hello"
./chatgpt.sh --cookies cookies.json -i
python chatgpt_agent.py --cookies cookies.json -p "Hello"
```

Raw chat responses are printed directly and never parsed or executed.
