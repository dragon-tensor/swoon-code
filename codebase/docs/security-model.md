# Security model and adversarial verification

This document records Swoon's trust boundaries, deterministic adversarial checks, opt-in live
release gate, reproducible packages, SBOM, checksums, manifest, CI, and tag/publication controls.
It is a threat model, not a claim that sandboxed software is invulnerable.

## Assets and trust boundaries

| Asset or actor | Trust treatment |
|---|---|
| Human operator | Authorizes the task, step extensions, destructive actions, credentials, and live gate |
| Browser cookies/storage state | Account credentials; local secret input, never AEML context |
| Hosted model response | Untrusted protocol input until strict parsing and validation succeed |
| Imported project and tool output | Untrusted data; cannot redefine schemas, roots, or policy |
| AEML interpreter | Local authority that validates and dispatches the fixed capability set |
| Command process | Hostile child confined to an offline disposable Linux sandbox |
| Session output | Persistent but isolated result; never automatically applied to the source project |
| Release artifact | Must be built and exercised independently of the source checkout |

The hosted model does not receive physical session paths, browser cookies, process IDs as control
handles, or ambient host credentials. It can still produce incorrect or malicious suggestions and
can use any capability the interpreter intentionally advertises. Human review of exported output
therefore remains necessary.

## Principal threats and controls

| Threat | Current controls | Residual risk / release test |
|---|---|---|
| Malformed, oversized, or entity-bearing XML | Byte/element/attribute/depth limits; DTD/entity/comment/PI/namespace refusal | Parser bugs; fixed-seed mutation smoke and security corpus |
| Browser-rendered source corruption | One XML code block; exact code-node extraction; strict bounded Base64 for sensitive text arguments | Provider may violate framing; parser repair and lossless round-trip tests |
| Prompt injection in task/project/result text | Deterministic XML escaping; explicit untrusted-data contract; fixed validator registry | Model may make poor choices within allowed tools; adversarial task evaluation and human review |
| Capability invention or replay | Prompt/validator registry match; unique durable action IDs; turn/session binding | Provider output quality varies; corpus and live AEML run |
| Host path or credential access | Virtual roots, no-follow descriptor opens, credential denylist, read-only imported input | Unknown credential filename or kernel/filesystem bug; regression tests and review |
| Destructive output action | Output-only policy, exact persisted guard, separate human approval | Human can approve a bad action; prompt displays exact target/reason |
| Command escape or network access | Shell-free argv checks, filtered snapshots, Bubblewrap namespaces, seccomp socket denial, baseline-aware outer limits, PID-namespace task supervisor | Kernel/Bubblewrap vulnerability; supported-host tests and independent review |
| Process/PID confusion | Opaque live-supervisor handles; persisted PIDs are diagnostic only; restart becomes `lost` | Same-process supervisor defects; lifecycle regression tests |
| Cookie disclosure | Owner-only regular file, bounded JSON/domain validation, opt-in private state/debug outputs | Compromised host or deliberately shared credential; revoke provider session |
| Web UI drift | Multiple selectors, bounded waits, structured failure, no automatic screenshot | Selectors and auth flow can still break; owner-run live gate for every release |
| Session data retention | Private storage, explicit show/export/delete commands, no in-place source mutation | Operator retains sensitive data too long; documented cleanup policy |
| Supply-chain substitution | Minimal dependency set; reproducible wheel/source archive; direct-dependency SPDX; commit-bound manifest; checksums; GitHub provenance | Dependency or CI compromise; ongoing update, artifact, and vulnerability review |

## Deterministic adversarial gate

Run the checked-in parser/validator corpus:

```bash
python3 scripts/aeml_eval.py
```

The corpus covers valid envelopes, surrounding prose, external entities, namespaces, truncation,
invented tools, input writes, missing destructive confirmation, turn/session substitution, action
replay, batched mutation, and markup-shaped text. It never executes an action. Unit tests add a
fixed-seed malformed-input smoke and verify that result-borne injection text remains XML text.

## Live owner-authorized gate

The live check is deliberately excluded from unattended tests. It uses a locally supplied private
credential, sends two visible prompts, imports only a script-created disposable project, demands a
successful offline test action, verifies the source is unchanged, exercises output export, and
deletes its session:

```bash
python3 scripts/live_acceptance.py \
  --cookies /private/path/storage-state.json \
  --acknowledge-provider-terms
```

Run it only when the operator has independently determined that the provider and account permit
the automation. Never upload the credential to CI or send it to a maintainer. A release candidate
that has not passed this gate on its target host is offline-verified, not live-verified.

## Security review cadence

Every release should rerun the full unit suite, adversarial corpus, installed wheel/source smokes,
reproducibility gate, and owner-authorized live gate. The release workflow deliberately creates a
draft rather than publishing automatically. Changes to paths, file mutation, confirmation, command
sandboxing, browser credentials, or release packaging require focused regression tests. A
hosted-service UI, browser runtime, kernel, and dependencies can change after release, so these
controls need ongoing maintenance and cannot make the project permanently safe “once and for
all.”
