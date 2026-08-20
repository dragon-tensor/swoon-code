# Third-party notices

Swoon Code's wheel contains the project's Python source and its own legal metadata. It does not
bundle its direct dependency, a browser binary, Bubblewrap, or util-linux.

## Direct Python dependency

### Playwright for Python

- Project: <https://github.com/microsoft/playwright-python>
- Declared range: `playwright>=1.50,<2`
- License reported by the upstream package: Apache License 2.0

Playwright is resolved and installed separately by the consumer. A release SBOM records this as a
direct dependency; it is not a fully resolved environment or vulnerability report.

## Separately installed runtime components

`python -m playwright install chromium` downloads Chromium and supporting files governed by their
own upstream licenses and notices. Consumers and distributors must review the material installed
for their platform rather than treating this project's Apache-2.0 license as covering it.

On supported Linux hosts, optional command tools use `bwrap` (Bubblewrap) and `prlimit` (normally
provided by util-linux). These are detected system prerequisites and are not copied into Swoon's
wheel or source archive. Their installed versions remain governed by their respective packages and
operating-system distribution notices.

If a future change bundles or copies third-party material, update this file, the SPDX generation
rules, and the release checklist before distribution.
