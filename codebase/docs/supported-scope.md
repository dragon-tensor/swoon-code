# Supported product scope

Swoon Code 0.1 is an experimental, bounded educational agent. Its release promise is narrower
than a general-purpose autonomous coding product: an AEML response can exercise only the tool
schemas advertised by the local interpreter, and every tool remains subject to the interpreter's
path, resource, confirmation, and sandbox policies.

## Supported capabilities

The supported workflow imports a project into a private session, keeps that input snapshot
read-only, and lets the agent build a separate output tree. Read, output-file, lifecycle,
dependency-declaration, and offline command tools are supported within their documented limits.

These boundaries are intentional in the 0.1 line:

- no network access from command sandboxes;
- no package download, installation, or lockfile regeneration;
- no Git mutation or credential access;
- no command-side change persists from the disposable command workspace;
- no write is applied directly to the imported project;
- destructive actions and dependency declarations require a separate human decision.

If a future release adds any of these capabilities, it must add a new explicit policy boundary;
prompt wording alone cannot widen the current one.

## Platforms

The relay, parser, session, and filesystem features target CPython 3.11 or newer. Full command
execution is supported only on 64-bit Linux x86-64 or AArch64 with `bwrap`, `prlimit`, and a system
`python3`. Atomic move and rename also require `renameat2(RENAME_NOREPLACE)`. Unsupported hosts
fail closed. A pure-Python wheel does not mean that every optional capability is cross-platform.

## Hosted-service transport

`ChatGPTWebTransport` is an experimental adapter for a user-authorized browser session. It is not
an official OpenAI integration, and web selectors can change without notice. Users must determine
whether their account, organization, location, and intended automation method permit its use.
The project does not bypass login, challenges, access controls, usage limits, or safeguards.

For a broadly distributed or production service, a provider-supported API transport should be
the supported default. The browser adapter should remain clearly labeled experimental and be
covered by a current live compatibility check whenever it is offered.

## Release claims

A green unit suite proves deterministic interpreter behavior; it does not prove that a hosted web
interface is currently compatible or that an LLM will complete every task. A public release also
requires the owner-run live acceptance gate described in `consumer-testing.md`. Until that gate
passes, the build is an offline-tested release candidate rather than a live-verified release.
