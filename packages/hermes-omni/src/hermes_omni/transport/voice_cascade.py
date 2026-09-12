"""Cascade pipeline for hermes-omni transport (voice lane).

``remote audio track → rtc.AudioStream(48k mono) → energy VAD → utterance WAV
→ STT → adapter message pipeline → agent turn → (reply intercepted by the
adapter's send hook) → piper TTS → 48k PCM published.``

One utterance at a time: while an utterance is being transcribed/dispatched a
new one is dropped with a log line (single-channel v1; barge-in and duplex
turn-taking are v2).  The cascade owns no LiveKit connection; it consumes
tracks the controller subscribed.
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from . import audio as audio_lib
from .config import VoiceConfig

log = logging.getLogger("hermes_omni.transport.voice_cascade")

#: LiveKit participant identity pattern (``user_<id>_<connection>``).
_IDENTITY_RE = re.compile(r"^user_(\d+)", re.IGNORECASE)


def speaker_from_identity(identity: Any) -> tuple[str, str]:
    """``(user_id, display_name)`` from a LiveKit participant identity."""
    text = str(identity or "").strip()
    match = _IDENTITY_RE.match(text)
    return (match.group(1) if match else text, text)


class VoiceCascade:
    """Inbound ear + transcript dispatcher for one voice session."""

    def __init__(self, session, adapter, config: VoiceConfig, *,
                 stt: Optional[Callable[[str], Optional[str]]] = None) -> None:
        self.session = session
        self.adapter = adapter
        self.config = config
        self._stt_override = stt  # test seam: wav path -> transcript
        self.segmenter = audio_lib.VadSegmenter(
            silence_ms=config.silence_ms,
            min_utterance_ms=config.min_utterance_ms,
            max_utterance_s=config.max_utterance_s,
            energy_threshold=config.energy_threshold,
        )
        self._busy = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        #: Single-slot follow-up queue (``voice.queue_utterance``): latest utterance wins.
        self._queued_utterance: Optional[tuple] = None
        self.closed = False
        self.stats: dict[str, int] = {
            "frames": 0, "bytes": 0, "utterances": 0, "dropped_busy": 0,
            "queued_busy": 0, "stt_calls": 0, "stt_failures": 0, "stt_fallback_calls": 0,
            "dispatched": 0, "short_dropped": 0, "dropped_unauthorized": 0,
        }
        self.last_transcript: Optional[str] = None
        self.dispatched_via: Optional[str] = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        self.closed = False

    async def stop(self) -> None:
        self.closed = True
        self._queued_utterance = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    # ── inbound audio ────────────────────────────────────────────────────

    def on_track_subscribed(self, track, publication=None, participant=None) -> None:
        """Room ``track_subscribed`` handler — start a reader for audio tracks."""
        rtc = getattr(self.session, "rtc", None)
        if rtc is None:
            return
        try:
            kind = getattr(track, "kind", None)
            if rtc is not None and kind is not None and hasattr(rtc, "TrackKind"):
                if kind != rtc.TrackKind.KIND_AUDIO:
                    return
        except Exception:  # unknown stub shapes: assume audio
            pass
        self._spawn(self._read_track(track, participant))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _read_track(self, track, participant) -> None:
        rtc = self.session.rtc
        stream = rtc.AudioStream(track, sample_rate=audio_lib.SAMPLE_RATE, num_channels=1)
        try:
            async for event in stream:
                if self.closed:
                    break
                frame = getattr(event, "frame", event)
                data = bytes(getattr(frame, "data", b""))
                self.stats["frames"] += 1
                self.stats["bytes"] += len(data)
                self.stats["short_dropped"] = self.segmenter.dropped_short
                for utterance in self.segmenter.feed(data):
                    await self._handle_utterance(utterance, participant)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a dead stream must not kill the session
            log.warning("hermes-omni: audio stream ended for %s: %s",
                        getattr(participant, "identity", "?"), e)
        finally:
            self.stats["short_dropped"] = self.segmenter.dropped_short
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass

    async def process_frame_bytes(self, data: bytes, participant=None) -> None:
        """Test/dry-run seam: feed raw 48k mono int16 bytes as a received frame."""
        self.stats["frames"] += 1
        self.stats["bytes"] += len(data)
        for utterance in self.segmenter.feed(data):
            await self._handle_utterance(utterance, participant)

    # ── utterance → transcript → dispatch ────────────────────────────────

    async def _handle_utterance(self, utterance: audio_lib.Utterance, participant=None) -> None:
        if self._busy.locked():
            if not self.config.queue_utterance:
                self.stats["dropped_busy"] += 1
                log.info("hermes-omni: dropping utterance (%.2fs) — previous still processing",
                         utterance.seconds)
                return
            # Single-slot queue: the newest follow-up replaces any stale pending one, so a
            # quick second thought is never lost while the first is still being processed.
            self._queued_utterance = (utterance, participant)
            self.stats["queued_busy"] += 1
            log.info("hermes-omni: queued utterance (%.2fs) — previous still processing",
                     utterance.seconds)
            return
        async with self._busy:
            pending: Optional[tuple] = (utterance, participant)
            while pending is not None and not self.closed:
                await self._process_utterance(*pending)
                # Yield once so a concurrent enqueue lands before the slot is checked.
                await asyncio.sleep(0)
                pending = self._queued_utterance
                self._queued_utterance = None
                if pending is not None:
                    log.info("hermes-omni: processing queued utterance (%.2fs)",
                             pending[0].seconds)

    async def _process_utterance(self, utterance: audio_lib.Utterance, participant=None) -> None:
        self.stats["utterances"] += 1
        speaker_id, speaker_name = speaker_from_identity(getattr(participant, "identity", ""))
        if not self._speaker_authorized(speaker_id):
            self.stats["dropped_unauthorized"] += 1
            log.info("hermes-omni: dropping transcript from unauthorized user %r", speaker_id)
            return
        text = await self._transcribe(utterance)
        if not text:
            return
        self.last_transcript = text
        log.info("hermes-omni: transcript from %s: %s", speaker_name or "?", text[:120])
        if self.config.transcripts == "channel":
            await self._echo_transcript(text, speaker_id)
        await self._dispatch(text, speaker_id, speaker_name)

    async def _transcribe(self, utterance: audio_lib.Utterance) -> Optional[str]:
        stt = self._stt_override or self._stt_callable()
        if stt is None:
            return None
        tmp_dir = Path(tempfile.mkdtemp(prefix="hermes-omni-stt-"))
        wav_path = tmp_dir / "utterance.wav"
        try:
            audio_lib.write_wav(wav_path, utterance.samples)
            self.stats["stt_calls"] += 1
            text = await asyncio.to_thread(stt, str(wav_path))
            text = (text or "").strip()
            if not text or _is_hallucination(text):
                if text:
                    self.stats["stt_failures"] += 1
                return None
            return text
        except Exception as e:
            self.stats["stt_failures"] += 1
            log.warning("hermes-omni: STT failed: %s", e)
            return None
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _stt_callable(self) -> Optional[Callable[[str], Optional[str]]]:
        """Resolve the configured STT engine → ``wav_path → transcript`` callable.

        ``stt.engine: qwen3asr_server`` wraps the HTTP engine with a fallback to
        ``stt.fallback_engine`` (default ``whispercpp``) so a down GPU server degrades to a
        local engine with a warning instead of dropping the utterance.
        """
        engine = (self.config.stt_engine or "hermes").strip().lower()
        primary = _ENGINE_TRANSCRIBERS.get(engine)
        if primary is None:
            primary = _hermes_transcribe
        fb_name = (self.config.stt_fallback_engine or "").strip().lower()
        fb = _ENGINE_TRANSCRIBERS.get(fb_name) if fb_name and fb_name != engine else None
        if fb is None:
            return lambda path: primary(self.config, path)

        def _with_fallback(path: str) -> Optional[str]:
            try:
                text = primary(self.config, path)
            except Exception:
                text = None
            if text is not None:
                return text
            self.stats["stt_fallback_calls"] += 1
            log.warning("hermes-omni: STT engine %r failed; falling back to %r", engine, fb_name)
            return fb(self.config, path)

        return _with_fallback

    def _speaker_authorized(self, speaker_id: str) -> bool:
        """Fail-closed allowlist gate on the adapter.

        Runs before STT so unauthorized audio is never transcribed or dispatched.
        """
        authorize = getattr(self.adapter, "_is_user_authorized", None)
        if not callable(authorize):
            log.warning("hermes-omni: adapter has no authorization check; allowing speaker %r "
                        "(authz missing)", speaker_id)
            return True
        try:
            return bool(authorize(str(speaker_id)))
        except Exception:
            log.exception("hermes-omni: authorization check failed; dropping transcript")
            return False

    # ── transcript routing ───────────────────────────────────────────────

    async def _echo_transcript(self, text: str, speaker_id: str) -> None:
        """``transcripts: channel`` — post the STT text into the bound channel.

        Uses the ``send_transcript`` callable on the session when available,
        otherwise falls back to the adapter's REST client.
        """
        chat_id = self.session.echo_chat_id
        send_fn = getattr(self.session, "send_transcript", None)
        if callable(send_fn):
            try:
                await send_fn(text, speaker_id)
                return
            except Exception as e:
                log.warning("hermes-omni: send_transcript callable failed: %s", e)
        # Fallback: adapter REST path
        rest = getattr(self.adapter, "_rest", None)
        if not chat_id or rest is None:
            log.debug("hermes-omni: transcript echo skipped (no postable bound channel)")
            return
        try:
            await rest.create_message(
                chat_id, content=f"**[Voice]** <@{speaker_id}>: {text[:1900]}")
        except Exception as e:
            log.warning("hermes-omni: transcript echo failed in %s: %s", chat_id, e)

    def _voice_input_callback_bound(self) -> bool:
        """True when the core wired ``/voice join`` (its full pipeline handles the turn)."""
        callback = getattr(self.adapter, "_voice_input_callback", None)
        if not callable(callback):
            return False
        try:
            return bool((getattr(self.adapter, "_voice_text_channels", {}) or {})
                        .get(int(self.session.guild_id)))
        except Exception:
            return False

    async def _dispatch(self, text: str, speaker_id: str, speaker_name: str) -> None:
        """Hand the transcript to the agent through the adapter's normal pipeline."""
        adapter = self.adapter
        if self._voice_input_callback_bound():
            self.dispatched_via = "core_callback"
            self.stats["dispatched"] += 1
            callback = adapter._voice_input_callback
            guild_id: Any = int(self.session.guild_id)
            user_id: Any = int(speaker_id) if str(speaker_id).isdigit() else speaker_id
            await callback(guild_id=guild_id, user_id=user_id, transcript=text)
            return
        self.dispatched_via = "handle_message"
        self.stats["dispatched"] += 1
        source = self.session.build_source(adapter, speaker_id, speaker_name)
        event = _build_event(source=source, text=text, guild_id=self.session.guild_id,
                             channel_prompt=self._voice_input_prompt())
        await adapter.handle_message(event)

    def _voice_input_prompt(self) -> Optional[str]:
        """Voice-mode preamble for the dispatched turn (``voice.input_prompt``; None = off).

        The core callback path resolves the same text via
        ``adapter._resolve_channel_prompt`` — keep both spellings working.
        """
        resolver = getattr(self.adapter, "_voice_input_prompt_for_chat", None)
        if not callable(resolver):
            return None
        try:
            return resolver(self.session.echo_chat_id) or None
        except Exception:
            log.debug("hermes-omni: voice input-prompt resolution failed", exc_info=True)
            return None

    def snapshot(self) -> dict:
        out = dict(self.stats)
        out.update({
            "speaking": self.segmenter.speaking,
            "last_transcript": self.last_transcript,
            "dispatched_via": self.dispatched_via,
            "pending_bytes": len(self.segmenter._pending),
        })
        return out


def _build_event(*, source, text: str, guild_id: str, channel_prompt: Optional[str] = None):
    """Synthetic event for the agent pipeline.

    Uses a SimpleNamespace for duck-typed compatibility — the calling adapter
    should handle the actual message dispatch.
    """
    msg = SimpleNamespace(guild_id=str(guild_id), guild=None)
    return SimpleNamespace(
        text=text,
        message_type="TEXT",
        source=source,
        channel_prompt=channel_prompt,
        raw_message=msg,
    )


# ── STT engines ──────────────────────────────────────────────────────────────

_HALLUCINATION_MIN_WORDS = 1
_ASR_SERVER_INSTRUCTION = "Transcribe this audio."

#: Engine name → (config, wav_path) → Optional[str]  (see :meth:`_stt_callable`).
_ENGINE_TRANSCRIBERS: dict[str, Any] = {}


def _is_hallucination(text: str) -> bool:
    """Reuse Hermes's whisper-hallucination filter when importable."""
    try:
        from tools.voice_mode import is_whisper_hallucination
        return bool(is_whisper_hallucination(text))
    except Exception:
        return False


# Registered at module bottom after every transcriber function is defined.


def _qwen3asr_server_transcribe(config: VoiceConfig, wav_path: str) -> Optional[str]:
    """GPU Qwen3-ASR server engine: POST the WAV as ``input_audio`` to a warm
    llama-server HTTP endpoint (``stt.server_url``, default ``http://127.0.0.1:8105``).
    Returns None on any failure so the cascade falls back to ``stt.fallback_engine``.
    """
    server = (config.stt_server_url or "").rstrip("/")
    if not server:
        log.warning("hermes-omni: ASR server URL not set — dropping request")
        return None
    try:
        raw = Path(wav_path).read_bytes()
    except OSError as e:
        log.warning("hermes-omni: ASR server read failed: %s", e)
        return None
    payload = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(raw).decode("ascii")}},
                {"type": "text", "text": _ASR_SERVER_INSTRUCTION},
            ],
        }],
        "max_tokens": 256,
        "temperature": 0.0,
    }
    request = urllib.request.Request(
        f"{server}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.stt_timeout_s) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as e:
        log.warning("hermes-omni: ASR server %s request failed: %s", server, e)
        return None
    transcript = _parse_asr_server_text(body)
    if not transcript:
        pass
    return transcript


def _parse_asr_server_text(payload: Any) -> Optional[str]:
    """Extract the transcript from a llama-server Qwen3-ASR response."""
    try:
        content = payload["choices"][0]["message"]["content"]
    except (TypeError, KeyError, IndexError):
        return None
    if not isinstance(content, str):
        return None
    # The model marks its transcript with ``language <lang><asr_text>``.
    if "<asr_text>" in content:
        content = content.split("<asr_text>", 1)[1]
    else:
        content = re.sub(r"^\s*language\s+\w+\s*", "", content, flags=re.IGNORECASE)
    return content.strip()


# Register the ASR server engine after its function is defined.
_ENGINE_TRANSCRIBERS["qwen3asr_server"] = _qwen3asr_server_transcribe


def _hermes_transcribe(config: VoiceConfig, wav_path: str) -> Optional[str]:
    """Hermes's own transcription path (``stt.provider: local`` → faster-whisper).

    Runs in a worker thread (the caller wraps this in ``to_thread``); the model
    is cached process-wide by ``tools.transcription_tools``.  Returns None on
    any failure (logged by the tools module).
    """
    try:
        from tools.transcription_tools import transcribe_audio
    except Exception as e:
        log.warning("hermes-omni: Hermes transcription path unavailable: %s", e)
        return None
    try:
        result = transcribe_audio(wav_path, model=config.stt_model, source="hermes_omni_transport")
    except Exception as e:
        log.warning("hermes-omni: Hermes transcription raised: %s", e)
        return None
    if not isinstance(result, dict) or not result.get("success"):
        log.warning("hermes-omni: Hermes transcription failed: %s",
                    (result or {}).get("error", "unknown error") if isinstance(result, dict) else result)
        return None
    return str(result.get("transcript") or "").strip() or None


def _whispercpp_transcribe(config: VoiceConfig, wav_path: str) -> Optional[str]:
    """whisper.cpp CLI engine (offline fallback): ``whisper-cli -m <ggml> -f <wav>``."""
    binary = Path(config.stt_binary)
    model = Path(config.stt_model_path)
    if not binary.is_file():
        log.error("hermes-omni: whisper.cpp binary not found at %s", binary)
        return None
    if not model.is_file():
        log.error("hermes-omni: whisper.cpp model not found at %s", model)
        return None
    tmp_dir = Path(tempfile.mkdtemp(prefix="hermes-omni-whispercpp-"))
    try:
        out_base = tmp_dir / "out"
        command = [
            str(binary), "-m", str(model), "-f", str(wav_path),
            "-t", str(config.stt_threads), "-l", config.stt_language,
            "--output-txt", "--output-file", str(out_base),
        ]
        proc = subprocess.run(command, capture_output=True, timeout=120)
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
            log.warning("hermes-omni: whisper.cpp exited %s: %s", proc.returncode, " | ".join(tail))
            return None
        transcript_path = out_base.with_suffix(".txt")
        if transcript_path.is_file():
            return transcript_path.read_text(encoding="utf-8", errors="replace").strip() or None
        # Fallback: parse stdout ("[00:00:00.000 --> ...]  text").
        text = re.sub(r"\[[^\]]*\]", " ", (proc.stdout or b"").decode("utf-8", "replace"))
        return " ".join(text.split()).strip() or None
    except subprocess.TimeoutExpired:
        log.warning("hermes-omni: whisper.cpp timed out")
        return None
    except Exception as e:
        log.warning("hermes-omni: whisper.cpp failed: %s", e)
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── engine registry (populated after all transcriber functions are defined) ──

_ENGINE_TRANSCRIBERS["hermes"] = _hermes_transcribe
_ENGINE_TRANSCRIBERS["whispercpp"] = _whispercpp_transcribe
_ENGINE_TRANSCRIBERS["qwen3asr_server"] = _qwen3asr_server_transcribe