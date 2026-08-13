# ChatGPT CLI

Headless browser terminal chatbot for ChatGPT. Uses your own session — no API key.

**⚠️ Experimental / educational.** Automated access may violate OpenAI's ToS.

## Setup

```bash
pip install playwright
playwright install chromium
```

## Usage

```bash
# Interactive
./chatgpt.sh --cookies cookies.json -i

# Single prompt
./chatgpt.sh --cookies cookies.json -p "What is Rust?"
```

## Getting cookies

1. Log into [chatgpt.com](https://chatgpt.com) in Chrome/Firefox
2. Install [Cookie-Editor](https://cookie-editor.com/)
3. Export → save as `cookies.json`
