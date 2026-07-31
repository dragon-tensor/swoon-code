from __future__ import annotations

import json
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
                    "name": "example",
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
            transport = ChatGPTWebTransport(cookie_path)

        self.assertEqual(transport.raw_cookies[0]["sameSite"], "Lax")
        self.assertEqual(transport.raw_cookies[0]["domain"], ".auth.openai.com")


if __name__ == "__main__":
    unittest.main()
