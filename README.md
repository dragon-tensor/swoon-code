# Swoon Code

Swoon Code is a browser-backed ChatGPT relay being developed into a coding agent through
AEML (Agent Execution Markup Language). It uses an authenticated ChatGPT browser session and
does not require an OpenAI API key.

**Experimental / educational.** Automated access may violate OpenAI's terms of service.

## Current status

The first four AEML foundation phases are implemented:

1. The original browser relay is split into an importable `swoon` package.
2. AEML messages, actions, results, contexts, and tool schemas have typed immutable models.
3. Assistant responses are parsed as strict, resource-limited XML with explicit truncation
   detection. Arbitrary source payloads can use CDATA.
4. The validator enforces sessions, turns, action IDs, control flow, tool schemas, read-only
   batching, input-root protection, confirmation declarations, and basic chunk rules.

No AEML tool is executed yet. Filesystem sessions, policy resolution, and tool execution are
later phases; keeping them disconnected ensures malformed protocol messages cannot touch the
machine.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m playwright install chromium
```

Export cookies from an authenticated ChatGPT session and save them as `cookies.json`. Both a
Cookie-Editor list and a Playwright storage-state object are accepted.

## Browser relay usage

```bash
# Interactive
./chatgpt.sh --cookies cookies.json -i

# Single prompt
./chatgpt.sh --cookies cookies.json -p "What is Rust?"

# Browser-visible debugging
./chatgpt.sh --cookies cookies.json --headed -v -p "Hello"
```

The legacy `chatgpt_agent.py` entrypoint remains compatible. The browser implementation now
lives in `swoon.transport.ChatGPTWebTransport`.

## AEML parsing and validation

```python
from swoon.aeml import AEMLParser, AEMLValidator

message = AEMLParser().parse(raw_assistant_response)
validated = AEMLValidator().validate(
    message,
    expected_turn=1,
    expected_session="sess_example",
)
```

Parsing and validation are side-effect free. A validated action is only a typed protocol
object; it is not permission to execute anything.

See `aeml_protocol_spec.md` for the protocol and `MIGRATION.md` for the original relay history.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
