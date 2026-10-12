"""LiveKit video publish seam (spec §6 W4).

``publish_frames_livekit`` publishes a sequence of :class:`~fluxer.video.duplex.Frame`
as a camera track on a LiveKit room: build ``rtc.VideoSource`` → ``LocalVideoTrack
.create_video_track`` → ``publish_track`` → ``capture_frame`` per frame at ``fps``
→ collect track stats → unpublish + disconnect.

The livekit import is lazy (inside the function).  This module is **not
live-tested in wave 4** — no Fluxer voice-channel joins during the C4b run (a
second join would kick it) — it is unit-tested against a fake ``livekit``
module.  The wire-level publish path itself is proven by the C4a voice probe
(``status/c4a-report.md`` §2.7-2.8: audio frames over the same API family,
``candidate_pair: PAIR_SUCCEEDED``, packets on the wire).  The integration test
that joins a channel and publishes real video frames is deferred to the voice
wave.

API verified against the installed ``livekit==1.1.18`` before coding:
``VideoSource(width, height)`` / ``LocalVideoTrack.create_video_track(name, source)``
/ ``rtc.VideoFrame(w, h, rtc.VideoBufferType.RGB24, data)`` /
``source.capture_frame(frame, timestamp_us=...)`` / ``track.get_stats()``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable, Mapping, Sequence

from .duplex import Frame, VideoError

__all__ = ["publish_frames_livekit", "summarize_track_stats"]

#: ``source`` argument → TrackSource attribute name.
_SOURCES = {
    "camera": "SOURCE_CAMERA",
    "screen": "SOURCE_SCREEN_SHARE",
    "screen_share": "SOURCE_SCREEN_SHARE",
    "microphone": "SOURCE_MICROPHONE",
}


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _stat_field(stat: Any, name: str) -> Any:
    """Read a (possibly protobuf) stats field, treating unset fields as None."""
    has_field = getattr(stat, "HasField", None)
    if callable(has_field):
        try:
            if not has_field(name):
                return None
        except (ValueError, AttributeError, TypeError):
            pass
    value = getattr(stat, name, None)
    return value


def summarize_track_stats(stats: Any) -> dict[str, Any]:
    """Compact, testable summary of ``track.get_stats()`` output.

    Keeps a per-entry view (type + the fields the C4a probe reported as the
    'money shot': packets/bytes counters, candidate-pair state, nominated) and
    an ``outbound_rtp_totals`` roll-up for the RTP counters.
    """
    try:
        items = list(stats or [])
    except TypeError:
        return {"count": 0, "types": {}, "entries": [], "outbound_rtp_totals": {}}
    entries: list[dict[str, Any]] = []
    types: dict[str, int] = {}
    for stat in items:
        entry: dict[str, Any] = {"type": str(_get(stat, "type", "") or "")}
        if not entry["type"]:
            descriptor = getattr(stat, "DESCRIPTOR", None)
            entry["type"] = str(getattr(descriptor, "name", "")) if descriptor is not None else "unknown"
        for field in ("packets_sent", "bytes_sent", "packets_received", "nominated", "state", "quality"):
            value = _stat_field(stat, field)
            if value is None:
                continue
            entry[field] = value if isinstance(value, (int, float, bool, str)) else str(value)
        entries.append(entry)
        types[entry["type"]] = types.get(entry["type"], 0) + 1
    totals: dict[str, int] = {}
    for entry in entries:
        if entry["type"] != "outbound-rtp":
            continue
        for key, value in entry.items():
            if key != "type" and isinstance(value, int) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return {"count": len(entries), "types": types, "entries": entries, "outbound_rtp_totals": totals}


def _to_rgb24(frame: Frame) -> bytes:
    """Packed ``frame`` → RGB24 bytes (rgba/bgr24 converted; others rejected)."""
    data = bytes(frame.data)
    fmt = frame.format
    if fmt == "rgb24":
        return data
    if fmt == "rgba":
        return bytes(component for i in range(0, len(data), 4) for component in data[i : i + 3])
    if fmt == "bgr24":
        return bytes(component for i in range(0, len(data), 3) for component in (data[i + 2], data[i + 1], data[i]))
    raise VideoError(f"publish_frames_livekit: unsupported frame format {fmt!r} (need rgb24/rgba/bgr24)")


async def publish_frames_livekit(
    endpoint: str,
    token: str,
    frames: Sequence[Frame] | Iterable[Frame],
    *,
    fps: float = 10.0,
    name: str = "hermes-video",
    source: str = "camera",
    hold: float = 0.0,
    stop_event: Any = None,
    width: int | None = None,
    height: int | None = None,
    timestamp_base_us: int | None = None,
    connect_timeout: float = 15.0,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Publish ``frames`` as a video track; returns a stats dict.

    ``stop_event`` (``asyncio.Event``) aborts early between frames; ``hold``
    keeps the track live after the last frame before stats are collected.
    """
    from livekit import rtc  # lazy: this module stays importable without livekit

    frame_list = list(frames)
    if not frame_list:
        raise VideoError("publish_frames_livekit needs at least one frame")
    target_w = int(width or frame_list[0].width)
    target_h = int(height or frame_list[0].height)
    for frame in frame_list:
        if frame.width != target_w or frame.height != target_h:
            raise VideoError(
                f"publish_frames_livekit needs uniform frames; got {frame.width}x{frame.height} "
                f"after {target_w}x{target_h}"
            )
    source_attr = _SOURCES.get(str(source).lower(), "SOURCE_CAMERA")
    started = time.monotonic()
    pushed = 0
    stopped = "completed"
    room = rtc.Room()
    publication = None
    track = None

    async def _run() -> dict[str, Any]:
        nonlocal pushed, stopped, publication, track
        await asyncio.wait_for(room.connect(endpoint, token), timeout=connect_timeout)
        video_source = rtc.VideoSource(target_w, target_h)
        track = rtc.LocalVideoTrack.create_video_track(name, video_source)
        options = rtc.TrackPublishOptions()
        options.source = getattr(rtc.TrackSource, source_attr)
        publication = await room.local_participant.publish_track(track, options)
        interval = (1.0 / fps) if fps and fps > 0 else 0.0
        base_us = int(timestamp_base_us) if timestamp_base_us is not None else 0
        for index, frame in enumerate(frame_list):
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                stopped = "stop_event"
                break
            frame_ts = float(frame.ts) if frame.ts else (index / fps if fps else 0.0)
            video_frame = rtc.VideoFrame(target_w, target_h, rtc.VideoBufferType.RGB24, _to_rgb24(frame))
            await video_source.capture_frame(video_frame, timestamp_us=base_us + int(frame_ts * 1_000_000))
            pushed += 1
            if interval:
                await asyncio.sleep(interval)
        if stopped == "completed" and hold > 0:
            await asyncio.sleep(hold)
        stats: Any = None
        if track is not None:
            try:
                stats = await track.get_stats()
            except Exception as exc:  # noqa: BLE001 - stats are best-effort evidence
                stats = [{"type": "error", "error": f"{type(exc).__name__}: {exc}"}]
        elapsed = time.monotonic() - started
        return {
            "frames_pushed": pushed,
            "frames_total": len(frame_list),
            "duration_s": round(elapsed, 3),
            "width": target_w,
            "height": target_h,
            "name": name,
            "source": str(source).lower(),
            "stopped": stopped,
            "stats": summarize_track_stats(stats),
        }

    try:
        if timeout is not None:
            return await asyncio.wait_for(_run(), timeout=timeout)
        return await _run()
    finally:
        if publication is not None:
            try:
                await asyncio.wait_for(room.local_participant.unpublish_track(publication.sid), timeout=5.0)
            except Exception:  # noqa: BLE001 - cleanup only
                pass
        try:
            await asyncio.wait_for(room.disconnect(), timeout=5.0)
        except Exception:  # noqa: BLE001 - cleanup only
            pass
