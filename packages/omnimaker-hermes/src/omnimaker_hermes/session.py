"""Hermes session and harness adapters for Omnimaker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from typing import Any, AsyncGenerator

from omnimaker.adapters.base import NodeAdapter
from omnimaker.types import Envelope, ExecutionHandle

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+")
_URL_RE = re.compile(r"https?://\S+")


def _clean_for_speech(text: str) -> str:
    """Strip markdown and URLs so TTS reads plain prose."""
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = _URL_RE.sub("", text)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*#+\s+", "", text, flags=re.M)
    text = text.replace("*", "")
    return re.sub(r"\s+", " ", text).strip()


def _take_sentences(buf: str) -> tuple[list[str], str]:
    """Split completed sentences off *buf*; returns (sentences, remainder)."""
    out: list[str] = []
    while True:
        match = None
        for m in _SENTENCE_END.finditer(buf):
            if m.start() >= 8:  # don't split tiny fragments ("OK. ...")
                match = m
                break
        if match is None:
            break
        out.append(buf[:match.end()].strip())
        buf = buf[match.end():]
    # Force-flush a very long unpunctuated run at a word boundary.
    if len(buf) > 240:
        cut = buf.rfind(" ", 0, 240)
        cut = 240 if cut == -1 else cut
        out.append(buf[:cut].strip())
        buf = buf[cut:].strip()
    return out, buf


def _split_fragments(text: str) -> list[str]:
    """Split a complete answer into clean, speakable fragments."""
    sentences, rest = _take_sentences(text)
    frags = [c for c in (_clean_for_speech(s) for s in sentences) if c]
    tail = _clean_for_speech(rest)
    if tail:
        frags.append(tail)
    return frags


def _get_hermes_tools(allow: list[str] | None = None) -> list[dict]:
    """Load tool definitions from the Hermes agent.

    *allow*, when given, filters the tool set to these names. Smaller
    tool sets mean smaller prompts — critical for voice latency on a
    cold prompt cache.
    """
    try:
        sys.path.insert(0, "/home/agent/.hermes/hermes-agent")
        from model_tools import get_tool_definitions
        tools = get_tool_definitions()
        if allow is not None:
            want = set(allow)
            tools = [t for t in tools if t.get("function", {}).get("name") in want]
        return tools
    except Exception:
        return []


def _execute_hermes_tool(name: str, args_json: str | dict) -> str:
    """Execute a tool call through the Hermes harness.

    handle_function_call expects the arguments as a dict. Tool-call
    arguments arrive from the model server as a JSON string, so decode
    first — passing the raw string is silently replaced with {} and
    every argument is dropped.
    """
    try:
        sys.path.insert(0, "/home/agent/.hermes/hermes-agent")
        from model_tools import handle_function_call
        args: Any = args_json
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {}
        result = handle_function_call(name, args)
        if isinstance(result, dict):
            return json.dumps(result)
        return str(result)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _load_agent_identity() -> str | None:
    """The agent's identity prompt — SOUL.md from HERMES_HOME — or None."""
    try:
        sys.path.insert(0, "/home/agent/.hermes/hermes-agent")
        from agent.prompt_builder import load_soul_md
        soul = load_soul_md()
        return soul.strip() if soul else None
    except Exception:
        logger.debug("hermes/session: could not load agent identity", exc_info=True)
        return None


class HermesSessionBackend(NodeAdapter):
    """Wraps a Hermes agent conversation as an omni node.

    Receives text input, processes it through the Hermes agent
    (with full tool/harness access), returns the response text.

    The final answer round streams from the model server: completed
    sentences are yielded as individual ``text`` envelopes while the
    rest of the answer is still generating, so a downstream mouth can
    start speaking early. Tool-call rounds assemble their calls
    silently and yield nothing.

    Config:
        profile: Hermes profile name (default "default")
        model: model override (optional)
        temperature: float (default 0.7)
        max_tokens: int (default 512)
        timeout_seconds: float (default 60)
        thinking: bool (default True) — model reasoning; off is faster
            but degrades spontaneous tool use
        tools: optional allow-list of Hermes tool names
    """

    accepts = {"input": ["text", "transcript", "tool_call"],
               "context": ["tool_result"]}
    emits = {"output": ["text"]}
    session_scope = "persistent"

    def __init__(self) -> None:
        super().__init__()
        self._config: dict = {}
        self._session_id: str = ""
        self._messages: list[dict] = []
        self._tool_defs: list[dict] = []
        self._graph_tools: dict[str, dict] = {}
        self._pending_graph_calls: list[str] = []

    async def open(self, session_id: str, config: dict) -> None:
        self._config = config
        self._session_id = session_id

        # Tool scoping: config.tools is an optional allow-list of tool names.
        # Scoping down cuts the prompt size dramatically (10K → ~2K tokens),
        # which is the difference between a 30s and a 5s cold first turn.
        allow = config.get("tools")
        self._tool_defs = _get_hermes_tools(allow)

        profile = config.get("profile", "default")
        # The node's system prompt is the agent identity (SOUL.md) BY DEFAULT —
        # a hermes/session node is the harness, identity included. Any
        # config.system_prompt is appended as node-level instructions.
        # `identity: false` gives a pure role node (talker personas, HAL).
        parts: list[str] = []
        if config.get("identity", True):
            identity = _load_agent_identity()
            if identity:
                parts.append(identity)
        if config.get("system_prompt"):
            parts.append(config["system_prompt"])
        if not parts:
            parts.append(f"You are a helpful assistant running profile: {profile}.")
        self._messages = [{"role": "system", "content": "\n\n".join(parts)}]

        # Prewarm the llama-server prompt cache so the first real turn
        # starts from a warm prefix instead of paying full cold processing.
        if config.get("prewarm", True):
            asyncio.ensure_future(self._prewarm())

    def bind_tools(self, tools: list[dict]) -> None:
        """Graph tools declared on this node — called by the engine after open().

        Each entry: ``{name, description, await}``. They are appended to
        the model's tool definitions; a call to one is emitted as a
        ``tool_call`` envelope for the engine to route — the adapter
        never executes graph tools itself. The response arrives later as
        a ``tool_result`` envelope and is fed back as the tool message.
        """
        for tool in tools or []:
            name = tool.get("name") or ""
            if not name:
                continue
            self._graph_tools[name] = tool
            self._tool_defs.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description") or (
                        f"Hand the user's request to the {name} target and "
                        f"get its answer back."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "The user's full request, restated "
                                    "completely — the target cannot see "
                                    "this conversation."),
                            },
                        },
                        "required": ["query"],
                    },
                },
            })

    @staticmethod
    def _chat_url(base_url: str) -> str:
        """Compose the chat-completions URL, tolerating a /v1-suffixed base."""
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    async def _prewarm(self) -> None:
        """Warm the prompt cache with system + tools (no side effects)."""
        try:
            import urllib.request
            warm_msgs = self._messages + [{"role": "user", "content": "ok"}]
            body = json.dumps({
                "messages": warm_msgs,
                "tools": self._tool_defs,
                "max_tokens": 1,
                "temperature": 0.0,
            }).encode()
            base_url = self._config.get("base_url",
                os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8080"))
            req = urllib.request.Request(
                self._chat_url(base_url),
                data=body, headers={"Content-Type": "application/json"})
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=120).read())
        except Exception:
            pass

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        text = ""
        if envelope.type == "tool_result":
            # A graph tool's response arrived (async invocation) — feed it
            # back into the conversation as the pending tool call's return.
            if not self._take_tool_result(envelope):
                return
        elif envelope.type == "tool_call":
            # This node is the *target* of a graph tool call — run the query.
            payload = envelope.payload if isinstance(envelope.payload, dict) else {}
            args = payload.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            text = args.get("query") or args.get("text") or ""
            if not text:
                return
        elif isinstance(envelope.payload, str):
            text = envelope.payload
        elif isinstance(envelope.payload, dict):
            text = envelope.payload.get("text", "")
        if not text and envelope.type != "tool_result":
            return

        try:
            async for item in self._run_turn(text):
                yield Envelope(
                    type=item["type"],
                    payload=item["payload"],
                    session_id=self._session_id,
                    turn_id=envelope.turn_id,
                    execution_id=handle.id,
                    metadata={"port": item["port"]} if item.get("port") else {},
                )
        except Exception as exc:
            yield Envelope(
                type="error",
                payload={"error": str(exc)},
                session_id=self._session_id,
                turn_id=envelope.turn_id,
                execution_id=handle.id,
            )

    def _take_tool_result(self, envelope: Envelope) -> bool:
        """Append an arriving graph-tool response to the message list.

        Returns True when a result was appended (the caller then runs a
        turn with no new user message — the tool message is the trigger).
        """
        payload = envelope.payload
        if isinstance(payload, dict):
            result = payload.get("result") or payload.get("text") or json.dumps(payload)
        else:
            result = str(payload or "")
        result = result.strip()
        if not result:
            return False
        call_id = self._pending_graph_calls.pop(0) if self._pending_graph_calls else ""
        self._messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": (result + "\n\n(Speak this to the user now, conversationally "
                        "and briefly. Do not mention tools, siblings, or the system.)"),
        })
        return True

    async def _run_turn(self, user_text: str) -> AsyncGenerator[dict, None]:
        """One conversational turn; yields output items as they form.

        Items are ``{"type": "text"|"tool_call", "payload": ..., "port": ...}``.
        Rounds stream from the model server. A round that ends with tool
        calls executes them (harness tools inline; graph tools are yielded
        as ``tool_call`` envelopes) and continues; a round that ends with
        text yields its completed sentences as they arrive. On an empty
        finish (reasoning can consume the whole budget), a plain no-tools /
        no-thinking recovery call forces a direct answer.
        """
        if user_text:
            self._messages.append({"role": "user", "content": user_text})

        graph_call_fired = False
        for _ in range(8):
            raw = ""
            buf = ""
            tool_calls: dict[int, dict] = {}

            async for event in self._stream_round():
                kind = event["kind"]
                if kind == "content":
                    raw += event["text"]
                    buf += event["text"]
                    sentences, buf = _take_sentences(buf)
                    for sentence in sentences:
                        cleaned = _clean_for_speech(sentence)
                        if cleaned:
                            yield {"type": "text", "payload": cleaned, "port": "text"}
                elif kind == "tool_call":
                    tc = event["tool_call"]
                    idx = int(tc.get("index") or 0)
                    slot = tool_calls.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]

            calls = [tool_calls[i] for i in sorted(tool_calls)]
            msg: dict[str, Any] = {"role": "assistant", "content": raw}
            if calls:
                msg["tool_calls"] = calls
            self._messages.append(msg)

            if not calls:
                if raw.strip():
                    tail = _clean_for_speech(buf)
                    if tail:
                        yield {"type": "text", "payload": tail, "port": "text"}
                    return
                # Empty finish — force a plain answer: no tools, no thinking,
                # with a nudge; then fall back to a short apology.
                self._messages.pop()
                logger.warning("hermes/session: empty model output — forcing a direct answer")
                self._messages.append({
                    "role": "user",
                    "content": "Give your final answer now, briefly, without searching.",
                })
                # Local servers can disable thinking for the recovery call;
                # remote reasoning models can't — give them budget instead.
                local = self._is_llama_server(self._config.get("base_url", ""))
                retry = await self._call_api(
                    max_tokens_override=(200 if local else 800), plain=True)
                self._messages.pop()
                rmsg = retry.get("message", {}) or {}
                if rmsg.get("content"):
                    self._messages.append(rmsg)
                    for fragment in _split_fragments(rmsg["content"]):
                        yield {"type": "text", "payload": fragment, "port": "text"}
                    return
                logger.warning("hermes/session: direct-answer recovery was empty too — returning fallback")
                yield {"type": "text", "payload": "I'm sorry, I couldn't complete that. Please ask me again.",
                       "port": "text"}
                return

            for tc in calls:
                name = tc.get("function", {}).get("name", "")
                args_raw = tc.get("function", {}).get("arguments", "{}")
                if name in self._graph_tools:
                    if graph_call_fired:
                        # One hand-off per turn — don't chain defers.
                        self._messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": "Already handed off — wait for the existing result.",
                        })
                        continue
                    try:
                        args = (json.loads(args_raw)
                                if isinstance(args_raw, str) and args_raw.strip() else {})
                    except ValueError:
                        args = {}
                    self._pending_graph_calls.append(tc.get("id", ""))
                    graph_call_fired = True
                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": ("Accepted — the result will arrive as a new tool "
                                    "message. Tell the user you're looking into it in one "
                                    "short, natural line, then stop."),
                    })
                    yield {
                        "type": "tool_call",
                        "payload": {"name": name, "arguments": args,
                                    "call_id": tc.get("id", "")},
                        "port": "tool_call",
                    }
                    continue
                result_content = _execute_hermes_tool(name, args_raw)
                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result_content,
                })

        yield {"type": "text", "payload": "I encountered a limit processing your request.",
               "port": "text"}

    async def _stream_round(self) -> AsyncGenerator[dict, None]:
        """Stream one completion round from the model server.

        Yields ``{"kind": "content"|"reasoning"|"tool_call", ...}`` events.
        Tool-call deltas are accumulated by the caller; reasoning is
        surfaced but dropped there. Closing this generator (interrupt
        cancel) closes the HTTP stream, which stops server-side generation.
        """
        import httpx

        cfg = self._config
        base_url = cfg.get("base_url", os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8080"))
        body: dict[str, Any] = {
            "messages": self._messages,
            "temperature": cfg.get("temperature", 0.7),
            "max_tokens": cfg.get("max_tokens", 512),
            "stream": True,
        }
        if cfg.get("model"):
            body["model"] = cfg["model"]
        # thinking: per-node config; default on. The flag is llama-server
        # specific (chat_template_kwargs) — only sent to local servers.
        if (cfg.get("thinking", True) is False
                and self._is_llama_server(base_url)):
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if self._tool_defs:
            body["tools"] = self._tool_defs
        headers = {"Content-Type": "application/json"}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        timeout = httpx.Timeout(connect=10.0, read=240.0, write=30.0, pool=30.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", self._chat_url(base_url), json=body,
                headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode(errors="replace")[:200]
                    raise RuntimeError(f"API error {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    if not data:
                        continue
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    choice = (obj.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        yield {"kind": "reasoning", "text": delta["reasoning_content"]}
                    if delta.get("content"):
                        yield {"kind": "content", "text": delta["content"]}
                    for tc in (delta.get("tool_calls") or []):
                        yield {"kind": "tool_call", "tool_call": tc}
                    if choice.get("finish_reason"):
                        return

    async def _thinker_loop(self, user_text: str) -> str:
        """Non-streaming view of a turn (compat for scripts/tests)."""
        parts: list[str] = []
        async for fragment in self._run_turn(user_text):
            parts.append(fragment)
        return " ".join(parts)

    async def _call_api(self, max_tokens_override: int | None = None, plain: bool = False) -> dict:
        """Send messages to the model API (non-streaming; prewarm + recovery).

        *plain*: tools-free, thinking-off request used by the empty-output
        recovery — forces a direct text answer from the current context.
        """
        import urllib.request
        import urllib.error

        cfg = self._config
        base_url = cfg.get("base_url", os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8080"))
        temperature = cfg.get("temperature", 0.7)
        max_tokens = max_tokens_override or cfg.get("max_tokens", 512)
        timeout_sec = cfg.get("timeout_seconds", 60)

        body_obj = {
            "messages": self._messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if cfg.get("model"):
            body_obj["model"] = cfg["model"]
        # thinking: llama-server flag — only sent to local servers.
        enable_thinking = (not plain) and cfg.get("thinking", True)
        if (not enable_thinking) and self._is_llama_server(base_url):
            body_obj["chat_template_kwargs"] = {"enable_thinking": False}
        if (not plain) and self._tool_defs:
            body_obj["tools"] = self._tool_defs
        body = json.dumps(body_obj).encode()
        headers = {"Content-Type": "application/json"}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = self._chat_url(base_url)

        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._http_post, url, body, headers),
                timeout=timeout_sec + 5,
            )
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"API error {e.code}") from e

        choices = result.get("choices", [])
        if not choices:
            return {"message": {"role": "assistant", "content": ""}}
        return choices[0]

    @staticmethod
    def _http_post(url: str, body: bytes, headers: dict) -> dict:
        import urllib.request
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())

    def _api_key(self) -> str | None:
        """API key from config, or from the env var named by api_key_env."""
        key = self._config.get("api_key")
        if not key:
            env_name = self._config.get("api_key_env")
            if env_name:
                key = os.environ.get(env_name)
        return key or None

    @staticmethod
    def _is_llama_server(base_url: str) -> bool:
        return "127.0.0.1" in base_url or "localhost" in base_url

    async def close(self) -> None:
        self._messages = []


class HermesHarnessBackend(NodeAdapter):
    """Dedicated harness node — accepts tool_call envelopes, returns tool_result.

    Connect other nodes to this via routes:
      thinker.tool_call -> harness.tool_call
      harness.tool_result -> thinker.tool_result
    """

    accepts = {"tool_call": ["tool_call"]}
    emits = {"tool_result": ["tool_result"]}
    session_scope = "persistent"

    async def open(self, session_id: str, config: dict) -> None:
        pass

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        payload = envelope.payload
        if not isinstance(payload, dict):
            return
        name = payload.get("name", "")
        args = payload.get("arguments", "{}")
        result = _execute_hermes_tool(name, args)
        yield Envelope(
            type="tool_result",
            payload={"name": name, "result": result},
            session_id=envelope.session_id,
            turn_id=envelope.turn_id,
            execution_id=handle.id,
        )

    async def close(self) -> None:
        pass
