# Consumer session management

Phase 19 exposes persisted work to the human without giving AEML any new authority. Session
management commands are local operations and never start a browser or contact a hosted service.
They use the same private application-data directory as `swoon agent`; pass `--session-dir` when
the agent used a custom location.

## Locate work

List sessions in deterministic ID order:

```bash
swoon session list
swoon session list --session-dir /private/session/storage
```

The table includes lifecycle status, consumed/maximum steps, and the last state-update timestamp.
A candidate directory with invalid state is reported as an error instead of being silently treated
as a healthy session.

Inspect one session and reveal its human-side physical paths:

```bash
swoon session show sess_EXAMPLE
```

The physical paths are printed only to the local terminal. AEML continues to see the virtual
`/input/<session>` and `/output/<session>` roots.

## Export output

The imported project is never modified in place. Export a completed or aborted session's output
to a destination that does not yet exist:

```bash
swoon session export sess_EXAMPLE ./swoon-result
```

Export rejects active/waiting sessions, symbolic links, hard links, special files, excessive
trees, an existing destination, and any destination inside session storage. It copies regular
bytes without an LLM round trip, preserves only owner-private executable/non-executable modes,
and removes a partial destination if validation fails.

Review the exported tree before copying or applying it to a real project. Command-sandbox changes
remain disposable; only explicit AEML filesystem operations appear in session output.

## Delete retained data

Completed and aborted sessions can be removed interactively:

```bash
swoon session delete sess_EXAMPLE
```

For automation, the confirmation must be explicit:

```bash
swoon session delete sess_EXAMPLE --yes
```

Non-terminal sessions are rejected by default. `--force-active --yes` exists for an abandoned
session after the user has independently established that no Swoon process is using it. A state
record containing running background work is never deleted, even with that flag. Deletion first
isolates the exact validated session directory under a randomized tombstone and then removes it;
it never accepts a path in place of a validated session ID.

Session data can contain imported source, generated source, prompts/results, plans, and command
output. Export what is needed and delete stale sessions according to the user's own retention and
privacy requirements.
