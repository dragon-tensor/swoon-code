"""Compatibility entrypoint for the original single-file ChatGPT relay."""

from swoon.cli import legacy_main as main
from swoon.transport import ChatGPTWebTransport

ChatGPT = ChatGPTWebTransport

__all__ = ["ChatGPT", "ChatGPTWebTransport", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
