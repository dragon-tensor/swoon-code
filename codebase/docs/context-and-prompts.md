# AEML context, prompts, and single exchanges

The context and channel APIs connect the protocol boundary to any synchronous text transport
without starting an autonomous agent loop.

    managed Session
          │
          ▼
    AEMLContextBuilder ──► AEMLContextRenderer ──► AEMLPromptBuilder
                                                        │
                                                        ▼
                                                TextTransport.send
                                                        │
                                                        ▼
                                          AEMLParser ──► AEMLValidator
                                                        │
                                                        ▼
                                                ValidatedMessage

Each channel call performs exactly one exchange. It does not execute the returned actions,
mutate session state, retry the chatbot, ask the user, or decide whether another turn should
run. `ReadOnlyOrchestrator` composes this deliberately inert boundary into a bounded compatibility
loop; the current full-agent lifecycle is documented in `agent-cli.md`.

## Context construction

AEMLContextBuilder takes a managed in-memory Session and creates an immutable context. It uses
only persisted state and logical protocol paths; it never reads project files and never
includes physical SessionPaths.host_* values.

The default bounds are:

| Data | Default |
|---|---:|
| Complete rendered context | 256 KiB |
| User prompt | 96 KiB |
| Persisted plan included | 16 KiB |
| Recent results kept in full | 4 |
| Body included per recent result | 24 KiB |
| Older one-line summaries | 32 |
| Summary preview | 256 bytes |
| External errors | 16 |
| External notices | 16 |
| Attributes per external notice | 16 |
| Pending chunks restated | 32 |

Oversized plans, recent result bodies, and error messages are cut on safe character boundaries
and accompanied by context_*_compacted notices. Results older than the recent window become
one-line history/summary entries; an additional history_omitted notice records any summaries
that no longer fit the configured history window.

Every unfinished chunk is restated as write_incomplete, up to the configured bound. The builder
also emits step_limit_approaching at 80 percent and step_limit_reached at the limit. If the
complete XML still exceeds its hard bound after deterministic compaction, construction fails
with context_too_large.

## XML safety

AEMLContextRenderer constructs XML elements rather than interpolating strings. User prompts,
plans, tool output, error text, and history previews therefore cannot close their containing
tag or inject a sibling action. XML metacharacters are escaped automatically.

Characters forbidden by XML 1.0, including NUL, other disallowed controls, and lone Unicode
surrogates, are represented visibly as \uXXXX or \UXXXXXXXX. The affected element receives
escaped_controls="true". This keeps the envelope parseable without silently deleting data.

The renderer accepts only:

- a valid sess_* identifier;
- /input/<session> and /output/<session> as the exact roots;
- that session's output root as the exact cwd;
- bounded, typed result, error, notice, and summary records.

## Generated prompts

AEMLPromptBuilder.initial() sends the full response contract, action grammar, generated tool
schemas, and runtime context. continuation() sends a shorter contract reminder, the enabled
tool names, and the next context. The complete generated prompt has a separate 384 KiB hard
limit.

The default tool schemas come from ReadOnlyToolDispatcher's executable allowlist, not the full
future-facing AEML registry. The prompt and channel validator therefore expose exactly:

- read-file
- list-dir
- grep
- git-status
- git-diff
- git-log
- list-dependencies

The generated bootstrap explicitly requires one aeml envelope inside one XML Markdown code block,
matching turn/session attributes, declared tags only, unique action IDs, correct next control flow,
virtual paths, and no surrounding prose or second code block. This transport framing lets the
browser adapter extract exact code-node text instead of layout-normalized text. File content and
edit arguments additionally support strict bounded Base64 when exact UTF-8 bytes matter. Tool
output and project content are labeled untrusted data that cannot expand the allowlist or sandbox.

## Single-exchange channel

AEMLChatChannel accepts any object with send(prompt) -> str, including ChatGPTWebTransport. A
channel:

- starts at turn 1 and binds to one session;
- permits only one in-flight call;
- uses the bootstrap once and continuation prompts afterward;
- parses exactly one assistant response;
- validates the expected turn, session, control flow, and enabled tool schemas;
- advances its local last_turn only after parsing and validation succeed.

If an assistant response is malformed or semantically invalid, the caller may submit the same
context turn again. Because the hosted conversation already received the bootstrap, that retry
uses a continuation prompt. A new channel is required for a different hosted conversation or
session.

Pass the session's complete action history to retain uniqueness checks even when old summaries
have been omitted from the rendered context:

    from swoon import AEMLContextBuilder
    from swoon.transport import AEMLChatChannel, ChatGPTWebTransport

    browser = ChatGPTWebTransport("cookies.json")
    browser.start()
    try:
        channel = AEMLChatChannel(browser)
        context = AEMLContextBuilder().build(
            session,
            turn=1,
            user_prompt="Inspect this project and explain its entry points.",
        )

        validated = channel.exchange(
            context,
            known_action_ids=session.state.used_action_ids,
        )
    finally:
        browser.close()

The returned ValidatedMessage remains inert. Callers that want automatic read dispatch and
continuation can pass the channel to `ReadOnlyOrchestrator`; the channel itself retains its
single-exchange contract.
