# Consumer build and test

Phase 15 makes the current Python application directly testable as a consumer CLI. The release
artifact is a platform-independent wheel, not a bundled Chromium executable. This keeps the
security-sensitive browser installation visible and updateable while still providing a normal
`swoon` command.

## Fastest source-tree check

The source checkout already supports Python's module launcher, even before installation:

```bash
.venv/bin/python -m swoon --version
.venv/bin/python -m swoon --help
.venv/bin/python -m swoon doctor
```

`doctor` checks the installed Playwright/Chromium files and reports whether the optional offline
command sandbox has `bwrap`, `prlimit`, a supported Linux architecture, and a system Python. It
does not read cookies unless explicitly asked. Use the stronger probe from a normal host terminal:

```bash
.venv/bin/python -m swoon doctor \
  --cookies cookies.json \
  --launch-browser
```

The launch probe can fail inside a container or development sandbox even when Chromium is
installed. Running it in the same terminal environment that will run Swoon is the meaningful
consumer check.

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
`swoon.__version__` agree, includes only the `swoon` package and wheel metadata, writes a complete
hashed `RECORD`, and never packages cookies, tests, sessions, caches, or project source outside the
runtime package.

Run the networkless acceptance test:

```bash
python3 scripts/smoke_wheel.py dist/swoon_code-0.1.0-py3-none-any.whl
```

The smoke runner creates a fresh temporary virtual environment, installs the wheel with
`--no-index --no-deps`, then verifies the installed `swoon --version`, root help, doctor help, and
package import. This specifically tests the built artifact and generated console entry point—not
the source checkout.

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

## Release boundary

The wheel is sufficient for local consumer testing and private distribution. A true single-file
executable is intentionally deferred: Chromium is a large external runtime with its own updates,
sandbox requirements, and platform-specific files. A later release phase can provide an installer
or application bundle that manages Python and Chromium together without pretending they are one
small binary.

The repository does not currently declare a redistribution license. Choose and add a `LICENSE`
before publishing the wheel publicly.
