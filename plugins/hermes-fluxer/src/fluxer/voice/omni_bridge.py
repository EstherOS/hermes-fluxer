"""OmniVoiceBridge — route LiveKit audio through hermes_omni Session (component graph v2).

The bridge owns the VAD segmenter and drives the session's ``feed_audio()`` /
``finalize_utterance()`` lifecycle.  Transcripts produced by the session's ASR
pipeline are delivered to the adapter as ``MessageEvent`` so the agent sees
them as a user turn.

The bridge does **not** own the LiveKit connection — it expects an active
:class:`~fluxer.voice.controller.VoiceSession` that provides the ``rtc`` module,
the ``room``, and a speaker ``AudioSource``.  The :class:`VoiceController` creates
an instance per voice channel and calls :meth:`start` after connecting the room.
"""

from __future__ import annotations

import array
import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from . import audio as audio_lib
from .config import VoiceConfig

log = logging.getLogger("fluxer.voice.omni_bridge")


class OmniVoiceBridge:
    """Connects a LiveKit voice channel to a hermes_omni :class:`~hermes_omni.session.Session`.

    Parameters
    ----------
    adapter
        The :class:`~fluxer.adapter.FluxerAdapter` instance (used for transcript
        dispatch and authorization).
    session
        A *started* :class:`~hermes_omni.session.Session` whose backends are
        already resolved and graph routes wired.
    voice_session
        The :class:`~fluxer.voice.controller.VoiceSession` for this guild/channel
        (provides ``rtc``, ``room``, and the speaker ``AudioSource``).
    config
        :class:`VoiceConfig` — VAD params, transcript mode, etc.
    """

    def __init__(
        self,
        adapter,
        session,
        voice_session,
        config: VoiceConfig,
    ) -> None:
        self.adapter = adapter
        self.session = session
        self.voice_session = voice_session
        self.config = config

        self._closed = False
        self._tasks: set[asyncio.Task] = set()

        # VAD segmenter
        self.segmenter = audio_lib.VadSegmenter(
            silence_ms=config.silence_ms,
            min_utterance_ms=config.min_utterance_ms,
            max_utterance_s=config.max_utterance_s,
            energy_threshold=config.energy_threshold,
        )

        # Last completed utterance (for unified file-based ASR flush)
        self._last_utterance: Any = None

        # Output audio pipeline: accumulate output-stream chunks, resample to
        # 48 kHz, publish as 20 ms frames to the LiveKit AudioSource.
        self._output_source: Any = None
        self._output_buffer = array.array("h")

        # Transcript dispatch — speaker context from the most recent mic track
        self._speaker_id: str | None = None
        self._speaker_name: str | None = None

        # Persistent streaming session (CrispASR --stream mode)
        self._stream_session: Any = None
        self._stream_started: bool = False

        self.stats: dict[str, int] = {
            "frames": 0,
            "utterances": 0,
            "transcripts": 0,
            "audio_out_chunks": 0,
            "audio_out_frames": 0,
            "vad_opens": 0,
            "vad_closes": 0,
            "interruptions": 0,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Activate the bridge.

        Spawns an async task that: starts the omni Session, hooks transcript
        delivery, kicks off the output pipeline, and — in unified streaming
        mode — opens a persistent streaming session.
        """
        self._closed = False
        self._spawn(self._start_async())
        # Register this bridge as the cascade handler so the controller
        # routes new participants' track_subscribed events to us.
        self.voice_session.cascade = self
        log.info(
            "OmniVoiceBridge starting  guild=%s  channel=%s",
            self.voice_session.guild_id,
            self.voice_session.channel_id,
        )

    async def _start_async(self) -> None:
        """Async bootstrap: start the omni Session, hook graph, start I/O."""
        # 1. Start the omni Session (resolves backends, wires graph routes)
        if not self.session._running:
            await self.session.start()
        # 2. Hook graph transcript delivery
        self._hook_graph()
        # 3. Output pipeline
        self._spawn(self._run_output())
        # 4. Unified streaming mode — open persistent session
        if self._is_streaming_mode():
            await self._run_stream_session()
        # 5. Subscribe to existing participants' tracks
        try:
            from livekit.rtc import TrackKind

            AUDIO_KIND = TrackKind.KIND_AUDIO
        except Exception:
            AUDIO_KIND = "audio"
        try:
            room = getattr(self.voice_session, "room", None)
            if room is not None:
                participants = getattr(room, "remote_participants", None) or {}
                for pid, participant in participants.items():
                    pubs = list(
                        getattr(participant, "track_publications", None) or {}.values()
                    )
                    for pub in pubs:
                        if getattr(pub, "kind", None) == AUDIO_KIND:
                            track = getattr(pub, "track", None)
                            if track is not None:
                                self.on_track_subscribed(track, pub, participant)
        except Exception as exc:
            log.warning(
                "OmniVoiceBridge: scanning existing participants failed: %s", exc
            )
        log.info(
            "OmniVoiceBridge started  guild=%s  channel=%s",
            self.voice_session.guild_id,
            self.voice_session.channel_id,
        )

    async def stop(self) -> None:
        """Deactivate the bridge, cancel every background task, flush output."""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        self._output_buffer = array.array("h")
        log.info(
            "OmniVoiceBridge stopped  guild=%s  channel=%s",
            self.voice_session.guild_id,
            self.voice_session.channel_id,
        )

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── graph hooking ─────────────────────────────────────────────────────

    def _hook_graph(self) -> None:
        """Hook the session's graph-mode transcript callback so the bridge
        can deliver transcripts to the adapter.

        Sets ``session._graph_transcript_callback`` — the session calls it
        from :meth:`finalize_utterance` when a transcript is ready.
        """
        async def _on_graph_transcript(text: str) -> None:
            await self._deliver_transcript(text)

        self.session._graph_transcript_callback = _on_graph_transcript

    # ── inbound: LiveKit mic → VAD → session.feed_audio() ───────────────────

    def on_track_subscribed(
        self, track, publication=None, participant=None
    ) -> None:
        """Start reading a LiveKit audio track.

        Only processes audio tracks; video and subtitle tracks are ignored.
        Extracts speaker identity from the LiveKit participant.
        """
        rtc = getattr(self.voice_session, "rtc", None)
        if rtc is None:
            return

        # Filter to audio tracks only
        try:
            kind = getattr(track, "kind", None)
            if kind is not None and hasattr(rtc, "TrackKind"):
                if kind != rtc.TrackKind.KIND_AUDIO:
                    log.debug("OmniVoiceBridge: skipping non-audio track %s", kind)
                    return
        except Exception:
            pass  # unknown stub shapes: assume audio

        from .cascade import speaker_from_identity

        identity = getattr(participant, "identity", "") if participant else ""
        speaker_id, speaker_name = speaker_from_identity(identity)
        self._speaker_id = speaker_id
        self._speaker_name = speaker_name

        log.debug(
            "OmniVoiceBridge: subscribed audio track for %s (%s)",
            speaker_name or speaker_id, speaker_id,
        )
        self._spawn(self._read_mic_track(track, speaker_id, speaker_name))

    async def _read_mic_track(
        self, track, speaker_id: str, speaker_name: str,
    ) -> None:
        """Read PCM frames from a LiveKit ``AudioStream`` in a continuous loop.

        Every received 20 ms frame (48 kHz mono int16):
        1. Is fed to :meth:`session.feed_audio` for ASR.
        2. Is routed through :attr:`segmenter` for energy-based VAD.
        3. Drives utterance finalization on VAD close.
        """
        rtc = self.voice_session.rtc
        if rtc is None:
            return

        stream = rtc.AudioStream(
            track, sample_rate=audio_lib.SAMPLE_RATE, num_channels=1,
        )

        # Track VAD speaking state across frames
        was_speaking = self.segmenter.speaking

        try:
            async for event in stream:
                if self._closed:
                    break

                frame = getattr(event, "frame", event)
                data = bytes(getattr(frame, "data", b""))
                self.stats["frames"] += 1

                # 1. Feed raw PCM to the session
                await self._safe_feed_audio(data)

                # 2. VAD segmentation
                utterances = self.segmenter.feed(data)
                now_speaking = self.segmenter.speaking

                # 2a. VAD open: speech started
                if not was_speaking and now_speaking:
                    await self._on_vad_open()

                # 2b. VAD close: utterance completed
                for _utterance in utterances:
                    self._last_utterance = _utterance
                    await self._on_vad_close()

                was_speaking = now_speaking

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "OmniVoiceBridge: mic stream ended for %s: %s",
                speaker_name or speaker_id, e,
            )
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass

    # ── VAD ─────────────────────────────────────────────────────────────────

    async def _on_vad_open(self) -> None:
        """Speech started — audio is already flowing via ``feed_audio``."""
        self.stats["vad_opens"] += 1
        log.debug("OmniVoiceBridge: VAD open (audio already flowing)")

    async def _on_vad_close(self) -> None:
        """Speech ended — finalize the utterance.

        Calls ``session.finalize_utterance()`` which signals end-of-stream
        to the ASR backend, waits for the transcript, and delivers it via
        the graph transcript callback.
        """
        self.stats["vad_closes"] += 1
        await self.session.finalize_utterance()
        # Flush the streaming session if present
        if (
            self._stream_session is not None
            and hasattr(self._stream_session, "flush")
        ):
            await self._safe_flush(self._stream_session)

    # ── session.feed_audio() wrapper ────────────────────────────────────────

    async def _safe_feed_audio(self, data: bytes) -> None:
        """Forward PCM bytes to the session (and streaming backend if active)."""
        # 1. Feed the session
        try:
            await self.session.feed_audio(data)
        except Exception as e:
            log.warning("OmniVoiceBridge: feed_audio failed: %s", e)
        # 2. If a persistent streaming session is open and started, feed raw PCM to it
        if self._stream_session is not None and self._stream_started:
            try:
                await self._stream_session.feed_audio(data)
            except Exception as e:
                log.warning("OmniVoiceBridge: stream feed_audio failed: %s", e)

    @staticmethod
    async def _safe_flush(session) -> None:
        """Call ``flush()`` on a streaming session with error isolation."""
        try:
            await session.flush()
        except Exception as e:
            log.warning("OmniVoiceBridge: flush failed: %s", e)

    # ── persistent streaming session (unified mode, no temp files) ───────────

    def _is_streaming_mode(self) -> bool:
        """True when the session has a realtime backend for streaming."""
        return getattr(self.session, "_realtime_backend", None) is not None

    async def _run_stream_session(self) -> None:
        """Open a persistent streaming session on the realtime backend.

        One subprocess, one model load.  Raw PCM chunks are piped via
        ``feed_audio()`` into the binary's stdin.  Transcripts (JSON-Line
        ``final`` events) are read from ``receive()`` and delivered to the
        adapter via ``_graph_transcript_callback``.
        """
        backend = getattr(self.session, "_realtime_backend", None)
        if backend is None:
            return
        try:
            self._stream_session = await backend.open_session()
            await self._stream_session.open()
            self._stream_started = True
            log.info("OmniVoiceBridge: streaming session opened on %s", backend.name)
        except Exception as exc:
            log.warning(
                "OmniVoiceBridge: failed to open streaming session: %s", exc
            )
            self._stream_session = None
            return

        try:
            async for part in self._stream_session.receive():
                if self._closed:
                    break
                if part.is_text:
                    text = part.text_of()
                    if text:
                        self.stats["transcripts"] += 1
                        # Deliver via graph transcript callback
                        if self.session._graph_transcript_callback is not None:
                            await self.session._graph_transcript_callback(text)
                else:
                    audio_data = part.data
                    if isinstance(audio_data, bytes) and audio_data:
                        try:
                            await self.session._output_queue.put(audio_data)
                        except Exception as e:
                            log.warning(
                                "OmniVoiceBridge: audio output push failed: %s", e,
                            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "OmniVoiceBridge: streaming session output ended: %s", exc
            )
        finally:
            try:
                await self._stream_session.close()
            except Exception:
                pass
            self._stream_session = None
            self._stream_started = False
            log.info("OmniVoiceBridge: streaming session closed")

    # ── outbound: session.output_stream() → LiveKit speaker ─────────────────

    async def _run_output(self) -> None:
        """Background task: drain ``session.output_stream()`` and publish audio
        frames to the LiveKit ``AudioSource``.
        """
        FRAME_CAP = audio_lib.FRAME_SAMPLES * 16000 // audio_lib.SAMPLE_RATE  # 320

        try:
            async for chunk in self.session.output_stream():
                if self._closed or not chunk:
                    break

                self.stats["audio_out_chunks"] += 1

                samples = audio_lib.bytes_to_samples(chunk)
                self._output_buffer.extend(samples)

                while len(self._output_buffer) >= FRAME_CAP:
                    frame_16k = self._output_buffer[:FRAME_CAP]
                    del self._output_buffer[:FRAME_CAP]

                    frame_48k = audio_lib.resample(
                        frame_16k, 16000, audio_lib.SAMPLE_RATE,
                    )
                    await self._publish_frame(frame_48k)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("OmniVoiceBridge: output stream error: %s", e)
        finally:
            if self._output_buffer:
                remaining = self._output_buffer
                self._output_buffer = array.array("h")
                rem_48k = audio_lib.resample(
                    remaining, 16000, audio_lib.SAMPLE_RATE,
                )
                for frame in audio_lib.pad_frames(rem_48k):
                    await self._publish_frame(frame)

    async def _publish_frame(self, samples: array.array) -> None:
        """Publish one 48 kHz frame to the LiveKit ``AudioSource``."""
        source = self._output_source or self.voice_session.source

        if source is None:
            source = await self._ensure_output_source()
            if source is None:
                return

        rtc = self.voice_session.rtc
        if rtc is None:
            return

        try:
            frame = rtc.AudioFrame.create(
                audio_lib.SAMPLE_RATE, 1, len(samples),
            )
            audio_lib.put_samples(frame, samples)
            await source.capture_frame(frame)
            self.stats["audio_out_frames"] += 1
        except Exception as e:
            log.warning("OmniVoiceBridge: publish frame failed: %s", e)

    async def _ensure_output_source(self) -> Any:
        """Create a LiveKit ``AudioSource`` and publish the speaker track.

        Mirrors :meth:`VoiceController._ensure_publisher`.  Stores the source
        on ``voice_session`` so the Bridge and the adapter's ``speak()`` path
        (when it falls back to legacy TTS) can share it.
        """
        rtc = self.voice_session.rtc
        room = self.voice_session.room
        if rtc is None or room is None:
            return None

        try:
            source = rtc.AudioSource(audio_lib.SAMPLE_RATE, 1)
            track = rtc.LocalAudioTrack.create_audio_track("hermes-voice", source)
            options = rtc.TrackPublishOptions()
            options.source = rtc.TrackSource.SOURCE_MICROPHONE

            publication = await asyncio.wait_for(
                room.local_participant.publish_track(track, options),
                timeout=15.0,
            )

            self.voice_session.source = source
            self.voice_session.track = track
            self.voice_session.publication = publication
            self.voice_session.publication_sid = str(
                getattr(publication, "sid", "") or ""
            ) or None
            self._output_source = source

            log.info(
                "OmniVoiceBridge: published speaker track sid=%s",
                self.voice_session.publication_sid,
            )
            return source

        except Exception as e:
            log.warning("OmniVoiceBridge: publish speaker track failed: %s", e)
            return None

    # ── transcript delivery ─────────────────────────────────────────────────

    async def _deliver_transcript(self, text: str) -> None:
        """Deliver the user's transcript as a ``MessageEvent`` to the adapter.

        Mirrors :meth:`~fluxer.voice.cascade.VoiceCascade._dispatch`:

        1. If ``voice.transcripts == \\\"channel\\\"``, echo the text into the bound
           channel.
        2. If the adapter has a ``_voice_input_callback`` (core voice-input
           path), call it directly.
        3. Otherwise build a synthetic ``MessageEvent`` and hand it to
           ``adapter.handle_message``.
        """
        text = (text or "").strip()
        if not text:
            return

        self.stats["transcripts"] += 1

        speaker_id = self._speaker_id or "?"
        speaker_name = self._speaker_name or speaker_id

        log.info(
            "OmniVoiceBridge: transcript from %s  (guild=%s): %.120s",
            speaker_name,
            self.voice_session.guild_id,
            text,
        )

        # Echo to the bound chat channel when configured
        if self.config.transcripts == "channel":
            await self._echo_transcript(text, speaker_id)

        adapter = self.adapter

        # Core voice-input callback path (``/voice join`` full pipeline)
        callback = getattr(adapter, "_voice_input_callback", None)
        if callable(callback):
            try:
                guild_id = int(self.voice_session.guild_id)
                user_id = (
                    int(speaker_id)
                    if str(speaker_id).isdigit()
                    else speaker_id
                )
                await callback(
                    guild_id=guild_id, user_id=user_id, transcript=text,
                )
            except Exception as e:
                log.warning(
                    "OmniVoiceBridge: voice input callback failed: %s", e,
                )
            return

        # Standard MessageEvent dispatch
        source = self.voice_session.build_source(
            adapter, speaker_id, speaker_name,
        )
        event = self._build_event(
            text=text,
            source=source,
            guild_id=self.voice_session.guild_id,
        )
        try:
            await adapter.handle_message(event)
        except Exception as e:
            log.warning(
                "OmniVoiceBridge: handle_message failed for transcript: %s", e,
            )

    async def _echo_transcript(self, text: str, speaker_id: str) -> None:
        """Post the transcript into the bound text channel (transcripts mode)."""
        chat_id = self.voice_session.echo_chat_id
        rest = getattr(self.adapter, "_rest", None)
        if not chat_id or rest is None:
            return
        try:
            await rest.create_message(
                chat_id,
                content=f"**[Voice]** <@{speaker_id}>: {text[:1900]}",
            )
        except Exception as e:
            log.warning(
                "OmniVoiceBridge: transcript echo failed in %s: %s",
                chat_id, e,
            )

    @staticmethod
    def _build_event(
        *, text: str, source, guild_id: str,
    ) -> Any:
        """Build a synthetic ``MessageEvent`` for the agent pipeline.

        Lazy-imports ``MessageEvent`` so a minimal import of this module
        doesn't pull in the gateway package.
        """
        from gateway.platforms.event import MessageEvent, MessageType

        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=SimpleNamespace(
                guild_id=str(guild_id), guild=None,
            ),
        )

    # ── snapshot ────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Current stats and bridge state."""
        out = dict(self.stats)
        out.update({
            "closed": self._closed,
            "segmenter_speaking": getattr(self.segmenter, "speaking", False),
            "speaker_id": self._speaker_id or "",
            "output_buffer_samples": len(self._output_buffer),
        })
        return out