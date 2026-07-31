"""Command-line entrypoint for the current browser relay."""

from __future__ import annotations

import argparse

from .transport import ChatGPTWebTransport


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ChatGPT terminal chatbot")
    parser.add_argument("--cookies", required=True)
    parser.add_argument("--prompt", "-p")
    parser.add_argument("--interactive", "-i", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--timeout", type=float, default=180, help="response timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.prompt and not args.interactive:
        parser.print_help()
        return 1

    client = ChatGPTWebTransport(
        args.cookies,
        verbose=args.verbose,
        headless=not args.headed,
        response_timeout=args.timeout,
    )
    try:
        client.start()
        if args.interactive:
            print("ChatGPT — type messages, /quit to exit")
            while True:
                try:
                    message = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not message:
                    continue
                if message == "/quit":
                    break
                print()
                print(client.send(message))
                print()
        else:
            print(client.send(args.prompt))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
