# Contributing

Contributions that strengthen AEML parsing, policy enforcement, consumer testing, documentation,
or portability are welcome. Keep changes bounded: the interpreter—not model output—must remain the
authority for paths, capabilities, confirmations, and execution.

## Development setup

Swoon supports Python 3.11 through 3.14. The deterministic unit and packaging checks use the
standard library; Playwright is needed only for browser diagnostics and owner-authorized live use.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
python3 scripts/aeml_eval.py
```

Before submitting a change, also build and exercise both consumer artifacts:

```bash
python3 scripts/build_wheel.py --out-dir /tmp/swoon-wheel
python3 scripts/smoke_wheel.py /tmp/swoon-wheel/swoon_code-0.1.0-py3-none-any.whl
python3 scripts/build_sdist.py --out-dir /tmp/swoon-sdist
python3 scripts/smoke_sdist.py /tmp/swoon-sdist/swoon_code-0.1.0.tar.gz
```

The full release rehearsal requires a Git checkout and an empty output directory:

```bash
python3 scripts/release.py --out-dir /tmp/swoon-release
```

Use `--allow-dirty` only while developing; such a manifest is explicitly marked non-clean and must
not be published.

## Security-sensitive changes

Add focused regression tests for every change to parsing, paths, mutations, confirmations,
credentials, subprocesses, browser state, session retention, or packaging. Model-provided text and
imported project content are untrusted data. Do not widen tool capabilities implicitly, add a
network fallback, silently downgrade sandboxing, or put credentials into tests or CI.

Run the owner-authorized live gate only on your own permitted account and disposable project. A
maintainer or contributor must never ask another person to submit browser state.

## Contributions and licensing

Keep commits focused and document user-visible behavior in `CHANGELOG.md`. By submitting a
contribution, you agree that it is your original work (or that you have the right to submit it) and
that it is provided under the repository's [Apache License 2.0](LICENSE). Add notices for copied or
adapted third-party material and preserve all applicable attribution.
