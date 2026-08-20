# Migration Guide — ChatGPT CLI

> Historical snapshot: this document describes the original relay before the package, AEML
> interpreter, and agent CLI were added. See `README.md` and `docs/agent-cli.md` for the current
> implementation.

## Project overview

A historical command-line experiment that relayed messages through a user-provided browser
session. This is not the current product description or a claim that browser automation is
permitted; see `README.md`, `RESPONSIBLE_USE.md`, and `docs/supported-scope.md`.

**Historical target:** Terminal-based exploration of a browser relay before AEML was restored.

---

## Repo structure

```
.
├── chatgpt_agent.py       # Single-file Python app (178 lines)
├── chatgpt.sh             # Shell wrapper (activates venv, runs the script)
├── cookies.json           # Live session cookies (exported from browser)
├── cookies.example.json   # Template with placeholder values
├── cookies.json.state     # Auto-generated Playwright storage state (saved on close)
├── README.md              # Quickstart docs
├── MIGRATION.md           # This file
├── .venv/                 # Python virtual environment
├── .gitignore
└── __pycache__/
```

---

## How it works (architecture)

1. **Browser launch** — Playwright spins up headless Chromium with your session cookies
2. **Session validation** — navigates to `chat.openai.com`, detects Cloudflare challenges, dismisses modals, finds the textarea
3. **Message relay** — types your prompt into the textarea, clicks send (or Enter), waits for response
4. **Response polling** — polls the DOM every 500ms checking for the "Stop generating" button to disappear and content to stabilize (3 consecutive identical reads)
5. **Output** — prints the last assistant message to stdout

There is **no agent loop, no tool execution, no system prompt injection**, no action block parsing. It is a pure terminal relay — what ChatGPT says is what you get.

### Key classes

| Class | Purpose |
|---|---|
| `ChatGPT` | Cookie loading, browser lifecycle, send/receive messages |

No other classes. Single-file Python.

### Key methods

| Method | Purpose |
|---|---|
| `__init__(cookie_path, verbose)` | Loads cookies, normalizes domains |
| `start()` | Launches Playwright, navigates, validates session |
| `send(text)` | Types message, clicks send, returns response |
| `_wait(timeout)` | Polls DOM until response stabilizes or timeout |
| `_dismiss_modals()` | Clicks dismiss on login/rate-limit modals |
| `close()` | Saves storage state, closes browser |

---

## Dependencies

| Package | Purpose |
|---|---|
| `playwright` (Python) | Headless browser automation |
| Chromium (via `playwright install chromium`) | Browser engine |

**No other dependencies.** No httpx, no requests, no OpenAI SDK.

---

## Setup (fresh machine)

```bash
# 1. Create venv and install
python3 -m venv .venv
.venv/bin/pip install playwright
.venv/bin/python -m playwright install chromium

# 2. Supply a private, authorized browser storage-state file.
#    Current credential requirements are documented in README.md.
```

---

## Usage

```bash
# Interactive mode (type messages, /quit to exit)
.venv/bin/python chatgpt_agent.py --cookies cookies.json -i

# Single prompt
.venv/bin/python chatgpt_agent.py --cookies cookies.json -p "Your message"

# With debug logs
.venv/bin/python chatgpt_agent.py --cookies cookies.json -v -p "Hello"
```

---

## Shell wrapper (`chatgpt.sh`)

At that historical snapshot it pointed to `/tmp/chatgpt-venv`; the current wrapper already uses
the repository-local environment. The old fix was:

```bash
# Line 4 — change path to local .venv
VENV="$(dirname "$0")/.venv"
```

---

## Cookies

The original prototype depended on a set of site-specific cookies. Their names and domains are
intentionally not retained as current setup advice: hosted interfaces and authentication details
change, and storage state is an account credential. Follow the current private-file and provider
authorization guidance instead.

**Session lifespan:** Unknown — varies. When expired, you'll see:
- `"Redirected to login. Refresh cookies from chatgpt.com."`
- A debug screenshot saved to a fixed temporary path (removed by the current private opt-in flow)

**Historical auto-save:** The prototype saved refreshed state automatically. Current Swoon
disables this by default and requires `--save-storage-state PATH` with private storage.

---

## Git history

```
cfd2c36  strip agent layers, revert to pure terminal chatbot    ← THEN CURRENT
20e3133  debug attemot 1                                          ← previous: full agent version
8a277f7  working interactive terminal
fcd34f6  First working prototype.
38d7d6a  Initial commit
```

---

## What was removed (previous agent version vs current chatbot)

The commit `cfd2c36` stripped ~312 lines of agent functionality:

- **Removed:** `SYSTEM_PROMPT` — the long instruction injected into every first turn telling ChatGPT to use action blocks
- **Removed:** `SYSTEM_REMINDER` — periodic reminder to use action blocks
- **Removed:** `parse_actions()` — regex parser for `[write:]`, `[read:]`, `[run]`, `[python]`, `[browse]`, `[fetch:]`, `[ls:]`, `[append:]`, `[edit:]`, `[sysinfo]` blocks
- **Removed:** `execute_action()` — the execution engine for all 10 action types (file ops, shell commands, Python eval, web fetch, DuckDuckGo search, system info)
- **Removed:** `_trim()` — output truncation utility
- **Removed:** `run_agent()` — the agent loop that sent prompt → parsed actions → executed → fed results back
- **Removed:** `turn_count` — tracking which turn to inject system prompts
- **Removed imports:** `os`, `re`, `subprocess`, `shutil`, `platform`, `pathlib.Path`
- **Removed constants:** `MAX_READ_SIZE`, `MAX_OUTPUT`
- **Renamed:** `ChatGPTAgent` → `ChatGPT`
- **Simplified:** `main()` now calls `client.send()` directly instead of `agent.run_agent()`

Nothing in that historical snapshot was agentic. The current package contains the bounded AEML
agent described in `README.md`.

---

## Historical improvements / known issues

This list records the old relay's gaps; several are resolved by the current package.

1. **Shell script path** — `chatgpt.sh` references `/tmp/chatgpt-venv`; needs updating to local `.venv`
2. **No `--headed` flag** — browser always runs headless; add for debugging
3. **No `--timeout` flag** — hardcoded 180s; previous version had it
4. **No cookie validation** — previous version checked for auth + Cloudflare cookies before launch
5. **No context manager** — previous `ChatGPTHeadless` supported `with client:` pattern
6. **Playwright browser download** — `playwright install chromium` downloads ~300MB
7. **Session expiry detection** — currently checks URL for "login" and screenshots as fallback; brittle if UI changes
8. **Modal selectors** — hardcoded modal IDs may break if OpenAI changes their frontend

---

## Key contacts / context

- **Project history:** maintained in Git; personal contact details are not runtime documentation
- **Language:** Python 3
- **Original intent:** Experiment with a terminal browser relay
- **Evolution:** Started as simple relay → became a full coding agent → stripped back to simple relay
