# Guarded dependency declarations

Phase 16 enables `install-dependency` and `remove-dependency` in `AgentToolDispatcher`. The names
come from the AEML protocol, but this phase deliberately implements declaration changes—not
package artifact installation. No package manager is launched, no network socket is opened, no
user cache or configuration is read, and no dependency script can execute.

Every change requires both `<expect_confirm>true</expect_confirm>` in AEML and a real host-side
approval. The confirmation guard binds the manifest path, device, inode, mode, timestamps, byte
size, and SHA-256. If the file changes while approval is pending, execution returns
`confirmation_stale`. The final replacement uses the existing descriptor-relative, no-follow,
atomic output mutation boundary.

## Supported manifests

| Manager | Manifest | Exact add form | Development scope |
|---|---|---|---|
| `pip` | PEP 621 `pyproject.toml`, otherwise `requirements.txt` | `name==version` | PEP 621 `dev`, otherwise existing `requirements-dev.txt` |
| `npm`, `pnpm`, `yarn` | `package.json` | `name@X.Y.Z` or `@scope/name@X.Y.Z` | `devDependencies` |
| `cargo` | `Cargo.toml` | `crate@X.Y.Z` | `dev-dependencies` |
| `go` | `go.mod` | `module/path@vX.Y.Z` | Not supported by Go manifests |
| `bundler` | `Gemfile` | `gem@X.Y.Z` | a `group :development` declaration |
| `composer` | `composer.json` | `vendor/package@X.Y.Z` | `require-dev` |

Removal accepts the registry package name without a version and removes its supported manifest
declaration. PEP 621 removal searches runtime and optional dependency groups. JSON removal searches
all recognized dependency sections. The requirements fallback refuses an ambiguous name found in
multiple `requirements*.txt` files.

Additions accept only conservative exact registry versions. URLs, Git references, local paths,
workspace aliases, version ranges, and floating tags such as `latest` are rejected before the
confirmation pause. This limits what the structured capability can persist; the general file tools
remain available for human-directed exceptional manifest edits.

## Lockfiles and artifacts

The interpreter cannot honestly update a dependency lock without a resolver and verified package
metadata. Phase 16 therefore fails with `lockfile_present` when a related lock exists:

- Python: `poetry.lock`, `uv.lock`, or `Pipfile.lock`
- JavaScript: `package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, or `yarn.lock`
- Rust, Go, Ruby, PHP: `Cargo.lock`, `go.sum`, `Gemfile.lock`, or `composer.lock`

It never deletes or silently leaves one stale. A successful result includes these machine-readable
facts:

```text
network=disabled
package_artifacts=not_installed
package_scripts=not_executed
lockfile=absent
```

The agent must not claim that imports are now available merely because a declaration changed.
Actual artifact acquisition and lock refresh remain a later capability requiring registry
allowlisting, provenance verification, bounded downloads, credential isolation, package-script
sandboxing, and an explicit promotion policy.

## Example

```xml
<action id="dep1">
  <tool>install-dependency</tool>
  <args>
    <manager>pip</manager>
    <package>httpx==0.28.1</package>
    <dev>true</dev>
  </args>
  <expect_confirm>true</expect_confirm>
</action>
```

Unfinished output chunk sequences block both dependency tools. Read-only compatibility dispatchers
still expose only `list-dependencies`; embedding code must opt into `AgentToolDispatcher` for these
mutations.
