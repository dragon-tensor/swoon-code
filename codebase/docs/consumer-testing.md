# Consumer build and test

The Python application is directly testable as a consumer CLI and as both wheel and source release
artifacts. Chromium remains a separate platform runtime. This keeps its
security-sensitive installation visible and updateable while still providing a normal `swoon`
command.

## Fastest source-tree check

The supported consumer setup hides Python environments and dependency installation:

```bash
# Run from the repository root
./setup/install.sh /path/to/cookies.json

# Then open a new terminal
swoon
swoon demo
```

Windows uses `setup\windows\install.cmd`; macOS uses the same top-level `setup/install.sh` as
Linux. Normal coding-agent sessions open Chromium and retain the same window until `/quit`, which
allows a human-verification check to be completed without restarting. The development launchers
add verbose transport logs.

When a hosted service requires a Cloudflare human-verification check, use `swoon auth`. It opens a
headed browser only for the user to complete the verification, refreshes the configured private
browser state, and exits. The adapter never automates the check. `swoon demo --headless` remains
available as an explicit opt-in and fails immediately with a clear remedy if challenged.

Developers working inside `codebase/` can still use Python's module launcher:

```bash
.venv/bin/python -m swoon --version
.venv/bin/python -m swoon --help
.venv/bin/python -m swoon doctor
```

`doctor` checks the installed Playwright/Chromium files and reports whether the optional offline
command sandbox has `bwrap`, `prlimit`, a supported Linux architecture, and a system Python. It
does not read cookies unless explicitly asked. A supplied POSIX cookie file must be a regular,
owner-only file (`chmod 600 cookies.json`). Export cookies while signed in and viewing
`https://chatgpt.com/`; an export made only from `auth.openai.com` is incomplete and is rejected.
Use an unencrypted JSON cookie list, not a Hotcleaner encrypted-backup file, and never provide an
export password to Swoon.
Use the stronger probe from a normal host terminal:

```bash
.venv/bin/python -m swoon doctor \
  --cookies cookies.json \
  --launch-browser
```

The launch probe can fail inside a container or development sandbox even when Chromium is
installed. Running it in the same terminal environment that will run Swoon is the meaningful
consumer check. This command validates cookie structure and browser launch. The subsequent relay
is the live authentication check: it refuses a visible logged-out ChatGPT page before sending a
prompt.

During a live exchange, the default 180-second `--timeout` is one response-wait window. By default,
two additional windows continue waiting for that same response; adjust them with
`--response-timeout-retries N`. No extension resubmits the prompt. Completion requires the assistant
message to remain unchanged for five seconds after the visible generation control disappears. Use
`--response-settle-time SECONDS` to increase that quiet window on slower connections. It must remain
shorter than `--timeout`. Exhausting every window aborts without returning partial text or sending a
repair prompt.

Run the deterministic adversarial protocol corpus as a separate offline gate:

```bash
python3 scripts/aeml_eval.py
```

It parses and validates hostile fixtures but never dispatches an action. The full threat model and
the explicitly owner-authorized live gate are documented in `security-model.md`.

## Build the wheel

The repository includes a deterministic standard-library builder, so creating the project wheel
does not depend on downloading setuptools or another build frontend:

```bash
python3 scripts/build_wheel.py
```

It creates:

```text
dist/swoon_code-0.1.0-py3-none-any.whl
```

The builder prints the artifact SHA-256. It validates that `pyproject.toml` and
`swoon.__version__` agree, includes only the `swoon` package, wheel metadata, and declared legal
files, writes a complete hashed `RECORD`, and never packages cookies, tests, sessions, caches, or
other project source outside the runtime package. Core metadata identifies `Apache-2.0` and the
wheel carries `LICENSE` and `NOTICE` under its standard `.dist-info/licenses/` directory.

Run the networkless acceptance test:

```bash
python3 scripts/smoke_wheel.py dist/swoon_code-0.1.0-py3-none-any.whl
```

The smoke runner creates a fresh temporary virtual environment, installs the wheel with
`--no-index --no-deps`, then verifies the installed `swoon --version`, root help, doctor help, and
package import. It also exercises session list/show/export/delete through the installed entry point.
This specifically tests the built artifact—not the source checkout.

## Build the source distribution

Build the deterministic PAX source archive and test it independently:

```bash
python3 scripts/build_sdist.py
python3 scripts/smoke_sdist.py dist/swoon_code-0.1.0.tar.gz
```

The smoke runner bounds and validates every archive member, refuses links and special files,
extracts with Python's safe data filter, rebuilds a wheel from the extracted tree, and passes that
wheel through the installed consumer smoke. Repeated source and wheel builds must be byte-identical.

For a complete release rehearsal from a clean Git worktree, use an empty destination:

```bash
python3 scripts/release.py --out-dir /tmp/swoon-release
```

The release directory contains a wheel, source archive, direct-dependency SPDX 2.3 JSON document,
`release-manifest.json`, and `SHA256SUMS`. The manifest binds artifact hashes to the Git commit and
records whether a development-only `--allow-dirty` override was used. Do not distribute dirty
rehearsal output. See [`release-checklist.md`](release-checklist.md).

## Install like a consumer

Create a separate environment and let pip resolve the declared Playwright dependency:

```bash
python3 -m venv swoon-consumer
swoon-consumer/bin/pip install dist/swoon_code-0.1.0-py3-none-any.whl
swoon-consumer/bin/python -m playwright install chromium
swoon-consumer/bin/swoon doctor --launch-browser --cookies cookies.json
```

On Windows, replace `bin/` with `Scripts/`. Keep the cookie file private and outside distributable
artifacts.

The browser adapter targets `https://chatgpt.com/`, uses Playwright's own current user agent, and
does not persist refreshed credentials or capture screenshots unless the matching CLI option is
provided. Read `supported-scope.md` before treating a successful browser launch as permission to
automate a connected service.

Try the direct relay first:

```bash
swoon-consumer/bin/swoon chat \
  --cookies cookies.json \
  --prompt "Reply with one short hello."
```

Then try the coding agent on a disposable project copy:

```bash
swoon-consumer/bin/swoon agent \
  --cookies cookies.json \
  --project /path/to/test-project \
  --prompt "Inspect this project, copy it to output, make one small improvement, and test it."
```

The CLI prints the session ID. Output persists in the private session directory described in the
session guide; the imported input stays sealed. File/read/lifecycle tools work independently of
the command sandbox. Foreground/background command tools require the additional Linux runtime
reported by `doctor` and fail closed when it is unavailable.

After the session reaches a terminal state, exercise the normal consumer retrieval path:

```bash
swoon-consumer/bin/swoon session show sess_EXAMPLE
swoon-consumer/bin/swoon session export sess_EXAMPLE ./accepted-output
```

Inspect `accepted-output`, then verify that the original test project is unchanged. Finally test
retention cleanup with `swoon session delete sess_EXAMPLE` and its exact confirmation prompt.

## Release boundary

The wheel is the normal Python consumer artifact; the source archive supports downstream rebuilds.
A true single-file executable is not provided: Chromium is a large external runtime with its own
updates, sandbox requirements, platform-specific files, licenses, and notices. A future installer
could manage Python and Chromium together, but must not present them as one small self-contained
binary.

The source and wheel are distributed under Apache-2.0. `RESPONSIBLE_USE.md` documents educational
intent, non-affiliation, and user responsibility without changing the software license.
`THIRD_PARTY_NOTICES.md` records the unbundled dependency/runtime boundary. Before publication,
confirm that the collective `Swoon Code contributors` attribution in `NOTICE` matches the ownership
policy you intend to use.
