# ChatGPT CLI

Headless browser relay for ChatGPT — uses your own session cookies. No API key, no credit card.

**⚠️ Experimental / educational use only.** Automated access may violate OpenAI's ToS.

## Setup

```bash
pip install playwright
playwright install chromium
```

## Usage

```bash
./chatgpt.sh --cookies cookies.json -p "What is Rust"

# Interactive mode
./chatgpt.sh --cookies cookies.json -i
```

## Getting cookies

1. Log into [chatgpt.com](https://chatgpt.com) in Chrome/Firefox
2. Install [Cookie-Editor](https://cookie-editor.com/) extension
3. Click it → **Export** → save as `cookies.json`
