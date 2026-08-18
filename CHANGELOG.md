# Changelog

Notable changes are recorded here. This project follows semantic versioning once releases are
tagged; the `0.x` series remains experimental and may introduce breaking changes.

## Unreleased

### Release candidate: 0.1.0

- Added a strict, resource-limited AEML parser, validator, prompt boundary, and resumable agent
  orchestration loop.
- Added isolated input/output sessions, guarded filesystem and dependency mutations, offline
  foreground execution, and supervised background processes.
- Added a browser-backed ChatGPT relay with owner-private credential handling and opt-in private
  state/debug artifacts.
- Added consumer session inspection, export, retention cleanup, and runtime diagnostics.
- Added deterministic adversarial tests, installed wheel/source smoke tests, an SPDX SBOM, release
  manifest and checksums, CI coverage, and an explicit owner-run live acceptance gate.
- Added Apache-2.0 licensing, responsible-use guidance, supported-scope documentation, security
  reporting, support, contribution, and release policies.

Before publishing `v0.1.0`, move these entries under `## 0.1.0 - YYYY-MM-DD` and complete
[`docs/release-checklist.md`](docs/release-checklist.md).
