# C3 report — Fluxer plugin, wave 2: attachments

Date: 2026-09-11 (08:31–08:40 UTC). Author: C3. Spec: `docs/spec-fluxer-plugin.md`
§2.4/§6-W2 (`W2 attachments`) + `docs/hermes-plugin-integration.md` §6 + `docs/fluxer-api-notes.md`
§2/§3. All work under `/home/agent/workspace/fluxer/`; the Hermes checkout was read-only
(its local dirty state pre-dates the shift — mtimes 00:36–01:55; C3 edited nothing outside
the workspace). The live gateway (PID 1) was never touched; no gateway commands were run; the
sandbox gateway was **not** running during any live step (`scripts/sandbox_status.sh` →
"Gateway is not running"), so the standalone adapter held the scoped bot lock freely.

## 1. Deliverables

| # | Deliverable | Path | State |
| --- | --- | --- | --- |
| 1 | Inbound/download + cache helpers (`download_attachment`, `message_type_for`, `cache_inbound_attachments`) | `plugin-src/fluxer/media.py` (new, 264 lines) | done |
| 2 | Adapter media methods (inbound wiring + `send_image_file`/`send_image`/`send_voice`/`send_video`/`send_document`/`send_multiple_images`) | `plugin-src/fluxer/adapter.py` (710 → 992 lines) | done |
| 3 | `create_message(..., flags=None)` — additive kwarg for the VOICE_MESSAGE attempt | `plugin-src/fluxer/rest.py` (506 → 510 lines) | done (deviation, §7) |
| 4 | Unit tests | `plugin-src/fluxer/tests/test_media.py` (new, 744 lines, 45 tests) | 126 passed total |
| 5 | Live E2E driver (+ bounded voice-note probe mode) | `scripts/e2e_media.py` (new, 551 lines) | live PASS |
| 6 | Evidence (logs + JSON + pytest/validate/doctor output) | `status/c3-evidence/…` | done |
| 7 | This report | `status/c3-report.md` | done |

## 2. What was built

**`media.py`** (spec §6-W2 "inbound download→cache + media_urls/media_type"):
- `download_attachment(url, *, timeout=60, max_bytes=None) -> bytes` — aiohttp GET with **no
  Authorization header** (Fluxer media reads are public; the path is the credential — api notes
  §3/§5), streamed read, `Content-Length` + running-total enforcement against
  `get_inbound_media_max_bytes()` (0/negative disables; same cap semantics as the shared cache
  funnels). Raises `AttachmentDownloadError` with clear reasons (invalid URL, HTTP status,
  timeout, transport error, oversize).
- `message_type_for(mime, *, is_voice_note=False)` — `image/*` → PHOTO; `audio/*` → VOICE when
  the voice-note flag is set else AUDIO; `video/*` → VIDEO; else DOCUMENT (charset params
  stripped).
- `cache_inbound_attachments(attachments, *, flags=0) -> (media_urls, media_types,
  message_type|None)` — per attachment: MIME from `content_type` (filename fallback when
  missing/octet-stream), oversize → skip + WARN (no download), download → WARN+skip on failure,
  cache via the class-specific helpers (`cache_image_from_bytes` / `cache_audio_from_bytes` /
  `cache_video_from_bytes` / `cache_document_from_bytes`, real signatures), strongest
  `message_type` wins: **image > voice > audio > video > document** (task priority; Discord’s
  `_attachment_message_type` uses first-attachment only — documented deviation). Voice-note
  detection: `flags & 8192` on the message **or** the attachment.

**`adapter.py` inbound** (`_handle_message_create`): when `msg["attachments"]` is non-empty,
caches via `cache_inbound_attachments` (inside try/except — media failure still delivers the
text), sets `MessageEvent.media_urls` / `media_types` / `message_type`, logs
`Fluxer: cached N attachment(s) for message <id> in <chat> (mime, …)`. Empty text + nothing
cached → `(the user sent an attachment that could not be retrieved)` (wave-1 placeholder
replaced). No-attachment behavior unchanged.

**`adapter.py` outbound** (all return `SendResult`; failure mapping reuses `_send_error`):
- `_send_media(...)` — `upload_attachment` → `create_message(attachments=[claim],
  content=first caption chunk, flags=…)`; remaining caption chunks follow as plain `send()`
  calls (never truncated; a continuation failure returns `success=False` with the media id in
  `message_id`). Missing file / not connected / upload error / create error → `success=False`
  (never a text-only “success”; integration §6.2).
- `send_voice` attempts `flags=8192` once; on a 400 the flag is dropped and the message retried
  without it (live-verified: the same upload claim is reusable after the rejected attempt).
- `send_image(url_or_path)` — `http(s)` URLs are downloaded to a temp file first (cap =
  `rest.MAX_ATTACHMENT_BYTES`, 50 MiB) then uploaded; `file://`/local paths upload directly;
  temp file always cleaned up. `send_multiple_images` bundles `(url, alt)` into messages of ≤10
  attachments (temp downloads, per-image skip on failure, success when ≥1 image delivered —
  base #106153 contract). `send_image_file`/`send_video`/`send_document` (`file_name`
  override) are thin wrappers.

## 3. Unit tests

```
cd /home/agent/.hermes/hermes-agent
./venv/bin/python -m pytest /home/agent/workspace/fluxer/plugin-src/fluxer/tests -q
→ 126 passed in 7.92s        (81 existing + 45 new; evidence: status/c3-evidence/pytest.txt)
```

`test_media.py` covers: `message_type_for` mapping incl. voice-note flag and charset params;
`download_attachment` happy path / HTTP error / streamed size cap / Content-Length cap /
invalid URL (fake aiohttp); `cache_inbound_attachments` happy path, per-class helper use,
strongest-media selection (video+image → PHOTO, doc+audio → AUDIO, voice note > plain audio),
oversize skip (download never called), download-failure tolerance, filename-hint documents,
filename→MIME fallback, empty input; adapter inbound (attachments land on the event, flags
passthrough, cache failure keeps the text, attachment-only message, unretrievable placeholder);
outbound (`send_image_file` success/missing file/5xx-retryable/400-not-retryable, caption
chunking + continuation-failure mapping, voice flag attempt + 400-retry-without + log text,
video, document `file_name`, not-connected for all five, URL download→temp→upload with cleanup
and download-failure honesty, local path + `file://`); multi-image single-message/chunking>10/
all-missing; command pass-through (`/new` unchanged, `<@bot> /stop` → `/stop`, `/new` with an
attachment keeps text+media, mid-text `/new` untouched).

Plugin checks (path-based, no home):
`hermes plugins validate` → exit 0 “Validation passed.”; `hermes plugins doctor` → exit 0
(`status/c3-evidence/plugins_validate.txt`, `plugins_doctor.txt`).

## 4. Live E2E (standalone adapter, nice -n 10, bounded)

```
cd /home/agent/workspace/fluxer
nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python scripts/e2e_media.py
→ RESULT: PASS (20/20 checks), exit 0        (logs: status/c3-evidence/e2e_media.{log,json})

nice -n 10 … scripts/e2e_media.py --voice-note-only
→ VOICE-NOTE PROBE: PASS (5/5 checks), exit 0  (e2e_media_voicenote.{log,json})
```

Main run (all in #general `1547815091221561347`, captions tagged `[c3-e2e]`):

| Send | Result | Read-back (REST `list_messages`) |
| --- | --- | --- |
| `send_image_file(test-dog.jpg)` | msg `1547888414701916160` | `test-dog.jpg`, 49874 B, `image/jpeg` |
| `send_voice(piper-test.wav)` | msg `1547888425036681216` | `piper-test.wav`, 179476 B = source |
| `send_video(c3_e2e_video.mp4)` | msg `1547888434327064576` | 4586 B = source, `video/mp4` |
| `send_document(c3_e2e_doc.txt)` | msg `1547888442585649152` | 39 B = source, `text/plain` |

- **Download proof**: document URL fetched with `media.download_attachment` → byte-exact
  (sha256 `75a9ec58f4af9a2d`, both 39 B). Image URL → 49874 B = declared size, JPEG magic
  `ff d8 ff`.
- **Image normalization (finding, not a bug)**: Fluxer re-encodes uploaded images —
  source 50098 B → stored/served 49874 B (Δ −224). Byte-equality therefore holds for
  voice/video/document but **not** for images; the harness asserts declared-size + magic for
  images instead (first harness iteration failed only on this and was corrected).
- **Inbound cache (live)**: `cache_inbound_attachments` ran against the real read-back
  attachments for all four classes → local files created under the standard cache dirs, correct
  MIME/MessageType (photo/audio/video/document), image cached bytes == platform-served bytes
  (sha `769f940565d02485`), others byte-exact vs source.
- **Full adapter path (live, mocked handler)**: the read-back document + image payloads were fed
  through `adapter._handle_message_create` with the author rewritten to the non-self user id →
  one `handle_message` call each, event carries `media_urls` (existing local files),
  `media_types` `["text/plain"]` / `["image/jpeg"]`, `message_type` document/photo, text intact;
  log lines `Fluxer: cached 1 attachment(s) …` + `Fluxer: message from c3-e2e-external in
  general (trigger=free_response)` captured.
- **VOICE_MESSAGE 8192 (live-verified mechanics)**: with a caption the create-message 400s
  (`INVALID_FORM_BODY` / `VOICE_MESSAGES_CANNOT_HAVE_CONTENT`); captionless it 400s too
  (`VOICE_MESSAGES_ATTACHMENT_WAVEFORM_REQUIRED` — the attachment claim must carry `waveform`
  data). Both times the adapter’s retry-without-flag path ran, reused the same upload claim and
  delivered the audio as a plain attachment (recorded: `voice_flag_outcome.retried_without_flag
  = true`). Native voice-note rendering is **not** achievable yet — waveform synthesis is a
  voice-lane (W3/C4) follow-up (openapi documents `waveform` only as “base64 encoded audio
  waveform data”; no sample format/count — do not guess).
- **Cleanup**: 9 messages created across the three live runs, all deleted; read-back after
  cleanup shows `remaining_tracked=[]`, `stray=[]`, channel back to its pre-run state
  (`pre_snapshot_count == channel_message_count == 1`). The one remaining message
  (`1547829325074534400`, older than this run, empty content) pre-exists C3 and was left
  untouched.
- **Hygiene**: 1.2 s sleeps between live writes, no 429s anywhere, ≤10 live messages total (9),
  bot token never printed, all temp files (`/tmp/c3_e2e_video.mp4`, `/tmp/c3_e2e_doc.txt`) removed.

## 5. Live-verified vs unit-only

| Hop | Live | Unit |
| --- | --- | --- |
| Outbound image/voice/video/document upload → attachment read-back | ✅ | ✅ |
| Inbound `download_attachment` (document byte-exact, image probe) | ✅ | ✅ (mocked aiohttp incl. caps) |
| `cache_inbound_attachments` all four media classes | ✅ | ✅ |
| Adapter inbound event (media_urls/types/message_type, non-self author → mocked handler) | ✅ | ✅ |
| Voice flag attempt + 400 → retry-without-flag (same claim) | ✅ | ✅ |
| Cleanup + channel verification | ✅ | n/a |
| `send_image` URL→temp→upload | — | ✅ (mocked download) |
| `send_multiple_images` (batching/chunking) | — (unit-only; same upload path as live sends) | ✅ |
| Caption chunking / continuation-failure mapping | — | ✅ |
| Error mapping (5xx/400/transport) | — | ✅ |

## 6. Deviations & notes

1. **`rest.py` additive change (PM note)**: `FluxerREST.create_message` gained
   `flags: int | None = None` (body field only when not None). Spec §2.1’s kwarg list did not
   include it, but §6-W2 requires the VOICE_MESSAGE attempt; additive and optional, no existing
   call site changes behavior.
2. Strongest-media `message_type` instead of Discord’s first-attachment rule (task-specified).
3. Voice notes: flag attempted but always falls back (platform requirements above) — audio is
   always delivered; the native voice bubble needs `waveform` (+ likely `duration`) in the claim.
4. Wave-1 placeholder text for unretrievable attachments replaced; no test asserted the old text.
5. `send_multiple_images` implemented (trivial per task) but not exercised live.

## 7. Open / unknowns

- **Non-self inbound → agent reply** leg remains open (needs kairo: webhook route blocked by
  missing MANAGE_WEBHOOKS, or a real message) — unchanged from wave 1; C3 only verified the
  handler-path with a mock.
- `waveform` byte format/length and `duration` semantics are undocumented in the openapi copy;
  whether Fluxer accepts a synthetic (e.g. flat) waveform is untested — deliberately not
  guessed.
- Whether non-JPEG images are normalized identically (same 50098→49874 behavior observed only
  for JPEG) — irrelevant to correctness (inbound accepts any size) but worth knowing.
- Message `flags` in read-back responses were 0 for all sends (voice-flag requests never
  produced a message, as expected).

## 8. Exact commands (reproduce)

```bash
# unit + plugin checks
cd /home/agent/.hermes/hermes-agent
./venv/bin/python -m pytest /home/agent/workspace/fluxer/plugin-src/fluxer/tests -q
./venv/bin/python hermes plugins validate /home/agent/workspace/fluxer/plugin-src/fluxer
./venv/bin/python hermes plugins doctor   /home/agent/workspace/fluxer/plugin-src/fluxer

# live E2E (sandbox gateway must be stopped; standalone adapter takes the scoped lock)
cd /home/agent/workspace/fluxer
nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python scripts/e2e_media.py
nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python scripts/e2e_media.py --voice-note-only
```

Evidence index: `status/c3-evidence/pytest.txt`, `plugins_validate.txt`, `plugins_doctor.txt`,
`e2e_media.log` + `e2e_media_run.json` (20/20), `e2e_media_voicenote.log` +
`e2e_media_voicenote.json` (5/5).
