"""LLM completion adapter for omni engine — calls OpenRouter or compatible API."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
import urllib.error
from typing import AsyncGenerator

from omnimaker.adapters.base import NodeAdapter
from omnimaker.types import Envelope, ExecutionHandle


class LLMCompletionAdapter(NodeAdapter):
    """Text-in, text-out via OpenRouter or any OpenAI-compatible API.

    Config:
        api_key_env: env var holding the API key (default "OPENROUTER_API_KEY")
        model: model identifier (default "deepseek/deepseek-v4-flash")
        base_url: API base URL (default "https://openrouter.ai/api/v1")
        temperature: float (default 0.7)
        max_tokens: int (default 1024)
        system_prompt: str (default "You are a helpful assistant.")
        timeout_seconds: float (default 30)
    """

    accepts = {"input": ["text", "transcript"]}
    emits = {"output": ["text"]}

    session_scope = "persistent"

    def __init__(self) -> None:
        super().__init__()
        self._config: dict = {}
        self._session_id: str = ""
        self._messages: list[dict] = []

    async def open(self, session_id: str, config: dict) -> None:
        self._config = config
        self._session_id = session_id
        self._messages = [{"role": "system",
                           "content": config.get("system_prompt",
                               "You are a helpful assistant.")}]

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        # Extract text from payload
        text = ""
        if isinstance(envelope.payload, str):
            text = envelope.payload
        elif isinstance(envelope.payload, dict):
            text = envelope.payload.get("text", "")
        if not text:
            return

        self._messages.append({"role": "user", "content": text})

        try:
            response = await self._call_api()
            self._messages.append({"role": "assistant", "content": response})

            yield Envelope(
                type="text",
                payload=response,
                session_id=self._session_id,
                turn_id=envelope.turn_id,
                execution_id=handle.id,
            )
        except Exception as exc:
            yield Envelope(
                type="error",
                payload={"error": str(exc)},
                session_id=self._session_id,
                turn_id=envelope.turn_id,
                execution_id=handle.id,
            )

    async def _call_api(self) -> str:
        """Make an HTTP POST to the OpenAI-compatible chat completions API."""
        cfg = self._config
        api_key_env = cfg.get("api_key_env", "OPENROUTER_API_KEY")
        api_key = os.environ.get(api_key_env, "")
        model = cfg.get("model", "deepseek/deepseek-v4-flash")
        base_url = cfg.get("base_url", "https://openrouter.ai/api/v1")
        temperature = cfg.get("temperature", 0.7)
        max_tokens = cfg.get("max_tokens", 1024)
        timeout_sec = cfg.get("timeout_seconds", 30)

        url = f"{base_url}/chat/completions"
        body = json.dumps({
            "model": model,
            "messages": self._messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        # Run HTTP request in executor to avoid blocking the event loop
        loop = asyncio.get_event_loop()

        def _request() -> dict:
            req = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST")
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                return json.loads(resp.read().decode())

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _request),
                timeout=timeout_sec + 5,
            )
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            raise RuntimeError(f"API error {e.code}: {error_body}") from e

        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError(f"API returned no choices: {result}")

        content = choices[0].get("message", {}).get("content", "")
        return content or ""

    async def close(self) -> None:
        self._messages = []