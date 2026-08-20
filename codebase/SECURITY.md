# Security policy

Swoon Code is an experimental coding-agent interpreter with deliberate security boundaries. A
successful test run is not a guarantee that the software, a browser provider, or an operating
system sandbox is invulnerable. Review [`docs/security-model.md`](docs/security-model.md) before
using it with valuable source code or an authenticated account.

## Supported versions

| Version | Security support |
|---|---|
| `0.1.x` | Best-effort fixes while the initial experimental series is maintained |
| Source snapshots and older versions | Unsupported; reproduce on the current default branch |

There is no long-term-support release. Security-sensitive behavior can change between `0.x`
versions and will be documented in [`CHANGELOG.md`](CHANGELOG.md).

## Reporting a vulnerability

Use the repository's
[private vulnerability reporting form](https://github.com/dragon-tensor/swoon-code/security/advisories/new)
when it is available. If private reporting is unavailable, open a minimal issue asking the
maintainers to enable a private channel; do not include exploit details in the public issue.

Never send a browser cookie, storage-state file, account identifier, private source tree, session
archive, or other secret to a maintainer. A useful report contains a redacted reproduction,
affected version/commit, operating system and architecture, expected boundary, observed behavior,
and whether the issue is already public.

Maintainers should acknowledge a private report within seven days when the project is actively
maintained, investigate without using the reporter's credentials, coordinate disclosure, and
credit the reporter if requested. This is a best-effort open-source process, not a service-level
agreement or bug-bounty program.

## In scope

- Bypassing AEML parsing, validation, turn/session binding, or capability checks
- Escaping virtual roots, following forbidden links, or reading host credentials
- Skipping a required real-human confirmation
- Escaping the offline disposable command sandbox
- Cross-session access, process-handle confusion, or unsafe session deletion/export
- Credential disclosure caused by Swoon defaults or release artifacts
- Release artifact substitution, omission, or reproducibility defects

Provider account recovery, provider policy decisions, social engineering, denial of service against
third-party services, and vulnerabilities in unmodified external products belong with their
respective providers. Do not test Swoon against accounts, systems, or data you do not own or have
explicit permission to use.
