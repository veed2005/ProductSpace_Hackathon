# Lane B brief: Channels + identity

**Owner:** @Edoubek1024 · **Stage plan:** [docs/PLAN.md](../PLAN.md) · **Full spec:** [docs/PROJECT_BRIEF.md](../PROJECT_BRIEF.md) (read Sections 1, 4, 5, 6, 7 "Identity and privacy", 14)

You own how people reach Formline and how Formline knows who they are. That means the Twilio number (SMS, MMS, and voice through ConversationRelay), webhook security, PINs and shared phones, and the reminder texts. Voice and messaging are equally important, first-class channels. Both call the same brain: `handle_turn(TurnRequest) -> TurnResult` (Lane A).

## You own

`app/channels/*` (messaging, voice, outbound), `app/core/identity.py`, `app/reminders.py`, `tests/test_channels_*.py`, `tests/test_identity_*.py`, `tests/test_reminders_*.py`, plus the Twilio console, ngrok, and the team's shared `.env` values (share them privately, never in git).

## You call

| Need | Function | Lane |
|---|---|---|
| The brain | `core.turn.handle_turn(TurnRequest) -> TurnResult` | A |
| Logging | `events.log_event / log_activity / log_message` | D |

**Others depend on you for:** real SMS in and out at M1 (everyone), `identity.*` (A), `reminders.create_reminder` (A) and `reminders.send_now` (D's demo controls), `identity.reset_pin` (D's dashboard), and `channel_switch` and `voice_latency` metrics events (D).

## Phases

### B1: Twilio live *(Stage 1, first hour; everyone needs this)*
- Buy or configure **one number** with Voice, SMS, and MMS.
- **Check US messaging registration now.** Twilio normally requires A2P 10DLC registration for US long-code SMS. On a trial account, verify every teammate's phone and confirm that texts deliver.
  - If registration blocks you, tell the team right away. Options are a trial account with verified numbers, a toll-free number (needs verification), or a paid account.
- Run `ngrok http 8000`, ideally with a reserved domain so the URL doesn't change. Point the number's Messaging and Voice webhooks at it (steps in the README).
- Share `TWILIO_*` and `PUBLIC_BASE_URL` with the team privately.
- **Done when:** a text from your phone to the number gets the placeholder menu back.

### B2: Robust messaging *(Stage 1 → Checkpoint M1)*
- Validate `X-Twilio-Signature` with `twilio.request_validator.RequestValidator`. Rebuild the URL from `PUBLIC_BASE_URL` (ngrok rewrites the host). Skip validation when `TWILIO_VALIDATE_SIGNATURES=false`.
- **MMS:** download `MediaUrl0..N` with HTTP basic auth (account SID and auth token) to `data/media/<MessageSid>_<i>.<ext>`, and pass the paths in `TurnRequest.media_paths`.
- Deduplicate Twilio retries by `MessageSid`.
- Never return a 500 to Twilio: on any error, reply with a friendly "Sorry, something went wrong. Please try again."
- Split replies longer than ~1,500 characters.
- Tests: `tests/test_channels_messaging.py`, with signature validation on and off and a mocked media download.
- **Done when:** Checkpoint M1 passes, and sending a photo produces a file in `data/media/`.

### B3: Identity hardening *(Stage 2)*
- **Shared phones:** helper functions so A can list a phone's profiles and switch the session's `profile_id`.
- **PIN:** lock after 3 wrong attempts. Set `Session.pin_verified_at` on success. `reset_pin` is used by the dashboard.
- `note_channel`: `log_event("channel_switch", from_=..., to=...)` when the channel changes.
- Write up the PIN model and its limits for D's `docs/PRIVACY.md`: caller ID can be spoofed, so the PIN gates any read-back.
- **Done when:** tests cover two profiles on one phone, lockout, and reset.

### B4: Reminders *(Stage 2 → Checkpoint M2)*
- `start_scheduler()`: an APScheduler `BackgroundScheduler` that checks every minute and calls `send_now` for pending reminders whose `due_at` has passed.
- Keep `create_reminder` and `send_now` signatures stable (A and D use them).
- Reminder texts must never include sensitive values.
- **Done when:** a reminder due one minute from now arrives on a real phone, and `send_now` works from a Python shell.

### B5: Voice via ConversationRelay *(Stage 3 → Checkpoint M3)*
- `POST /twilio/voice` returns TwiML `<Connect><ConversationRelay url="wss://<PUBLIC_BASE_URL host>/twilio/voice/relay" .../>`, with a welcome greeting and language and voice settings.
- **Websocket** `/twilio/voice/relay`:
  - On each `prompt` message, call `handle_turn` (via `run_in_threadpool`) and send the reply back as `text` tokens.
  - Honor `TurnResult.end_call` and `TurnResult.language` (switch the TTS voice).
  - Send `followup_sms` via `send_sms`.
  - Handle `interrupt` and `dtmf` messages; DTMF is handy for entering the PIN.
- **Latency is everything:** measure from prompt received to first token sent, and log `voice_latency`. If replies are slow, stream partial text.
- If ConversationRelay is unavailable or problematic, **tell the team before switching** to Media Streams with a separate speech-to-text and text-to-speech provider.
- **Done when:** Checkpoint M3 passes (call, Spanish, hang up, finish by text).

### B6: Demo hardening *(Stage 4)*
- Fallback TwiML if the websocket fails ("Please text this number instead").
- Pre-verify the demo phones, including the flip phone.
- Record a successful call as backup footage for D's backup video.

## How to work

- Start a Claude Code session in this repo and say: **"Start Phase B1."** The session hook tells Claude you're Lane B and points it at this file.
- Without a phone, test with the simulator (`uv run python scripts/simulate.py`) or by sending form posts to `/twilio/messaging` with signature validation off.
- After each phase: tests pass → PR → merge → post a summary in chat.
