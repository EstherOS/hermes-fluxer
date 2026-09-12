# Morning demo — fluxer walkthrough (draft; finalize after wave 4)

Sandbox-first; live activation optional at the end. I (Esther) run all the setup
side; this is the list of things to try from your side.

1. **Wake-up ping** — message me in Discord; I start the sandbox gateway with the
   plugin + voice config (auto-join the test voice channel).
2. **Text (closes the last open leg)** — say anything in the test guild `#general`
   (it's a free-response channel there) or DM the bot. Expect a reply. This is the
   one live hop we couldn't close overnight without you (bot lacked MANAGE_WEBHOOKS
   for our simulated-user trick — granting it is optional but handy).
3. **Commands** — type `/new` in the DM → confirm flow (text fallback: `/approve`,
   `/always`, `/cancel`).
4. **Attachments** — send an image/file; ask the bot for a picture back.
5. **Voice** — join `General` under Voice Channels in the fluxer web client and talk.
   With transcripts on, the channel text shows what was heard/said; replies come
   back as speech. (Voice is provably working on our side — join, LiveKit, TTS out;
   the listen-from-human leg is the one you'll be testing.)
6. **Video (if wired by then)** — screenshare/camera in the call → frame captions;
   or ask for a generated clip.
7. **Go live (optional)** — `scripts/activate-live.sh` then restart the container
   (see `docs/ACTIVATION.md`; ~2 minutes).

Notes: sandbox allows all users (dev); live will be restricted to your account
(`FLUXER_ALLOWED_USERS`, already staged). Everything rolls back with
`scripts/deactivate-live.sh`.
