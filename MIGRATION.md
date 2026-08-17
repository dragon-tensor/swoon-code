# Migration Guide — ChatGPT CLI

> Historical snapshot: this document describes the original relay before the package, AEML
> interpreter, and agent CLI were added. See `README.md` and `docs/agent-cli.md` for the current
> implementation.

## Project overview

A command-line chatbot that relays messages to ChatGPT via a headless Playwright browser. Uses your own session cookies — no API key, no OpenAI billing.

**Target:** Terminal users who want ChatGPT without paying or using the web UI.

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

# 2. Export cookies
#    - Log into chatgpt.com in Chrome/Firefox
#    - Install Cookie-Editor extension
#    - Export as JSON → save as cookies.json
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

Currently points to `/tmp/chatgpt-venv` — **needs updating** before it can be used. Fix:

```bash
# Line 4 — change path to local .venv
VENV="$(dirname "$0")/.venv"
```

---

## Cookies

The app needs 4 cookies exported from an authenticated ChatGPT session:

| Cookie name | Domain |
|---|---|
| `f_clearance` | `.openai.com` |
| `oai-client-auth-info` | `.openai.com` |
| `unified_session_manifest` | `.chat.openai.com` |
| `usc_*` | `.chat.openai.com` |

**Session lifespan:** Unknown — varies. When expired, you'll see:
- `"Redirected to login. Refresh cookies from chatgpt.com."`
- A debug screenshot saved to `/tmp/chatgpt_debug.png`

**Auto-save:** On close, the app saves Playwright's storage state to `cookies.json.state` (not used for login, but may contain refreshed cookies).

---

## Git history

```
cfd2c36  strip agent layers, revert to pure terminal chatbot    ← CURRENT
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

Nothing in the current version is agentic. It is a thin relay.

---

## Possible improvements / known issues

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

- **Author:** dragon-tensor (dragon.tensor@gmail.com)
- **Language:** Python 3
- **Original intent:** Experiment to access ChatGPT without paying for API
- **Evolution:** Started as simple relay → became a full coding agent → stripped back to simple relay
