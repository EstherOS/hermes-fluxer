# Activation — taking fluxer live on the main gateway

Status: **staged, not active.** The plugin runs inside the gateway process; the live
gateway is container PID 1, so activation = install + enable + **restart from outside**
(no hot reload — verified in `docs/hermes-plugin-integration.md` §2).

## Preconditions (already handled where noted)
- `FLUXER_BOT_TOKEN` in `~/.hermes/.env` ✔
- `FLUXER_ALLOWED_USERS=1473728643747861346` (kairo), `FLUXER_HOME_CHANNEL` (DM),
  `FLUXER_HOME_CHANNEL_NAME` ✔ (staged; takes effect at restart)
- Sandbox gateway stopped (same bot identity lock) — activator does this.
- Plugin enabled as a **user plugin**: `~/.hermes/plugins/fluxer` + `plugins.enabled`.
  (Bundled-copy install is the alternative; user plugin survives checkout updates.)

## Steps (run when kairo approves — ideally with him around)

```bash
# 1) stage everything (stops sandbox, copies plugin, enables, verifies)
/home/agent/workspace/fluxer/scripts/activate-live.sh

# 2) restart the container/gateway from the HOST (never from inside — PID 1):
podman restart hermes          # if managed as container "hermes"
# or, if the quadlet set is installed:
systemctl --user restart hermes.service
# or however you usually restart it.

# 3) verify
hermes gateway status
hermes logs --follow --level INFO | grep -i fluxer     # expect "Fluxer: connected as Esther"
# then DM "@Esther" on fluxer.app — the message should get a reply.
```

## Rollback
```bash
/home/agent/workspace/fluxer/scripts/deactivate-live.sh   # disable + move aside
# then restart the container the same way
```

## Notes
- Re-run `activate-live.sh` after any plugin update to sync the copy (then restart).
- Sandbox must stay stopped while live is active (one bot identity, machine-global lock).
- Voice/live-testing on the live gateway: same config keys under
  `gateway.platforms.fluxer.extra.voice` as the sandbox.

## Voice config for the live gateway

Add under `gateway.platforms.fluxer.extra` (in `config.yaml`). Keep tool paths
**absolute** — the plugin's built-in defaults are repo-relative and only correct
in the dev checkout layout, not after the user-plugin copy:

```yaml
voice:
  enabled: true
  auto_channels: []            # e.g. ["<voice channel id>"] to auto-join
  guild_id: "<guild id>"
  session_binding: channel     # ephemeral | channel | invoke
  transcripts: channel         # off | channel
  stt:
    engine: whispercpp
    threads: 2
    binary: /home/agent/workspace/fluxer-local/gpu/tools/whisper-bin-ubuntu-x64/whisper-cli
    model_path: /home/agent/workspace/fluxer-local/models/ggml-tiny.en.bin
  tts:
    engine: piper
    binary: /home/agent/workspace/fluxer-local/gpu/tools/piper/piper
    model: /home/agent/workspace/fluxer-local/models/en_US-lessac-medium.onnx
```
