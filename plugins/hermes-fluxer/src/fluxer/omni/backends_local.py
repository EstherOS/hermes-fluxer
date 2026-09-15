"""Short-context LLM completion via local llama-server, with tool support."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
import urllib.error
from typing import Any, AsyncGenerator

from omnimaker.adapters.base import NodeAdapter
from omnimaker.types import Envelope, ExecutionHandle


class LlamaServerCompletion(NodeAdapter):
    """Text-in, text-out via a local llama-server instance (no tools)."""

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
        self._messages = [{
            "role": "system",
            "content": config.get("system_prompt",
                "You are a helpful assistant. Respond very briefly."),
        }]

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        text = ""
        if isinstance(envelope.payload, str):
            text = envelope.payload
        elif isinstance(envelope.payload, dict):
            text = envelope.payload.get("text", "")
        if not text:
            return

        self._messages.append({"role": "user", "content": text})

        try:
            response = await self._call_completion()
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

    async def _call_completion(self) -> str:
        cfg = self._config
        base_url = cfg.get("base_url", "http://127.0.0.1:8080")
        temperature = cfg.get("temperature", 0.7)
        max_tokens = cfg.get("max_tokens", 256)
        timeout_sec = cfg.get("timeout_seconds", 60)

        body = json.dumps({
            "messages": self._messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }).encode()
        headers = {"Content-Type": "application/json"}

        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._http_post, base_url, body, headers),
                timeout=timeout_sec + 5,
            )
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"completion error {e.code}") from e

        choices = result.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "")

    @staticmethod
    def _http_post(base_url: str, body: bytes,
                   headers: dict) -> dict:
        url = f"{base_url}/v1/chat/completions"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())

    async def close(self) -> None:
        self._messages = []