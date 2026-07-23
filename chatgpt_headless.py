import json
import argparse
import sys
import os
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


OLD_AUTH_COOKIES = ["__Secure-next-auth.session-token"]
NEW_AUTH_COOKIES = ["unified_session_manifest"]
CLOUDFLARE_COOKIES = ["f_clearance", "cf_clearance"]

SAMESITE_MAP = {
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
}


def load_cookies(path):
    with open(path) as f:
        cookies = json.load(f)
    if not isinstance(cookies, list):
        raise ValueError("Cookies file must contain a JSON array of cookie objects")
    names = {c.get("name") for c in cookies}

    has_old = any(c in names for c in OLD_AUTH_COOKIES)
    has_new = any(c in names for c in NEW_AUTH_COOKIES)
    has_cf = any(c in names for c in CLOUDFLARE_COOKIES)

    if not has_old and not has_new:
        raise ValueError(
            f"No auth cookie found. Need one of {OLD_AUTH_COOKIES} "
            f"(old auth) or {NEW_AUTH_COOKIES} (new auth). "
            f"Got: {sorted(names)}"
        )

    if not has_cf:
        cf_hint = " (this may cause Cloudflare challenges)" if not has_cf else ""
        print(
            f"Warning: No Cloudflare clearance cookie found{cf_hint}",
            file=sys.stderr,
        )

    for c in cookies:
        if "sameSite" in c and isinstance(c["sameSite"], str):
            c["sameSite"] = SAMESITE_MAP.get(c["sameSite"].lower(), c["sameSite"])

    _expand_openai_domain(cookies)
    return cookies


def _expand_openai_domain(cookies):
    for c in cookies:
        domain = c.get("domain", "")
        if "openai.com" in domain and not domain.startswith("."):
            c["domain"] = "." + domain.lstrip(".")
            c["hostOnly"] = False


def find_cookie(cookies, name):
    for c in cookies:
        if c.get("name") == name:
            return c.get("value")
    return None


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


class ChatGPTHeadless:
    def __init__(self, cookies_path, headless=True, verbose=False):
        self.cookies_path = cookies_path
        self.headless = headless
        self.verbose = verbose
        self.cookies = load_cookies(cookies_path)
        self.playwright_cm = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.session_token = (
            find_cookie(self.cookies, "unified_session_manifest")
            or find_cookie(self.cookies, "__Secure-next-auth.session-token")
            or ""
        )

    def log(self, msg):
        if self.verbose:
            print(f"[chatgpt] {msg}", file=sys.stderr)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    def start(self):
        self.log("Launching browser...")
        self.playwright_cm = sync_playwright()
        self.playwright = self.playwright_cm.__enter__()
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
        )
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            ),
        )
        self.context.add_cookies(self.cookies)
        self.page = self.context.new_page()
        self.page.set_default_timeout(30000)
        self._navigate()
        self.log("Ready.")

    def _navigate(self):
        self.log("Navigating to chat.openai.com...")
        self.page.goto("https://chat.openai.com", wait_until="domcontentloaded")

        self.log(f"Current URL: {self.page.url}")

        title = self.page.title()
        cl = 0
        while "just a moment" in title.lower() and cl < 60:
            time.sleep(2)
            title = self.page.title()
            cl += 1
            self.log(f"Cloudflare challenge... ({cl * 2}s)")
        if cl > 0:
            self.log(f"Cloudflare resolved. Title: {title}")

        if "login" in self.page.url.lower():
            session_val = self.session_token or ""
            masked = session_val[:12] + "..." if len(session_val) > 15 else "empty"
            raise RuntimeError(
                f"Redirected to login page. Session token ({masked}) may be expired."
            )

        try:
            self.page.wait_for_selector(TEXTAREA_SELECTORS[0], timeout=15000)
            self.log("Textarea found, session is valid.")
        except PlaywrightTimeout:
            self.log("Trying fallback selectors for textarea...")
            found = False
            for sel in TEXTAREA_SELECTORS:
                try:
                    self.page.wait_for_selector(sel, timeout=5000)
                    self.log(f"Found textarea with selector: {sel}")
                    found = True
                    break
                except PlaywrightTimeout:
                    continue
            if not found:
                self.page.screenshot(path="/tmp/chatgpt_debug.png")
                self.log(f"Page title: {self.page.title()}")
                self.log(f"Page URL: {self.page.url}")
                raise RuntimeError(
                    "Could not find the prompt textarea. Session may be expired "
                    "or the UI changed. Saved debug screenshot to /tmp/chatgpt_debug.png."
                )

    def _find_textarea(self):
        for sel in TEXTAREA_SELECTORS:
            el = self.page.query_selector(sel)
            if el:
                return el
        raise RuntimeError("Could not locate the prompt textarea.")

    def _wait_for_completion(self, timeout=120000):
        self.log("Waiting for response to complete...")
        last_text = ""
        stable_checks = 0
        deadline = time.time() + timeout / 1000

        while time.time() < deadline:
            stop_visible = False
            for sel in STOP_BUTTON_SELECTORS:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    stop_visible = True
                    break

            if not stop_visible:
                messages = self.page.query_selector_all(MESSAGE_SELECTOR)
                if messages:
                    current = messages[-1].inner_text()
                    if current and current != last_text:
                        if current == last_text:
                            stable_checks += 1
                            if stable_checks >= 3:
                                return current
                        last_text = current
                    else:
                        stable_checks += 1
                        if stable_checks >= 5:
                            return current if current else last_text
                else:
                    time.sleep(0.5)
                    continue
            else:
                stable_checks = 0

            time.sleep(0.5)

        self.log("Response timed out, returning partial text.")
        messages = self.page.query_selector_all(MESSAGE_SELECTOR)
        if messages:
            return messages[-1].inner_text()
        return ""

    def _wait_for_streaming_complete(self, timeout=120000):
        return self._wait_for_completion(timeout)

    def ask(self, prompt, timeout=120000):
        self.page.wait_for_load_state("networkidle")

        textarea = self._find_textarea()
        textarea.click()
        textarea.fill("")
        self.page.keyboard.type(prompt, delay=10)
        time.sleep(0.3)

        send_btn = None
        for sel in SEND_BUTTON_SELECTORS:
            btn = self.page.query_selector(sel)
            if btn and btn.is_visible():
                send_btn = btn
                break

        if send_btn:
            send_btn.click()
        else:
            self.page.keyboard.press("Enter")

        return self._wait_for_streaming_complete(timeout)

    def close(self):
        if self.context:
            storage = self.context.storage_state()
            storage_path = self.cookies_path + ".updated"
            with open(storage_path, "w") as f:
                json.dump(storage.get("cookies", []), f, indent=2)
            self.log(f"Updated cookies saved to {storage_path}")
        if self.browser:
            self.browser.close()
        if self.playwright_cm:
            self.playwright_cm.__exit__(None, None, None)


def main():
    parser = argparse.ArgumentParser(
        description="ChatGPT headless client using browser session cookies"
    )
    parser.add_argument(
        "--cookies",
        required=True,
        help="Path to cookies JSON file exported from browser",
    )
    parser.add_argument("--prompt", required=True, help="Message to send to ChatGPT")
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Max seconds to wait for response (default: 120)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in visible (headed) mode for debugging",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Print debug logs to stderr"
    )
    args = parser.parse_args()

    if not os.path.isfile(args.cookies):
        print(f"Error: cookies file not found: {args.cookies}", file=sys.stderr)
        sys.exit(1)

    try:
        client = ChatGPTHeadless(
            cookies_path=args.cookies,
            headless=not args.headed,
            verbose=args.verbose,
        )
        with client:
            response = client.ask(args.prompt, timeout=args.timeout * 1000)
            print(response)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
