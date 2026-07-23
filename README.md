# ChatGPT Headless

Use your browser session cookies to talk to ChatGPT from the command line — no API key, no rate limits beyond what the free web UI gives you.

## How it works

Launches a headless Chromium via [Playwright](https://playwright.dev/python), loads your authenticated session cookies, sends a prompt through the web UI, and returns the streaming response.

## Setup

```bash
# Install dependencies (one-time)
pip install playwright
playwright install chromium
```

Or use the provided wrapper:

```bash
./chatgpt.sh --cookies cookies.json --prompt "Hello"
```

## Usage

```bash
python chatgpt_headless.py --cookies cookies.json --prompt "Your message here"
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--cookies` | required | Path to cookies JSON file |
| `--prompt` | required | Message to send |
| `--timeout` | 120 | Max seconds to wait for response |
| `--headed` | off | Show browser window (for debugging) |
| `--verbose` / `-v` | off | Print debug logs to stderr |

## Getting your cookies

1. Log into [chat.openai.com](https://chat.openai.com) in your browser
2. Install a cookie export extension (e.g. [Cookie-Editor](https://cookie-editor.com/))
3. Export cookies as JSON
4. Save the file as `cookies.json` in this directory

The script accepts both old-style (`__Secure-next-auth.session-token`) and new unified auth (`unified_session_manifest`, `usc_*`, `f_clearance`) cookies.

## Example

```bash
./chatgpt.sh --cookies cookies.json --prompt "Explain quantum computing in one sentence" -v
```

Use `--headed` if you want to watch the browser automate itself.
