# fluxer — Hermes lane

Fluxer as a first-class Hermes surface: text, commands, attachments, voice, and
an omnimodal engine (image/audio/video in + out) with clean seams — a single
"omniplex" model or a stitched local stack are both just config.

Built overnight 2026-09-11. Sandbox-tested end to end; one staged step away from
live (`docs/ACTIVATION.md`).

## Layout
- `plugin-src/fluxer/` — the plugin (REST + gateway clients, adapter, media,
  `voice/` LiveKit lane, `omni/` engine seams, `video/` local duplexer, tests).
- `docs/` — API notes, plugin-integration guide, spec, GPU guide, omni-model
  feasibility, activation steps, morning demo.
- `status/` — reports + raw evidence per wave (c1…c5, c4a/c4b).
- `scripts/` — probe, selftests, e2e drivers, sandbox helpers, omni smoke tests,
  activate/deactivate.
- `gpu/`, `models/` — Vulkan toolchain + GGUF assets (see gpu-and-models.md).

## Run things
```bash
PY=/home/agent/.hermes/hermes-agent/venv/bin/python
cd ~/.hermes/hermes-agent && $PY -m pytest ~/workspace/fluxer/plugin-src/fluxer/tests -q   # 242 tests
~/workspace/fluxer/scripts/sandbox_start.sh     # dev gateway (never touches the live one)
~/workspace/fluxer/scripts/sandbox_status.sh    # status + recent log
~/workspace/fluxer/scripts/sandbox_stop.sh
```

Status: `STATUS.md`.
