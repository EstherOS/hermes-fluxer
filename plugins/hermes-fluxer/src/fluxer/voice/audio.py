"""Audio helpers for the Fluxer voice lane (spec §6-W3).

Stdlib only (``audioop`` on 3.11 for C-speed resample/RMS; numpy is *not*
required).  No livekit import here — everything operates on plain int16
sample buffers so it is unit-testable offline.

Conventions:
* 48 kHz mono Int16 is the wire format (LiveKit AudioSource).
* frames are 20 ms (960 samples) — the probe's proven chunking.
"""

from __future__ import annotations

import array
import logging
import math
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

try:  # stdlib on 3.11 (removed in 3.13); fallbacks below cover its absence
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", DeprecationWarning)
        import audioop as _audioop
except Exception:  # pragma: no cover - 3.13+
    _audioop = None

log = logging.getLogger("fluxer.voice.audio")

SAMPLE_RATE = 48_000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 960
SILENCE_TAIL_MS = 250

Samples = array.array  # array('h') — int16 mono
SampleLike = Union[bytes, bytearray, memoryview, array.array]


# ── sample buffers ───────────────────────────────────────────────────────────

def samples_to_bytes(samples: array.array) -> bytes:
    return samples.tobytes()


def bytes_to_samples(data: SampleLike) -> array.array:
    """Any int16 buffer → ``array('h')`` (odd trailing byte ignored)."""
    if isinstance(data, array.array) and data.typecode == "h":
        return array.array("h", data)
    raw = bytes(data)
    out = array.array("h")
    out.frombytes(raw[: len(raw) // 2 * 2])
    return out


def rms(samples: SampleLike) -> float:
    """Root-mean-square of an int16 buffer (0.0 for empty input)."""
    raw = samples if isinstance(samples, (bytes, bytearray, memoryview)) else samples.tobytes()
    if len(raw) < 2:
        return 0.0
    if _audioop is not None:
        return float(_audioop.rms(raw, 2))
    data = bytes_to_samples(raw)
    if not data:
        return 0.0
    return math.sqrt(sum(float(sample) * sample for sample in data) / len(data))


def resample(samples: array.array, src_rate: int, dst_rate: int) -> array.array:
    """Mono int16 resample.  ``audioop.ratecv`` when available, linear otherwise."""
    if src_rate == dst_rate or not samples:
        return array.array("h", samples)
    if _audioop is not None and hasattr(_audioop, "ratecv"):
        converted, _state = _audioop.ratecv(samples.tobytes(), 2, 1, int(src_rate), int(dst_rate), None)
        return array.array("h", converted)
    return _linear_resample(samples, src_rate, dst_rate)


def _linear_resample(samples: array.array, src_rate: int, dst_rate: int) -> array.array:
    """Probe-style linear interpolation (fallback path)."""
    n_out = max(1, int(len(samples) * dst_rate / src_rate))
    out = array.array("h", bytes(2 * n_out))
    last = len(samples) - 1
    for i in range(n_out):
        pos = i * last / max(n_out - 1, 1)
        i0 = int(pos)
        frac = pos - i0
        i1 = min(i0 + 1, last)
        out[i] = int(samples[i0] * (1.0 - frac) + samples[i1] * frac)
    return out


# ── wav i/o ──────────────────────────────────────────────────────────────────

@dataclass
class WavInfo:
    src_rate: int
    src_channels: int
    src_seconds: float
    out_rate: int = SAMPLE_RATE
    out_seconds: float = 0.0
    path: str = ""


def read_wav(path: Union[str, Path]) -> Tuple[array.array, WavInfo]:
    """Read a 16-bit WAV, downmix to mono (channel 0); keep the source rate."""
    with wave.open(str(path), "rb") as w:
        channels, rate, width, frames = (
            w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes())
        raw = w.readframes(frames)
    info = WavInfo(src_rate=rate, src_channels=channels, src_seconds=round(frames / rate, 3),
                   path=str(path))
    if width != 2:
        raise ValueError(f"expected 16-bit WAV, got width={width} for {path}")
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    if channels > 1:
        samples = array.array("h", samples[0::channels])
    return samples, info


def load_wav_48k_mono(path: Union[str, Path]) -> Tuple[array.array, WavInfo]:
    """Read any int16 WAV → 48 kHz mono int16 (the LiveKit publish format)."""
    samples, info = read_wav(path)
    if info.src_rate != SAMPLE_RATE:
        samples = resample(samples, info.src_rate, SAMPLE_RATE)
    info.out_seconds = round(len(samples) / SAMPLE_RATE, 3)
    return samples, info


def write_wav(path: Union[str, Path], samples: array.array, rate: int = SAMPLE_RATE) -> str:
    """Write mono int16 samples as a WAV; returns the path as a string."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(samples.tobytes())
    return str(path)


def silence(seconds: float, rate: int = SAMPLE_RATE) -> array.array:
    """Zero-filled mono int16 buffer of ``seconds``."""
    return array.array("h", bytes(2 * max(0, int(seconds * rate))))


# ── livekit frame helpers (duck-typed; no livekit import) ───────────────────

def put_samples(frame, samples: array.array) -> None:
    """Bulk-copy int16 samples into an ``AudioFrame.data`` buffer (probe pattern)."""
    byte_view = frame.data.cast("B")
    if len(samples) * 2 != len(byte_view):
        raise ValueError(f"frame size mismatch: {len(samples)} samples vs {len(byte_view)} bytes")
    byte_view[:] = samples.tobytes()


def pad_frames(samples: array.array, frame_samples: int = FRAME_SAMPLES) -> List[array.array]:
    """Split into fixed-size frames; the last one is zero-padded."""
    frames: List[array.array] = []
    for i in range(0, len(samples), frame_samples):
        chunk = samples[i:i + frame_samples]
        if len(chunk) < frame_samples:
            chunk = array.array("h", list(chunk) + [0] * (frame_samples - len(chunk)))
        frames.append(chunk)
    return frames


# ── VAD / utterance segmentation ─────────────────────────────────────────────

@dataclass
class Utterance:
    """One completed speech segment (48 kHz mono int16)."""

    samples: array.array
    seconds: float
    reason: str = "silence"  # silence | max_length | flush
    frames: int = 0


class VadSegmenter:
    """Energy-based VAD: silence-debounced utterance segmentation.

    Pure state machine over 20 ms frames — no I/O, fully unit-testable.  A short
    pre-roll keeps the first phoneme of an utterance from being clipped.
    """

    def __init__(self, *, silence_ms: int = 900, min_utterance_ms: int = 300,
                 max_utterance_s: float = 30.0, energy_threshold: float = 400.0,
                 pre_roll_ms: int = 200, frame_ms: int = FRAME_MS,
                 onset_ms: int = 300) -> None:
        self.silence_ms = max(1, int(silence_ms))
        self.min_utterance_ms = max(0, int(min_utterance_ms))
        self.onset_ms = max(0, int(onset_ms))
        self.max_utterance_s = max(1.0, float(max_utterance_s))
        self.energy_threshold = float(energy_threshold)
        self.frame_ms = max(1, int(frame_ms))
        self._frame_samples = SAMPLE_RATE * self.frame_ms // 1000
        self._pre_roll_frames = max(0, int(pre_roll_ms) // self.frame_ms)
        self._pending = array.array("h")
        self._pre_roll: List[array.array] = []
        self._utterance: List[array.array] = []
        self._speaking = False
        self._silence_run_ms = 0
        self._voiced_ms = 0
        self._onset_reached = False
        self._onset_consumed = False
        self.dropped_short = 0

    # -- public ----------------------------------------------------------

    def feed(self, data: SampleLike) -> List[Utterance]:
        """Feed arbitrary-length int16 data; return zero or more completed utterances."""
        self._pending.extend(bytes_to_samples(data))
        out: List[Utterance] = []
        while len(self._pending) >= self._frame_samples:
            frame = self._pending[: self._frame_samples]
            del self._pending[: self._frame_samples]
            utterance = self._process_frame(frame)
            if utterance is not None:
                out.append(utterance)
        return out

    def flush(self) -> Optional[Utterance]:
        """End any in-progress utterance (session end / stream close)."""
        if not self._speaking or not self._utterance:
            self._reset()
            return None
        return self._finish(reason="flush")

    @property
    def speaking(self) -> bool:
        return self._speaking

    def take_onset(self) -> bool:
        """Consume the speech-onset edge — True once per utterance.

        Fires when voiced audio has sustained for ``onset_ms``: long enough
        to filter coughs and transients, short enough for barge-in.
        """
        if self._onset_reached and not self._onset_consumed:
            self._onset_consumed = True
            return True
        return False

    # -- internals -------------------------------------------------------

    def _process_frame(self, frame: array.array) -> Optional[Utterance]:
        voiced = rms(frame) >= self.energy_threshold
        if not self._speaking:
            if not voiced:
                if self._pre_roll_frames:
                    self._pre_roll.append(frame)
                    if len(self._pre_roll) > self._pre_roll_frames:
                        self._pre_roll.pop(0)
                return None
            # speech starts: pre-roll + this frame
            self._speaking = True
            self._silence_run_ms = 0
            self._voiced_ms = self.frame_ms
            self._utterance = list(self._pre_roll) + [frame]
            self._pre_roll = []
        else:
            self._utterance.append(frame)
            if voiced:
                self._voiced_ms += self.frame_ms
                self._silence_run_ms = 0
            else:
                self._silence_run_ms += self.frame_ms
            if self._silence_run_ms >= self.silence_ms:
                return self._finish(reason="silence")
        if not self._onset_reached and self._voiced_ms >= self.onset_ms:
            self._onset_reached = True
        if self._voiced_ms >= int(self.max_utterance_s * 1000):
            return self._finish(reason="max_length")
        return None

    def _finish(self, *, reason: str) -> Optional[Utterance]:
        frames = self._utterance or []
        samples = array.array("h")
        for frame in frames:
            samples.extend(frame)
        voiced_ms = self._voiced_ms
        self._reset()
        if reason == "flush" or voiced_ms >= self.min_utterance_ms:
            return Utterance(samples=samples, seconds=round(len(samples) / SAMPLE_RATE, 3),
                             reason=reason, frames=len(frames))
        self.dropped_short += 1
        log.debug("Fluxer voice: dropping %dms utterance (min_utterance_ms=%d)",
                  voiced_ms, self.min_utterance_ms)
        return None

    def _reset(self) -> None:
        self._utterance = []
        self._pre_roll = []
        self._speaking = False
        self._silence_run_ms = 0
        self._voiced_ms = 0
        self._onset_reached = False
        self._onset_consumed = False
