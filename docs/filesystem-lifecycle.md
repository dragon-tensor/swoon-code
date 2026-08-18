# Persistent filesystem lifecycle

Phase 14 adds five output-only tools to `AgentToolDispatcher`:

- `delete-file`
- `delete-dir`
- `move`
- `rename`
- `chmod`

They extend the Phase 11 mutation boundary rather than the command sandbox. Their changes persist
in the session output root; input remains read-only. The read-only compatibility dispatcher still
advertises and executes none of them.

## Semantics

| Tool | Accepted target | Behavior |
|---|---|---|
| `delete-file` | Existing regular output file, never the root | Always pauses for a real-human decision, then unlinks the exact guarded entry |
| `delete-dir` | Existing output directory, never the root | Always pauses, validates a bounded safe tree, then removes it recursively |
| `move` | Existing output file/directory to a missing output path | Atomically relocates the entry without replacement |
| `rename` | Existing output file/directory to a missing name in the same parent | Uses the same atomic no-replace operation; cross-parent requests must use `move` |
| `chmod` | Existing regular output file | Sets exactly owner-private `0600` or `0700`; directories and broader modes are rejected |

`move` and `rename` do not merge directories and never overwrite a destination. On Linux they use
`renameat2(RENAME_NOREPLACE)` between already-verified parent descriptors. If that atomic primitive
or the filesystem support is unavailable, the tool returns `platform_unsupported`; there is no
check-then-overwrite fallback. Moving a directory into itself or a descendant is rejected.

## Delete confirmation

Both delete schemas require `<expect_confirm>true</expect_confirm>`. That declaration is only the
model's request for a decision; it is not approval. Before yielding to the host, the interpreter:

1. resolves the output path through `PathPolicy`;
2. opens it through descriptor-relative, no-follow access;
3. builds a bounded metadata snapshot of the exact file or complete directory tree;
4. persists the raw action, reserved action ID, human-readable impact, timestamp, and opaque
   snapshot guard; and
5. changes the session to `waiting_user`.

The guard includes virtual path, entry type, device/inode identity, owner, mode, link count, size,
and nanosecond change/modify timestamps. Directory guards include every descendant in sorted path
order. Approval recomputes the guard. A changed file, added/removed child, permission change, or
replacement returns `confirmation_stale` without beginning deletion.

The default directory preflight is limited by `MutationToolLimits.max_copy_entries` (100,000),
`max_copy_bytes` (512 MiB of regular-file logical size), and `max_lifecycle_depth` (256 levels).
Exceeding a bound returns `lifecycle_too_large` before asking the human. Symbolic links,
hard-linked regular files, special files, and credential-shaped descendants fail closed; delete
never uses a parent request to bypass the normal protected-path policy.

The CLI uses its existing exact-action controls:

```bash
swoon agent --cookies cookies.json --resume sess_EXAMPLE \
  --approve-pending --non-interactive

swoon agent --cookies cookies.json --resume sess_EXAMPLE \
  --deny-pending --non-interactive
```

A denial records a failure result and changes nothing. Approval may occur in another process.
Neither option applies to later actions.

## Descriptor boundary and failure behavior

Every operation revalidates its authorization immediately before mutation. Existing parents and
entries are opened relative to verified directory descriptors with no-follow flags. File delete is
one namespace unlink. Directory delete performs a complete safe preflight before its recursive
pass, verifies each opened child again, and synchronizes changed directories. Move/rename verifies
the destination after the kernel operation and synchronizes both parents. `chmod` operates on the
opened regular-file descriptor, never a path selected after validation.

A recursive directory removal cannot be committed as one portable filesystem transaction. A host
crash or a concurrent same-user writer during the removal can therefore leave a partially removed
tree. In that case no successful action result is persisted; a later request must inspect the
remaining output and ask for a new guarded confirmation. Session storage and its owner-private
output directory remain part of the trusted local interpreter boundary.

## Chunk-state integration

Lifecycle tools cannot act on an unfinished chunk path. Directory delete and relocation also
block when an unfinished path is inside their source or destination scope. After success, the
filesystem result and chunk bookkeeping are written to session state together:

- deletion removes finalized chunk records at or below the deleted path;
- move/rename remaps records from the source prefix to the destination prefix; and
- a destination metadata collision returns `chunk_state_conflict` before mutation.

This prevents stale sequence records from blocking a later create at a deleted name and keeps
finalized chunk history aligned with relocated files.
