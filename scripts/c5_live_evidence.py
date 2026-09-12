#!/home/agent/.hermes/hermes-agent/venv/bin/python
"""c5_live_evidence.py — bounded live evidence for wave 4 (omni seams + video).

Proves the seams dispatch to real local backends:

  a) ``LocalVideoDuplexer.ingest_video`` on the 2 s dog clip — native ``--video``
     path (auto mode) and the frame loop (per-caption timestamps);
  b) ``render.card_video`` + ``render.annotate_video`` → ffprobe-verified MP4s;
  c) registry end-to-end: audio_in (whisper.cpp + Qwen3-ASR GPU) and audio_out
     (piper + Qwen3-TTS GPU) through ``registry.get_backend(...).process(...)``;
  d) ``Cascade`` facade dry-run: audio_in → text on jfk.wav.

All subprocess work goes through the plugin's own nice'd, timeout-bounded
runners; this script just orchestrates and writes evidence JSON under
``status/c5-evidence/``.  No downloads, bounded runs, ``. gpu/gpu-env.sh``
equivalent env is applied by the backends themselves.

Usage:
    nice -n 10 ./venv/bin/python scripts/c5_live_evidence.py [--skip-gpu] [--fps 2]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/agent/workspace/fluxer")
CHECKOUT = Path("/home/agent/.hermes/hermes-agent")
sys.path.insert(0, str(CHECKOUT))
sys.path.insert(0, str(ROOT / "plugin-src"))

from fluxer.omni import cascade, profile, registry, types  # noqa: E402
from fluxer.video import LocalVideoDuplexer, render  # noqa: E402

DOG_SRC = ROOT / "models/test-dog.jpg"
JFK = ROOT / "models/jfk.wav"


def run_ffmpeg_cli(argv: list[str], *, timeout: float = 120.0) -> dict:
    """Plain ffmpeg/ffprobe invocation for clip prep + probes (nice'd)."""
    proc = subprocess.run(
        ["nice", "-n", "10", *[str(a) for a in argv]],
        capture_output=True, text=True, timeout=timeout,
    )
    return {"argv": [str(a) for a in argv], "rc": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()[-400:]}


def probe(path: Path) -> dict:
    fmt = run_ffmpeg_cli([
        "ffprobe", "-v", "error", "-show_entries", "format=duration,format_name",
        "-of", "json", str(path),
    ])
    stream = run_ffmpeg_cli([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,r_frame_rate",
        "-of", "json", str(path),
    ])
    out: dict = {"path": str(path), "size": path.stat().st_size if path.exists() else 0}
    try:
        out["format"] = json.loads(fmt["stdout"]).get("format", {})
        out["stream"] = (json.loads(stream["stdout"]).get("streams") or [{}])[0]
    except json.JSONDecodeError:
        out["format_error"] = fmt
    return out


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def event_dict(event) -> dict:
    return {"ts": event.ts, "kind": event.kind, "text": event.text, "meta": event.meta}


async def step_a(evidence: Path, fps: float, log) -> dict:
    """(a) caption the 2 s dog clip via LocalVideoDuplexer.ingest_video."""
    inputs = evidence / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    clip = inputs / "dog-2s.mp4"
    # exact §3.4 regeneration command (bounded, no downloads)
    regen = run_ffmpeg_cli([
        "ffmpeg", "-y", "-loop", "1", "-i", str(DOG_SRC), "-t", "2", "-r", "2",
        "-vf", "scale=320:-2", "-pix_fmt", "yuv420p", str(clip),
    ])
    log(f"[a] clip regenerated rc={regen['rc']} -> {clip} ({clip.stat().st_size} bytes)")

    result: dict = {"clip": str(clip), "regenerate": regen, "auto": {}, "frames": {}}

    # a1: auto mode (short clip → native --video path)
    duplexer = LocalVideoDuplexer(root=ROOT, mode="auto", fps=fps)
    started = time.time()
    events = [event_dict(e) async for e in duplexer.ingest_video(clip, fps=fps)]
    result["auto"] = {
        "wall_s": round(time.time() - started, 2),
        "events": events,
        "stats": duplexer.stats,
        "mode_used": (events[0]["meta"].get("mode") if events else None),
    }
    log(f"[a] auto ingest -> {len(events)} event(s) mode={result['auto']['mode_used']} "
        f"in {result['auto']['wall_s']}s")

    # a2: frame loop with per-caption timestamps
    duplexer2 = LocalVideoDuplexer(root=ROOT, mode="frames", fps=1.0, max_frames=4)
    started = time.time()
    events2 = [event_dict(e) async for e in duplexer2.ingest_video(clip, fps=1.0, mode="frames")]
    result["frames"] = {
        "wall_s": round(time.time() - started, 2),
        "events": events2,
        "stats": duplexer2.stats,
    }
    log(f"[a] frame-loop ingest -> {len(events2)} events in {result['frames']['wall_s']}s")
    (evidence / "a_ingest_events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events + events2) + "\n", encoding="utf-8"
    )
    return result


async def step_b(evidence: Path, caption_events: list[dict], log) -> dict:
    """(b) card_video + annotate_video, ffprobe-verified."""
    out_dir = evidence / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {}

    card_out = out_dir / "card.mp4"
    started = time.time()
    await render.card_video(
        ["Fluxer omni engine — wave 4", "video out: ffmpeg card render", "next: timestamped annotation"],
        card_out, duration_per_line=1.0, text_dir=out_dir / "texts-card",
    )
    result["card"] = {"wall_s": round(time.time() - started, 2), "probe": probe(card_out)}
    log(f"[b] card.mp4 -> {result['card']['probe']['format'].get('duration')}s "
        f"{result['card']['probe']['stream'].get('width')}x{result['card']['probe']['stream'].get('height')}")

    src = evidence / "inputs/dog-2s.mp4"
    ann_out = out_dir / "annotated.mp4"
    events = [e for e in caption_events if e["kind"] == "caption" and e.get("text")]
    started = time.time()
    await render.annotate_video(src, events, ann_out, text_dir=out_dir / "texts-ann")
    result["annotated"] = {"wall_s": round(time.time() - started, 2), "probe": probe(ann_out), "events_used": len(events)}
    log(f"[b] annotated.mp4 -> {result['annotated']['probe']['format'].get('duration')}s "
        f"({len(events)} caption events overlaid)")

    # a still frame from the annotated video, for eyeball/vision verification
    still = out_dir / "annotated-mid.png"
    still_run = run_ffmpeg_cli([
        "ffmpeg", "-v", "error", "-y", "-i", str(ann_out), "-vf", "select=eq(n\\,2)", "-frames:v", "1", str(still),
    ])
    result["annotated_still"] = {"path": str(still), "rc": still_run["rc"], "size": still.stat().st_size if still.exists() else 0}
    return result


async def step_c(evidence: Path, skip_gpu: bool, log) -> dict:
    """(c) registry end-to-end dispatch for audio_in / audio_out."""
    result: dict = {}

    # audio_in — CPU whisper.cpp
    backend = registry.get_backend("local.whispercpp", root=ROOT)
    started = time.time()
    parts = await backend.process(types.Part.audio(str(JFK)))
    result["audio_in_whispercpp"] = {
        "wall_s": round(time.time() - started, 2),
        "text": parts[0].data,
        "meta": parts[0].meta,
    }
    log(f"[c] whispercpp: {parts[0].data[:80]!r} ({result['audio_in_whispercpp']['wall_s']}s)")

    if not skip_gpu:
        # audio_in — GPU Qwen3-ASR via llama-mtmd-cli
        backend = registry.get_backend("local.qwen3asr", root=ROOT)
        started = time.time()
        parts = await backend.process(types.Part.audio(str(JFK)))
        result["audio_in_qwen3asr"] = {
            "wall_s": round(time.time() - started, 2),
            "text": parts[0].data,
            "meta": parts[0].meta,
        }
        log(f"[c] qwen3asr (GPU): {parts[0].data[:80]!r} ({result['audio_in_qwen3asr']['wall_s']}s)")

    # audio_out — CPU piper
    out_dir = evidence / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)
    backend = registry.get_backend("local.piper", root=ROOT)
    started = time.time()
    parts = await backend.process(types.Part.text("Fluxer omni engine. Local speech via the registry."))
    produced = Path(parts[0].data)
    kept = out_dir / "piper.wav"
    kept.write_bytes(produced.read_bytes())
    result["audio_out_piper"] = {
        "wall_s": round(time.time() - started, 2),
        "wav": str(kept),
        "bytes": kept.stat().st_size,
        "probe": probe(kept),
    }
    log(f"[c] piper -> {kept.name} {kept.stat().st_size} bytes")

    if not skip_gpu:
        backend = registry.get_backend("local.qwen3tts", root=ROOT, speaker_file=str(JFK), frames=120)
        started = time.time()
        parts = await backend.process(types.Part.text("Hello from the Fluxer omni engine."))
        produced = Path(parts[0].data)
        kept = out_dir / "qwen3tts.wav"
        kept.write_bytes(produced.read_bytes())
        result["audio_out_qwen3tts"] = {
            "wall_s": round(time.time() - started, 2),
            "wav": str(kept),
            "bytes": kept.stat().st_size,
            "probe": probe(kept),
        }
        log(f"[c] qwen3tts (GPU) -> {kept.name} {kept.stat().st_size} bytes ({result['audio_out_qwen3tts']['wall_s']}s)")

    write_json(evidence / "c_registry.json", result)
    return result


async def step_d(evidence: Path, log) -> dict:
    """(d) Cascade facade dry-run: stitched audio_in → text on jfk.wav."""
    cfg = {
        "omni": {
            "profiles": {
                "c5-probe": {
                    "mode": "stitched",
                    "bindings": {"audio_in": {"backend": "local.whispercpp"}, "text_out": {"backend": "agent"}},
                }
            }
        }
    }
    resolved = profile.resolve_profile(cfg, "c5-probe")
    run = cascade.Cascade(resolved)
    started = time.time()
    parts = await run.collect(types.Part.audio(str(JFK)))
    result = {
        "profile": {"name": resolved.name, "mode": resolved.mode, "bindings": {k: v.backend for k, v in resolved.bindings.items()}},
        "wall_s": round(time.time() - started, 2),
        "parts": [{"kind": p.kind, "data": p.data, "meta": p.meta} for p in parts],
    }
    log(f"[d] cascade audio_in->text: {str(parts[0].data)[:80]!r} ({result['wall_s']}s)")
    write_json(evidence / "d_cascade.json", result)
    return result


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence-dir", default=str(ROOT / "status/c5-evidence"))
    ap.add_argument("--skip-gpu", action="store_true", help="skip the two GPU steps in (c)")
    ap.add_argument("--fps", type=float, default=2.0)
    args = ap.parse_args()

    try:
        os.nice(10)
    except OSError:
        pass

    evidence = Path(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)

    summary: dict = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "python": sys.version.split()[0],
        "cwd": str(ROOT),
        "steps": {},
    }

    async def guarded(name, coro):
        started = time.time()
        try:
            summary["steps"][name] = {"status": "ok", "wall_s": round(time.time() - started, 2), "result": await coro}
        except Exception as exc:  # noqa: BLE001 - evidence script records failures
            summary["steps"][name] = {
                "status": "error", "wall_s": round(time.time() - started, 2),
                "error": f"{type(exc).__name__}: {exc}",
            }
            log(f"[{name}] FAILED: {type(exc).__name__}: {exc}")

    a = await guarded("a_video_ingest", step_a(evidence, args.fps, log))
    caption_events = []
    if summary["steps"]["a_video_ingest"]["status"] == "ok":
        caption_events = summary["steps"]["a_video_ingest"]["result"]["frames"]["events"]
    await guarded("b_render", step_b(evidence, caption_events, log))
    await guarded("c_registry", step_c(evidence, args.skip_gpu, log))
    await guarded("d_cascade", step_d(evidence, log))

    summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    summary["statuses"] = {k: v["status"] for k, v in summary["steps"].items()}
    write_json(evidence / "summary.json", summary)
    log(f"summary: {summary['statuses']}")
    return 0 if all(v == "ok" for v in summary["statuses"].values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
