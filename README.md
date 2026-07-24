# ChatGPT Agent

Headless browser agent for ChatGPT. Uses your own session — no API key.

**⚠️ Experimental / educational.** Automated access may violate OpenAI's ToS.

## Setup

```bash
pip install playwright httpx
playwright install chromium
```

## Usage

```bash
# Interactive
./chatgpt.sh --cookies cookies.json -i

# Single prompt
./chatgpt.sh --cookies cookies.json -p "write hello.py with print('hi')"
```

## Agent capabilities

| Block | Description |
|---|---|
| `[write:/path] ... [/write]` | Create or overwrite a file |
| `[read:/path] [/read]` | Read a file |
| `[append:/path] ... [/append]` | Append to a file |
| `[edit:/path] Find: ... Replace: ... [/edit]` | Find & replace in file |
| `[ls:/path] [/ls]` | List directory |
| `[run] ... [/run]` | Run shell command |
| `[python] ... [/python]` | Run Python inline |
| `[fetch:url] [/fetch]` | Fetch a web page |
| `[browse] ... [/browse]` | Search DuckDuckGo |
| `[sysinfo] [/sysinfo]` | System info (OS, CPU, disk) |

ChatGPT outputs these blocks, the agent executes them and feeds results back.

## Getting cookies

1. Log into [chatgpt.com](https://chatgpt.com) in Chrome/Firefox
2. Install [Cookie-Editor](https://cookie-editor.com/)
3. Export → save as `cookies.json`