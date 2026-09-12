#!/usr/bin/env python3
"""
miniomni2_probe.py — minimal headless runner for Mini-Omni2 on this box.

Purpose (fluxer C7, wave 6): feed a WAV (optionally + image) into the
gpt-omni/mini-omni2 torch stack and get (a) streamed text out and (b) a
24 kHz WAV of the model's speech back — plus a machine-readable metrics
line for latency/VRAM bookkeeping.

Run with the dedicated venv (torch 2.3.1+cu121, litgpt 0.4.3, snac 1.2.0 …):

    . gpu/gpu-env.sh
    nice -n 10 .venv-torch/bin/python scripts/miniomni2_probe.py \
        --audio mini-omni2/data/samples/output1.wav \
        --out   status/c7-evidence/output.wav

Vision path (audio question + image):

    nice -n 10 .venv-torch/bin/python scripts/miniomni2_probe.py \
        --audio mini-omni2/data/samples/vision_qa_audio.wav \
        --image mini-omni2/data/samples/vision_qa_image.jpg \
        --out   status/c7-evidence/output-vision.wav

Notes / constraints (learned the hard way, see status/c7-report.md):
- MUST run with CWD-independent explicit paths: the repo's defaults are
  relative (./checkpoint, ./data/samples/...), so we pass everything absolute.
- Model stack: 0.5B Qwen2 LLM (fp32 ~2.8 GB) + whisper-small + CLIP ViT-B/32
  + SNAC 24 kHz → ~4.7–5.3 GiB VRAM on the 6 GB GTX 1060. Do not run
  concurrently with other GPU jobs.
- Generation pace measured: ~30 tokens/s (Pascal sm_61, fp32, stock code).
- The upstream code prints its own logs; we keep them on stderr and print
  exactly one JSON summary line on stdout (prefix ``PROBE_JSON``).
- Bounded by --max-tokens (upstream cap: model.max_seq_length=2048 — the
  upstream code asserts max_returned_tokens > prompt length too).
"""

import argparse
import json
import os
import sys
import time

FLUXER = os.environ.get("FLUXER_WORKSPACE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REPO = os.path.join(FLUXER, "mini-omni2")


def build_parser():
    p = argparse.ArgumentParser(description="Headless Mini-Omni2 probe (wav -> text + wav).")
    p.add_argument("--audio", required=True, help="input WAV path (any sample rate; whisper-resampled)")
    p.add_argument("--image", default=None, help="optional image path -> vision-QA path instead of audio-QA")
    p.add_argument("--out", required=True, help="output WAV path (24 kHz mono, written by the model)")
    p.add_argument("--repo", default=DEFAULT_REPO, help="mini-omni2 repo checkout (default: %(default)s)")
    p.add_argument("--ckpt", default=None, help="checkpoint dir (default: <repo>/checkpoint)")
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--max-tokens", type=int, default=2048, help="max_returned_tokens (bounded generation)")
    p.add_argument("--temperature", type=float, default=0.9)
    p.add_argument("--stream-stride", type=int, default=4)
    p.add_argument("--warmup", action="store_true", help="run one warm-up pass (adds ~15-30 s)")
    p.add_argument("--json-out", default=None, help="also write the JSON metrics to this file")
    return p


def main():
    args = build_parser().parse_args()
    repo = os.path.abspath(args.repo)
    ckpt = os.path.abspath(args.ckpt) if args.ckpt else os.path.join(repo, "checkpoint")
    out = os.path.abspath(args.out)
    json_out = os.path.abspath(args.json_out) if args.json_out else None
    assert os.path.isdir(repo), f"repo dir not found: {repo}"
    assert os.path.isdir(ckpt), f"checkpoint dir not found: {ckpt}"
    assert os.path.isfile(args.audio), f"audio not found: {args.audio}"
    if args.image:
        assert os.path.isfile(args.image), f"image not found: {args.image}"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    # The repo vendors a (patched) litgpt; imports resolve against the repo dir.
    sys.path.insert(0, repo)
    os.chdir(repo)  # some upstream helpers still use relative sample paths

    import torch

    torch.set_grad_enabled(False)

    metrics = {
        "audio_in": os.path.abspath(args.audio),
        "image_in": os.path.abspath(args.image) if args.image else None,
        "out_wav": out,
        "device": args.device,
        "device_name": None,
        "load_s": None,
        "warmup_s": None,
        "run_s": None,
        "out_audio_s": None,
        "rtf_out_over_run": None,
        "vram_peak_alloc_mib": None,
        "vram_peak_reserved_mib": None,
        "text": None,
        "ok": False,
        "error": None,
    }

    if torch.cuda.is_available():
        metrics["device_name"] = torch.cuda.get_device_name(0)

    try:
        if args.image:
            from inference_vision import OmniVisionInference
            cls = OmniVisionInference
        else:
            from inference import OmniInference
            cls = OmniInference

        t0 = time.time()
        client = cls(ckpt_dir=ckpt, device=args.device)
        metrics["load_s"] = round(time.time() - t0, 2)
        print(f"[probe] model loaded in {metrics['load_s']}s", file=sys.stderr)

        if args.warmup:
            t0 = time.time()
            # warm the exact path we are about to use
            if args.image:
                client.warm_up(audio_sample=os.path.abspath(args.audio), image_sample=os.path.abspath(args.image))
            else:
                client.warm_up(sample=os.path.abspath(args.audio))
            metrics["warmup_s"] = round(time.time() - t0, 2)
            print(f"[probe] warmup in {metrics['warmup_s']}s", file=sys.stderr)

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()

        t0 = time.time()
        text_parts = []
        if args.image:
            gen = client.run_vision_AA_batch_stream(
                os.path.abspath(args.audio), os.path.abspath(args.image),
                stream_stride=args.stream_stride,
                max_returned_tokens=args.max_tokens,
                temperature=args.temperature,
                save_path=out,
            )
        else:
            gen = client.run_AT_batch_stream(
                os.path.abspath(args.audio),
                stream_stride=args.stream_stride,
                max_returned_tokens=args.max_tokens,
                temperature=args.temperature,
                save_path=out,
            )
        for _audio_stream, text_stream in gen:
            if text_stream:
                text_parts.append(text_stream)
                print(text_stream, end="", flush=True)
        metrics["run_s"] = round(time.time() - t0, 2)
        print(f"\n[probe] generation loop done in {metrics['run_s']}s", file=sys.stderr)
        metrics["text"] = "".join(text_parts).strip()

        if torch.cuda.is_available():
            metrics["vram_peak_alloc_mib"] = round(torch.cuda.max_memory_allocated() / 2**20, 1)
            metrics["vram_peak_reserved_mib"] = round(torch.cuda.max_memory_reserved() / 2**20, 1)

        if os.path.isfile(out):
            import soundfile as sf
            info = sf.info(out)
            metrics["out_audio_s"] = round(info.duration, 2)
            if metrics["run_s"]:
                metrics["rtf_out_over_run"] = round(info.duration / metrics["run_s"], 2)
        metrics["ok"] = True
    except Exception as e:  # noqa: BLE001 — probe: surface everything
        import traceback
        traceback.print_exc()
        metrics["error"] = f"{type(e).__name__}: {e}"

    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2, ensure_ascii=False)
    line = "PROBE_JSON " + json.dumps(metrics, ensure_ascii=False)
    print(line)
    return 0 if metrics["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
