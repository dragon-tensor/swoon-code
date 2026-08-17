# Output filesystem mutations

Phase 11 adds the first writable AEML boundary. `AgentToolDispatcher` exposes the seven
read-only tools plus six filesystem mutations:

- `create-file`
- `overwrite-file`
- `append-file`
- `edit-file`
- `copy-file`
- `copy-dir`

All destinations are under the current session's output root. Input remains read-only, and
delete, move, rename, chmod, commands, package changes, builds, tests, and Git mutations remain
disabled.

## Operation semantics

| Tool | Source/target rule | Behavior |
|---|---|---|
| `create-file` | Missing output file; parent must exist | Publishes complete UTF-8 content without replacing an existing entry |
| `overwrite-file` | Existing regular output file | Atomically replaces content and preserves executable/non-executable mode |
| `append-file` | Existing regular output file | Rewrites old bytes plus UTF-8 content as one atomic replacement |
| `edit-file` | Existing UTF-8 output file | Replaces exactly one `old_str`; zero or multiple matches fail unchanged |
| `copy-file` | Existing input/output file to missing output file | Copies binary bytes interpreter-side and preserves executability |
| `copy-dir` | Existing input/output directory to output | Exclusively creates a missing destination, or copies into `output:.` only when it is empty |

Content in create/overwrite/append/edit actions defaults to a 512 KiB limit. Files read for
append/edit/copy default to 64 MiB. Directory copies default to 100,000 entries and 512 MiB in
total. These are interpreter limits and do not depend on the hosted model following a prompt.

## Filesystem boundary

Every path first passes `PathPolicy`. Mutation I/O then opens verified parent directories with
descriptor-relative no-follow operations. File creation writes and synchronizes a private
temporary inode before publishing it with no-replace link semantics. Overwrite, append, and
edit write and synchronize a sibling temporary file before atomic replacement. Created files
are owner-only (`0600`, or `0700` when copied/preserved as executable); directories are `0700`.

Symbolic links, hard-linked regular files, special files, non-portable paths, cross-session
paths, input write targets, and credential-shaped names fail closed. Directory copies omit
denied source entries such as `.env` and `.git/config`, so copying input to output does not make
those entries readable to later tools. A copy into an output descendant of its own source is
rejected.

Handled directory-copy failures remove entries created by that operation. A process or host
crash during a multi-file directory copy can still leave an incomplete destination; unlike a
single-file replacement, an entire directory tree cannot be committed atomically on every
supported filesystem. No successful action result is persisted in that case.

## Destructive overwrite confirmation

Replacing a non-empty file requires two independent signals:

1. AEML must declare `<expect_confirm>true</expect_confirm>`.
2. The embedding host must approve the persisted action through `confirmation=True` or the CLI.

The session stores the exact raw action, reserved action ID, reason, timestamp, and an opaque
guard derived from the target's identity, metadata, and content hash. Status becomes
`waiting_user`. Approval can therefore occur in another process without trusting the model to
repeat the action. If the target changes while waiting, approval returns
`confirmation_stale` and nothing is overwritten. A denial records a failure result and leaves
the file untouched.

```python
outcome = agent.run(session, "Update the configuration")
if outcome.reason.value == "awaiting_confirmation":
    outcome = agent.run(outcome.session, None, confirmation=True)
```

The terminal agent asks interactively. A non-interactive run exits with code 6 and can later be
resumed with exactly one of:

```bash
swoon agent --cookies cookies.json --resume sess_EXAMPLE --approve-pending --non-interactive
swoon agent --cookies cookies.json --resume sess_EXAMPLE --deny-pending --non-interactive
```

These flags decide only the action already stored in that session. They are rejected for a new
session or a resumed session without a pending action.

## Chunk sequences

`create-file` or `overwrite-file` can start a sequence with `seq="1"`. An unfinished sequence
must continue on the same path through `append-file` with the exact next sequence. The file,
chunk advancement, and successful action result are committed to session state together after
the filesystem operation. Reads, edits, copies, dependency inspection, and Git diffs that
depend on an unfinished output are blocked with `write_incomplete` until `final="true"`.

## APIs

`ReadOnlyToolDispatcher` and `ReadOnlyOrchestrator` remain available and reject all mutations.
Writable embedding code must opt into the broader dispatcher, prompt schemas, and orchestrator:

```python
from swoon import AEMLPromptBuilder, AgentOrchestrator, AgentToolDispatcher
from swoon.transport import AEMLChatChannel

dispatcher = AgentToolDispatcher(sessions)
channel = AEMLChatChannel(
    browser,
    prompt_builder=AEMLPromptBuilder(dispatcher.tool_specs),
)
agent = AgentOrchestrator(sessions, channel, dispatcher=dispatcher)
outcome = agent.run(session, "Copy the input project and update app.py")
```

The prompt and validator derive their schemas from the same executable allowlist. Merely adding
a tool to the protocol registry never makes it executable.
