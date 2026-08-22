# Supervised background commands

`AgentToolDispatcher` exposes three supervised background capabilities:

- `run-command-background`
- `stream-output`
- `kill-process`

They support long-running offline checks and watchers without blocking an AEML turn. They are not
host processes, persistent jobs, or networked dev servers. Every command runs inside the same
filtered, disposable Bubblewrap boundary used by foreground execution: socket creation
is denied, output/input snapshots are isolated, and command-side filesystem changes are discarded.

## Launch and ownership

`run-command-background` accepts one shell-free `cmd` and an optional `max_output_lines`. A
successful result returns an opaque handle such as `proc_...`; it never returns a PID as control
authority. The interpreter retains the sandbox snapshot and a live `Popen` object while a daemon
supervisor captures combined stdout/stderr.

Only that exact in-memory supervisor may signal the process. The registry key contains both the
physical managed-session root and the opaque handle, so a handle from another session cannot be
used. A PID is persisted only for diagnostics. If state says `running` after the launching
interpreter is gone, reconciliation records `lost`/`supervisor_lost` and never calls `kill` with
the persisted PID. This is important because an operating system may have reassigned it.

Launch also retains only a disposable sandbox workspace. New files, edits, build artifacts, and
formatter changes disappear when the process terminates. Persistent changes still require the
explicit filesystem tools.

## Output streaming

The supervisor incrementally decodes combined output as UTF-8 with replacement for malformed
bytes, replaces XML-forbidden controls, and writes a private `0600` session log. Capture stops at
the configured byte or launch-time line bound; crossing either bound kills the process with reason
`output_limit`.

`stream-output` accepts:

- the exact process `handle`;
- an optional non-negative `offset` in sanitized UTF-8 bytes;
- an optional `max_output_lines` for this response.

When `offset` is omitted, streaming continues from the furthest persisted offset. The result
reports `next_offset`, the stable `output_bytes` snapshot, current status, exit code, and
termination reason. A response is `partial` when more captured bytes remain. Callers continue
with `next_offset`; an offset in the middle of a UTF-8 character fails with `invalid_offset`.
Reads use only the byte prefix committed by the supervisor, so a concurrent write cannot expose a
partially written character.

The furthest cursor and the corresponding AEML action result are committed in one session-state
update. If result persistence fails, the cursor does not advance, so a later stream cannot skip
bytes the agent never received.

Logs are opened without following links and must remain private, single-link regular files within
the capture bound. They remain available after normal exit, explicit kill, limit termination, or
CLI shutdown so a later turn or invocation can read the final bounded output.

## Termination and status

`kill-process` accepts only a session record's opaque handle. A live owned process is terminated
as a process group and persisted as `killed` with reason `user`. Calling it for an already terminal
record returns a structured failure without sending another signal.

Persisted statuses are:

| Status | Meaning |
|---|---|
| `running` | Last reconciled as active; control still requires the matching live supervisor |
| `exited` | The command ended without an interpreter-requested kill; exit code may be nonzero |
| `killed` | User, limit, lifecycle, or supervisor handling terminated it |
| `lost` | State survived but its in-memory supervisor did not |

Termination reasons are `exited`, `user`, `output_limit`, `runtime_limit`, `session_end`,
`host_exit`, `supervisor_error`, and `supervisor_lost`. Terminal timestamps and metadata are
immutable; output counters and stream offsets are monotonic.

`AgentOrchestrator` stops live work before marking a session completed or aborted. The CLI also
stops and persists all live work in a `finally` path before browser/host exit, including
non-interactive human pauses and transport failures. A process can therefore span AEML turns and
interactive waits only while the same interpreter remains alive; it is deliberately not a daemon
that survives Swoon.

## Bounds and failure behavior

Defaults from `CommandToolLimits` are:

| Boundary | Default |
|---|---:|
| background wall runtime | 3,600 seconds |
| startup readiness timeout | 30 seconds |
| captured combined output | 8 MiB |
| launch output lines | 10,000 |
| concurrently running records | 8 per session |
| lifetime process records | 128 per session |
| one streamed AEML result | 64 KiB / 1,000 lines when omitted |

The foreground CPU, memory, file, process, descriptor, snapshot, and tmpfs limits still apply.
An unfinished output chunk blocks a new background launch because its snapshot would be
ambiguous; streaming and killing an existing handle remain available.

The interpreter waits for a trusted in-sandbox readiness marker before publishing a handle. A
missing runtime, sandbox setup failure, startup timeout, or pre-readiness output overflow returns a
tool error and cleans up the process, log, and temporary snapshot. No failure path falls back to
host execution.

## Embedding API

```python
from swoon import AgentToolDispatcher, CommandToolLimits

dispatcher = AgentToolDispatcher(
    sessions,
    command_limits=CommandToolLimits(
        background_max_runtime_seconds=900,
        background_default_output_lines=5_000,
        max_background_processes=4,
        max_background_records=64,
    ),
)
```

Use `dispatcher.tool_specs` for both prompt generation and validation. Embeddings that stop while
a session is still active should call `dispatcher.shutdown_background(session, reason=...)`.
The global exit hook is a final process-kill safeguard, but explicit shutdown is what can persist
an exact lifecycle reason before storage closes.
