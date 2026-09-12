# C5 report — omni engine seams + local video duplexer (wave 4)

**Date:** 2026-09-11 ~08:45–09:10 UTC · **Author:** C5 · **Verdict: ✅ complete**
(all four builds implemented, unit-tested green, bounded live evidence green; the
two LiveKit touchpoints are implemented-but-unexercised by design — no voice
channel joins during the C4b run).

Basis: `docs/omni-models-feasibility.md` (§2 four-senses map, §3.3 verified
commands, §3.4 gotchas), `docs/spec-fluxer-plugin.md` §6 ("omni engine"),
`docs/gpu-and-models.md`.  Slot-in: `plugin-src/fluxer/omni/` + `plugin-src/fluxer/video/`
(subpackages of the plugin — nothing downstream cares whether one model or five
serve the senses).

---

## 1. Deliverables

| Path | Lines | What |
|---|---|---|
| `plugin-src/fluxer/omni/types.py` | 296 | `Sense`/`Direction`, `Part`, `SenseBinding`, `DuplexSession`/`SenseBackend`/`DuplexBackend` protocols, error family, `UNIFIED_CONFIG_EXAMPLE` |
| `plugin-src/fluxer/omni/registry.py` | 1015 | `register_backend/get_backend/list_backends/describe_backends`, `run_command` (nice'd, bounded), `apply_gpu_env`, 11 builtin thin backends, pure argv builders |
| `plugin-src/fluxer/omni/profile.py` | 319 | `omni.profiles.<name>` parsing: stitched/unified modes, typo detection (did-you-mean), default fallback profile |
| `plugin-src/fluxer/omni/cascade.py` | 244 | `Cascade` (turn-based push→chain→yield), `CascadeSession` (`DuplexSession` facade), `duplex_session()` for unified mode |
| `plugin-src/fluxer/video/duplex.py` | 117 | `Frame` / `VideoEvent` / `VideoDuplexer` seam (frames in, events out) |
| `plugin-src/fluxer/video/local_backend.py` | 676 | `LocalVideoDuplexer`: `ingest_video` (native `--video` + frame-loop fallback), `push_frame`+batching, `attach_track` (lazy livekit), motion/rolling-summary events |
| `plugin-src/fluxer/video/render.py` | 533 | `card_video`, `annotate_video` (timestamped overlay, width-wrapped), `relay_frames`, `gen_backend` seam (documented, unimplemented) |
| `plugin-src/fluxer/video/publish.py` | 205 | `publish_frames_livekit` (lazy livekit, stats summary) — **implemented, not live-tested** |
| `plugin-src/fluxer/tests/test_omni.py` | 736 | 45 tests: registry, argv/timeouts/env assertions, profile matrix, cascade fakes, null/agent errors, real bounded subprocess tests |
| `plugin-src/fluxer/tests/test_video.py` | 903 | 36 tests: frame validation, argv builders, duplexer state machine (fake captions), native/frames/auto fallback, attach_track + publish with a fake livekit module, renderers with mocked ffmpeg |
| `scripts/c5_live_evidence.py` | 299 | bounded live-evidence orchestrator (steps a–d below) |
| `status/c5-evidence/` | 18 files, 580 KB | raw evidence (see §5) |
| **total new code** | ~5.5k lines | |

Lane discipline: only the files above were written. `adapter.py`, `voice/`,
`tests/test_adapter.py`, `tests/test_media.py`, `tests/test_voice.py` were not
touched. Full suite stayed green throughout:
`cd /home/agent/.hermes/hermes-agent && ./venv/bin/python -m pytest /home/agent/workspace/fluxer/plugin-src/fluxer/tests -q`
→ **240 passed** (81 of them C5's; the rest C1/C2/C4a/C4b).

---

## 2. The engine (what "seam" means here)

* **Vocabulary** (`types.py`): a `Part` is `kind` (`text|image|audio|video`) +
  `data` (str bytes path) + `mime` + `meta`.  Slots are named
  `"<sense>_<direction>"` (`audio_in`, `video_out`, …), canonical set in `SLOTS`.
* **Registry** (`registry.py`): `register_backend(name, factory)` /
  `get_backend(name, **opts)` / `list_backends()`.  Builtins are **thin
  wrappers** — stdlib only at import, GPU env applied per call, everything
  bounded and `nice -n 10`:

  | slot | backends | notes |
  |---|---|---|
  | text | `agent` (marker: serviced by the adapter core / `Cascade(think=...)`), `local.llama_server` (health + chat over HTTP) | |
  | audio_in | `local.whispercpp` (CPU), `local.qwen3asr` (GPU; CLI default, llama-server `input_audio` transport implemented + documented, not live-tested) | `-c 1024` per §3.4 |
  | audio_out | `local.piper` (CPU), `local.qwen3tts` (GPU `llama-tts`, voice cloning) | `-c 1024` gotcha respected |
  | image_in | `local.smolvlm` | `caption_file()` reused by the video frame loop |
  | video_in | `local.smolvlm_video` (wraps the video duplexer) | native `--video`, `--video-fps`, `-c 8192` per §3.4 |
  | video_out | `local.render` (ffmpeg card/annotate) | |
  | any | `null` (fails loudly) · duplex: `null_duplex` (raises with the exact future config shape) | |

* **Profiles** (`profile.py`): `resolve_profile(cfg, name)` parses
  `omni.profiles.<name>`; unknown slots get a did-you-mean, unknown backends are
  errors (validated against the registry catalog), unknown spec keys warn.
  No config → the builtin fallback `local-stitched` (the exact §2 verified set).

  ```yaml
  omni:
    default_profile: local-stitched
    profiles:
      local-stitched:
        mode: stitched
        bindings:
          text_out:  {backend: agent}              # documents the thinker seam
          audio_in:  {backend: local.whispercpp}   # or local.qwen3asr (GPU)
          audio_out: {backend: local.piper}        # or local.qwen3tts (GPU)
          image_in:  {backend: local.smolvlm}
          video_in:  {backend: local.smolvlm_video}
          video_out: {backend: local.render}
      unified-future:                               # not available on 6 GB — see doc §1/§5
        mode: unified
        backend: null_duplex                        # replace with a registered duplex backend
        senses: [text, audio, image, video]
        options: {base_url: "http://127.0.0.1:8090/v1", model: "qwen2.5-omni-3b",
                  modalities: [text, audio], transport: websocket}
  ```

* **Cascade** (`cascade.py`): `push(part)` runs **understand** (the `*_in`
  slot) → **think** (host hook; `agent` is the marker) → **express**
  (`output_senses=("audio",)` runs `audio_out`).  `Cascade.session()` returns a
  `DuplexSession` (`CascadeSession` for stitched, the unified backend's own
  session otherwise) so downstream code calls stitched and unified identically;
  `null_duplex` raises with the config shape when no unified model exists.

* **Video** (`video/`): `Frame`(bytes,w,h,format,ts) in → `VideoEvent`
  (caption/motion/summary/error + ts) out.  Files: `ingest_video` picks native
  `--video` for short clips (auto: `duration*fps ≤ 8`), falls back to the
  ffmpeg+SmolVLM2 frame loop with per-caption timestamps.  Live:
  `push_frame` (motion energy + bounded ring + caption batches + optional
  rolling summary hook) and `attach_track` (lazy livekit, sub-sampled, stop
  event).  OUT: `card_video`, `annotate_video`, `relay_frames` (ffmpeg only)
  and `publish_frames_livekit`.

---

## 3. Lessons worth keeping (already baked into the code)

1. **drawtext escaping: use `textfile=`, not `text=`.**  The quoted +
   backslash-escaped form (`text='It\'s …'`) fails outright on this ffmpeg; a
   per-line temp file + `expansion=none` renders apostrophes, `%`, commas,
   brackets and colons verbatim (verified live).  Only sandbox paths reach the
   filtergraph.
2. **drawtext does not wrap.**  Long captions on a 320 px clip were silently
   cropped both sides (saw it in the first annotated frame).  `render.wrap_for_width`
   now wraps to the ffprobe-measured source width (3 lines + ellipsis).
3. **`llama.cpp` b10903 Vulkan intermittently segfaults *after printing its
   output*** (rc=-11; observed 3× tonight across `llama-tts`, `llama-mtmd-cli`
   ASR and a frame caption — complete artifact, clean transcript on stdout).
   The backends now accept usable output when the process exits abnormally and
   record `exit_code`/`warning` in `Part.meta` (logged for caption paths); a
   crash with no output still raises `BackendError`.  This is a real flake
   source to expect in production voice/video runs.
4. `whisper-cli` `-nt` still needs timestamp-line stripping (parser handles it);
   Qwen3-ASR stdout is `language English<asr_text>…` (parser handles it).

---

## 4. Verified-vs-not matrix

| Capability | State |
|---|---|
| registry register/get/list/describe; unknown-backend + kind errors | ✅ unit-tested |
| profile stitched/unified parse, typos, missing/unknown, fallback, warnings | ✅ unit-tested |
| cascade push (understand/think/express), session facade, unified delegation | ✅ unit-tested (fakes) |
| subprocess backends: argv, `-c 1024`/`-c 8192`, timeouts, GPU env, stdin | ✅ unit-tested (mocked runner) + real bounded runner tests |
| audio_in dispatch → real transcript (`local.whispercpp`, `local.qwen3asr`) | ✅ **live** (step c) |
| audio_out dispatch → real WAV (`local.piper`, `local.qwen3tts`) | ✅ **live** (step c) |
| video ingest → real captions (native + frames, timestamps) | ✅ **live** (step a) |
| card / annotate / relay renderers | ✅ **live** (step b; relay unit-tested + hand-verified in dev) |
| cascade facade dry-run audio_in→text on jfk.wav | ✅ **live** (step d) |
| `attach_track` (livekit video track → batch captions) | ⚠️ implemented + unit-tested with fake livekit; **not joined live** (C4b rule) |
| `publish_frames_livekit` (frames → LiveKit track) | ⚠️ implemented + unit-tested with fake livekit; **not joined live**; wire-level publish proven by C4a's audio probe |
| `local.llama_server` HTTP chat / ASR server transport | ⚠️ implemented + unit-tested (mocked HTTP); not live-run (no server started tonight) |
| unified/duplex single model | ❌ omitted by design — `null_duplex` + config shape documented (doc §1/§5) |

Also verified: `video/publish.py` (and the whole video package) imports **without**
importing livekit (subprocess test asserts `livekit not in sys.modules` after
import).  No GPU processes or VRAM left behind after runs (`nvidia-smi` → 0 MiB;
no orphan `llama-*`/`ffmpeg`/`whisper-cli`).

---

## 5. Live evidence (`status/c5-evidence/`)

Rerun (bounded, nice'd, no downloads):
```bash
cd /home/agent/workspace/fluxer && . gpu/gpu-env.sh
nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python scripts/c5_live_evidence.py
```

Canonical run 2026-09-11 09:06:55–09:07:40Z (44 s wall, all four steps `ok`;
console in `c5-run.console.log`, machine summary in `summary.json`):

* **(a) video ingest** — dog clip regenerated with the exact §3.4 command
  (`inputs/dog-2s.mp4`, 27 488 bytes):
  * auto mode → native `--video` path, 1 caption in 11.6 s
    (*"In the image, a small black puppy is the main subject…"*);
  * frame loop (fps=1) → 2 captions with ts 0.0 / 1.0 s in 6.7 s
    (*"…black Labrador Retriever sitting on a wooden surface…"*).
  Raw events: `a_ingest_events.jsonl`.
* **(b) renderers** — `renders/card.mp4` (3.000 s, 640×360, h264) and
  `renders/annotated.mp4` (2.000 s, 320×240, h264, 2 caption events overlaid);
  `annotated-mid.png`/`card-mid.png` visually verified (wrapped caption box,
  readable cards).
* **(c) registry dispatch** — `c_registry.json`:
  * `local.whispercpp` → exact jfk transcript, 9.4 s (CPU);
  * `local.qwen3asr` → exact transcript (punctuation included), 1.8 s (GPU);
    this run hit the teardown segfault *after* printing — accepted with
    `exit_code: -11` + warning in `Part.meta` (the §3 lesson in action);
  * `local.piper` → `audio/piper.wav` (3.52 s) ; `local.qwen3tts` →
    `audio/qwen3tts.wav` (2.40 s, 115 244 B) ;
  * closed loop: whisper.cpp re-transcription of both WAVs
    (`audio/retranscribe.json`) — intelligible, matches to within tiny.en's
    error rate on the phrase "Fluxer omni" (first run: exact
    "Hello from the Fluxer Omni Engine.").
* **(d) cascade dry-run** — profile `c5-probe` (stitched: `audio_in →
  local.whispercpp`, `text_out → agent`), `Cascade.collect(audio part)` →
  exact jfk transcript as a text `Part` (`d_cascade.json`).

---

## 6. Tests

81 new tests (`test_omni.py` 45, `test_video.py` 36), all mocked-subprocess
except intentionally-real bounded ones (`run_command` timeout/spawn,
`run_ffmpeg` rc paths, lazy-import check).  Coverage highlights: registry
lifecycle, argv builders (incl. `-c 1024` / `-c 8192` / `-nt` / `--video-fps`),
env (`VK_DRIVER_FILES`, `LD_LIBRARY_PATH`), parse helpers, profile matrix
(stiched/unified/typos/missing/unknown/multiple/fallback/warnings), cascade
fakes + session FIFO + closed-session error, null/agent errors, teardown-crash
tolerance, frame validation, motion energy, duplexer state machine (push/pull
ordering, batch auto-scheduling, summary hook, error events, close semantics),
ingest auto/native/frames + fallback + partial failures, `attach_track` &
`publish` with fake livekit modules, renderers (textfile contents, enable
windows, wrapping, `gen_backend` seam), video package import-laziness.

---

## 7. Integration notes (for the voice wave / adapter merge)

* **Voice lane (C4b / wave 3)**: consume `omni.resolve_profile(cfg.extra,
  profile_name)` for the `omni` profile selection; for a turn use
  `Cascade(profile, think=<agent callable>).collect(audio_part,
  output_senses=("audio",))` and play the returned audio `Part`.  For a future
  unified model, call `await duplex_session(profile)` instead — same session
  interface.  `CascadeSession` is FIFO half-duplex today; interruption is the
  caller's policy (drop the TTS, restart the loop), as the feasibility doc says.
* **adapter.py**: no changes made by C5 (C4b owns it).  Suggested wiring:
  map inbound voice/audio media to `Part.audio(path)` (media.py already caches
  paths), use `registry.get_backend("local.qwen3asr")` when GPU budget allows,
  `local.whispercpp` otherwise; video lane: `LocalVideoDuplexer.attach_track`
  once a remote video track exists, `publish_frames_livekit` to send frames
  back; both are lazy-import seams — no livekit dependency at import.
* **Deferred integration tests (need a human/second bot in the channel)**:
  1) join voice → `attach_track` → captions land in the transcript;
  2) `publish_frames_livekit` → a second participant sees video
     (`candidate_pair: PAIR_SUCCEEDED`, `frames_pushed` stats).
  Keep out of tonight's channel per the C4b rule.

## 8. Unknowns / open items

* The rc=-11 teardown flake is **handled but not root-caused** (llama.cpp
  b10903 Vulkan + Pascal; happens at/after output).  Watch for it in long-lived
  runs; if it ever interrupts output mid-stream, the failure surfaces normally.
* Caption quality is SmolVLM2-256M-grade (generic, sometimes repeats phrases);
  `local.smolvlm` accepts any llama-mtmd-cli-capable caption model if upgraded.
* `native_max_frames=8` is a heuristic from the verified 2 s@2 fps case (`-c 8192`);
  longer native clips may need more ctx or will fall to the frame loop.
* `attach_track`/`publish` API shapes were read from installed livekit 1.1.18
  and faked in tests; first live join may still surprise (e.g. video frame
  formats beyond RGB24 are converted, others rejected with a clear error).
* `piper` output re-transcription shows minor artifacts ("Fluxaromni") — a
  tiny.en artifact on an unfamiliar token, not a failure; quality-by-ear
  assessment remains open (as in the feasibility doc §7).
