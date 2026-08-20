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


class FakeCodeElement:
    def __init__(self, text: str) -> None:
        self.text = text

    def text_content(self) -> str:
        return self.text


class FakeCodeMessage(FakeMessage):
    def __init__(self, rendered_text: str, code_texts: list[str]) -> None:
        super().__init__(rendered_text)
        self.code_texts = code_texts

    def query_selector_all(self, selector: str):
        if selector == "pre code":
            return [FakeCodeElement(text) for text in self.code_texts]
        return []


class FakeVisibleElement:
    def is_visible(self) -> bool:
        return True


class FakePromptElement(FakeVisibleElement):
    def __init__(self) -> None:
        self.values: list[str] = []

    def fill(self, value: str) -> None:
        self.values.append(value)


class FakeButton(FakeVisibleElement):
    def __init__(self) -> None:
        self.clicks = 0

    def click(self) -> None:
        self.clicks += 1


class FakeKeyboard:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def type(self, value: str, **_options) -> None:
        self.events.append(("type", value))

    def press(self, value: str) -> None:
        self.events.append(("press", value))


class FakePage:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self.messages = messages

    def query_selector(self, selector: str):
        return None

    def query_selector_all(self, selector: str):
        return self.messages

    def screenshot(self, *, path: str) -> None:
        Path(path).write_bytes(b"png")


class FakeSubmitPage(FakePage):
    def __init__(self) -> None:
        super().__init__([])
        self.prompt = FakePromptElement()
        self.send_button = FakeButton()
        self.keyboard = FakeKeyboard()

    def query_selector(self, selector: str):
        if selector == "#prompt-textarea":
            return self.prompt
        if selector == "button[data-testid='send-button']":
            return self.send_button
        return None


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
    def __init__(
        self,
        *,
        selector_found: bool = True,
        logged_out: bool = False,
        cloudflare_challenge: bool = False,
    ) -> None:
        super().__init__([])
        self.selector_found = selector_found
        self.logged_out = logged_out
        self.cloudflare_challenge = cloudflare_challenge
        self.url = (
            "https://chatgpt.com/?__cf_chl_rt_tk=test"
            if cloudflare_challenge
            else "https://chatgpt.com/"
        )
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

    def query_selector(self, selector: str):
        if self.selector_found and selector == "#prompt-textarea":
            return FakePromptElement()
        if self.logged_out and selector == "button:has-text('Log in')":
            return FakeVisibleElement()
        if self.cloudflare_challenge and selector == ".cf-turnstile":
            return FakeVisibleElement()
        return None

    def wait_for_selector(self, selector: str, *, timeout: int):
        self.waited.append(selector)
        if self.selector_found or self.cloudflare_challenge:
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
        transport.response_settle_time = 1.0
        transport.response_timeout_retries = 0
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

    def test_send_fills_multiline_prompt_atomically_then_clicks_once(self) -> None:
        page = FakeSubmitPage()
        transport = self.make_transport(page)
        transport.response_timeout = 180
        multiline = "<aeml>\n  <context>one</context>\n</aeml>"

        with (
            patch.object(transport, "_wait_for_new_response", return_value="response") as wait,
            patch("swoon.transport.chatgpt_web.time.sleep"),
        ):
            result = transport.send(multiline)

        self.assertEqual(result, "response")
        self.assertEqual(page.prompt.values, [multiline])
        self.assertEqual(page.send_button.clicks, 1)
        self.assertEqual(page.keyboard.events, [])
        wait.assert_called_once_with(
            previous_count=0,
            previous_text="",
            timeout=180,
            timeout_retries=0,
        )

    def test_message_text_prefers_one_lossless_aeml_code_block(self) -> None:
        transport = self.make_transport(FakePage([]))
        exact = (
            '<aeml turn="1" session="sess_x">\n'
            '  <action id="a"><args><content>def f():\n    return 1\n'
            '</content></args></action>\n</aeml>'
        )
        message = FakeCodeMessage("flattened rendered text", [exact])

        self.assertEqual(transport._message_text(message), exact)

    def test_message_text_ignores_ambiguous_or_non_aeml_code_blocks(self) -> None:
        transport = self.make_transport(FakePage([]))
        multiple = FakeCodeMessage("rendered", ["<aeml/>", "second"])
        unrelated = FakeCodeMessage("rendered", ["print('hello')"])

        self.assertEqual(transport._message_text(multiple), "rendered")
        self.assertEqual(transport._message_text(unrelated), "rendered")

    def test_wait_returns_a_stable_new_response(self) -> None:
        page = FakePage([FakeMessage("old"), FakeMessage("new")])
        transport = self.make_transport(page)
        with (
            patch(
                "swoon.transport.chatgpt_web.time.monotonic",
                side_effect=[0.0, 0.5, 1.0, 1.5],
            ),
            patch("swoon.transport.chatgpt_web.time.sleep"),
        ):
            result = transport._wait_for_new_response(
                previous_count=1,
                previous_text="old",
                timeout=5,
            )
        self.assertEqual(result, "new")

    def test_wait_does_not_return_a_partial_response_at_timeout(self) -> None:
        page = FakePage([FakeMessage("old"), FakeMessage("partial AEML")])
        transport = self.make_transport(page)
        with (
            patch(
                "swoon.transport.chatgpt_web.time.monotonic",
                side_effect=[0.0, 0.1, 0.2, 0.3],
            ),
            patch("swoon.transport.chatgpt_web.time.sleep"),
        ):
            with self.assertRaisesRegex(TimeoutError, "no follow-up prompt was sent"):
                transport._wait_for_new_response(
                    previous_count=1,
                    previous_text="old",
                    timeout=0.25,
                )

    def test_wait_can_extend_without_resubmitting_the_prompt(self) -> None:
        page = FakePage([FakeMessage("old")])
        transport = self.make_transport(page)
        transport.response_settle_time = 0.05

        def messages(_selector: str):
            current = [FakeMessage("old")]
            if clock[0] >= 0.3:
                current.append(FakeMessage("complete"))
            return current

        clock = [0.0]

        def monotonic() -> float:
            value = clock[0]
            clock[0] += 0.1
            return value

        page.query_selector_all = messages
        with (
            patch("swoon.transport.chatgpt_web.time.monotonic", side_effect=monotonic),
            patch("swoon.transport.chatgpt_web.time.sleep"),
        ):
            result = transport._wait_for_new_response(
                previous_count=1,
                previous_text="old",
                timeout=0.15,
                timeout_retries=2,
            )

        self.assertEqual(result, "complete")

    def test_wait_requires_generation_control_to_disappear(self) -> None:
        page = FakePage([FakeMessage("old"), FakeMessage("complete")])
        transport = self.make_transport(page)
        with (
            patch.object(
                transport,
                "_generation_is_active",
                side_effect=[True, True, True, False, False],
            ),
            patch(
                "swoon.transport.chatgpt_web.time.monotonic",
                side_effect=[0.0, 0.5, 1.0, 1.5, 2.0, 2.5],
            ),
            patch("swoon.transport.chatgpt_web.time.sleep"),
        ):
            result = transport._wait_for_new_response(
                previous_count=1,
                previous_text="old",
                timeout=5,
            )
        self.assertEqual(result, "complete")

    def test_send_gate_waits_for_an_active_generation(self) -> None:
        transport = self.make_transport(FakePage([]))
        with (
            patch.object(
                transport,
                "_generation_is_active",
                side_effect=[True, True, False],
            ),
            patch(
                "swoon.transport.chatgpt_web.time.monotonic",
                side_effect=[0.0, 0.5, 1.0],
            ),
            patch("swoon.transport.chatgpt_web.time.sleep") as sleep,
        ):
            transport._wait_until_ready_to_send(timeout=5)
        sleep.assert_called_once()

    def test_response_settle_time_must_fit_inside_timeout(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": "chatgpt.com",
                    "path": "/",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.json"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "shorter than"):
                ChatGPTWebTransport(
                    cookie_path,
                    response_timeout=5,
                    response_settle_time=5,
                )

    def test_cookie_loader_accepts_playwright_storage_state(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "redacted",
                    "domain": "chatgpt.com",
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
        self.assertEqual(transport.raw_cookies[0]["domain"], "chatgpt.com")
        self.assertEqual(transport.raw_cookies[0]["path"], "/")

    def test_cookie_loader_normalizes_chrome_same_site_variants(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "sameSite": "no_restriction",
                },
                {
                    "name": "auxiliary",
                    "value": "secret",
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "sameSite": "unspecified",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.json"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)
            transport = ChatGPTWebTransport(cookie_path)

        self.assertEqual(transport.raw_cookies[0]["sameSite"], "None")
        self.assertNotIn("sameSite", transport.raw_cookies[1])

    def test_cookie_loader_rejects_auth_site_only_export(self) -> None:
        state = {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": ".auth.openai.com",
                    "path": "/",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.json"
            cookie_path.write_text(json.dumps(state), encoding="utf-8")
            cookie_path.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "no chatgpt.com cookie"):
                ChatGPTWebTransport(cookie_path)

    def test_cookie_loader_rejects_encrypted_hotcleaner_backup(self) -> None:
        backup = {
            "url": "https://www.hotcleaner.com/cookie-editor/",
            "version": 2,
            "data": "ZW5jcnlwdGVkLWNvb2tpZS1iYWNrdXA=",
        }
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.json"
            cookie_path.write_text(json.dumps(backup), encoding="utf-8")
            cookie_path.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "Encrypted Hotcleaner.*not supported"):
                ChatGPTWebTransport(cookie_path)

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
                with self.assertRaisesRegex(RuntimeError, "message composer was not found"):
                    transport.start()
                transport.close()

        self.assertEqual(page.screenshot_calls, [])

    def test_headless_start_reports_cloudflare_human_verification_immediately(self) -> None:
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
            page = FakeStartPage(selector_found=False, cloudflare_challenge=True)
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
                with self.assertRaisesRegex(RuntimeError, "Run `swoon auth`"):
                    transport.start()
                transport.close()

        self.assertEqual(page.waited, [])

    def test_start_rejects_logged_out_page_with_private_debug_capture(self) -> None:
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
            page = FakeStartPage(logged_out=True)
            browser = FakeLaunchedBrowser(page)
            manager = FakePlaywrightManager(FakeChromium(browser))
            sync_api = types.ModuleType("playwright.sync_api")
            sync_api.sync_playwright = lambda: manager
            playwright = types.ModuleType("playwright")

            with patch.dict(
                sys.modules,
                {"playwright": playwright, "playwright.sync_api": sync_api},
            ):
                transport = ChatGPTWebTransport(
                    cookie_path,
                    debug_directory=root / "debug",
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "session is not authenticated.*Debug screenshot",
                ):
                    transport.start()
                transport.close()

        self.assertEqual(len(page.screenshot_calls), 1)


if __name__ == "__main__":
    unittest.main()
