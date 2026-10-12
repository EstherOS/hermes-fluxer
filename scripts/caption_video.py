#!/home/agent/.hermes/hermes-agent/venv/bin/python
"""caption_video.py — frame-by-frame video captioner for the Fluxer omni engine.

NOTE: this container has no `python3` on PATH; run via this shebang (the Hermes
venv python, stdlib-only script) or explicitly:
    nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python scripts/caption_video.py ...

Extracts frames from a video with ffmpeg and captions each one with the
installed SmolVLM2-256M-Video GGUF via llama.cpp `llama-mtmd-cli` (Vulkan),
writing JSONL (one record per frame: timestamp + caption).

This is the *reliable* video path on the GTX 1060 6GB: llama-mtmd-cli also
supports native `--video` (ffmpeg sampling, verified 2026-09-11), but per-frame
captioning gives timestamps, allows frame filtering, and keeps each GPU run
bounded (~10 s per frame incl. model load).

Usage:
    scripts/caption_video.py VIDEO [--fps 0.5] [--max-frames 12] [--width 512]
                                   [--prompt "Describe what you see."]
                                   [--out models/omni/out/<stem>-captions.jsonl]

Environment overrides: SMOL_MODEL, SMOL_MMPROJ, LLAMA_DIR
Requires: ffmpeg + ffprobe on PATH; repo GPU env is applied automatically
(same two vars as gpu/gpu-env.sh) if not already set.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/agent/workspace/fluxer")
GPU_DIR = ROOT / "gpu"
LLAMA_DIR = Path(os.environ.get("LLAMA_DIR", ROOT / "gpu/tools/llama-b10903"))
MODEL = Path(os.environ.get("SMOL_MODEL", ROOT / "models/smolvlm2-256m.gguf"))
MMPROJ = Path(os.environ.get("SMOL_MMPROJ", ROOT / "models/mmproj-smolvlm2.gguf"))
GENERATION_TIMEOUT = 150  # seconds per frame (bounded; cold load ~5-10 s)


def apply_gpu_env() -> None:
    """Mirror gpu/gpu-env.sh without needing a shell source."""
    os.environ.setdefault("VK_DRIVER_FILES", str(GPU_DIR / "nvidia_icd_egl.json"))
    egl_lib = GPU_DIR / "extract-egl/usr/lib/x86_64-linux-gnu"
    os.environ["LD_LIBRARY_PATH"] = f"{egl_lib}:{os.environ.get('LD_LIBRARY_PATH', '')}"


def run(cmd: list[str], timeout: int = GENERATION_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["nice", "-n", "10", *cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def extract_frames(video: Path, out_dir: Path, fps: float, width: int) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()
    proc = run([
        "ffmpeg", "-v", "error", "-y", "-i", str(video),
        "-vf", f"fps={fps},scale={width}:-2", "-q:v", "3",
        str(out_dir / "frame_%05d.jpg"),
    ], timeout=180)
    if proc.returncode != 0:
        sys.exit(f"ffmpeg failed:\n{proc.stderr}")
    return sorted(out_dir.glob("frame_*.jpg"))


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def caption_frame(frame: Path, prompt: str, n_predict: int = 64) -> str:
    proc = run([
        str(LLAMA_DIR / "llama-mtmd-cli"),
        "-m", str(MODEL), "--mmproj", str(MMPROJ),
        "--image", str(frame), "-p", prompt,
        "-n", str(n_predict), "-ub", "64", "-b", "128", "-c", "2048",
    ])
    if proc.returncode != 0:
        return f"<error rc={proc.returncode}: {proc.stderr.strip()[-200:]}>"
    # llama-mtmd-cli logs go to stderr; the reply is on stdout.
    text = _ANSI.sub("", proc.stdout).strip()
    # If the prompt got echoed, keep only what follows its last occurrence.
    if prompt in text:
        text = text.rsplit(prompt, 1)[-1].strip()
    return " ".join(text.split())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("--fps", type=float, default=0.5, help="frames per second to sample (default 0.5)")
    ap.add_argument("--max-frames", type=int, default=12)
    ap.add_argument("--width", type=int, default=512)
    ap.add_argument("--prompt", default="Describe what you see.")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    for f in (MODEL, MMPROJ):
        if not f.exists():
            sys.exit(f"missing model file: {f}")

    apply_gpu_env()
    os.nice(10) if hasattr(os, "nice") else None

    stem = args.video.stem
    frames_dir = ROOT / "models/omni/out" / f"frames-{stem}"
    out_path = args.out or (ROOT / "models/omni/out" / f"{stem}-captions.jsonl")

    frames = extract_frames(args.video, frames_dir, args.fps, args.width)
    if not frames:
        sys.exit("no frames extracted")
    frames = frames[: args.max_frames]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for idx, frame in enumerate(frames):
            t_sec = round(idx / args.fps, 2)
            caption = caption_frame(frame, args.prompt)
            rec = {"frame": frame.name, "t_sec": t_sec, "caption": caption}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"[{t_sec:6.2f}s] {caption}", flush=True)

    print(f"\n{len(frames)} frames captioned -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
