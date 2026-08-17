# Session management

Phase 5 introduces persistent, isolated session state without enabling any AEML tool execution.

## Logical and physical paths

The protocol always exposes these POSIX-style virtual roots:

```text
/input/<session_id>
/output/<session_id>
```

`SessionManager` maps them to private host directories under an application data directory. A
custom physical base can be supplied for tests or embedding, but it is never sent to the hosted
chatbot.

## Creating and loading

```python
from swoon.session import SessionManager

manager = SessionManager()
session = manager.create("/path/to/project", max_steps=40)

print(session.id)
print(session.paths.input_root)
print(session.paths.output_root)

resumed = manager.load(session.id)
```

The source project is copied into the session input directory. Symbolic links, hard links,
special files, excessive file counts, and excessive byte counts are rejected. Input files are
sealed with owner-read-only permissions; executable files retain owner execute permission.

These permissions are tamper evidence and defense in depth. The later command-execution phase
must additionally mount input read-only in its OS sandbox because a process running as the file
owner could otherwise change permission bits.

## Persisted state

`state.json` contains:

- Session identity, lifecycle status, timestamps, and revision
- Current and maximum step counts
- Persisted plan
- Completed action/result ledger with normalized action digests for idempotency
- Ordered result history
- Every action ID accepted for dispatch, including attempts that returned an error
- Chunk sequence state
- Background-process handles and output offsets
- One exact destructive action awaiting human confirmation, including its target guard

Updates use an exclusive per-session lock, an optimistic revision check, file `fsync`, atomic
replacement, and directory `fsync`. A stale in-memory session cannot overwrite a newer state.

The state loader validates the complete JSON schema and rejects mismatched IDs, invalid enums,
duplicate records, impossible timestamps, input-root chunks, broad state-file permissions, or a
writable/tampered input tree.

The current state schema is version 4. Versions 1 through 3 remain readable and are upgraded on
the next state update; older completed-result history seeds the durable used-action-ID set.
Version 4 adds the optional pending-confirmation record.

## Lifecycle operations

`SessionManager` currently provides:

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

`reserve_action_ids` is called before dispatch, so an attempted action ID cannot be reused after
a tool failure or process restart. `extend_step_limit` succeeds only while the session is
waiting at an exhausted limit; this keeps budget approval on the human-facing API side.

`request_confirmation` requires an already-reserved action ID and atomically changes the
session to `waiting_user`. A normal status transition cannot reopen it as active; orchestration
must approve/deny the exact persisted action, or abort. Successful approval and denial both
clear the pending record while recording the result. Terminal abort/completion also clear it.

None of these methods execute an AEML action or resolve an LLM-provided filesystem path. Policy,
tool execution, and orchestration remain separate boundaries.
