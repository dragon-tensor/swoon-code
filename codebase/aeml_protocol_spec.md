# AEML — Agent Execution Markup Language
### Protocol spec v0.4 — for swoon code

This is the contract between a hosted chatbot (no machine access, turn-based, message-size
limited) and a local interpreter (full machine access, no reasoning) that lets the two together
behave like a coding agent. The chatbot never touches the OS. The interpreter never reasons.
Every action crosses the boundary as a tagged block that either side can parse without
understanding the other's intent.

---

## 0. Design principles

1. **One physical action per write-turn.** A single LLM reply may batch multiple *read-only*
   actions, but never more than one *write/execute* action.
2. **Two fixed roots: `/output` and `/input`, identical on Windows/Linux/Mac.** Not
   OS-mapped, not configurable per-project — the interpreter presents the same two paths
   regardless of host.
3. **One session = one folder pair.** On a new chat, the interpreter creates
   `/output/<session_id>/` and `/input/<session_id>/`. Every path the LLM writes is relative to
   one of these, scoped to that session — a session can never see another session's folder.
4. **`/input` is read-only to the LLM, always.** User-supplied source files live there. The LLM
   may `read-file`, `list-dir`, `grep` inside `/input/<session_id>/`, but every write tool
   rejects `root="input"` outright.
5. **This boundary holds even against a direct user instruction.** If the human types "read
   /etc/hosts" or "just write straight into /input", the interpreter refuses — the sandbox is an
   interpreter-level invariant, not an LLM-level suggestion, so it can't be talked out of it
   from either side of the conversation.
6. **The LLM never talks to the OS directly.** Only through declared tags. Unknown tags are
   rejected, not guessed at.
7. **The interpreter never trusts the LLM.** Every tool name, path, and argument is validated
   before anything executes.
8. **State the LLM can't remember is re-injected every turn** — cwd, both roots, step count,
   short result history.
9. **Confirmation for destructive actions comes from the human at the terminal, never from
   another chatbot turn.**
10. **Anticipating the chatbot's own message-size limit is the LLM's responsibility, not the
    interpreter's.** There's no published per-message input/output limit to check against, so
    the interpreter can't predict it either — it only provides the *mechanism* (chunked writes,
    §7) for the LLM to use when it judges content is getting large. Guidance to self-chunk
    proactively belongs in swoon code's system prompt to the chatbot, not in enforceable code.
11. **`<thought>` is never surfaced to the human**, in any mode. The read-only orchestrator
    discards it and does not persist it in session state.

---

## 1. Message envelope

```
<aeml turn="7" session="sess_a1b2c3">
  ...tags from §2...
</aeml>
```

```
<aeml_context turn="7" session="sess_a1b2c3"
              output_root="/output/sess_a1b2c3" input_root="/input/sess_a1b2c3" step="7/40">
  ...tags from §3...
</aeml_context>
```

---

## 2. Tags the LLM writes (LLM → interpreter)

| Tag | Purpose | Attrs | Body |
|---|---|---|---|
| `<plan>` | Optional persisted roadmap, available for the host UI, not executed | — | numbered steps |
| `<thought>` | Private scratch reasoning. Discarded, never executed or surfaced (§0.11) | — | free text |
| `<action id="...">` | Wraps one tool invocation | `id` unique per session | see below |
| &nbsp;&nbsp;`<tool>` | Which tool | — | name, must match §5 |
| &nbsp;&nbsp;`<path root="output\|input">` | Path arg, relative to the given root | `root` optional, defaults to `output` | e.g. `app.py` |
| &nbsp;&nbsp;`<args>` | Extra key-value args, tool-specific | — | `<key>value</key>` pairs (see §5 for per-tool schema) |
| &nbsp;&nbsp;`<chunk seq="N" final="true\|false">` | Marks this action as one piece of a multi-turn write (§7) | `seq`, `final` | — |
| &nbsp;&nbsp;`<expect_confirm>` | Marks action destructive; interpreter pauses for human approval | — | `true`/`false` |
| `<say>` | Message to the human, no tool call | — | free text |
| `<ask_user>` | Pause and wait for a real human reply | — | the question |
| `<next>` | `proceed` \| `await_result` \| `await_user` \| `done` \| `abort` | — | — |
| `<complete>` | Final closing block | — | summary |

Rules unchanged from v0.1: a turn may batch multiple `<action>` blocks only if every one is
read-only; `<next>` mandatory on every non-`<complete>` turn; `root="input"` on any write tool
is rejected before execution (`<error code="input_readonly">`).

Because AEML is parsed as XML, free-text arguments containing source code should use CDATA so
characters such as `<` and `&` cannot corrupt the envelope:

```
<args><content><![CDATA[if value < 10:
    print("safe payload")]]></content></args>
```

---

## 3. Tags the interpreter writes (interpreter → LLM)

| Tag | Purpose |
|---|---|
| `<user_prompt>` | The human's current task text; present when a new human prompt enters the session |
| `<plan>` | Restates the compact persisted plan |
| `<history><summary id="..." tool="..." status="...">` | One-line records for older results outside the full-result window |
| `<result id="..." lines="120-160">` | Wraps tool output; `lines` present when the action used `start_line`/`end_line` |
| `<error id="..." code="...">` | Structured failure, fixed code enum (§8) |
| `<status>` | `success` \| `failure` \| `partial` \| `timeout` |
| `<output>` | Escaped text body of a successful, partial, or timed-out result |
| `<message>` | Escaped human-readable body of a structured error |
| `<truncated total_bytes="..." offset="...">` | Reactive cut for size — different from the LLM's own proactive `<chunk>` (§7) |
| `<env>` | Restates `output_root`, `input_root`, `cwd`, and session `status` every turn |
| `<system_notice type="...">` | step-limit approaching, confirmation pending, **`likely_truncated_by_message_limit`** (§7) |

The interpreter constructs this XML structurally, never by concatenating untrusted text.
Metacharacters in prompts, plans, results, errors, and summaries are escaped. A character that
XML 1.0 cannot represent is emitted visibly as a literal `\uXXXX` or `\UXXXXXXXX`;
the containing element receives `escaped_controls="true"`. Both roots and the environment
are stamped with logical session paths only, never physical host paths.

---

## 4. Protocol / turn lifecycle

```
0. New chat starts. Interpreter creates /output/<session_id>/ and /input/<session_id>/.
   If the user pointed swoon code at an existing project, it's copied into /input/<session_id>/
   at this step — read-only reference material from here on.
1. User sends the build prompt.
2. Interpreter generates a bootstrap from the enabled tool schemas and sends turn 1:
   <aeml_context> with both roots, environment, and the prompt.
3. Chatbot replies: <plan> (optional) + first <action> + <next>.
4. Interpreter validates tool name, root+path (must resolve inside the matching session
   folder), args. If <expect_confirm>true</expect_confirm>: pauses, asks the human directly
   in the terminal, never asks the chatbot. Executes.
5. Interpreter wraps output in <result id="matching-id">, <status>, fresh <env>, and sends the
   next <aeml_context> with a compact continuation reminder.
6. Loop continues until <complete>, <ask_user>, or a hard stop (step limit, abort, unrecoverable
   error, or an unresolved chunk sequence — §7).
7. On <complete>, interpreter prints the summary and closes the session.
```

The transport bridge performs one numbered exchange per call. The autonomous orchestrator owns
the repetition, action execution, retries, user pauses, and lifecycle transitions.

In the read-only implementation, one new numbered AEML turn consumes one persisted session
step. A malformed or invalid response retries that same turn and does not consume another step.
Validated action IDs are durably reserved before dispatch, including IDs whose tool execution
returns an error. Conversation turns are channel-local; a new channel attached to a persisted
session starts at turn 1 while retaining the session's global step and action-ID history.

---

## 5. Capability tree — what needs to be a tool

Only things that require touching the real machine. Anything the hosted chatbot already does in
its own head (web research, math, translation, its own memory feature, explaining pasted code)
stays out of AEML entirely — no round trip for something local execution can't improve on.

```
cli-agent
├── terminal
│   ├── run-command            (foreground, blocking, hard timeout, optional max_output_lines)
│   ├── run-command-background (offline long-running work — returns a handle, not output)
│   ├── kill-process           (needs handle from run-command-background)
│   ├── stream-output          (chunked reads from a background process, by handle + offset)
│   └── get-env / set-env
├── filesystem
│   ├── read                                              (root: output OR input)
│   │   ├── read-file          (args: start_line, end_line — optional, omit for whole file)
│   │   ├── list-dir           (args: recursive, pattern — optional glob filter)
│   │   └── grep               (args: pattern, max_results, context_lines)
│   ├── write                                             (root: output ONLY)
│   │   ├── create-file        (supports <chunk> — §7)
│   │   ├── overwrite-file     (destructive if target non-empty → expect_confirm; supports <chunk>)
│   │   ├── append-file        (the tool chunk continuations use)
│   │   ├── edit-file          (str_replace-style patch — preferred over overwrite; no chunking,
│   │   │                       since a patch should already be small — see §7 note)
│   │   ├── copy-file / copy-dir  (input→output or output→output, interpreter-level —
│   │   │                          content never passes through the LLM, so this is the
│   │   │                          preferred way to bring an existing /input file into /output
│   │   │                          without burning message budget retyping it)
│   ├── delete                 (always destructive → expect_confirm; output root only)
│   │   ├── delete-file
│   │   └── delete-dir
│   ├── move / rename          (output root only)
│   └── chmod                  (regular output files; owner-private 0600/0700 only)
├── package-management
│   ├── install-dependency
│   ├── remove-dependency
│   └── list-dependencies
├── version-control (git — operates on /output/<session_id> as the repo root)
│   ├── init / status / diff / log (max_count)   (read-only, batchable)
│   ├── add / commit
│   ├── branch / checkout
│   ├── push / pull                              (needs credentials — see §6.1)
│   └── merge / rebase                           (destructive → expect_confirm)
├── build-and-test
│   ├── run-build
│   ├── run-tests
│   └── run-linter
├── session-state (interpreter-managed, LLM never writes these directly)
│   ├── session_id → output/input folder pair
│   ├── step counter / max-steps guard
│   └── plan / todo persistence
└── human-interaction
    ├── ask-user               (blocking, real human)
    ├── confirm-destructive    (blocking, real human)
    └── status-update          (non-blocking <say>)

OUT OF SCOPE — the hosted chatbot already does these, don't wrap them:
  web search / research, general Q&A, math, translation, summarizing pasted text,
  the chatbot's own hosted "memory" feature, explaining code shown in-chat.
```

General rule applied throughout: **any tool whose output can be large gets an explicit scoping
param the LLM sets proactively** (`start_line`/`end_line`, `recursive`/`pattern`, `max_results`,
`max_count`, `max_output_lines`) — on top of, not instead of, the interpreter's reactive
`<truncated>` fallback in §8. The proactive param avoids wasting a turn on an oversized result
in the first place; the reactive fallback catches what wasn't scoped.

---

## 6. Sandbox rules

- Both roots are fixed and identical across host OS: `/output/<session_id>/`,
  `/input/<session_id>/`. These are logical protocol paths mapped internally to private host
  session directories. No absolute paths outside these, no `..` traversal — `<error
  code="path_escape">`.
- Root-relative paths always use `/` separators. Drive paths, backslashes, embedded `.`
  segments, empty segments, control characters, and platform-reserved device names are
  rejected so the accepted namespace is identical on Windows/Linux/Mac.
- Symbolic-link path components and hard-linked regular files are rejected. An authorized path
  is fingerprinted and revalidated immediately before use; a changed path fails closed.
- `/input/<session_id>/` is read-only to every tool, no exceptions, not even
  `overwrite-file` — attempting a write there is `<error code="input_readonly">` before
  execution, regardless of who asked for it (LLM output or the human's own prompt text).
- A session can only ever resolve paths inside its own `<session_id>` pair — no cross-session
  reads, even read-only ones.
- Fixed denylist inside both roots regardless of path resolution: `.env`, `.git/config`,
  `*.pem`, `id_rsa*`, anything credential-shaped. Blocks both a hallucinated instruction and an
  injected one from exfiltrating secrets that happen to live in the project folder. Directory
  listings filter denied children as well.
- Command tools are not shell escape hatches for these rules — path-shaped arguments inside a
  command string are checked against the same sandbox before execution.
- In the Phase 12/13 command implementations, commands are split into a fixed argv and are never
  passed to an interpreter-selected shell. Direct shell operators, traversal, cross-session
  absolute paths, credential paths, and URL arguments fail before launch.
- Foreground and background commands receive credential-filtered snapshots rather than the physical session
  directories. Input is mounted read-only; output is copied into a size-limited tmpfs. All
  command-side changes are discarded, so `run-command` cannot bypass overwrite/delete policy.
- Command sandboxes are offline: their environment is cleared and socket creation is denied by
  inherited seccomp in addition to user/mount/PID/IPC/UTS isolation and resource limits. A host
  without the required 64-bit Linux sandbox primitives fails closed.

### 6.1 Credentials
The LLM never sees or supplies credentials. The interpreter injects them from local config at
execution time — a `git push` action from the LLM names only the remote/branch.

---

## 7. Chunked writes — handling the chatbot's message-size limit

There's no published per-message limit for chatgpt.com's web chat, so the interpreter can't
check content against a known threshold before sending — this has to be judgment on the LLM's
side, applied *before* it starts writing, not discovered mid-write. What the interpreter can do
is give it a clean mechanism for splitting a large write across turns, and recognize the specific
failure shape when the LLM misjudges and gets cut off anyway.

**Mechanism** — any write action can carry a `<chunk>` tag:

```
<action id="c1">
  <tool>create-file</tool><path>app.py</path>
  <args><content>...first piece...</content></args>
  <chunk seq="1" final="false"/>
</action>
```
```
<action id="c2">
  <tool>append-file</tool><path>app.py</path>
  <args><content>...next piece...</content></args>
  <chunk seq="2" final="true"/>
</action>
```

Rules:
- `seq="1"` must use `create-file` (or `overwrite-file`, with its normal confirm rule). Every
  later `seq` for the same path in the same session must use `append-file`.
- Interpreter tracks the last `seq` seen per path. A `seq` that isn't exactly `previous + 1` is
  rejected — `<error code="chunk_sequence_error">` — no silent gap-filling.
- Until a `<chunk final="true">` is received for a path, that file is "in progress." Any other
  action that depends on it — `run-command`, `read-file`, `edit-file`, lifecycle changes, tests,
  git-add — is rejected with `<error code="write_incomplete">` until finalized.
- `edit-file` (str_replace-style patches) generally shouldn't need chunking — if a single patch
  is large enough to hit the limit, that's a signal it should be decomposed into several smaller
  `edit-file` calls against the same file rather than one chunked mega-patch.
- `copy-file`/`copy-dir` never need chunking — content moves interpreter-side, never through the
  LLM's context at all. This is the main relief valve for the "existing large file" case.

**Detecting an unintentional cutoff** (the LLM misjudged and got cut off mid-message, as
opposed to writing genuinely malformed AEML): if a reply ends without a closing `</aeml>` tag,
treat that as `likely_truncated_by_message_limit` rather than a generic `parse_error`. The
read-only interpreter responds on a repair attempt:

```
<system_notice type="likely_truncated_by_message_limit" attempt="1" remaining="2"/>
```

The incomplete envelope is never executed and no partial action ID is accepted. The LLM must
return one complete replacement envelope for the same turn. Proactive `<chunk>` actions still
split large writes across valid, complete protocol turns once write execution is enabled.

---

## 8. Edge cases

| Edge case | Rule |
|---|---|
| Malformed/unparseable AEML, closing tag present | `<system_notice type="parse_error">`, up to 2 retries, then `<error code="malformed_output">` and end session. |
| Reply ends with no closing `</aeml>` | Treated as `likely_truncated_by_message_limit` (§7), not `parse_error`; the incomplete envelope is not executed and the same turn is retried. |
| Unknown `<tool>` name | `<error code="unknown_tool">`, valid list from §5, no execution. |
| Path resolves outside the session's own root pair | `<error code="path_escape">`. Two occurrences in a session → hard stop. |
| Write with `root="input"` | `<error code="input_readonly">` before execution, no exceptions. |
| Destructive action | Requires `<expect_confirm>`; human at the terminal confirms, never the chatbot. |
| Chunk out of sequence | `<error code="chunk_sequence_error">`, no gap-filling. |
| Action depends on a file with an unfinalized chunk | `<error code="write_incomplete">`. |
| Tool output too large, LLM didn't scope it (no start_line/max_results/etc.) | `<truncated total_bytes="..." offset="0">` + preview; LLM pages with a follow-up scoped read. |
| Binary file requested via `read-file` | `<error code="binary_unsupported">`. |
| Multiple write actions in one reply | `<error code="batch_write_not_allowed">` — only read-only actions batch. |
| Runaway session | Default `max_steps=40` (configurable). 80% → `step_limit_approaching` notice. At 100%, the loop pauses for explicit human-side step approval. |
| Tool execution failure | `<error code="tool_failed">` with stderr. Read ops: 1 silent retry. Write/execute ops: 0 automatic retries. |
| `<ask_user>` mid-loop | Loop pauses fully; only a real human reply resumes it. |
| Cross-session path reference | `<error code="path_escape">` — sessions are fully isolated. |
| Session/context growing large over many turns | Keep bounded `<plan>` text, the last N results in bounded full form, and bounded one-line history summaries. Emit compaction/omission notices and always re-stamp both roots and `<env>`. |
| Empty directory listed | Normal `<result>`, empty body, not an error. |

---

## 9. Worked trace — Case A: single build prompt, empty session

**User → swoon code:** "Build a simple Flask hello-world API." *(new chat → session `sess_9f2`)*

```
Turn 0 (interpreter, no LLM call)
  creates /output/sess_9f2/ and /input/sess_9f2/ (empty — nothing to import)

Turn 1 (interpreter → LLM)
<aeml_context turn="1" session="sess_9f2"
              output_root="/output/sess_9f2" input_root="/input/sess_9f2" step="1/40">
  <env output_root="/output/sess_9f2" input_root="/input/sess_9f2"
       cwd="/output/sess_9f2" status="active"/>
  <user_prompt>Build a simple Flask hello-world API.</user_prompt>
</aeml_context>

Turn 1 (LLM → interpreter)
<aeml turn="1" session="sess_9f2">
  <plan>
    1. Confirm output/ is empty
    2. Create app.py
    3. Create requirements.txt
    4. Declare an exact dependency, syntax-check
  </plan>
  <action id="a1"><tool>list-dir</tool><path root="output">.</path></action>
  <next>await_result</next>
</aeml>

Turn 2 (interpreter → LLM) →
<result id="a1"><status>success</status><output /></result>  (empty)

Turn 2 (LLM → interpreter)
<aeml turn="2" session="sess_9f2">
  <action id="a2">
    <tool>create-file</tool><path root="output">app.py</path>
    <args><content>...flask hello world source...</content></args>
    <chunk seq="1" final="true"/>
  </action>
  <next>await_result</next>
</aeml>

Turn 3-4: requirements.txt created the same way, single chunk each (both small enough to fit
in one message — no reason to split a two-line file).

Turn 5 (LLM → interpreter)
<aeml turn="5" session="sess_9f2">
  <action id="a5"><tool>install-dependency</tool>
    <args><manager>pip</manager><package>Flask==3.1.0</package></args>
    <expect_confirm>true</expect_confirm>
  </action>
  <next>await_result</next>
</aeml>

Turn 6 (LLM → interpreter)
<aeml turn="6" session="sess_9f2">
  <action id="a6"><tool>run-command</tool>
    <args><cmd>python -m compileall app.py</cmd><timeout>10</timeout><max_output_lines>50</max_output_lines></args>
  </action>
  <next>await_result</next>
</aeml>

Turn 7 (LLM → interpreter)
<aeml turn="7" session="sess_9f2">
  <complete>Created a Flask hello-world app in /output/sess_9f2 (app.py, requirements.txt),
  dependencies installed, smoke-tested successfully.</complete>
</aeml>
```

---

## 10. Worked trace — Case B: modifying an existing project, with git

**User → swoon code:** "Here's my project [points swoon code at a local folder]. Add a /health
endpoint and commit it." *(new chat → session `sess_7c1`)*

```
Turn 0 (interpreter, no LLM call)
  creates /output/sess_7c1/ and /input/sess_7c1/
  copies the user-pointed project into /input/sess_7c1/  (read-only reference from here on)

Turn 1 (interpreter → LLM) — both roots stamped, prompt included

Turn 1 (LLM → interpreter)
<aeml turn="1" session="sess_7c1">
  <plan>
    1. List /input to see what exists
    2. Read app.py from /input
    3. Copy the project into /output (so it becomes the writable working copy)
    4. Patch /output/app.py with the /health route
    5. Run tests, git init + commit in /output
  </plan>
  <action id="b1"><tool>list-dir</tool><path root="input">.</path></action>
  <action id="b2"><tool>read-file</tool><path root="input">app.py</path><args><start_line>1</start_line><end_line>60</end_line></args></action>
  <next>await_result</next>
</aeml>

Turn 2 (interpreter → LLM) — dir listing (b1) + first 60 lines of app.py (b2), <result lines="1-60">

Turn 2 (LLM → interpreter)
<aeml turn="2" session="sess_7c1">
  <action id="b3"><tool>copy-dir</tool>
    <args><from root="input">.</from><to root="output">.</to></args>
  </action>
  <next>await_result</next>
</aeml>
```
*(`copy-dir` moves the whole project input→output at the interpreter level — app.py's full
content never has to pass through the LLM's own context just to relocate it.)*
```

Turn 3 (LLM → interpreter)
<aeml turn="3" session="sess_7c1">
  <action id="b4">
    <tool>edit-file</tool><path root="output">app.py</path>
    <args>
      <old_str>if __name__ == "__main__":</old_str>
      <new_str>@app.route("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":</new_str>
    </args>
  </action>
  <next>await_result</next>
</aeml>

Turn 4 (LLM → interpreter)
<aeml turn="4" session="sess_7c1"><action id="b5"><tool>run-tests</tool></action><next>await_result</next></aeml>

Turn 5 (LLM → interpreter)
<aeml turn="5" session="sess_7c1"><action id="b6"><tool>git-init</tool></action><next>await_result</next></aeml>

Turn 6 (LLM → interpreter)
<aeml turn="6" session="sess_7c1"><action id="b7"><tool>git-add</tool><path root="output">.</path></action><next>await_result</next></aeml>

Turn 7 (LLM → interpreter)
<aeml turn="7" session="sess_7c1">
  <action id="b8"><tool>git-commit</tool><args><message>Add /health endpoint</message></args></action>
  <next>await_result</next>
</aeml>

Turn 8 (LLM → interpreter)
<aeml turn="8" session="sess_7c1">
  <complete>Copied the project into /output/sess_7c1, added GET /health to app.py, tests pass,
  initialized git and committed as "Add /health endpoint".</complete>
</aeml>
```

Note what changed from v0.1's version of this trace: reading `app.py` used a line range instead
of the whole file, and bringing the existing project into the writable root used `copy-dir`
instead of the LLM re-typing file contents it had already read — both are the two "wherever
needed" params/tools doing their job of keeping message size down.

---

## 11. Resolved and remaining items

Resolved by the session-management implementation:

- `max_steps` defaults to 40 per session. The interpreter has a global default and creation of
  an individual session may override it.
- Explicitly loading an existing `session_id` resumes the same `/output/<id>` + `/input/<id>`
  pair and its persisted state. Starting without a session ID always creates a fresh pair.

Resolved by the read-only tool implementation:

- Reactive read-tool output truncation defaults to 64 KiB and is configurable through the
  interpreter's read limits. The result reports its full generated byte count with offset zero.
- `grep`'s pattern is a literal UTF-8 substring, not a regular expression. This keeps matching
  deterministic and avoids regex denial-of-service at the interpreter boundary.
- Read-only Git commands run only against a bounded disposable repository snapshot with local,
  global, and system Git configuration disabled. Credential paths and external object stores are
  excluded before Git runs.

Resolved by the context and prompt implementation:

- Interpreter context is deterministic, XML-safe, byte-bounded, and contains logical roots only.
- Recent results remain available in bounded full form while older results become bounded
  one-line summaries with explicit compaction and omission notices.
- Bootstrap tool schemas are generated from the executable allowlist. The same reduced schema map
  validates assistant responses, so future-facing registry entries are not accidentally enabled.
- A session-bound channel performs one transport exchange at a time and advances its turn only
  after the assistant response parses and validates.

Resolved by the read-only orchestration implementation:

- The loop owns step advancement, plan persistence, read dispatch, result/error feedback,
  completion, abort, human questions, and step-limit pauses.
- Parse, truncation, and validation failures retry the same protocol turn up to the configured
  repair bound; exhaustion aborts with `malformed_output`.
- Every validated action ID is persisted before execution, so failed IDs cannot be reused after
  a later turn or process restart.
- Step-limit extension requires an explicit human-facing API argument while waiting at the
  exhausted limit; AEML cannot extend its own budget.

Resolved by the agent CLI implementation:

- `swoon agent` creates or resumes persistent sessions and drives every orchestration outcome.
- Real terminal answers resume `<ask_user>` pauses; `/abort` persists a human abort.
- Step extensions require an interactive numeric approval or the resume-only
  `--additional-steps` option. Non-interactive pauses return a distinct resumable exit code.
- `swoon chat` and the legacy entrypoints remain unstructured relays and never execute AEML.

Resolved by the output filesystem mutation implementation:

- `create-file`, `overwrite-file`, `append-file`, `edit-file`, `copy-file`, and `copy-dir` are
  executable through a separate agent allowlist; input remains read-only.
- File publication is descriptor-relative, no-follow, bounded, and atomic. Directory copies use
  exclusive destinations, filter credential-shaped entries, and clean up handled failures.
- Chunk sequences advance atomically with successful action results and continue to block
  dependent reads until finalization.
- Non-empty overwrites persist the exact action and an opaque target snapshot guard before
  returning for real-human approval. Approval survives process restart and fails closed if the
  target changed; denial leaves it untouched.

Resolved by the foreground command implementation:

- `run-command`, `run-build`, `run-tests`, and `run-linter` execute one foreground argv in a
  bounded Bubblewrap sandbox; no interpreter-selected shell or unsandboxed fallback exists.
- Both virtual roots are copied through the descriptor-relative policy boundary. Credential
  entries are omitted, input stays read-only, and a bounded tmpfs working output is destroyed
  after every run, regardless of exit status.
- Host environment and credentials are cleared, socket creation is denied through inherited
  seccomp, system paths are read-only, nested user namespaces are disabled, and CPU/wall time,
  memory, file, process, descriptor, snapshot, tmpfs, and captured-output limits are enforced.
- Exit zero/nonzero/timeout become structured, persisted results with bounded combined output.
  Safety-capture overflow terminates the process and returns an error without persisting a
  misleading partial completion.
- Managed build/test/linter commands use fixed per-ecosystem argv templates. Omitted manager
  detection must find exactly one ecosystem; target is one bounded argument, never command text.

Resolved by the background command implementation:

- `run-command-background` reuses the offline disposable command sandbox and publishes an opaque,
  session-scoped process handle only after the trusted launcher readiness marker appears.
- A live in-memory supervisor owns the exact process object, incrementally captures sanitized
  UTF-8 into a private bounded log, and terminates on line, byte, runtime, session, or host limits.
- `stream-output` is the liveness query and heartbeat: it reports status plus a stable byte count
  and continues through a validated UTF-8 `next_offset`. `kill-process` signals only the exact
  live supervisor associated with the session handle.
- Persisted PIDs are diagnostic only and are never used for signals. A `running` record without
  its original live supervisor becomes `lost`/`supervisor_lost`, preventing PID-reuse attacks.
- Terminal orchestration and every CLI exit explicitly stop live work and persist the reason.
  State schema v5 records monotonic output counters plus immutable exit/reason/end metadata.

Resolved by the persistent filesystem lifecycle implementation:

- `delete-file`, `delete-dir`, `move`, `rename`, and `chmod` execute only inside output. The
  read-only compatibility dispatcher continues to reject them.
- Both delete tools always persist the exact action plus a bounded file/tree metadata guard and
  pause for a real-human decision. Approval after any guarded change fails with
  `confirmation_stale`; denial changes nothing.
- Directory preflight and removal reject links, hard-linked files, special entries, protected
  descendants, roots, and configured entry/byte overflow. Every opened entry is reverified
  through descriptor-relative no-follow access.
- Move uses Linux `renameat2(RENAME_NOREPLACE)` and rename additionally requires one unchanged
  parent. Missing atomic kernel support fails closed; an existing or racing destination is never
  replaced, and recursive directory destinations are rejected.
- `chmod` accepts only `0600`/`0700` on opened regular files. Broader modes and directories are
  outside the capability.
- Unfinished chunks block lifecycle operations. A successful delete clears chunk records under
  its scope, while move/rename remaps them with the action result under the session lock.

Resolved by the consumer release implementation:

- `python -m swoon`, the installed `swoon` entry point, and `--version` expose the same CLI.
- `swoon doctor` validates Playwright/Chromium installation, optional cookie JSON, and the
  command-sandbox prerequisites without contacting ChatGPT.
- A deterministic standard-library builder produces a pure-Python wheel with hashed `RECORD` and
  console metadata while excluding cookies, tests, sessions, and caches.
- A network-disabled smoke runner installs that wheel into a fresh virtual environment and tests
  the installed entry point rather than importing the source checkout.

Resolved by the guarded dependency declaration implementation:

- `install-dependency` and `remove-dependency` require one package plus a supported manager,
  mutate one recognized output manifest atomically, and always pause for real-human confirmation.
- Additions require exact registry versions. URLs, paths, version ranges, floating tags, inherited
  credentials, network access, and package script execution are outside the capability.
- A related lockfile causes a fail-closed `lockfile_present` result rather than an inconsistent
  manifest/lock pair. Approval is guarded by the manifest identity, metadata, size, and SHA-256.
- Results explicitly report `package_artifacts=not_installed`; the historical tool name does not
  imply that Phase 16 has downloaded or promoted executable third-party code.

Resolved by the release licensing implementation:

- Source code is licensed under the unmodified Apache License 2.0 with a root `LICENSE` and
  informational `NOTICE`; responsible-use guidance is kept separate from the license terms.
- `pyproject.toml` declares the `Apache-2.0` SPDX expression and its legal files using PEP 639.
- The deterministic wheel emits core metadata 2.4, packages `LICENSE` and `NOTICE` beneath
  `.dist-info/licenses/`, hashes them in `RECORD`, and smoke-tests their installed metadata.
- The README and `RESPONSIBLE_USE.md` state the educational purpose, non-affiliation, credential
  precautions, and user responsibility without claiming that intent overrides provider terms.

Remaining open items:

- Whether a future network-service capability should retain and join a dedicated isolated network
  namespace so a dev server can be tested without exposing host networking. Phase 13 remains
  socket-denied and does not claim dev-server support.
- How package artifact acquisition should grant narrowly scoped network access, verify provenance,
  refresh locks, and promote package state without exposing user credentials or package caches.
- How Git mutations should isolate hooks, configuration, credentials, and destructive history
  changes while preserving exact-action confirmation.
- Whether a future reviewed-patch workflow should allow selected command-generated changes to be
  promoted from the disposable workspace without bypassing destructive confirmation.
