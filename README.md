# fluxer — Hermes lane

Fluxer as a first-class Hermes surface: text, commands, attachments, voice, and
an omnimodal engine (image/audio/video in + out) with clean seams — a single
"omniplex" model or a stitched local stack are both just config.

## Layout
- `packages/omnimaker/` — the graph engine (routes envelopes between nodes; adapters do the work).
- `packages/omnimaker-hermes/` — the `hermes/session` adapter (a Hermes agent conversation as an engine node).
- `plugins/hermes-fluxer/src/fluxer/` — the plugin (REST + gateway clients, adapter, media,
  `voice/` LiveKit lane, `omni/` engine seams, `video/` local duplexer, tests).
- `docs/` — API notes, plugin-integration guide, design + spec docs, GPU guide,
  omni-model feasibility, activation steps, morning demo.
- `status/` — reports + raw evidence (c1…c7).
- `scripts/` — probe, selftests, e2e drivers, sandbox helpers, omni smoke tests,
  activate/deactivate.
- `reference/` — Fluxer OpenAPI spec.
- Model/GPU assets live in the sibling `fluxer-local/` directory (see `docs/gpu-and-models.md`).

## Run things
```bash
PY=/home/agent/.hermes/hermes-agent/venv/bin/python
cd ~/.hermes/hermes-agent && $PY -m pytest ~/workspace/fluxer/plugins/hermes-fluxer/src/fluxer/tests -q
~/workspace/fluxer/scripts/sandbox_start.sh     # dev gateway (never touches the live one)
~/workspace/fluxer/scripts/sandbox_status.sh    # status + recent log
~/workspace/fluxer/scripts/sandbox_stop.sh
```

Status: `STATUS.md`.
