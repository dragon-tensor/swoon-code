import json
import sys
import time
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

SAMESITE_MAP = {"lax": "Lax", "strict": "Strict", "none": "None"}

TEXTAREA_SELECTORS = [
    "#prompt-textarea",
    "textarea[placeholder*='message' i]",
    "textarea[placeholder*='Send' i]",
    "[contenteditable='true']",
]
SEND_BUTTON_SELECTORS = [
    "button[data-testid='send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label*='send' i]",
]
STOP_BUTTON_SELECTORS = [
    "button[data-testid='stop-button']",
    "button[aria-label='Stop generating']",
    "button[aria-label='Stop streaming']",
]
MESSAGE_SELECTOR = "[data-message-author-role='assistant']"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

SYSTEM_PROMPT = """You are connected to the user's machine. You can create files, run commands, and search the web.

Use these blocks when the user asks you to do something on their machine:

[write:/absolute/path/file]
content here
[/write]

[run]
command here
[/run]

[browse]
search query
[/browse]

How it works:
1. You output the block
2. I execute it on the machine
3. I show you the result
4. Then you respond to the user

You can chain multiple blocks in one response. Always respond naturally after the results come back."""

SYSTEM_REMINDER = "\n(If you need to use the machine, use [write:], [run], or [browse] blocks.)"


def parse_actions(text: str) -> list[dict]:
    actions = []
    # [write:/path] ... [/write]
    for m in re.finditer(r'\[write:([^\]]+)\]\s*(.*?)\s*\[/write\]', text, re.DOTALL):
        actions.append({"type": "write", "path": m.group(1).strip(), "content": m.group(2)})
    # [run] ... [/run]
    for m in re.finditer(r'\[run\]\s*(.*?)\s*\[/run\]', text, re.DOTALL):
        actions.append({"type": "run", "command": m.group(1).strip()})
    # [browse] ... [/browse]
    for m in re.finditer(r'\[browse\]\s*(.*?)\s*\[/browse\]', text, re.DOTALL):
        actions.append({"type": "browse", "query": m.group(1).strip()})
    return actions


def execute_action(action: dict) -> str:
    t = action["type"]
    if t == "write":
        path = action["path"]
        content = action["content"]
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"Written to {path} ({len(content)} bytes)"
        except Exception as e:
            return f"Write error: {e}"

    if t == "run":
        cmd = action["command"]
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=60
            )
            out = r.stdout.strip()
            err = r.stderr.strip()
            parts = []
            if out:
                parts.append(f"--- stdout ---\n{out}")
            if err:
                parts.append(f"--- stderr ---\n{err}")
            parts.append(f"exit code: {r.returncode}")
            return "\n".join(parts) or "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out (60s)"
        except Exception as e:
            return f"Run error: {e}"

    if t == "browse":
        query = action["query"]
        if not query:
            return "No query."
        import httpx
        try:
            r = httpx.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            results = re.findall(
                r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>',
                r.text,
            )
            return "\n".join(f"{t}: {u}" for u, t in results[:5]) or "No results."
        except Exception as e:
            return f"Browse error: {e}"

    return f"Unknown action: {t}"


class ChatGPTAgent:
    def __init__(self, cookie_path: str, verbose: bool = False):
        self.cookie_path = cookie_path
        self.verbose = verbose
        self._playwright_cm = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.turn_count = 0
        self._load_cookies()

    def log(self, msg: str):
        if self.verbose:
            print(f"[agent] {msg}", file=sys.stderr)

    def _load_cookies(self):
        with open(self.cookie_path) as f:
            raw = json.load(f)
        for c in raw:
            if "sameSite" in c and isinstance(c["sameSite"], str):
                c["sameSite"] = SAMESITE_MAP.get(c["sameSite"].lower(), c["sameSite"])
            if "domain" in c and "openai.com" in c["domain"] and not c["domain"].startswith("."):
                c["domain"] = "." + c["domain"].lstrip(".")
                c["hostOnly"] = False
        self.raw_cookies = raw
        self.log(f"Loaded {len(raw)} cookies")

    def start(self):
        from playwright.sync_api import sync_playwright
        self._playwright_cm = sync_playwright()
        self.playwright = self._playwright_cm.__enter__()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
        )
        self.context.add_cookies(self.raw_cookies)
        self.page = self.context.new_page()
        self.page.set_default_timeout(60000)

        self.log("Navigating to chat.openai.com...")
        self.page.goto("https://chat.openai.com", wait_until="domcontentloaded")
        for _ in range(60):
            title = self.page.title()
            if "just a moment" in title.lower():
                time.sleep(2)
                continue
            break
        self.log(f"Page: {self.page.url}")

        if "login" in self.page.url.lower():
            raise RuntimeError("Redirected to login. Refresh cookies from chatgpt.com.")

        for sel in TEXTAREA_SELECTORS:
            try:
                self.page.wait_for_selector(sel, timeout=5000)
                self.log("Chat input found")
                return
            except Exception:
                continue
        self.page.screenshot(path="/tmp/chatgpt_debug.png")
        raise RuntimeError("Chat input not found. Session expired?")

    def send(self, text: str) -> str:
        el = None
        for sel in TEXTAREA_SELECTORS:
            e = self.page.query_selector(sel)
            if e and e.is_visible():
                el = e
                break
        if not el:
            raise RuntimeError("Chat input not found")

        el.click()
        el.fill("")
        self.page.keyboard.type(text, delay=5)
        time.sleep(0.3)

        btn = None
        for sel in SEND_BUTTON_SELECTORS:
            b = self.page.query_selector(sel)
            if b and b.is_visible():
                btn = b
                break
        if btn:
            btn.click()
        else:
            self.page.keyboard.press("Enter")

        return self._wait()

    def _wait(self, timeout=180) -> str:
        last = ""
        stable = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            stop = any(
                b.is_visible()
                for s in STOP_BUTTON_SELECTORS
                if (b := self.page.query_selector(s))
            )
            if not stop:
                msgs = self.page.query_selector_all(MESSAGE_SELECTOR)
                if msgs:
                    cur = msgs[-1].inner_text()
                    if cur:
                        if cur == last:
                            stable += 1
                            if stable >= 3:
                                return cur
                        else:
                            stable = 0
                        last = cur
            else:
                stable = 0
            time.sleep(0.5)
        self.log("Timed out")
        msgs = self.page.query_selector_all(MESSAGE_SELECTOR)
        return msgs[-1].inner_text() if msgs else ""

    def run_agent(self, user_message: str) -> str:
        self.turn_count += 1
        if self.turn_count == 1:
            full = SYSTEM_PROMPT + "\n\n" + user_message
        elif self.turn_count % 3 == 0:
            full = user_message + SYSTEM_REMINDER
        else:
            full = user_message

        self.log(f"Turn {self.turn_count}, sending to ChatGPT...")
        raw = self.send(full)
        self.log(f"Response ({len(raw)} chars)")

        actions = parse_actions(raw)
        if not actions:
            return raw

        self.log(f"Found {len(actions)} action(s)")
        results = []
        for a in actions:
            self.log(f"  Executing: {a['type']}")
            result = execute_action(a)
            self.log(f"  Result: {result[:80]}...")
            results.append(f"[result for {a['type']}]\n{result}")

        result_text = "\n\n".join(results)
        feedback = f"{result_text}\n\nContinue your response based on these results."
        self.log("Sending results back to ChatGPT...")
        final_raw = self.send(feedback)
        return final_raw

    def close(self):
        try:
            self.context.storage_state(path=self.cookie_path + ".state")
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self._playwright_cm.__exit__(None, None, None)
        except Exception:
            pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ChatGPT Agent")
    parser.add_argument("--cookies", required=True)
    parser.add_argument("--prompt", "-p")
    parser.add_argument("--interactive", "-i", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if not args.prompt and not args.interactive:
        parser.print_help()
        sys.exit(1)

    agent = ChatGPTAgent(args.cookies, verbose=args.verbose)
    try:
        agent.start()

        if args.interactive:
            print("Agent — type messages, /quit to exit")
            while True:
                try:
                    msg = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not msg:
                    continue
                if msg == "/quit":
                    break
                print()
                resp = agent.run_agent(msg)
                print(resp)
                print()
        else:
            resp = agent.run_agent(args.prompt)
            print(resp)
    finally:
        agent.close()


if __name__ == "__main__":
    main()
