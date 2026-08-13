# Virtual path policy

Phase 6 adds a single authorization boundary for all future filesystem tools. It resolves
AEML's logical `/input/<session_id>` and `/output/<session_id>` roots to the private physical
directories owned by `SessionManager`.

```python
from swoon.aeml import PathRef, Root
from swoon.policy import PathAccess, PathExistence, PathKind, PathPolicy

policy = PathPolicy(session.paths)
authorized = policy.resolve(
    PathRef("src/app.py", Root.INPUT),
    access=PathAccess.READ,
    existence=PathExistence.MUST_EXIST,
    kind=PathKind.FILE,
)
```

## Enforced before execution

- Paths are relative, forward-slash-separated, and portable across supported host systems.
- Absolute paths, drive paths, backslashes, `..`, embedded dot segments, empty segments,
  control characters, and reserved device names are rejected.
- A session can resolve only through its own physical input/output roots.
- Input always rejects write authorization.
- Credential-shaped paths are denied in both input and output, whether or not they exist.
- Existing path components must be real directories/files rather than symbolic links.
- Hard-linked regular files are rejected by default.
- Missing create targets require an existing, contained parent directory.
- Root-level writes require a deliberate `allow_root=True`; ordinary writes cannot target the
  entire session output directory accidentally.

Directory-listing implementations must use `visible_child_names` so entries such as `.env` and
`.git/config` do not leak through a parent listing.

## Race-aware authorization

A `ResolvedPath` records device/inode/type fingerprints for the root and every existing path
component. A tool must call `policy.revalidate(resolved)` immediately before use. Replacement,
newly created targets, deleted parents, or inserted links become `path_changed` failures.

This closes the policy-time gap but does not replace safe filesystem syscalls. Future tool
executors must still use no-follow/open-at primitives where available and must not pass raw LLM
paths directly to standard file or shell operations.

The path policy only authorizes. It does not open, read, create, edit, delete, or execute
anything.
