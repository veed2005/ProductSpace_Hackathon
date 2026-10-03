# Formline

**Any form, any phone.** Call or text one number to fill out government and public-service forms, or text a photo of a letter to get it explained in plain language, in your language. Works from a flip phone.

A live web dashboard shows forms filling in, documents being explained, and memory being reused in real time.

> Prototype for a hackathon. Uses fake personas only. Nothing is submitted to any government system; completed forms end as verified, downloadable PDFs.

**Team:** read [docs/TEAM.md](docs/TEAM.md) first (lanes, file ownership, git workflow). The full spec is in [docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md).

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env        # then fill in keys
uv run uvicorn app.main:app --reload
```

Chat with Formline in the terminal (no Twilio needed):

```bash
uv run python scripts/simulate.py --phone +15550001111 --channel sms
```

- Health check: http://localhost:8000/health
- Dashboard: http://localhost:8000/dashboard

Run tests:

```bash
uv run pytest            # offline tests (LLM mocked)
uv run pytest -m live    # tests that hit the real Anthropic API
```

## Twilio + ngrok setup

1. **Expose the local server.**
   ```bash
   ngrok http 8000
   ```
   Copy the `https://….ngrok-free.app` URL into `PUBLIC_BASE_URL` in `.env` and restart the server.
2. **Get a Twilio number** with Voice, SMS, and MMS capability (Console → Phone Numbers → Buy a number). On a trial account you can only text and call verified numbers, so verify each demo phone under *Verified Caller IDs*.
3. **Point the number at Formline** (Console → Phone Numbers → your number):
   - *Messaging* → "A message comes in" → Webhook, `POST`, `{PUBLIC_BASE_URL}/twilio/messaging`
   - *Voice* → "A call comes in" → Webhook, `POST`, `{PUBLIC_BASE_URL}/twilio/voice`
4. **Credentials.** Put `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_PHONE_NUMBER` in `.env`.
5. **Signature validation** is on by default. Set `TWILIO_VALIDATE_SIGNATURES=false` only for local testing with hand-crafted requests.

The ngrok URL changes every time you restart ngrok on a free plan, so update `PUBLIC_BASE_URL` and the Twilio webhooks when it does (or use a reserved ngrok domain).

## Repo layout

```
app/
  main.py, config.py, db.py, models.py
  core/        handle_turn, identity/PIN, intent router, channel style rules
  engines/     form engine, document engine, status engine
  memory/      canonical profile facts with provenance and freshness
  pdf/         fill + verify
  llm/         Anthropic client, tool definitions, prompts
  channels/    Twilio messaging webhook, voice (ConversationRelay)
  dashboard/   live partner dashboard
forms/<form_id>/   form.pdf, schema.json, meta.json
scripts/       simulate.py, ingest_form.py, seed_demo.py, reset_demo.py
tests/
docs/          TRADEOFFS.md, PRIVACY.md, DEMO_SCRIPT.md
```

## Build status

- [x] Phase 0: setup, models, FastAPI skeleton
- [x] Team scaffolding: lane contracts and working stubs, sample form, simulator, CI
- [x] D1: canonical profile keys, demo seed/reset
- [x] D2: live dashboard v1 (people, transcript, fields by source, verification, PDF download)
- [ ] Phase 1: brain + CLI simulator
- [ ] Phase 2: SMS/MMS
- [ ] Phase 3: document engine
- [ ] Phase 4: PDF fill, verification, run-back
- [ ] Phase 5: memory
- [ ] Phase 6: form ingestion
- [ ] Phase 7: voice
- [ ] Phase 8: dashboard + metrics
- [ ] Phase 9: reminders
- [ ] Phase 10: demo hardening
