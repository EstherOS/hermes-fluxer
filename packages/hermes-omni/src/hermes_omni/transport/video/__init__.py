"""hermes-omni video lane: duplexer + local backend + render + publish.

* IN — ``LocalVideoDuplexer.ingest_video`` / ``push_frame`` / ``attach_track``
  (files and live LiveKit tracks; ffmpeg + SmolVLM2, no torch);
* OUT — ``render.card_video`` / ``render.annotate_video`` /
  ``render.relay_frames`` (ffmpeg only) and ``publish_frames_livekit``
  (lazy livekit import).

Everything here is implementation-agnostic about the frame source: the seam is
:class:`Frame` in / :class:`VideoEvent` out.
"""

from .duplex import Frame, VideoDuplexer, VideoError, VideoEvent
from .local_backend import (
    CAPTION_PROMPT,
    LocalVideoDuplexer,
    frame_from_livekit,
    motion_energy,
)
from .render import annotate_video, card_video, relay_frames
from .publish import publish_frames_livekit, summarize_track_stats

__all__ = [
    "Frame",
    "VideoEvent",
    "VideoDuplexer",
    "VideoError",
    "LocalVideoDuplexer",
    "CAPTION_PROMPT",
    "frame_from_livekit",
    "motion_energy",
    "card_video",
    "annotate_video",
    "relay_frames",
    "publish_frames_livekit",
    "summarize_track_stats",
]