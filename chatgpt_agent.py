import json
import sys
import time
import os
import re
import subprocess
import shutil
import platform
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

SYSTEM_PROMPT = """You are connected to the user's machine with filesystem, shell, and web access.

Use these blocks to interact with the machine:

[write:/path/to/file]
content
[/write]

[read:/path/to/file]
[/read]

[append:/path/to/file]
content to add
[/append]

[edit:/path/to/file]
Find: text to find
Replace: replacement text
[/edit]

[ls:/path/to/dir]
[/ls]

[run]
command
[/run]

[python]
print("hello")
[/python]

[fetch:https://example.com]
[/fetch]

[browse]
search query
[/browse]

[sysinfo]
[/sysinfo]

How it works:
1. You output action block(s)
2. I execute them and show results
3. You respond naturally

Chain multiple blocks in one response. Paths can be absolute or relative to the project."""

SYSTEM_REMINDER = "\n(Use [write:], [read:], [run], [python], [browse], [fetch:], [ls:], [append:], [edit:] or [sysinfo] if needed.)"

MAX_READ_SIZE = 512 * 1024
MAX_OUTPUT = 50000


def parse_actions(text: str) -> list[dict]:
    actions = []
    for m in re.finditer(r'\[write:([^\]]+)\]\s*(.*?)\s*\[/write\]', text, re.DOTALL):
        actions.append({"type": "write", "path": m.group(1).strip(), "content": m.group(2)})
    for m in re.finditer(r'\[read:([^\]]+)\]\s*\[/read\]', text, re.DOTALL):
        actions.append({"type": "read", "path": m.group(1).strip()})
    for m in re.finditer(r'\[append:([^\]]+)\]\s*(.*?)\s*\[/append\]', text, re.DOTALL):
        actions.append({"type": "append", "path": m.group(1).strip(), "content": m.group(2)})
    for m in re.finditer(r'\[edit:([^\]]+)\]\s*Find:\s*(.*?)\s*Replace:\s*(.*?)\s*\[/edit\]', text, re.DOTALL):
        actions.append({"type": "edit", "path": m.group(1).strip(), "find": m.group(2).strip(), "replace": m.group(3).strip()})
    for m in re.finditer(r'\[ls:([^\]]+)\]\s*\[/ls\]', text, re.DOTALL):
        actions.append({"type": "ls", "path": m.group(1).strip()})
    for m in re.finditer(r'\[run\]\s*(.*?)\s*\[/run\]', text, re.DOTALL):
        actions.append({"type": "run", "command": m.group(1).strip()})
    for m in re.finditer(r'\[python\]\s*(.*?)\s*\[/python\]', text, re.DOTALL):
        actions.append({"type": "python", "code": m.group(1).strip()})
    for m in re.finditer(r'\[fetch:([^\]]+)\]\s*\[/fetch\]', text, re.DOTALL):
        actions.append({"type": "fetch", "url": m.group(1).strip()})
    for m in re.finditer(r'\[browse\]\s*(.*?)\s*\[/browse\]', text, re.DOTALL):
        actions.append({"type": "browse", "query": m.group(1).strip()})
    for m in re.finditer(r'\[sysinfo\]\s*\[/sysinfo\]', text, re.DOTALL):
        actions.append({"type": "sysinfo"})
    return actions


def _trim(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n... (truncated, {len(text)} total chars)"
    return text


def execute_action(action: dict) -> str:
    t = action["type"]

    if t == "write":
        path = action["path"]
        content = action["content"]
        try:
            p = Path(path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"Written {len(content)} bytes to {p}"
        except Exception as e:
            return f"Write error: {e}"

    if t == "read":
        path = Path(action["path"]).expanduser()
        if not path.exists():
            return f"File not found: {path}"
        if path.is_dir():
            return f"Is a directory: {path}"
        if path.stat().st_size > MAX_READ_SIZE:
            return f"File too large ({path.stat().st_size} bytes, max {MAX_READ_SIZE})"
        try:
            content = path.read_text(errors="replace")
            return _trim(content)
        except Exception as e:
            return f"Read error: {e}"

    if t == "append":
        path = Path(action["path"]).expanduser()
        content = action["content"]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(content + "\n")
            return f"Appended {len(content)} bytes to {path}"
        except Exception as e:
            return f"Append error: {e}"

    if t == "edit":
        path = Path(action["path"]).expanduser()
        find = action["find"]
        replace = action["replace"]
        if not path.exists():
            return f"File not found: {path}"
        try:
            content = path.read_text(errors="replace")
            if find not in content:
                return f"Text not found in {path}: {find[:100]}"
            count = content.count(find)
            new_content = content.replace(find, replace)
            path.write_text(new_content)
            return f"Replaced {count} occurrence(s) in {path}"
        except Exception as e:
            return f"Edit error: {e}"

    if t == "ls":
        path = Path(action["path"]).expanduser()
        if not path.exists():
            return f"Not found: {path}"
        if not path.is_dir():
            return f"Not a directory: {path}"
        try:
            entries = list(path.iterdir())
            dirs = sorted(e.name + "/" for e in entries if e.is_dir())
            files = sorted(e.name for e in entries if e.is_file())
            total = len(dirs) + len(files)
            lines = [f"{path} ({total} entries):"]
            for d in dirs:
                lines.append(f"  {d}")
            for f in files:
                size = (path / f).stat().st_size
                lines.append(f"  {f} ({size} bytes)")
            return "\n".join(lines)
        except Exception as e:
            return f"Ls error: {e}"

    if t == "run":
        cmd = action["command"]
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
            parts = []
            if r.stdout.strip():
                parts.append(r.stdout.strip())
            if r.stderr.strip():
                parts.append(f"stderr:\n{r.stderr.strip()}")
            parts.append(f"exit: {r.returncode}")
            return _trim("\n".join(parts))
        except subprocess.TimeoutExpired:
            return "Command timed out (120s)"
        except Exception as e:
            return f"Run error: {e}"

    if t == "python":
        code = action["code"]
        try:
            import io
            out = io.StringIO()
            err = io.StringIO()
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = out, err
            try:
                exec(code, {"__builtins__": __builtins__})
            finally:
                sys.stdout, sys.stderr = old_out, old_err
            parts = []
            if out.getvalue().strip():
                parts.append(out.getvalue().strip())
            if err.getvalue().strip():
                parts.append(f"stderr:\n{err.getvalue().strip()}")
            return _trim("\n".join(parts)) if parts else "(no output)"
        except Exception as e:
            return f"Python error: {e}"

    if t == "fetch":
        url = action["url"]
        import httpx
        try:
            r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)
            text = r.text[:60000]
            return _trim(f"HTTP {r.status_code} ({len(r.text)} bytes)\n\n{text}")
        except Exception as e:
            return f"Fetch error: {e}"

    if t == "browse":
        query = action["query"]
        if not query:
            return "No query."
        import httpx
        try:
            r = httpx.get("https://html.duckduckgo.com/html/", params={"q": query},
                          headers={"User-Agent": USER_AGENT}, timeout=15)
            results = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>', r.text)
            return "\n".join(f"{t}: {u}" for u, t in results[:8]) or "No results."
        except Exception as e:
            return f"Browse error: {e}"

    if t == "sysinfo":
        try:
            uname = platform.uname()
            total, used, free = shutil.disk_usage("/")
            info = [
                f"OS: {uname.system} {uname.release}",
                f"Host: {uname.node}",
                f"Arch: {uname.machine}",
                f"CPU: {os.cpu_count()} cores",
                f"Python: {sys.version}",
                f"CWD: {Path.cwd()}",
                f"Disk: {free // (2**30)} GB free / {total // (2**30)} GB total",
            ]
            return "\n".join(info)
        except Exception as e:
            return f"Sysinfo error: {e}"

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
            tag = a.get("path") or a.get("query") or a.get("type")
            results.append(f"[result for {a['type']}: {tag}]\n{result}")

        feedback = "\n\n".join(results) + "\n\nContinue your response based on these results."
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