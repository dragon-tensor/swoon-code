from __future__ import annotations

import json
import os
import stat
import tempfile
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


if __name__ == "__main__":
    unittest.main()
