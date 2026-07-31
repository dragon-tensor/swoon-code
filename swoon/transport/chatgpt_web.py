"""Synchronous Playwright transport for the ChatGPT web application.

This module deliberately knows nothing about AEML. It sends text and returns
the next assistant message; protocol parsing belongs to :mod:`swoon.aeml`.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


SAMESITE_MAP = {"lax": "Lax", "strict": "Strict", "none": "None"}

TEXTAREA_SELECTORS = (
    "#prompt-textarea",
    "textarea[placeholder*='message' i]",
    "textarea[placeholder*='Send' i]",
    "[contenteditable='true']",
)
SEND_BUTTON_SELECTORS = (
    "button[data-testid='send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label*='send' i]",
)
STOP_BUTTON_SELECTORS = (
    "button[data-testid='stop-button']",
    "button[aria-label='Stop generating']",
    "button[aria-label='Stop streaming']",
)
MESSAGE_SELECTOR = "[data-message-author-role='assistant']"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


class ChatGPTWebTransport:
    """Send text to ChatGPT through an authenticated browser session."""

    def __init__(
        self,
        cookie_path: str | Path,
        verbose: bool = False,
        *,
        headless: bool = True,
        response_timeout: float = 180,
    ) -> None:
        self.cookie_path = Path(cookie_path)
        self.verbose = verbose
        self.headless = headless
        self.response_timeout = response_timeout
        self._playwright_cm: Any = None
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.raw_cookies = self._load_cookies()

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[chatgpt] {message}", file=sys.stderr)

    def _load_cookies(self) -> list[dict[str, Any]]:
        with self.cookie_path.open(encoding="utf-8") as cookie_file:
            raw: Any = json.load(cookie_file)

        if isinstance(raw, dict):
            raw = raw.get("cookies")
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ValueError("Cookie file must be a cookie list or Playwright storage state")

        cookies: list[dict[str, Any]] = []
        for original in raw:
            cookie = dict(original)
            same_site = cookie.get("sameSite")
            if isinstance(same_site, str):
                cookie["sameSite"] = SAMESITE_MAP.get(same_site.lower(), same_site)
            domain = cookie.get("domain")
            if isinstance(domain, str) and "openai.com" in domain and not domain.startswith("."):
                cookie["domain"] = "." + domain.lstrip(".")
                cookie["hostOnly"] = False
            cookies.append(cookie)

        self.log(f"Loaded {len(cookies)} cookies")
        return cookies

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright_cm = sync_playwright()
        self.playwright = self._playwright_cm.__enter__()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
        )
        self.context.add_cookies(self.raw_cookies)
        self.page = self.context.new_page()
        self.page.set_default_timeout(60_000)

        self.log("Navigating to chat.openai.com...")
        self.page.goto("https://chat.openai.com", wait_until="domcontentloaded")
        for _ in range(60):
            if "just a moment" not in self.page.title().lower():
                break
            time.sleep(2)

        self.log(f"Page: {self.page.url}")
        self._dismiss_modals()
        if "login" in self.page.url.lower():
            raise RuntimeError("Redirected to login. Refresh cookies from chatgpt.com.")

        for selector in TEXTAREA_SELECTORS:
            try:
                self.page.wait_for_selector(selector, timeout=5_000)
                self.log("Chat input found")
                return
            except Exception:
                continue

        self.page.screenshot(path="/tmp/chatgpt_debug.png")
        raise RuntimeError("Chat input not found. Session expired?")

    def _dismiss_modals(self) -> bool:
        if self.page is None:
            return False
        for modal_id in ("modal-no-auth-soft-rate-limit-inline-auth", "modal-no-auth-login"):
            modal = self.page.query_selector(f"#{modal_id}")
            if modal and modal.is_visible():
                self.log(f"Modal detected: {modal_id}")
                button = modal.query_selector("button, a")
                if button and button.is_visible():
                    self.log("Dismissing modal...")
                    button.click()
                    self.page.wait_for_timeout(2_000)
                    return True
                self.log("Could not dismiss modal")
                return False
        return False

    def send(self, text: str) -> str:
        if self.page is None:
            raise RuntimeError("Transport has not been started")

        self._dismiss_modals()
        old_messages = self.page.query_selector_all(MESSAGE_SELECTOR)
        previous_count = len(old_messages)
        previous_text = old_messages[-1].inner_text() if old_messages else ""

        prompt = None
        for selector in TEXTAREA_SELECTORS:
            element = self.page.query_selector(selector)
            if element and element.is_visible():
                prompt = element
                break
        if prompt is None:
            raise RuntimeError("Chat input not found")

        prompt.click()
        prompt.fill("")
        self.page.keyboard.type(text, delay=5)
        time.sleep(0.3)

        button = None
        for selector in SEND_BUTTON_SELECTORS:
            candidate = self.page.query_selector(selector)
            if candidate and candidate.is_visible():
                button = candidate
                break
        if button is not None:
            button.click()
        else:
            self.page.keyboard.press("Enter")

        return self._wait_for_new_response(
            previous_count=previous_count,
            previous_text=previous_text,
            timeout=self.response_timeout,
        )

    def _wait_for_new_response(
        self,
        *,
        previous_count: int,
        previous_text: str,
        timeout: float,
    ) -> str:
        last = ""
        stable_reads = 0
        saw_new_response = False
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            self._dismiss_modals()
            stop_visible = any(
                button.is_visible()
                for selector in STOP_BUTTON_SELECTORS
                if (button := self.page.query_selector(selector))
            )
            messages = self.page.query_selector_all(MESSAGE_SELECTOR)
            if messages:
                current = messages[-1].inner_text()
                saw_new_response = len(messages) > previous_count or current != previous_text
                if saw_new_response and current and not stop_visible:
                    if current == last:
                        stable_reads += 1
                        if stable_reads >= 3:
                            return current
                    else:
                        last = current
                        stable_reads = 0
                else:
                    stable_reads = 0
            time.sleep(0.5)

        self.log("Timed out waiting for a new assistant response")
        messages = self.page.query_selector_all(MESSAGE_SELECTOR)
        if messages:
            current = messages[-1].inner_text()
            if len(messages) > previous_count or current != previous_text:
                return current
        raise TimeoutError("No new assistant response arrived before the timeout")

    def close(self) -> None:
        if self.context is not None:
            try:
                self.context.storage_state(path=str(self.cookie_path) + ".state")
            except Exception:
                pass
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
        if self._playwright_cm is not None:
            try:
                self._playwright_cm.__exit__(None, None, None)
            except Exception:
                pass

        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        self._playwright_cm = None
