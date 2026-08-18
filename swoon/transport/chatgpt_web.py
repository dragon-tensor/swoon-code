"""Synchronous Playwright transport for the ChatGPT web application.

This module deliberately knows nothing about AEML. It sends text and returns
the next assistant message; protocol parsing belongs to :mod:`swoon.aeml`.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SAMESITE_MAP = {"lax": "Lax", "strict": "Strict", "none": "None"}
CHATGPT_URL = "https://chatgpt.com/"
MAX_COOKIE_FILE_BYTES = 2 * 1024 * 1024
ALLOWED_COOKIE_DOMAINS = ("openai.com", "chatgpt.com")
_COOKIE_FIELDS = frozenset(
    {
        "name",
        "value",
        "url",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
        "partitionKey",
    }
)

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

class ChatGPTWebTransport:
    """Send text to ChatGPT through an authenticated browser session."""

    def __init__(
        self,
        cookie_path: str | Path,
        verbose: bool = False,
        *,
        headless: bool = True,
        response_timeout: float = 180,
        storage_state_path: str | Path | None = None,
        debug_directory: str | Path | None = None,
    ) -> None:
        self.cookie_path = Path(cookie_path).expanduser()
        self.verbose = verbose
        self.headless = headless
        self.response_timeout = response_timeout
        self.storage_state_path = (
            Path(storage_state_path).expanduser() if storage_state_path is not None else None
        )
        self.debug_directory = (
            Path(debug_directory).expanduser() if debug_directory is not None else None
        )
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
        raw = self._read_private_json(self.cookie_path)

        if isinstance(raw, dict):
            raw = raw.get("cookies")
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ValueError("Cookie file must be a cookie list or Playwright storage state")
        if not raw:
            raise ValueError("Cookie file contains no cookies")

        cookies: list[dict[str, Any]] = []
        for original in raw:
            cookie = {key: value for key, value in original.items() if key in _COOKIE_FIELDS}
            if "expires" not in cookie and "expirationDate" in original:
                cookie["expires"] = original["expirationDate"]
            name = cookie.get("name")
            value = cookie.get("value")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Every cookie requires a non-empty name")
            if not isinstance(value, str) or not value:
                raise ValueError(f"Cookie {name!r} requires a non-empty value")
            if self._looks_like_placeholder(name) or self._looks_like_placeholder(value):
                raise ValueError("Cookie file still contains example placeholder values")

            domain = cookie.get("domain")
            url = cookie.get("url")
            if domain is None and url is None:
                raise ValueError(f"Cookie {name!r} requires a domain or URL")
            if domain is not None:
                if not isinstance(domain, str) or not self._allowed_domain(domain):
                    raise ValueError(f"Cookie {name!r} is outside an approved service domain")
            if url is not None:
                if not isinstance(url, str):
                    raise ValueError(f"Cookie {name!r} has an invalid URL")
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or not self._allowed_domain(
                    parsed.hostname
                ):
                    raise ValueError(f"Cookie {name!r} has an unapproved URL")

            same_site = cookie.get("sameSite")
            if isinstance(same_site, str):
                cookie["sameSite"] = SAMESITE_MAP.get(same_site.lower(), same_site)
                if cookie["sameSite"] not in SAMESITE_MAP.values():
                    raise ValueError(f"Cookie {name!r} has an invalid sameSite value")
            elif same_site is not None:
                raise ValueError(f"Cookie {name!r} has an invalid sameSite value")
            if "path" not in cookie and domain is not None:
                cookie["path"] = "/"
            cookies.append(cookie)

        self.log(f"Loaded {len(cookies)} cookies")
        return cookies

    @staticmethod
    def _looks_like_placeholder(value: str) -> bool:
        folded = value.strip().casefold()
        return (
            folded.startswith("paste_your_")
            or folded.startswith("replace_")
            or folded in {"changeme", "example", "placeholder"}
        )

    @staticmethod
    def _allowed_domain(value: str) -> bool:
        domain = value.strip().casefold().lstrip(".").rstrip(".")
        return any(
            domain == allowed or domain.endswith("." + allowed)
            for allowed in ALLOWED_COOKIE_DOMAINS
        )

    @staticmethod
    def _read_private_json(path: Path) -> Any:
        try:
            item = path.lstat()
        except OSError as error:
            raise ValueError("Cookie file is missing or unreadable") from error
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
            raise ValueError("Cookie path must be a regular file, not a symbolic link")
        if os.name == "posix" and stat.S_IMODE(item.st_mode) & 0o077:
            raise ValueError("Cookie file must be owner-only; run chmod 600 on it")
        if item.st_size > MAX_COOKIE_FILE_BYTES:
            raise ValueError("Cookie file exceeds the 2 MiB safety limit")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise ValueError("Cookie file is missing or unreadable") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (
                (opened.st_dev, opened.st_ino) != (item.st_dev, item.st_ino)
            ):
                raise ValueError("Cookie file changed while it was being opened")
            with os.fdopen(descriptor, "rb", closefd=False) as cookie_file:
                payload = cookie_file.read(MAX_COOKIE_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(payload) > MAX_COOKIE_FILE_BYTES:
            raise ValueError("Cookie file exceeds the 2 MiB safety limit")
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Cookie file must contain valid UTF-8 JSON") from error

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright_cm = sync_playwright()
        self.playwright = self._playwright_cm.__enter__()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        self.context.add_cookies(self.raw_cookies)
        self.page = self.context.new_page()
        self.page.set_default_timeout(60_000)

        self.log("Navigating to chatgpt.com...")
        self.page.goto(CHATGPT_URL, wait_until="domcontentloaded")
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

        debug_note = ""
        if self.debug_directory is not None:
            try:
                screenshot = self._capture_debug_screenshot()
                debug_note = f" Debug screenshot: {screenshot}."
            except Exception as error:
                debug_note = f" Debug capture failed ({error.__class__.__name__})."
        raise RuntimeError(f"Chat input not found. Session expired?{debug_note}")

    def _capture_debug_screenshot(self) -> Path:
        if self.page is None or self.debug_directory is None:
            raise RuntimeError("Debug screenshot capture is not enabled")
        directory = self.debug_directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        item = directory.lstat()
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
            raise ValueError("Debug artifact path must be a regular directory")
        if os.name == "posix" and stat.S_IMODE(item.st_mode) & 0o077:
            raise ValueError("Debug artifact directory must be owner-only")
        descriptor, raw_path = tempfile.mkstemp(
            prefix="swoon-chatgpt-",
            suffix=".png",
            dir=directory,
        )
        os.close(descriptor)
        screenshot = Path(raw_path)
        try:
            screenshot.chmod(0o600)
            self.page.screenshot(path=str(screenshot))
            screenshot.chmod(0o600)
        except Exception:
            screenshot.unlink(missing_ok=True)
            raise
        return screenshot

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
        errors: list[str] = []
        if self.context is not None and self.storage_state_path is not None:
            try:
                state = self.context.storage_state()
                self._write_private_json(self.storage_state_path, state)
            except Exception as error:
                errors.append(f"storage-state save failed ({error.__class__.__name__})")
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception as error:
                errors.append(f"browser close failed ({error.__class__.__name__})")
        if self._playwright_cm is not None:
            try:
                self._playwright_cm.__exit__(None, None, None)
            except Exception as error:
                errors.append(f"Playwright close failed ({error.__class__.__name__})")

        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        self._playwright_cm = None
        if errors:
            raise RuntimeError("; ".join(errors))

    @staticmethod
    def _write_private_json(path: Path, payload: Any) -> None:
        parent = path.parent
        try:
            parent_item = parent.lstat()
        except OSError as error:
            raise ValueError("Storage-state parent directory does not exist") from error
        if stat.S_ISLNK(parent_item.st_mode) or not stat.S_ISDIR(parent_item.st_mode):
            raise ValueError("Storage-state parent must be a regular directory")
        if os.name == "posix" and stat.S_IMODE(parent_item.st_mode) & 0o077:
            raise ValueError("Storage-state directory must be owner-only")
        if path.exists() and path.is_symlink():
            raise ValueError("Storage-state destination cannot be a symbolic link")

        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            path.chmod(0o600)
            if hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
