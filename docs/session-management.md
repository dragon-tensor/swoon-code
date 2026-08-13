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
- Completed action/result ledger for idempotency
- Ordered result history
- Chunk sequence state
- Background-process handles and output offsets

Updates use an exclusive per-session lock, an optimistic revision check, file `fsync`, atomic
replacement, and directory `fsync`. A stale in-memory session cannot overwrite a newer state.

The state loader validates the complete JSON schema and rejects mismatched IDs, invalid enums,
duplicate records, impossible timestamps, input-root chunks, broad state-file permissions, or a
writable/tampered input tree.

## Lifecycle operations

`SessionManager` currently provides:

- `advance_step`
- `set_plan`
- `set_status`
- `record_action_result`
- `record_chunk`
- `register_process`
- `update_process`

None of these methods execute an AEML action or resolve an LLM-provided filesystem path. Those
capabilities remain separated until the path-policy and tool-execution phases.
