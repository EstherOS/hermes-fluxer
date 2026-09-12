# C1 report — Fluxer protocol clients (wave 1)

Scope: spec `docs/spec-fluxer-plugin.md` §2.1–2.3 + §4 (REST client, gateway client,
models helpers, unit tests, live selftest, this report). Written by coder C1.

## Deliverables (all exist)

| Path | What |
| --- | --- |
| `plugin-src/fluxer/models.py` | `parse_message` / `parse_user` / `parse_channel`, `AT_MENTION_RE`, small accessors |
| `plugin-src/fluxer/rest.py` | `FluxerREST` + `FluxerAPIError` (rate-limit aware, presigned uploads) |
| `plugin-src/fluxer/gatewayws.py` | `FluxerGatewayClient` (heartbeat, dispatch, resume, backoff, connection events) |
| `plugin-src/fluxer/tests/conftest.py` | sys.path bootstrap (shared with C2's tests) |
| `plugin-src/fluxer/tests/test_rest.py` | 18 unit tests, no network |
| `plugin-src/fluxer/tests/test_gatewayws.py` | 13 unit tests, local websockets mock server |
| `scripts/client_selftest.py` | live selftest (bounded, nice'd, cleans up) |
| `status/c1-selftest.log` | raw selftest output |
| `status/c1-report.md` | this file |

## Unit tests — 31 tests, all passing, no network

```
$ cd /home/agent/.hermes/hermes-agent
$ ./venv/bin/python -m pytest \
    /home/agent/workspace/fluxer/plugin-src/fluxer/tests/test_rest.py \
    /home/agent/workspace/fluxer/plugin-src/fluxer/tests/test_gatewayws.py -q
...............................                                          [100%]
31 passed in 2.19s
```

Per file (collect-only): `test_rest.py` 18 tests, `test_gatewayws.py` 13 tests.

Coverage per spec §4:

* REST: auth header (`Bot <token>`), error envelope mapping (status/code/message,
  no retry on 4xx), 429 retry honouring body `retry_after` + `Retry-After` with the
  3-retry cap and the sleep cap, 5xx backoff retry + exhaustion, transport-error
  retry, `X-RateLimit-*` bucket map, exhausted-bucket pre-sleep, message body
  shaping (Nones dropped, 201 accepted), list params, delete/typing 204 → `None`,
  open_dm body, upload singlepart (upload_url used **verbatim**, no auth header on
  the PUT, declared content-type), upload multipart (parts sorted, slices correct,
  `/attachments/complete` called), >50 MiB local reject.
* Gateway: HELLO→IDENTIFY(raw token, properties, presence, no `intents`)→READY,
  heartbeat send/ack + last-seq in beats, dispatch routing + `seq` tracking,
  per-event error isolation, resume after drop (op6 with session_id+seq), resume
  rejected → fresh IDENTIFY, close 4004 → `non_retryable: True` + no reconnect,
  heartbeat ack timeout → reconnect, backoff ladder `1,2,5,10,30,60` (capped),
  op3/op4 send helpers, 4096-byte frame guard, clean `stop()`.

Test design notes: REST tests run against local aiohttp test servers; gateway tests
against local `websockets` mock servers; a tiny `asynctest` wrapper runs async test
bodies so no pytest-asyncio dependency is needed. `conftest.py` puts the Hermes
checkout root and `plugin-src` on `sys.path`; if the package `__init__` import
(which pulls in C2's `adapter.py`) ever fails mid-edit, it falls back to a
path-only `fluxer` package stub so C1 tests stay independent.

## Live selftest — PASS (11/11), first run, no code changes needed

```
$ cd /home/agent/workspace/fluxer
$ nice -n 10 timeout 150 /home/agent/.hermes/hermes-agent/venv/bin/python \
      scripts/client_selftest.py 2>&1 | tee status/c1-selftest.log
[selftest] fluxer protocol clients live check — 2026-09-11T08:14:01.026306+00:00
  [PASS] env: FLUXER_BOT_TOKEN — loaded from /home/agent/workspace/fluxer/.env (len=63, value not printed)
  [PASS] REST get_me — username=Esther id=1547828742208888832
  [PASS] REST get_gateway_info — url=wss://gateway.fluxer.app
  [ws-conn] ready: {'session_id': 'C6DEA7C55EC799D6EFB84AE38F292727', 'user': '1547828742208888832'}
  [PASS] gateway READY — user_id=1547828742208888832 session_id=C6DEA7C55EC799D6EFB84AE38F292727
  [ws] MESSAGE_CREATE id=1547882919870074880 channel=1547815091221561347
  [PASS] REST create_message — content='[selftest] fluxer client ok 2026-09-11T08:14:06Z'
  [PASS] gateway MESSAGE_CREATE captured (own message) — content matches
  [ws] MESSAGE_UPDATE id=1547882919870074880
  [PASS] REST edit_message — edited_timestamp=2026-09-11T08:14:07.838Z
  [PASS] gateway MESSAGE_UPDATE captured (own edit) — content matches
  [ws] MESSAGE_CREATE id=1547882931865784320
  [PASS] 2500-char message probe — accepted (len=2518) id=1547882931865784320
  [cleanup] deleted message 1547882919870074880
  [cleanup] deleted message 1547882931865784320
  [PASS] cleanup: every created message deleted
  [PASS] connection events seen
RESULT: PASS (11/11)
```

Notes / interpretation:

* Own-message MESSAGE_CREATE **does** arrive on the live gateway (matches the
  `docs/captures/` finding) — captured by id + content equality.
* 2500-char message accepted → effective bot limit >2500 (the 4000 cap claim is
  plausible; boundary not probed further, per spec).
* Rate limits respected: 0.3–1.0 s sleeps between calls; 2 creates + 1 edit +
  2 deletes + connection traffic — well under the 20/10 s create bucket.
* Cleanup verified by read-back afterwards (read-only `list_messages`, limit 10):
  `leftover [selftest] messages: none` — #general contains only the pre-existing
  system message.

## Deviations / additions to the frozen interfaces

Frozen signatures in §2.1–2.3 are implemented as written. Additive, backwards-safe
choices (none change a frozen call shape):

1. `FluxerGatewayClient.__init__` takes optional keyword-only test/tuning knobs:
   `ack_timeout`, `retry_backoff`, `max_reconnect_attempts`, `start_timeout`.
   All default to spec behaviour (45 s ack timeout, ladder 1/2/5/10/30/60).
2. New `FluxerGatewayError(RuntimeError)` with `.retryable` / `.code` — used to
   surface `start()` failures (`retryable=False` for 4004/identify-rejection).
   Not in the frozen spec; the adapter can catch it or just react to connection
   events.
3. Non-retryable close codes = `{4003, 4004, 4012}` (spec requires 4004 +
   identify rejection; 4003 auth-failed and 4012 bad-version are the same class
   of terminal failures). All other closes retry on the ladder.
4. `reconnect_failed` is also emitted when consecutive reconnect attempts are
   exhausted (`max_reconnect_attempts`, default 10) with `non_retryable: False`
   so the adapter can mark degraded+fatal(retryable).
5. REST `expected` is strict: a 2xx not listed raises `FluxerAPIError`; the
   default `(200, 201, 204)` covers every route we call.
6. `upload_attachment` rejects >50 MiB locally with `ValueError` before the plan
   call (server still authoritative for `FILE_SIZE_TOO_LARGE`). `_put_bytes`
   retries transient 5xx/network once-per-backoff ladder (PUT is idempotent);
   multipart parts are PUT with no Content-Type, singlepart with the declared type.
7. `models.py` adds three small accessors (`display_name`, `message_author_id`,
   `message_is_bot`, `mentioned_user_ids`) beyond the literals in §2.3.
8. `is_connected` property on the gateway client (convenience for the adapter).
9. `conftest.py` is shared with C2's `test_adapter.py` (same directory) and
   contains the package-stub fallback described above.
10. The selftest bootstraps imports itself (checkout root + plugin-src, same
    fallback) so it does not depend on the adapter being healthy.

## Unknowns / not exercised

* Multipart upload (>10 MiB) and the 429-retry path were not exercised live (no
  large file sent, no 429 observed) — both are unit-tested against local servers.
* Gateway resume/backoff/reconnect paths are unit-tested only; the live run had
  no drop to resume from.
* Exact bot content cap: 2500 was accepted; the 4000 boundary was not probed.
* Voice (`update_voice_state` op4) is send-only here; LiveKit work is wave 3.
