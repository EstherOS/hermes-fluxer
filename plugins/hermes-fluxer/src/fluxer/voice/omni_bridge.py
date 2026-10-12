"""OmniVoiceBridge — platform audio I/O for the omni engine graph.

Routes LiveKit mic audio into the graph via ``@fluxer.audio`` and plays
output from ``@fluxer.audio_out`` back to the LiveKit speaker track.

The bridge owns audio I/O only — never the turn pipeline. Everything
after audio enters the graph is the graph's responsibility, defined by
profile routes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from pathlib import Path
from typing import Any, Callable

from omnimaker.runtime import Session as OmniSession
from omnimaker.types import Envelope

log = logging.getLogger(__name__)

MIN_UTTERANCE_SEC = 0.3


class OmniVoiceBridge:
    """Bridges a LiveKit voice room to an omni Session graph.

    Compatible with the controller's ``session.cascade`` slot.
    """

    def __init__(self, adapter: Any, omni_session: OmniSession,
                 voice_session: Any, config: Any) -> None:
        self.adapter = adapter
        self.omni_session = omni_session
        self.voice_session = voice_session
        self.config = config

        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self._output_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)
        self._turn_task: asyncio.Task | None = None
        self._turn_id: str | None = None

        # Controller-set callbacks
        self.on_audio_out: Callable[[bytes], None] | None = None
        self.on_transcript: Callable[[str, str, str], None] | None = None
        self.on_stop_playback: Callable[[], None] | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._spawn(self._run_async_start())
        self._spawn(self._run_output())

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        # Detach output handler from binding
        binding = self.omni_session.get_binding("fluxer")
        if binding and hasattr(binding, 'detach_output_handler'):
            binding.detach_output_handler(self.omni_session.id)
        await self.omni_session.stop()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run_async_start(self) -> None:
        try:
            await self.omni_session.start()
            log.info("OmniVoiceBridge: session %s started", self.omni_session.id)
            # Wire @fluxer.audio_out → our queue
            binding = self.omni_session.get_binding("fluxer")
            if binding and hasattr(binding, 'attach_output_handler'):
                await binding.open(self.omni_session.id, {})
                binding.attach_output_handler(self.omni_session.id, self._push_output)
        except Exception as exc:
            log.error("OmniVoiceBridge: session start failed: %s", exc)

    # ── Input: LiveKit AudioStream → VAD → WAV → graph ────────────────

    def on_track_subscribed(self, track: Any, publication: Any,
                             participant: Any) -> None:
        """Subscribe to a remote participant's mic track."""
        rtc = getattr(self.voice_session, "rtc", None)
        if rtc is None:
            log.warning("OmniVoiceBridge: on_track_subscribed but voice_session.rtc is None")
            return
        try:
            kind = getattr(track, "kind", None)
            if kind is not None and hasattr(rtc, "TrackKind"):
                if kind != rtc.TrackKind.KIND_AUDIO:
                    return
        except Exception:
            pass
        from fluxer.voice.audio import SAMPLE_RATE
        pid = getattr(participant, "identity", "?")
        log.info("OmniVoiceBridge: starting AudioStream for %s", pid)
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        self._spawn(self._read_stream(stream, participant))

    async def _read_stream(self, stream: Any, participant: Any) -> None:
        """Read AudioStream frames, run VAD, feed utterances to graph."""
        from fluxer.voice.audio import VadSegmenter, write_wav

        segmenter = VadSegmenter()
        try:
            async for event in stream:
                if not self._running:
                    break
                frame = getattr(event, "frame", event)
                data = bytes(getattr(frame, "data", b""))
                if not data:
                    continue
                for utterance in segmenter.feed(data):
                    await self._feed_utterance(utterance, participant)
                if segmenter.take_onset():
                    self._on_speech_onset()
            final = segmenter.flush()
            if final is not None:
                await self._feed_utterance(final, participant)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("OmniVoiceBridge: audio stream ended: %s", e)
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass

    async def _feed_utterance(self, utterance: Any, participant: Any) -> None:
        """Write utterance WAV, feed into graph via @fluxer.audio."""
        if utterance.seconds < MIN_UTTERANCE_SEC:
            return
        tmp_dir = Path(tempfile.mkdtemp(prefix="omni-utt-"))
        wav_path = tmp_dir / "utt.wav"
        try:
            from fluxer.voice.audio import write_wav
            write_wav(wav_path, utterance.samples, rate=48000)
            wav_bytes = wav_path.read_bytes()
        except Exception as exc:
            log.warning("OmniVoiceBridge: WAV write failed: %s", exc)
            self._rmtree(tmp_dir)
            return
        # Feed into graph as a single envelope
        env = Envelope(
            type="audio",
            payload=wav_bytes,
            session_id=self.omni_session.id,
            turn_id="",
            execution_id="",
            source="@fluxer.audio",
        )
        # Feed as a tracked task so the mic read loop keeps running (barge-in
        # detection depends on it); interrupt() and stop() cancel this task.
        self._turn_task = self._spawn(self._run_feed(env))
        self._rmtree(tmp_dir)

    async def _run_feed(self, env: Envelope) -> None:
        try:
            self._turn_id = await self.omni_session.feed("@fluxer.audio", env)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("OmniVoiceBridge: graph feed failed: %s", exc)

    def _on_speech_onset(self) -> None:
        """User started speaking — flush local playback and signal the graph.

        The graph decides what stops (routes from ``@fluxer.speech`` to
        control endpoints, e.g. ``brain.cancel`` / ``mouth.cancel``); the
        bridge flushes only its own playback queue so the speaker goes quiet
        immediately.
        """
        self._spawn(self._signal_speech())

    async def _signal_speech(self) -> None:
        dropped = 0
        while True:
            try:
                self._output_queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if callable(self.on_stop_playback):
            try:
                self.on_stop_playback()
            except Exception:
                log.exception("OmniVoiceBridge: stop_playback callback failed")
        try:
            await self.omni_session.feed("@fluxer.speech", Envelope(
                type="speech",
                payload=None,
                session_id=self.omni_session.id,
                turn_id="",
                execution_id="",
                source="@fluxer.speech",
            ))
        except Exception as exc:
            log.error("OmniVoiceBridge: speech signal feed failed: %s", exc)
        log.info("OmniVoiceBridge: speech onset — flushed %d segment(s), graph signalled",
                 dropped)

    async def interrupt(self, reason: str = "barge") -> None:
        """Stop the in-flight turn and drop queued playback (barge-in).

        Uses the engine's interrupt verb (cancels the turn's executions —
        adapter cleanup runs: streams close, subprocesses die); falls back to
        cancelling the turn task if the engine call fails. Queued output
        segments are dropped via the controller's stop_playback callback.
        """
        try:
            cancelled = await self.omni_session.interrupt()
            log.info("OmniVoiceBridge: interrupted %d execution(s) (%s)",
                     cancelled, reason)
        except Exception:
            log.exception("OmniVoiceBridge: engine interrupt failed — cancelling turn task")
            task = self._turn_task
            self._turn_task = None
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        dropped = 0
        while True:
            try:
                self._output_queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if callable(self.on_stop_playback):
            try:
                self.on_stop_playback()
            except Exception:
                log.exception("OmniVoiceBridge: stop_playback callback failed")
        log.info("OmniVoiceBridge: interrupted (%s) — turn cancelled, %d segment(s) dropped",
                 reason, dropped)

    # ── Output: graph @fluxer.audio_out → queue → LiveKit speaker ──────

    def _push_output(self, envelope: Envelope) -> None:
        """Called by FluxerBinding when audio hits @fluxer.audio_out."""
        if isinstance(envelope.payload, bytes):
            try:
                self._output_queue.put_nowait(envelope.payload)
            except asyncio.QueueFull:
                log.warning("OmniVoiceBridge: output queue full")

    async def _run_output(self) -> None:
        """Drain output queue, push PCM to LiveKit speaker track."""
        while self._running:
            try:
                pcm = await asyncio.wait_for(self._output_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            # If the controller wired a callback, use it
            if self.on_audio_out:
                try:
                    result = self.on_audio_out(pcm)
                    if result is not None and hasattr(result, '__await__'):
                        await result
                    continue
                except Exception as exc:
                    log.error("OmniVoiceBridge: audio output error: %s", exc)
                    continue

            # No callback: push directly to the session's LiveKit AudioSource
            source = getattr(self.voice_session, "source", None)
            if source is not None:
                try:
                    import array
                    samples = array.array("h", memoryview(pcm).cast("h"))
                    from fluxer.voice.audio import FRAME_SAMPLES, pad_frames
                    for frame in pad_frames(samples):
                        source.capture_frame(frame)
                    continue
                except Exception as exc:
                    log.warning("OmniVoiceBridge: direct push failed: %s", exc)
                    continue

            # Neither callback nor AudioSource — audio lost
            log.warning(
                "OmniVoiceBridge: audio output lost — no on_audio_out callback "
                "and no voice_session.source. Caller must set on_audio_out, or "
                "the session must have an AudioSource after join."
            )

    # ── Test seam ──────────────────────────────────────────────────────

    async def feed_pcm(self, pcm_bytes: bytes) -> None:
        """Direct PCM injection for offline testing (bypasses LiveKit)."""
        from fluxer.voice.audio import VadSegmenter
        segmenter = VadSegmenter()
        for utterance in segmenter.feed(pcm_bytes):
            await self._feed_utterance(utterance, None)

    # ── Helper ─────────────────────────────────────────────────────────

    @staticmethod
    def _rmtree(path: Path) -> None:
        import shutil
        shutil.rmtree(path, ignore_errors=True)