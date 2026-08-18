from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from chatgpt_agent import ChatGPT
from swoon.transport import ChatGPTWebTransport


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self) -> str:
        return self.text


class FakePage:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self.messages = messages

    def query_selector(self, selector: str):
        return None

    def query_selector_all(self, selector: str):
        return self.messages

    def screenshot(self, *, path: str) -> None:
        Path(path).write_bytes(b"png")


class FakeContext:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def storage_state(self) -> dict[str, object]:
        return self.state


class FakeBrowser:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if self.error is not None:
            raise self.error


class FakeStartPage(FakePage):
    def __init__(self, *, selector_found: bool = True) -> None:
        super().__init__([])
        self.selector_found = selector_found
        self.url = "https://chatgpt.com/"
        self.goto_calls: list[tuple[str, str]] = []
        self.waited: list[str] = []
        self.default_timeout: int | None = None
        self.screenshot_calls: list[str] = []

    def set_default_timeout(self, timeout: int) -> None:
        self.default_timeout = timeout

    def goto(self, url: str, *, wait_until: str) -> None:
        self.goto_calls.append((url, wait_until))

    def title(self) -> str:
        return "ChatGPT"

    def wait_for_selector(self, selector: str, *, timeout: int):
        self.waited.append(selector)
        if self.selector_found:
            return object()
        raise TimeoutError("missing")

    def screenshot(self, *, path: str) -> None:
        self.screenshot_calls.append(path)
        super().screenshot(path=path)


class FakeBrowserContext:
    def __init__(self, page: FakeStartPage) -> None:
        self.page = page
        self.cookies: list[dict[str, object]] = []

    def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self.cookies = cookies

    def new_page(self) -> FakeStartPage:
        return self.page


class FakeLaunchedBrowser(FakeBrowser):
    def __init__(self, page: FakeStartPage) -> None:
        super().__init__()
        self.context = FakeBrowserContext(page)
        self.context_options: dict[str, object] | None = None

    def new_context(self, **options):
        self.context_options = options
        return self.context


class FakeChromium:
    def __init__(self, browser: FakeLaunchedBrowser) -> None:
        self.browser = browser
        self.headless: bool | None = None

    def launch(self, *, headless: bool) -> FakeLaunchedBrowser:
        self.headless = headless
        return self.browser


class FakePlaywrightManager:
    def __init__(self, chromium: FakeChromium) -> None:
        self.playwright = types.SimpleNamespace(chromium=chromium)
        self.exited = False

    def __enter__(self):
        return self.playwright

    def __exit__(self, *_args) -> None:
        self.exited = True


class ChatGPTWebTransportTests(unittest.TestCase):
    def make_transport(self, page: FakePage) -> ChatGPTWebTransport:
        transport = object.__new__(ChatGPTWebTransport)
        transport.page = page
        transport.verbose = False
        return transport

    def test_compatibility_alias_is_preserved(self) -> None:
        self.assertIs(ChatGPT, ChatGPTWebTransport)

    def test_wait_never_returns_the_previous_response(self) -> None:
        transport = self.make_transport(FakePage([FakeMessage("previous answer")]))
        with (
            patch(
                "swoon.transport.chatgpt_web.time.monotonic",
                side_effect=[0.0, 0.1, 0.2, 0.3],
            ),
            patch("swoon.transport.chatgpt_web.time.sleep"),
        ):
            with self.assertRaises(TimeoutError):
                transport._wait_for_new_response(
                    previous_count=1,
                    previous_text="previous answer",
                    timeout=0.25,
                )

    def test_wait_returns_a_stable_new_response(self) -> None:
        page = FakePage([FakeMessage("old"), FakeMessage("new")])
        transport = self.make_transport(page)
        with (
            patch(
                "swoon.transport.chatgpt_web.time.monotonic",
                side_effect=[0.0, 0.1, 0.2, 0.3, 0.4],
            ),
            patch("swoon.transport.chatgpt_web.time.sleep"),
        ):
            result = transport._wait_for_new_response(
                previous_count=1,
                previous_text="old",
                timeout=5,
            )
        self.assertEqual(result, "new")

    def test_cookie_loader_accepts_playwright_storage_state(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "redacted",
                    "domain": "auth.openai.com",
                    "path": "/",
                    "sameSite": "lax",
                }
            ],
            "origins": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "state.json"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)
            transport = ChatGPTWebTransport(cookie_path)

        self.assertEqual(transport.raw_cookies[0]["sameSite"], "Lax")
        self.assertEqual(transport.raw_cookies[0]["domain"], "auth.openai.com")
        self.assertEqual(transport.raw_cookies[0]["path"], "/")

    def test_cookie_loader_rejects_broad_permissions_symlinks_and_placeholders(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX permission semantics are required")
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cookie_path = root / "cookies.json"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "chmod 600"):
                ChatGPTWebTransport(cookie_path)

            cookie_path.chmod(0o600)
            link = root / "link.json"
            link.symlink_to(cookie_path)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                ChatGPTWebTransport(link)

            state["cookies"][0]["value"] = "PASTE_YOUR_VALUE_HERE"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "placeholder"):
                ChatGPTWebTransport(cookie_path)

    def test_cookie_loader_rejects_lookalike_domains_and_discards_extension_fields(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": "evilopenai.com",
                    "path": "/",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.json"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "approved service domain"):
                ChatGPTWebTransport(cookie_path)

            state["cookies"][0].update(
                domain="chatgpt.com",
                hostOnly=True,
                storeId="0",
                expirationDate=2_000_000_000,
            )
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)
            transport = ChatGPTWebTransport(cookie_path)

        self.assertEqual(transport.raw_cookies[0]["expires"], 2_000_000_000)
        self.assertNotIn("hostOnly", transport.raw_cookies[0])
        self.assertNotIn("storeId", transport.raw_cookies[0])

    def test_close_saves_opt_in_state_atomically_with_private_permissions(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX permission semantics are required")
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            cookie_path = root / "cookies.json"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)
            saved = root / "refreshed.json"
            transport = ChatGPTWebTransport(cookie_path, storage_state_path=saved)
            transport.context = FakeContext({"cookies": [], "origins": []})
            browser = FakeBrowser()
            transport.browser = browser

            transport.close()

            self.assertTrue(browser.closed)
            self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["cookies"], [])
            self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
            self.assertFalse(any(root.glob(".refreshed.json.*.tmp")))

    def test_close_reports_cleanup_errors_after_clearing_handles(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.json"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)
            transport = ChatGPTWebTransport(cookie_path)
            transport.browser = FakeBrowser(RuntimeError("close failed"))

            with self.assertRaisesRegex(RuntimeError, "browser close failed"):
                transport.close()

            self.assertIsNone(transport.browser)
            self.assertIsNone(transport.page)

    def test_debug_capture_is_opt_in_unique_and_private(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX permission semantics are required")
        transport = self.make_transport(FakePage([]))
        with tempfile.TemporaryDirectory() as directory:
            debug_directory = Path(directory) / "debug"
            transport.debug_directory = debug_directory

            screenshot = transport._capture_debug_screenshot()

            self.assertEqual(screenshot.parent, debug_directory)
            self.assertEqual(screenshot.read_bytes(), b"png")
            self.assertEqual(stat.S_IMODE(debug_directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(screenshot.stat().st_mode), 0o600)

    def test_start_uses_current_site_default_user_agent_and_validated_cookies(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.json"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)
            page = FakeStartPage()
            browser = FakeLaunchedBrowser(page)
            chromium = FakeChromium(browser)
            manager = FakePlaywrightManager(chromium)
            sync_api = types.ModuleType("playwright.sync_api")
            sync_api.sync_playwright = lambda: manager
            playwright = types.ModuleType("playwright")

            with patch.dict(
                sys.modules,
                {"playwright": playwright, "playwright.sync_api": sync_api},
            ):
                transport = ChatGPTWebTransport(cookie_path, headless=False)
                transport.start()
                transport.close()

        self.assertFalse(chromium.headless)
        self.assertEqual(browser.context_options, {"viewport": {"width": 1280, "height": 800}})
        self.assertEqual(page.goto_calls, [("https://chatgpt.com/", "domcontentloaded")])
        self.assertEqual(page.default_timeout, 60_000)
        self.assertEqual(browser.context.cookies[0]["domain"], ".chatgpt.com")
        self.assertTrue(browser.closed)
        self.assertTrue(manager.exited)

    def test_start_failure_does_not_capture_a_screenshot_without_opt_in(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.json"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)
            page = FakeStartPage(selector_found=False)
            browser = FakeLaunchedBrowser(page)
            manager = FakePlaywrightManager(FakeChromium(browser))
            sync_api = types.ModuleType("playwright.sync_api")
            sync_api.sync_playwright = lambda: manager
            playwright = types.ModuleType("playwright")

            with patch.dict(
                sys.modules,
                {"playwright": playwright, "playwright.sync_api": sync_api},
            ):
                transport = ChatGPTWebTransport(cookie_path)
                with self.assertRaisesRegex(RuntimeError, "Chat input not found"):
                    transport.start()
                transport.close()

        self.assertEqual(page.screenshot_calls, [])


if __name__ == "__main__":
    unittest.main()
