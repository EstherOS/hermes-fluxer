# Hermes-Fluxer — a Fluxer plugin for Hermes Agent

[Hermes Agent](https://hermes-agent.nousresearch.com/docs) is a personal AI agent that lives in your chats. This repo connects it to **[Fluxer](https://fluxer.app)** — a chat platform for communities, friend groups, and DMs — so your agent can live in your Fluxer guilds: answering messages, running commands, sharing files, and joining voice channels to talk.

> **This is a monorepo.** Using Fluxer with Hermes needs only the plugin in `plugins/hermes-fluxer/`. The two `packages/` directories hold an optional realtime engine for advanced voice & video pipelines — if they gain traction, they may be split into their own repositories.

## What it does

- **Chat** — replies in guild channels and DMs. In guilds the bot answers when it's @mentioned (or when a message starts with `/`); a channel can be set to free-response, where it answers everything.
- **Commands** — see [Slash commands](#slash-commands).
- **Attachments** — receives images, files, and voice notes; sends files back.
- **Voice & video** — joins Fluxer voice channels and talks out of the box on Hermes' own voice pipeline: your configured speech-to-text hears the room, the agent answers, and your text-to-speech speaks the reply back into the channel. Plug in `omnimaker` when you want more — streaming, local model backends, custom graphs, a video lane. Text and attachments work everywhere.

## Setup

You'll need a running Hermes Agent (see the [docs](https://hermes-agent.nousresearch.com/docs)) and a bot token from [fluxer.app](https://fluxer.app).

1. **Install the plugin** — from a clone of this repo:

   ```bash
   git clone https://github.com/ygypt/hermes-fluxer
   cd hermes-fluxer
   cp -r plugins/hermes-fluxer/src/fluxer ~/.hermes/plugins/fluxer
   hermes plugins enable fluxer-platform
   ```

2. **Give Hermes the token** — run `hermes gateway setup` and pick Fluxer (it prompts for the token), or write it into `~/.hermes/.env`:

   ```bash
   FLUXER_BOT_TOKEN=<id>.<secret>        # create a bot app in Fluxer; the token is "<id>.<secret>"
   FLUXER_ALLOWED_USERS=<your user id>    # who may talk to the bot (fail-closed)
   # FLUXER_HOME_CHANNEL=<channel id>     # optional: default channel for cron/notifications
   ```

3. **Restart the gateway** — `hermes gateway restart` — then DM the bot or @mention it in a guild.

Voice needs LiveKit and a Hermes STT/TTS provider — listening and speaking ride Hermes' own voice pipeline. `omnimaker` is optional (streaming, local models, custom graphs, video). Voice keys live under `gateway.platforms.fluxer.extra.voice`; engine profiles under the top-level `omni:` section (template in `docs/ACTIVATION.md`).

## Slash commands

- `/new` — start a fresh conversation
- `/stop` — stop the current run
- `/model` — show or change the model
- `/approve`, `/deny` — answer a pending dangerous-command approval (`/approve once|session|always`)
- `/voice` — toggle voice replies; `/voice on|off|tts` set the mode, `/voice join|leave|status` act on voice channels
- `/sethome` — make this chat the home channel (where cron jobs and notifications are delivered)
- `/update` — update Hermes Agent in place
- `/help` — everything else Hermes supports

## Omnimaker — the realtime engine

`packages/omnimaker` is a small streaming engine that wires "senses" into a graph: nodes with roles — ears (speech → text), mouth (text → speech), thinker (the agent), eyes (images) — connected by routes. The Fluxer plugin uses it when you configure an engine profile — streaming pipelines, local model backends, and the video lane. It contains no platform code, so any platform can drive it.

Docs: [`packages/omnimaker/docs`](packages/omnimaker/docs/README.md)

## Omnimaker-Hermes — the Hermes adapter

`packages/omnimaker-hermes` plugs a real Hermes agent into that graph as a node (`hermes/session`): full tool access, memory, and personality, driven by the engine like any other backend.

Docs: [`packages/omnimaker-hermes/docs`](packages/omnimaker-hermes/docs/README.md)

## Repository layout

- `plugins/hermes-fluxer/` — the plugin: Fluxer REST + gateway clients, adapter, attachment handling, voice/omni/video lanes, tests
- `packages/omnimaker/` — the graph engine
- `packages/omnimaker-hermes/` — the Hermes agent adapter for the engine
- `docs/` — specs, API notes, activation guide, model & GPU notes
- `tests/` — headless end-to-end pipeline tests
- `scripts/` — probes and live-test drivers used during development
- `status/` — development reports and evidence

## Development

```bash
# unit tests (the suite imports gateway.*, so use a Hermes checkout venv)
cd ~/.hermes/hermes-agent
./venv/bin/python -m pytest <repo>/plugins/hermes-fluxer/src/fluxer/tests -q

# plugin health check: import + registration against the real runtime
hermes plugins doctor <repo>/plugins/hermes-fluxer/src/fluxer
```

## License

MIT — see [`LICENSE`](LICENSE).
