# Changelog

Notable changes are recorded here. This project follows semantic versioning once releases are
tagged; the `0.x` series remains experimental and may introduce breaking changes.

## Unreleased

### Release candidate: 0.1.0

- Reject incomplete authentication-page-only and encrypted Hotcleaner cookie exports before
  browser startup, detect ChatGPT guest mode explicitly, and recognize current textarea-style
  message composers.
- Normalize Chrome/Cookie-Editor `sameSite` variants for Playwright and report doctor remedies
  only for the component that actually failed.
- Prevent overlapping ChatGPT turns by waiting for generation to end and for a configurable
  unchanged-response window; timeouts now reject partial AEML instead of triggering repair spam.
- Insert multiline AEML into the ChatGPT composer atomically so newline characters cannot submit
  incomplete prompts as separate messages.
- Add a persistent `swoon agent --interactive` terminal console for successive coding tasks in one
  session.
- Replace the consumer layout with `codebase/`, named `work/input|output/` folders, and
  platform-specific `setup/` installers; add one-word `swoon` and two-word `swoon NAME` launches.
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
