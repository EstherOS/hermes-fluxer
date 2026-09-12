# Status — night shift COMPLETE (2026-09-11)

## Final state
- **Fluxer lane built + verified in the sandbox.** All waves green: clients (C1),
  adapter/text (C2), attachments (C3), voice probe (C4a), voice module (C4b),
  omni engine + video duplexer (C5). **242 unit tests**; live evidence: client
  selftest 11/11, media 20/20, voice probe ×2, c4b cascade dry-run, c5 registry +
  render runs; sandbox E2E: gateway connected as Esther, voice auto-joined,
  cron-reply delivered, cleanups verified.
- **Sandbox is RUNNING right now**: bot online (`Esther`), sitting in the test
  guild's voice channel, ready for a first real human message.
- Final-pass fix: loader-context bug (absolute lazy targets in `voice/__init__.py`
  — passed tests, broke under the real gateway loader; now package-relative +
  regression test `test_loader_context.py`).
- Activation staged: `scripts/activate-live.sh` + `docs/ACTIVATION.md` (needs a
  host-side container restart). Live env vars staged in `~/.hermes/.env`.
- Snapshot: `~/.hermes/patches/fluxer-plugin-20260911.tar.gz` (+ README-fluxer.md).

## Open / for kairo
1. Say hi in `#general` (test guild) — closes the non-self inbound leg.
   DM inbound for bots is still an open question (needs your client).
2. Join the voice channel and talk — tests the human-receive leg live
   (bot hears → transcribes → answers with speech; transcripts in channel text).
3. Optional: grant the bot MANAGE_WEBHOOKS (automated E2E), and tell me if you
   want the lane **live** on the main gateway (activation doc).
4. Known limits: voice is half-duplex v1 (barge-in later); turn latency ~2–5 s+
   (local STT+TTS on a 2-core CPU); GPU is a 1060 6GB (not 1650).

## Where everything lives
- Workspace: `/home/agent/workspace/fluxer` (host: `~/hermes/workspace/fluxer`)
- Reports: `status/c1…c5-report.md`, `c4a/c4b` · Docs: `docs/`
- Sandbox home: `sandbox/hermes-home` · plugin: `plugin-src/fluxer`

## Log
- 07:35 handoff · 07:43 research out · 08:05 wave 1 · 08:24 verified · 08:31 wave 2
- 08:50 waves 3+4 · 09:10 all waves verified · 09:13 sandbox boot + voice auto-join
- 09:1x final pass (loader fix, snapshot, docs, report) — night shift complete.
