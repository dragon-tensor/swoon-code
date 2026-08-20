# Session management

Phase 5 introduces persistent, isolated session state without enabling any AEML tool execution.

## Logical and physical paths

The protocol always exposes these POSIX-style virtual roots:

```text
/input/<session_id>
/output/<session_id>
```

The consumer-facing `WorkspaceSessionManager` maps a human name to matching visible folders:

```text
work/input/<name>
work/output/<name>
```

Crash-safe state and locks live separately under hidden `work/.sessions/`. Those physical metadata
paths are never sent to the hosted chatbot. The original nested `SessionManager` remains available
for embedding and backward-compatible tests.

## Creating and loading

```python
from swoon.session import WorkspaceSessionManager, session_id_for_workspace

manager = WorkspaceSessionManager("/path/to/work")
session = manager.create(session_id=session_id_for_workspace("my-project"), max_steps=40)

print(session.id)
print(session.paths.input_root)
print(session.paths.output_root)

resumed = manager.load(session.id)
```

Users may populate `work/input/my-project/` before creating the session. The manager validates and
adopts that tree, then seals it read-only. Alternatively, a source project can be copied into a new
named input directory. Symbolic links, hard links, special files, excessive file counts, and
excessive byte counts are rejected.

These permissions are tamper evidence and defense in depth. Phase 12 additionally copies a
credential-filtered input snapshot and mounts it read-only in the command sandbox, because a
process running as the file owner could otherwise change permission bits.

## Persisted state

`state.json` contains:

- Session identity, lifecycle status, timestamps, and revision
- Current and maximum step counts
- Persisted plan
- Completed action/result ledger with normalized action digests for idempotency
- Ordered result history
- Every action ID accepted for dispatch, including attempts that returned an error
- Chunk sequence state
- Background-process handles, bounded output counters, lifecycle reasons, and terminal metadata
- One exact destructive action awaiting human confirmation, including its target guard

Updates use an exclusive per-session lock, an optimistic revision check, file `fsync`, atomic
replacement, and directory `fsync`. A stale in-memory session cannot overwrite a newer state.

The state loader validates the complete JSON schema and rejects mismatched IDs, invalid enums,
duplicate records, impossible timestamps, input-root chunks, broad state-file permissions, or a
writable/tampered input tree.

The current state schema is version 5. Versions 1 through 4 remain readable and are upgraded on
the next state update; older completed-result history seeds the durable used-action-ID set.
Version 4 added the optional pending-confirmation record. Version 5 adds background output bytes,
line bounds, exit codes, termination reasons, end timestamps, and the terminal `lost` state.
Phase 14 needs no schema bump: lifecycle operations reuse existing chunk records and the existing
pending-confirmation shape.

## Lifecycle operations

`SessionManager` currently provides:

- `list_session_ids`
- `export_output`
- `delete_session`
- `advance_step`
- `extend_step_limit`
- `set_plan`
- `set_status`
- `reserve_action_ids`
- `record_action_result`
- `record_chunk`
- `request_confirmation`
- `clear_pending_confirmation`
- `register_process`
- `update_process`

The three human-side lifecycle methods do not expand AEML's tool registry. Export accepts only a
new destination outside session storage and only terminal state, reusing bounded safe-copy rules.
Deletion validates the exact session layout, refuses recorded running processes, and requires
terminal state unless the human-facing caller supplies a separate force decision. The CLI adds its
own deletion confirmation before calling this API.

`reserve_action_ids` is called before dispatch, so an attempted action ID cannot be reused after
a tool failure or process restart. `extend_step_limit` succeeds only while the session is
waiting at an exhausted limit; this keeps budget approval on the human-facing API side.

`request_confirmation` requires an already-reserved action ID and atomically changes the
session to `waiting_user`. A normal status transition cannot reopen it as active; orchestration
must approve/deny the exact persisted action, or abort. Successful approval and denial both
clear the pending record while recording the result. Terminal abort/completion also clear it.

`record_action_result` can apply lifecycle chunk metadata in the same locked state replacement as
the successful result. Delete removes records under its path; move/rename remaps a source prefix
to its destination and rejects duplicate destination records. Unfinished sequences are blocked
before any lifecycle mutation reaches this method.

Process PIDs are diagnostic data, not durable authority. Only the in-memory supervisor object
that launched a process may signal it. After an interpreter restart, a record still marked
`running` is reconciled to `lost` with reason `supervisor_lost`; the stored PID is never reused.
Output offsets and byte counts move only forward, and terminal status/reason/exit metadata is
immutable. This prevents PID reuse or state replay from turning `kill-process` into an arbitrary
host signal.

None of these methods execute an AEML action or resolve an LLM-provided filesystem path. Policy,
tool execution, and orchestration remain separate boundaries.
