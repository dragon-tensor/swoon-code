# Foreground command sandbox

Phase 12 adds four executing capabilities to `AgentToolDispatcher`:

- `run-command`
- `run-build`
- `run-tests`
- `run-linter`

They are foreground verification tools. Every invocation blocks until the command exits, reaches
its hard timeout, or exceeds its capture bound. Background processes, environment mutation,
package installation/removal, network access, and Git execution remain disabled.

## Disposable execution model

Commands never run against the physical session directories. Before launch, the interpreter
copies both roots into bounded snapshots using descriptor-relative no-follow reads. It omits the
same credential-shaped paths hidden from every other tool and rejects symbolic links, hard links,
special files, oversized files, or a snapshot that exceeds its entry/byte limits.

Bubblewrap receives the filtered input snapshot as read-only and copies the filtered output seed
into a size-limited tmpfs. The command's cwd is the normal logical output root, for example
`/output/sess_EXAMPLE`; the logical input root is also present read-only. When the process exits,
the tmpfs is destroyed. New files, edits, deletes, build artifacts, caches, and linter fixes are
therefore discarded regardless of exit status.

This is intentional. An opaque command cannot bypass `overwrite-file` confirmation, future
delete confirmation, or the interpreter's atomic mutation primitives. Persistent changes must be
requested through explicit filesystem tools. A result always includes
`workspace_changes=discarded` so the hosted reasoning side cannot mistake a successful formatter
or build for a persisted mutation.

## OS boundary

The default runtime requires 64-bit Linux on x86-64 or AArch64, plus compatible `bwrap`,
`prlimit`, and a system `python3`. Missing primitives fail closed with `tool_unavailable` or
`platform_unsupported`; the command is never retried outside a sandbox.

Each invocation applies all of these boundaries:

- new user, mount, PID, IPC, UTS, and cgroup-when-available namespaces;
- nested user namespaces disabled and verified disabled;
- an empty root containing only read-only system runtime directories, minimal `/etc` runtime
  files, a fresh `/proc`, a minimal `/dev`, the two filtered roots, and bounded tmpfs mounts;
- an inherited seccomp filter that rejects socket creation, so network isolation does not depend
  on a host permitting creation of another network namespace;
- a cleared, fixed environment with private temporary cache locations and non-interactive package
  and Git settings;
- no inherited stdin, credentials, proxy variables, user home, package caches, or Git config;
- a new process session plus PID namespace cleanup on timeout/output-limit termination;
- wall-clock, CPU, address-space, file-size, process-count, open-file, output, snapshot, and tmpfs
  limits.

The default limits are:

| Boundary | Default |
|---|---:|
| `run-command` wall timeout | 30 seconds |
| managed build/test/linter timeout | 120 seconds |
| captured combined stdout/stderr | 8 MiB |
| result returned to AEML | 64 KiB |
| output lines when omitted | 1,000 |
| snapshot | 100,000 entries / 512 MiB total / 64 MiB per file |
| writable workspace tmpfs | 512 MiB |
| temporary tmpfs | 256 MiB |
| address space | 2 GiB |
| one created file | 256 MiB |
| processes / open files | 256 / 256 |

`CommandToolLimits` can reduce or increase these interpreter-side bounds. AEML's `timeout` is
still schema-limited to 1–3,600 seconds and `max_output_lines` to 1–100,000.

## Command parsing and paths

`run-command` parses `cmd` into argv with POSIX quoting and launches it directly. The interpreter
does not invoke a shell, perform variable expansion, expand globs, or process redirects/pipes.
Standalone shell operators such as `|`, `&&`, `;`, and `>` are rejected. An explicitly requested
shell binary remains inside the identical OS sandbox and cannot widen its mounts, environment, or
seccomp policy.

Direct absolute path arguments are accepted only under this session's exact logical input/output
roots. Direct traversal, cross-session absolute paths, credential-shaped names, and network URLs
fail before launch. Relative executable paths are authorized against output. These checks provide
early protocol errors; the empty mount namespace remains the final enforcement layer for paths
embedded inside interpreter code or a nested command language.

## Managed build, test, and lint commands

The `manager` argument selects one fixed argv template. If omitted, exactly one supported project
ecosystem must be detectable from manifests in output; zero or multiple matches fail with
`manager_not_detected` or `manager_ambiguous`. `target`, when supplied, is one bounded argv value
and is never reparsed as command text.

| Manager | Build | Tests | Linter |
|---|---|---|---|
| `pip` | `python3 -m build` | `python3 -m pytest` | `python3 -m ruff check` |
| `npm` / `pnpm` | `run build` | `run test` | `run lint` |
| `yarn` | `yarn build` | `yarn test` | `yarn lint` |
| `cargo` | `cargo build` | `cargo test` | `cargo clippy --all-targets --all-features` |
| `go` | `go build ./...` | `go test ./...` | `go vet ./...` |
| `bundler` | `bundle exec rake build` | `bundle exec rake test` | `bundle exec rubocop .` |
| `composer` | `run-script build` | `run-script test` | `run-script lint` |

The sandbox is offline and does not expose the user's package caches. Managed commands therefore
work with system-provided or already-vendored dependencies. Installing dependencies is a separate
future capability with a different network and provenance policy.

## Results

Combined stdout/stderr is decoded safely, stripped of physical session/snapshot paths, XML-control
sanitized, line-scoped, and UTF-8-byte bounded. Exit zero produces `success` or `partial`; nonzero
produces `failure`; a wall timeout produces `timeout`. Nonzero and timeout results are persisted so
the agent can inspect them on the next turn. A capture safety overflow terminates the process and
returns `output_limit_exceeded` without persisting an incomplete result.

```xml
<result id="tests1">
  <status>success</status>
  <output>exit_code=0
workspace_changes=discarded
denied_paths_omitted=1
...</output>
</result>
```

## Embedding API

```python
from swoon import AgentToolDispatcher, CommandToolLimits

dispatcher = AgentToolDispatcher(
    sessions,
    command_limits=CommandToolLimits(
        default_timeout_seconds=20,
        managed_timeout_seconds=90,
        max_capture_bytes=4 * 1024 * 1024,
    ),
)
```

As in earlier phases, the prompt and validator must use `dispatcher.tool_specs`. Adding an
executing schema to the protocol registry alone never enables it.
