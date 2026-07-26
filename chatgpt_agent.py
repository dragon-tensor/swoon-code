import json
import sys
import time

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


class ChatGPT:
    def __init__(self, cookie_path: str, verbose: bool = False):
        self.cookie_path = cookie_path
        self.verbose = verbose
        self._playwright_cm = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._load_cookies()

    def log(self, msg: str):
        if self.verbose:
            print(f"[chatgpt] {msg}", file=sys.stderr)

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
        self._dismiss_modals()

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

    def _dismiss_modals(self) -> bool:
        for mid in ["modal-no-auth-soft-rate-limit-inline-auth", "modal-no-auth-login"]:
            modal = self.page.query_selector(f"#{mid}")
            if modal and modal.is_visible():
                self.log(f"Modal detected: {mid}")
                btn = modal.query_selector("button, a")
                if btn and btn.is_visible():
                    self.log("Dismissing modal...")
                    btn.click()
                    self.page.wait_for_timeout(2000)
                    return True
                self.log("Could not dismiss modal")
                return False
        return False

    def send(self, text: str) -> str:
        self._dismiss_modals()
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
            self._dismiss_modals()
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
    parser = argparse.ArgumentParser(description="ChatGPT terminal chatbot")
    parser.add_argument("--cookies", required=True)
    parser.add_argument("--prompt", "-p")
    parser.add_argument("--interactive", "-i", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if not args.prompt and not args.interactive:
        parser.print_help()
        sys.exit(1)

    client = ChatGPT(args.cookies, verbose=args.verbose)
    try:
        client.start()

        if args.interactive:
            print("ChatGPT — type messages, /quit to exit")
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
                resp = client.send(msg)
                print(resp)
                print()
        else:
            resp = client.send(args.prompt)
            print(resp)
    finally:
        client.close()


if __name__ == "__main__":
    main()
