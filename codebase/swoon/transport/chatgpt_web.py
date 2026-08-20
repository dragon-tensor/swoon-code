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


SAMESITE_MAP = {
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
    "no_restriction": "None",
}
SAMESITE_OMIT = frozenset({"unspecified"})
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
    "textarea[placeholder*='Ask' i]",
    "textarea",
    "[contenteditable='true']",
    "[contenteditable='plaintext-only']",
    "[role='textbox']",
)
CLOUDFLARE_CHALLENGE_SELECTORS = (
    "iframe[src*='challenges.cloudflare.com']",
    "[name='cf-turnstile-response']",
    ".cf-turnstile",
)
LOGGED_OUT_SELECTORS = (
    "button[data-testid='login-button']",
    "a[data-testid='login-button']",
    "a[href*='/auth/login']",
    "button:has-text('Log in')",
    "a:has-text('Log in')",
    "button:has-text('Sign up')",
    "a:has-text('Sign up')",
)
SEND_BUTTON_SELECTORS = (
    "button[data-testid='send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label*='send' i]",
)
STOP_BUTTON_SELECTORS = (
    "button[data-testid='stop-button']",
    "button[data-testid*='stop' i]",
    "button[aria-label='Stop generating']",
    "button[aria-label='Stop streaming']",
    "button[aria-label*='stop' i]",
    "button:has-text('Stop generating')",
)
MESSAGE_SELECTOR = "[data-message-author-role='assistant']"
POLL_INTERVAL_SECONDS = 0.5
DEFAULT_RESPONSE_SETTLE_SECONDS = 5.0

class ChatGPTWebTransport:
    """Send text to ChatGPT through an authenticated browser session."""

    def __init__(
        self,
        cookie_path: str | Path,
        verbose: bool = False,
        *,
        headless: bool = True,
        response_timeout: float = 180,
        response_timeout_retries: int = 0,
        response_settle_time: float = DEFAULT_RESPONSE_SETTLE_SECONDS,
        storage_state_path: str | Path | None = None,
        debug_directory: str | Path | None = None,
    ) -> None:
        self.cookie_path = Path(cookie_path).expanduser()
        self.verbose = verbose
        self.headless = headless
        if response_timeout <= 0:
            raise ValueError("Response timeout must be positive")
        if response_settle_time <= 0:
            raise ValueError("Response settle time must be positive")
        if response_settle_time >= response_timeout:
            raise ValueError("Response settle time must be shorter than the response timeout")
        if type(response_timeout_retries) is not int or not 0 <= response_timeout_retries <= 10:
            raise ValueError("Response timeout retries must be an integer from 0 to 10")
        self.response_timeout = response_timeout
        self.response_timeout_retries = response_timeout_retries
        self.response_settle_time = response_settle_time
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

        if self._is_hotcleaner_encrypted_backup(raw):
            raise ValueError(
                "Encrypted Hotcleaner Cookie Editor backups are not supported; export an "
                "unencrypted JSON cookie list while viewing https://chatgpt.com/"
            )
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
                normalized_same_site = (
                    same_site.strip().casefold().replace("-", "_").replace(" ", "_")
                )
                if normalized_same_site in SAMESITE_OMIT:
                    cookie.pop("sameSite", None)
                else:
                    cookie["sameSite"] = SAMESITE_MAP.get(
                        normalized_same_site,
                        same_site,
                    )
                if (
                    "sameSite" in cookie
                    and cookie["sameSite"] not in SAMESITE_MAP.values()
                ):
                    raise ValueError(f"Cookie {name!r} has an invalid sameSite value")
            elif same_site is not None:
                raise ValueError(f"Cookie {name!r} has an invalid sameSite value")
            if "path" not in cookie and domain is not None:
                cookie["path"] = "/"
            cookies.append(cookie)

        if not any(self._cookie_targets_chatgpt(cookie) for cookie in cookies):
            raise ValueError(
                "Cookie file has no chatgpt.com cookie; export cookies while signed in at "
                "https://chatgpt.com/, not from auth.openai.com"
            )

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
    def _is_hotcleaner_encrypted_backup(raw: Any) -> bool:
        if not isinstance(raw, dict):
            return False
        url = raw.get("url")
        data = raw.get("data")
        if not isinstance(url, str) or not isinstance(data, str) or not data:
            return False
        hostname = urlsplit(url).hostname
        if not hostname:
            return False
        normalized = hostname.casefold().rstrip(".")
        return normalized == "hotcleaner.com" or normalized.endswith(".hotcleaner.com")

    @staticmethod
    def _allowed_domain(value: str) -> bool:
        domain = value.strip().casefold().lstrip(".").rstrip(".")
        return any(
            domain == allowed or domain.endswith("." + allowed)
            for allowed in ALLOWED_COOKIE_DOMAINS
        )

    @staticmethod
    def _cookie_targets_chatgpt(cookie: dict[str, Any]) -> bool:
        domain = cookie.get("domain")
        if isinstance(domain, str):
            normalized = domain.strip().casefold().lstrip(".").rstrip(".")
            if normalized == "chatgpt.com" or normalized.endswith(".chatgpt.com"):
                return True
        url = cookie.get("url")
        if isinstance(url, str):
            hostname = urlsplit(url).hostname
            if hostname:
                normalized = hostname.casefold().rstrip(".")
                return normalized == "chatgpt.com" or normalized.endswith(".chatgpt.com")
        return False

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
        self.log(f"Page: {self.page.url}")
        self._dismiss_modals()
        if "login" in self.page.url.lower():
            raise RuntimeError(
                "Redirected to login. Export fresh cookies while signed in at "
                f"https://chatgpt.com/.{self._startup_debug_note()}"
            )
        if self._is_logged_out():
            raise RuntimeError(
                "ChatGPT session is not authenticated. Export fresh cookies while signed in at "
                f"https://chatgpt.com/.{self._startup_debug_note()}"
            )

        if self._cloudflare_challenge_visible():
            self._wait_for_human_verification()

        startup_selector = ", ".join(
            (*TEXTAREA_SELECTORS, *CLOUDFLARE_CHALLENGE_SELECTORS)
        )
        try:
            self.page.wait_for_selector(
                startup_selector,
                timeout=min(int(self.response_timeout * 1_000), 60_000),
            )
        except Exception:
            pass

        if self._visible_composer() is not None:
            self.log("Chat input found")
            return
        if self._cloudflare_challenge_visible():
            self._wait_for_human_verification()
            if self._visible_composer() is not None:
                self.log("Chat input found after human verification")
                return
        if self._is_logged_out():
            raise RuntimeError(
                "ChatGPT session is not authenticated. Run `swoon auth` with an authorized "
                f"account and try again.{self._startup_debug_note()}"
            )

        raise RuntimeError(
            "ChatGPT message composer was not found; the page may have changed or still be "
            f"loading.{self._startup_debug_note()}"
        )

    def _visible_composer(self) -> Any | None:
        if self.page is None:
            return None
        for selector in TEXTAREA_SELECTORS:
            try:
                element = self.page.query_selector(selector)
                if element and element.is_visible():
                    return element
            except Exception:
                continue
        return None

    def _cloudflare_challenge_visible(self) -> bool:
        if self.page is None:
            return False
        if "__cf_chl_" in self.page.url:
            return True
        for selector in CLOUDFLARE_CHALLENGE_SELECTORS:
            try:
                element = self.page.query_selector(selector)
                if element and element.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _wait_for_human_verification(self) -> None:
        if self.page is None:
            raise RuntimeError("Transport has not been started")
        if self.headless:
            raise RuntimeError(
                "Cloudflare requires human verification, which cannot be completed in headless "
                "mode. Run `swoon auth`, complete the verification in the opened browser, then "
                f"run the agent again.{self._startup_debug_note()}"
            )
        print(
            "[chatgpt] Complete the human-verification check in the open browser window.",
            file=sys.stderr,
            flush=True,
        )
        self.log("Cloudflare verification is visible; waiting for the human in the browser")
        try:
            self.page.wait_for_selector(
                ", ".join(TEXTAREA_SELECTORS),
                timeout=int(self.response_timeout * 1_000),
            )
        except Exception as error:
            raise RuntimeError(
                "Human verification was not completed before the startup timeout."
                f"{self._startup_debug_note()}"
            ) from error

    def _is_logged_out(self) -> bool:
        if self.page is None:
            return False
        for selector in LOGGED_OUT_SELECTORS:
            try:
                element = self.page.query_selector(selector)
                if element and element.is_visible():
                    self.log(f"Logged-out control detected: {selector}")
                    return True
            except Exception:
                continue
        return False

    def _startup_debug_note(self) -> str:
        if self.debug_directory is None:
            return ""
        try:
            screenshot = self._capture_debug_screenshot()
            return f" Debug screenshot: {screenshot}."
        except Exception as error:
            return f" Debug capture failed ({error.__class__.__name__})."

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

        self._wait_until_ready_to_send(timeout=self.response_timeout)
        self._dismiss_modals()
        old_messages = self.page.query_selector_all(MESSAGE_SELECTOR)
        previous_count = len(old_messages)
        previous_text = self._message_text(old_messages[-1]) if old_messages else ""

        prompt = self._visible_composer()
        if prompt is None:
            raise RuntimeError("Chat input not found")

        # ``keyboard.type`` turns embedded newlines into Enter key events on ChatGPT's
        # contenteditable composer, which can submit one broken prompt per line. Playwright's
        # fill operation sets the complete multiline value through one input event instead.
        prompt.fill(text)
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
            timeout_retries=getattr(self, "response_timeout_retries", 0),
        )

    @staticmethod
    def _message_text(message: Any) -> str:
        """Prefer exact code-node text over layout-normalized rendered text."""

        try:
            code_blocks = message.query_selector_all("pre code")
        except Exception:
            code_blocks = []
        if len(code_blocks) == 1:
            try:
                exact = code_blocks[0].text_content()
            except Exception:
                exact = None
            if isinstance(exact, str) and exact.strip().startswith("<aeml"):
                return exact.strip()
        return message.inner_text()

    def _generation_is_active(self) -> bool:
        if self.page is None:
            return False
        for selector in STOP_BUTTON_SELECTORS:
            try:
                button = self.page.query_selector(selector)
                if button and button.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _wait_until_ready_to_send(self, *, timeout: float) -> None:
        """Never submit a prompt while the page says a response is still streaming."""

        if not self._generation_is_active():
            return
        self.log("A response is still generating; waiting before sending the next prompt")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._generation_is_active():
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            "ChatGPT was still generating the previous response; no new prompt was sent"
        )

    def _wait_for_new_response(
        self,
        *,
        previous_count: int,
        previous_text: str,
        timeout: float,
        timeout_retries: int = 0,
    ) -> str:
        last = ""
        last_change_at: float | None = None
        deadline = time.monotonic() + timeout

        while True:
            now = time.monotonic()
            if now >= deadline:
                if timeout_retries <= 0:
                    break
                timeout_retries -= 1
                self.log(
                    "Assistant is still responding; extending the wait without sending "
                    f"another prompt ({timeout_retries} extension(s) remain)"
                )
                deadline = now + timeout
                continue
            self._dismiss_modals()
            generation_active = self._generation_is_active()
            messages = self.page.query_selector_all(MESSAGE_SELECTOR)
            if messages:
                current = self._message_text(messages[-1])
                is_new_response = len(messages) > previous_count or current != previous_text
                if is_new_response and current:
                    if current != last:
                        last = current
                        last_change_at = now
                    elif (
                        not generation_active
                        and last_change_at is not None
                        and now - last_change_at >= self.response_settle_time
                    ):
                        self.log(
                            "Assistant response finished and remained stable for "
                            f"{self.response_settle_time:g} seconds"
                        )
                        return current
            time.sleep(POLL_INTERVAL_SECONDS)

        self.log("Timed out before the assistant response finished")
        raise TimeoutError(
            "ChatGPT did not finish a new assistant response before the timeout; "
            "no follow-up prompt was sent"
        )

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
