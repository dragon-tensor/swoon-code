import json
import os
import sys
import time
import uuid
import re
from dataclasses import dataclass, field
from typing import Optional

SAMESITE_MAP = {"lax": "Lax", "strict": "Strict", "none": "None"}

CF_COOKIE_NAMES = ["f_clearance", "cf_clearance"]
AUTH_COOKIE_NAMES = ["__Secure-next-auth.session-token", "unified_session_manifest"]

BASE_URL = "https://chatgpt.com"
API_URL = f"{BASE_URL}/backend-api/conversation"
AUTH_SESSION_URL = f"{BASE_URL}/api/auth/session"

DEVICE_ID = str(uuid.uuid4())
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

SYSTEM_PROMPT = """You are an AI assistant in an automated pipeline. Your responses MUST use these tags so the pipeline can parse them:

### response ###
Your main reply text here
### /response ###

### action:browse ###
{"query": "search query here"}
### /action:browse ###

### action:dalle ###
{"prompt": "image description", "style": "vivid"}
### /action:dalle ###

### action:code ###
Your code here
### /action:code ###

### action:result ###
Data returned to you after the pipeline executes an action
### /action:result ###

RULES:
1. Always wrap main text in ### response ### tags
2. When you need a tool, emit the action tag BEFORE your response
3. The pipeline executes actions and sends results back via ### action:result ###
4. After receiving results, produce final response
5. Every section must have both open and close tags
6. JSON inside action tags must be valid
7. Never output text outside of tags"""

SYSTEM_PROMPT_SHORT = "\n\nREMINDER: Use ### response ### and ### /response ### tags, and ### action:* ### tags for tools."


@dataclass
class TagSection:
    type: str
    subtype: Optional[str] = None
    content: str = ""
    json_data: dict = field(default_factory=dict)


@dataclass
class ToolCall:
    tool: str
    args: dict
    result: Optional[str] = None


def parse_system_prompt_tags(text: str) -> list[TagSection]:
    sections = []
    pattern = r"###\s*(\w+(?::\w+)?)\s*###\s*(.*?)\s*###\s*/\1\s*###"
    for m in re.finditer(pattern, text, re.DOTALL):
        tag = m.group(1).strip()
        content = m.group(2).strip()
        json_data = {}
        subtype = None
        if ":" in tag:
            parts = tag.split(":", 1)
            tag_type = parts[0]
            subtype = parts[1]
        else:
            tag_type = tag
        if content.startswith("{"):
            try:
                json_data = json.loads(content)
            except json.JSONDecodeError:
                pass
        sections.append(
            TagSection(type=tag_type, subtype=subtype, content=content, json_data=json_data)
        )
    return sections


def extract_text_from_sse(data: dict) -> str:
    parts = []
    msg = data.get("message", {})
    content = msg.get("content", {})
    for part in content.get("parts", []):
        if isinstance(part, str):
            parts.append(part)
    return "".join(parts)


def extract_tool_calls_from_sse(data: dict) -> list[ToolCall]:
    calls = []
    msg = data.get("message", {})
    content = msg.get("content", {})
    tool_calls = content.get("tool_calls", []) or msg.get("tool_calls", [])
    for tc in tool_calls:
        tool_name = tc.get("tool", {}).get("name", "")
        args_raw = tc.get("tool", {}).get("arguments", {})
        if tool_name == "dalle.text2im":
            tool_name = "dalle"
            prompt = ""
            if isinstance(args_raw, dict):
                prompts = args_raw.get("prompts", [])
                if prompts:
                    prompt = prompts[0]
                args_raw = {"prompt": prompt}
        elif tool_name == "browser":
            tool_name = "browse"
        calls.append(ToolCall(tool=tool_name, args=args_raw))
    return calls


def extract_citations_from_sse(data: dict) -> list[str]:
    citations = data.get("message", {}).get("metadata", {}).get("citations", [])
    if not citations:
        citations = data.get("message", {}).get("content", {}).get("citations", [])
    urls = []
    for c in citations:
        if isinstance(c, dict):
            url = c.get("url", c.get("link", ""))
            if url:
                urls.append(url)
        elif isinstance(c, str):
            urls.append(c)
    return urls


class CookieManager:
    def __init__(self, path: str, verbose: bool = False):
        self.path = path
        self.verbose = verbose
        self.cookies: dict[str, str] = {}
        self._load()

    def log(self, msg: str):
        if self.verbose:
            print(f"[cookies] {msg}", file=sys.stderr)

    def _load(self):
        with open(self.path) as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            raise ValueError("Cookies must be a JSON array")
        for c in raw:
            if "sameSite" in c and isinstance(c["sameSite"], str):
                c["sameSite"] = SAMESITE_MAP.get(c["sameSite"].lower(), c["sameSite"])
        self.raw = raw
        self.log(f"Loaded {len(raw)} cookies")
        self._check_required()

    def _check_required(self):
        names = {c.get("name") for c in self.raw}
        has_auth = any(c in names for c in AUTH_COOKIE_NAMES)
        has_cf = any(c in names for c in CF_COOKIE_NAMES)
        if not has_auth:
            self.log("WARNING: No auth cookie found")
        if not has_cf:
            self.log("WARNING: No Cloudflare clearance cookie found")

    def save(self, path: Optional[str] = None):
        target = path or self.path
        with open(target, "w") as f:
            json.dump(self.raw, f, indent=2)
        self.log(f"Saved {len(self.raw)} cookies to {target}")


class SSEResult:
    def __init__(self):
        self.full_text = ""
        self.tool_calls: list[ToolCall] = []
        self.citations: list[str] = []
        self.conversation_id: Optional[str] = None
        self.parent_message_id: Optional[str] = None
        self.raw_lines: list[str] = []

    def feed_line(self, line: str):
        self.raw_lines.append(line)
        if not line.startswith("data: "):
            return
        payload = line[6:].strip()
        if payload == "[DONE]":
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        conv_id = data.get("conversation_id")
        if conv_id:
            self.conversation_id = conv_id
        parent_id = data.get("message", {}).get("id")
        if parent_id:
            self.parent_message_id = parent_id
        text = extract_text_from_sse(data)
        if text:
            self.full_text += text
        calls = extract_tool_calls_from_sse(data)
        self.tool_calls.extend(calls)
        citations = extract_citations_from_sse(data)
        self.citations.extend(citations)


class ChatGPTClient:
    def __init__(self, cookie_path: str, verbose: bool = False):
        self.cookie_path = cookie_path
        self.verbose = verbose
        self.cm = CookieManager(cookie_path, verbose=verbose)
        self.conversation_id: Optional[str] = None
        self.parent_message_id: Optional[str] = None
        self.model = "auto"
        self.turn_count = 0
        self.browser = None
        self.context = None
        self.page = None

    def log(self, msg: str):
        if self.verbose:
            print(f"[client] {msg}", file=sys.stderr)

    def start_browser(self):
        if self.browser:
            return
        from playwright.sync_api import sync_playwright
        self.log("Starting Playwright browser...")
        self.playwright = sync_playwright().__enter__()
        self.browser = self.playwright.chromium.launch(
            headless=True,
            channel="chrome",
        )

        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
        )
        self.context.add_cookies(self.cm.raw)
        self.page = self.context.new_page()
        self.page.set_default_timeout(60000)

        self.log("Navigating to chatgpt.com...")
        self.page.goto(BASE_URL, wait_until="domcontentloaded")

        cl = 0
        while cl < 60:
            title = self.page.title()
            if "just a moment" in title.lower():
                time.sleep(2)
                cl += 1
                self.log(f"Cloudflare... ({cl * 2}s)")
            else:
                break
        if cl > 0:
            self.log("Cloudflare resolved")

        if "login" in self.page.url.lower():
            self.log("Login page reached — session may be expired")
        else:
            self.log(f"Page loaded: {self.page.url}")

        self.context.storage_state(path=self.cookie_path + ".state")
        self.log("Browser ready")

    def stop_browser(self):
        if self.browser:
            self.context.storage_state(path=self.cookie_path + ".state")
            self.browser.close()
        if hasattr(self, "playwright") and self.playwright:
            self.playwright.__exit__(None, None, None)
        self.browser = None
        self.context = None
        self.page = None
        self.log("Browser closed")

    def _make_payload(self, messages: list[dict], action: str = "next") -> dict:
        return {
            "action": action,
            "messages": messages,
            "parent_message_id": self.parent_message_id or str(uuid.uuid4()),
            "model": self.model,
            "timezone_offset_min": -300,
            "history_and_training_disabled": False,
            "force_paragen": False,
            "force_rate_limit": False,
            "conversation_id": self.conversation_id,
        }

    def _build_messages(self, user_message: str, system: Optional[str] = None) -> list[dict]:
        msgs = []
        if system:
            msgs.append({
                "id": str(uuid.uuid4()),
                "role": "system",
                "content": {"content_type": "text", "parts": [system]},
            })
        msgs.append({
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": {"content_type": "text", "parts": [user_message]},
        })
        return msgs

    def _fetch_via_browser(self, payload: dict) -> list[str]:
        js = f"""
        (async () => {{
            try {{
                const payload = {json.dumps(payload)};
                const resp = await fetch("{API_URL}", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                        "oai-device-id": "{DEVICE_ID}"
                    }},
                    body: JSON.stringify(payload)
                }});
                if (!resp.ok) {{
                    const body = await resp.text();
                    return JSON.stringify({{error: true, status: resp.status, body: body.substring(0, 500)}});
                }}
                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let lines = [];
                let buffer = "";
                while (true) {{
                    const {{done, value}} = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, {{stream: true}});
                    const parts = buffer.split("\\n");
                    buffer = parts.pop() || "";
                    for (const part of parts) {{
                        if (part.startsWith("data: ")) {{
                            lines.push(part);
                            if (part.trim() === "data: [DONE]") {{
                                return JSON.stringify({{done: true, lines}});
                            }}
                        }}
                    }}
                }}
                return JSON.stringify({{done: true, lines}});
            }} catch(e) {{
                return JSON.stringify({{error: true, message: e.message}});
            }}
        }})()
        """
        result = self.page.evaluate(js)
        data = json.loads(result) if isinstance(result, str) else result
        if data.get("error"):
            status = data.get("status", "")
            body = data.get("body", data.get("message", ""))
            raise RuntimeError(f"API request failed (status {status}): {body}")
        return data.get("lines", [])

    def ask(self, prompt: str, system_prompt: Optional[str] = None) -> dict:
        self.turn_count += 1
        inject_system = system_prompt or (
            SYSTEM_PROMPT if self.turn_count == 1
            else (SYSTEM_PROMPT_SHORT if self.turn_count % 3 == 0 else None)
        )
        messages = self._build_messages(prompt, system=inject_system)

        if not self.page:
            self.start_browser()

        payload = self._make_payload(messages)
        lines = self._fetch_via_browser(payload)

        result = SSEResult()
        for line in lines:
            result.feed_line(line)

        if result.conversation_id:
            self.conversation_id = result.conversation_id
        if result.parent_message_id:
            self.parent_message_id = result.parent_message_id

        text = result.full_text.strip()
        tags = parse_system_prompt_tags(text)
        extracted_text = "".join(t.content + "\n" for t in tags if t.type == "response")
        final_text = extracted_text.strip() or text

        actions = [t for t in tags if t.type == "action"]
        action_tool_calls = [ToolCall(tool=a.subtype or a.content, args=a.json_data) for a in actions]

        return {
            "text": final_text,
            "tags": tags,
            "native_tool_calls": result.tool_calls,
            "action_tool_calls": action_tool_calls,
            "citations": result.citations,
            "conversation_id": self.conversation_id,
            "turn": self.turn_count,
        }

    def close(self):
        self.stop_browser()


class AgentLoop:
    def __init__(self, client: ChatGPTClient, max_actions: int = 5):
        self.client = client
        self.max_actions = max_actions

    def run(self, user_input: str) -> dict:
        resp = self.client.ask(user_input)
        action_depth = 0
        all_tool_calls = []

        while action_depth < self.max_actions:
            pending = resp.get("action_tool_calls", []) or []
            pending_native = resp.get("native_tool_calls", []) or []
            if not pending and not pending_native:
                break
            action_depth += 1

            for tc in pending_native:
                self.client.log(f"Native tool: {tc.tool}")
                result = self._execute_native_tool(tc)
                all_tool_calls.append({"tool": tc.tool, "args": tc.args, "result": result})
                resp = self.client.ask(self._wrap_result(result, tc.tool))

            for ac in pending:
                self.client.log(f"Action: {ac.tool}")
                result = self._execute_custom_action(ac)
                all_tool_calls.append({"tool": ac.tool, "args": ac.args, "result": result})
                resp = self.client.ask(self._wrap_result(result, ac.tool))

        return {
            "text": resp.get("text", ""),
            "tool_calls": all_tool_calls + [
                {"tool": tc.tool, "args": tc.args, "result": None}
                for tc in (resp.get("native_tool_calls", []) or [])
            ],
            "citations": resp.get("citations", []),
            "conversation_id": self.client.conversation_id,
            "turns": self.client.turn_count,
        }

    def _wrap_result(self, result: str, tag: str) -> str:
        return f"### action:result:{tag} ###\n{result}\n### /action:result:{tag} ###"

    def _execute_native_tool(self, tc: ToolCall) -> str:
        if tc.tool in ("browse", "browser"):
            return self._browse(tc.args.get("query", ""))
        if tc.tool == "dalle":
            return self._dalle(tc.args.get("prompt", ""))
        return f"Tool '{tc.tool}' not implemented."

    def _execute_custom_action(self, ac: ToolCall) -> str:
        if ac.tool in ("browse", "browser"):
            return self._browse(ac.args.get("query", ac.content))
        if ac.tool == "dalle":
            return self._dalle(ac.args.get("prompt", ac.content))
        return f"Action '{ac.tool}' not implemented."

    def _browse(self, query: str) -> str:
        if not query:
            return "No query."
        import httpx
        try:
            r = httpx.get("https://html.duckduckgo.com/html/", params={"q": query},
                          headers={"User-Agent": USER_AGENT}, timeout=15)
            results = []
            for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>', r.text):
                results.append(f"{m.group(2).strip()}: {m.group(1)}")
                if len(results) >= 5:
                    break
            return "\n".join(results) if results else "No results."
        except Exception as e:
            return f"Browse error: {e}"

    def _dalle(self, prompt: str) -> str:
        return f"[DALL-E would generate: '{prompt}']"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ChatGPT Agent Client")
    parser.add_argument("--cookies", required=True)
    parser.add_argument("--prompt", "-p")
    parser.add_argument("--interactive", "-i", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if not args.prompt and not args.interactive:
        parser.print_help()
        sys.exit(1)

    client = ChatGPTClient(args.cookies, verbose=args.verbose)
    agent = AgentLoop(client)

    if args.interactive:
        print("ChatGPT Agent — commands: /new, /conv, /quit")
        while True:
            try:
                msg = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not msg:
                continue
            if msg == "/quit":
                break
            if msg == "/conv":
                print(f"Conv ID: {client.conversation_id}")
                continue
            if msg.startswith("/new "):
                client.conversation_id = None
                client.parent_message_id = None
                client.turn_count = 0
                msg = msg[5:]

            result = agent.run(msg)
            if result["tool_calls"]:
                print(f"[{len(result['tool_calls'])} tool calls]")
            print(f"\n{result['text']}")
    else:
        result = agent.run(args.prompt)
        print(result["text"])

    client.close()


if __name__ == "__main__":
    main()
