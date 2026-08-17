# Read-only AEML tools

Phase 7 is the first AEML phase that can inspect real session data. It deliberately exposes no
write tool and no arbitrary command tool.

## Execution boundary

`ReadOnlyToolDispatcher` accepts `ValidatedAction` objects and checks each action against the
canonical schema and a seven-tool allowlist. Mutating/executing schemas fail with
`write_tool_disabled`; declared read tools without an implementation fail with
`unsupported_read_tool`.

Successful results are atomically stored in the session action ledger with a SHA-256 digest of
the normalized action. Reusing an action ID for the exact same action returns its stored result
without reading again; changing any argument under that ID is rejected. Protocol errors are not
stored as results. When the Phase 9 orchestrator is used, it reserves every validated action ID
before dispatch, so an ID whose tool attempt fails is still durably unavailable for reuse.
Read failures marked as transient receive one silent retry.

Unfinished output chunks block reads that depend on those paths.

## Filesystem access

The filesystem tools use `PathPolicy` followed by descriptor-relative, no-follow file opens.
Every directory component's device, inode, and type are checked after opening. Platforms that
cannot provide this primitive fail closed with `platform_unsupported`.

- `read-file` accepts optional inclusive line ranges, requires UTF-8 text, and returns
  `binary_unsupported` for binary/non-UTF-8 content.
- `list-dir` returns deterministic `d path/` and `f path size` records. Recursive traversal and
  POSIX-style glob filtering are optional.
- `grep` performs literal UTF-8 substring matching, recursively when its target is a directory.
  `max_results` and `context_lines` scope the response.

Credential-shaped and non-portable names are filtered from traversal. Per-file, total-scan,
entry-count, line-length, and output limits bound resource use. Reactive output truncation
defaults to 64 KiB and produces a partial `Result` with `Truncation(total_bytes, offset=0)`.

## Git inspection

`git-status`, `git-diff`, and `git-log` use fixed argument arrays, never a shell. Before invoking
Git, Swoon copies the authorized output repository to a disposable snapshot:

- credential-shaped files, including `.git/config`, are omitted;
- symbolic links, hard links, special files, and external object-store metadata are rejected;
- repository, user, and system Git configuration are disabled;
- pagers, prompts, optional locks, external diffs, text conversion, hooks, and filesystem
  monitors are disabled;
- command runtime and captured output are bounded;
- the process group is killed on timeout.

Status and diff paths are checked against the path policy again. Diff content is requested only
for the resulting safe path list. Git log exposes hash, timestamp, author name, and subject, but
not author email.

This fixed Git subprocess remains narrower than Phase 12's separate `run-command` capability.
`ReadOnlyToolDispatcher` still rejects general commands; only `AgentToolDispatcher` opts into the
offline disposable OS sandbox described in `foreground-commands.md`.

## Dependency inspection

`list-dependencies` parses recognized manifests directly and never invokes a package manager:

- Python: `pyproject.toml`, `requirements*.txt`
- JavaScript: `package.json`, with npm/pnpm/yarn lockfile detection
- Rust: `Cargo.toml`
- Go: `go.mod`
- PHP: `composer.json`
- Ruby: `Gemfile.lock` or basic `Gemfile` declarations

Manifest size and output are bounded. URL user-info and common secret query parameters are
redacted before results leave the interpreter.
