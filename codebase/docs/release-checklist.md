# Release checklist

Automated checks establish an offline-verified candidate; a release owner remains responsible for
provider permission, live behavior, legal review, and the final publication decision.

## Prepare

- [ ] Choose a semantic version and update both `pyproject.toml` and `swoon/__init__.py`.
- [ ] Move the matching entries in `CHANGELOG.md` under a dated version heading.
- [ ] Confirm supported Python/platform claims and all user-visible documentation.
- [ ] Review `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md`, dependency declarations, and SPDX
      metadata; obtain qualified legal advice when needed.
- [ ] Review open security reports and dependency advisories.
- [ ] Confirm no cookies, storage state, screenshots, sessions, build output, or private data are
      tracked by Git.

## Verify offline

From a clean Git worktree, run:

```bash
python3 scripts/release.py --out-dir /tmp/swoon-release
sha256sum -c /tmp/swoon-release/SHA256SUMS
```

The driver runs unit tests, the AEML adversarial corpus, wheel and source-archive smoke tests,
byte-for-byte reproducibility checks, SPDX generation, and release manifest/checksum generation.
Do not publish an artifact whose manifest says `"dirty": true`.

- [ ] Inspect the wheel and source archive contents for secrets and unexpected files.
- [ ] Confirm the manifest commit is the intended reviewed commit.
- [ ] Test `swoon doctor --launch-browser` on each claimed consumer platform.
- [ ] Review CI results, including Python 3.11–3.14, Linux ARM64, sandbox, browser-launch, and
      release-candidate jobs.

## Verify the live boundary

Never place credentials in CI. On a disposable project and an account whose owner has independently
confirmed the provider permits the use, run:

```bash
python3 scripts/live_acceptance.py \
  --cookies /private/path/storage-state.json \
  --acknowledge-provider-terms
```

- [ ] Record the tested candidate commit, date, platform, browser/runtime versions, and result in
      private release-owner notes—never the credential.
- [ ] Verify the source project remained unchanged and remove the disposable Swoon session.
- [ ] If the gate is skipped or fails, describe the candidate as offline-verified, not live-verified.

## Tag and publish

- [ ] Commit the finalized changelog and version, rerun the clean release command, and sign the
      release commit/tag according to project policy.
- [ ] Create and push exactly `vVERSION`; the release workflow rejects a tag/version or tag/HEAD
      mismatch.
- [ ] Review the generated draft GitHub release, provenance attestation, SBOM, manifest, checksums,
      wheel, and source archive before publishing the draft.
- [ ] Install the downloaded wheel in a new consumer environment and repeat the fast smoke check.
- [ ] Publish to a package index only as a separate, explicitly authorized operation; the checked-in
      workflow does not publish to PyPI.
- [ ] Announce known limitations, experimental status, and live-verification status.

## After release

- [ ] Verify GitHub's displayed checksums/attestations and all documentation links.
- [ ] Monitor security reports and browser/provider drift.
- [ ] Start a new `Unreleased` changelog section and retain the release artifacts.
