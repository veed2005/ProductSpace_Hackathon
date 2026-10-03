# Formline

**Call the internet.** Have a website open on your computer, call Formline from any phone (even a flip phone or a landline), and say what you want to do. Formline operates the site for you through a Chrome extension, asks you on the phone when the site needs something only you know, and asks permission before anything final. Start here: **[docs/CALL_THE_INTERNET.md](docs/CALL_THE_INTERNET.md)** (setup, demo, safety, tests).

Callers without a paired browser fill out forms entirely by phone, in English or Spanish, with spelling checks, verified explanations, warnings about important clauses, and an emailed receipt: **[docs/PHONE_FORMS.md](docs/PHONE_FORMS.md)**.

## The original form assistant

**Any form, any phone.** Call or text one number to fill out government and public-service forms, or text a photo of a letter to get it explained in plain language, in your language. Works from a flip phone.

A live web dashboard shows forms filling in, documents being explained, and memory being reused in real time.

> Prototype for a hackathon. Uses fake personas only. Nothing is submitted to any government system; completed forms end as verified, downloadable PDFs.

**Team:** read [docs/TEAM.md](docs/TEAM.md) first (lanes, file ownership, git workflow). The full spec is in [docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (it installs the right Python for you).

```bash
git clone https://github.com/veed2005/ProductSpace_Hackathon.git
cd ProductSpace_Hackathon
sh scripts/setup.sh                       # installs deps, enables git hooks, creates .env, runs tests, shows your lane
uv run uvicorn app.main:app --reload      # always run commands from the repo folder
```

Then open Claude Code in this folder and tell it what to work on (every phase in the lane plans is merged; see [Build status](#build-status)). Setting up on a new computer: [Moving to another computer](docs/CALL_THE_INTERNET.md#moving-to-another-computer) lists what git doesn't carry (`.env`, `data/`, ngrok's token, the extension).

Chat with Formline in the terminal (no Twilio needed):

```bash
uv run python scripts/simulate.py --phone +15550001111 --channel sms
```

- Health check: http://localhost:8000/health
- Dashboard: http://localhost:8000/dashboard (only from the computer running the server, never through ngrok)

Run tests:

```bash
uv run pytest            # offline tests (LLM mocked)
uv run pytest -m live    # tests that hit the real LLM API
uv run python scripts/e2e_golden_path.py   # browser agent end to end: Chromium + extension + simulated call
```

## Twilio + ngrok setup

1. **Expose the local server.** Run `ngrok config add-authtoken <token>` once per computer. With a reserved domain (the team has one; it's in `PUBLIC_BASE_URL`):
   ```bash
   ngrok http --url=https://your-name.ngrok-free.dev 8000
   ```
   Without one, run `ngrok http 8000`, copy its `https://…` URL into `PUBLIC_BASE_URL` in `.env`, and restart the server.
2. **Get a Twilio number** with Voice, SMS, and MMS capability (Console → Phone Numbers → Buy a number). On a trial account you can only text and call verified numbers, so verify each demo phone under *Verified Caller IDs*.
3. **Point the number at Formline** (Console → Phone Numbers → your number):
   - *Messaging* → "A message comes in" → Webhook, `POST`, `{PUBLIC_BASE_URL}/twilio/messaging`
   - *Voice* → "A call comes in" → Webhook, `POST`, `{PUBLIC_BASE_URL}/twilio/voice`
4. **Credentials.** Put `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_PHONE_NUMBER` in `.env`.
5. **Signature validation** is on by default. Set `TWILIO_VALIDATE_SIGNATURES=false` only for local testing with hand-crafted requests.
6. **Account settings.** Outgoing calls need US geo permissions, and outgoing texts need A2P 10DLC registration. Neither is in `.env`; see [Twilio account settings](docs/CALL_THE_INTERNET.md#twilio-account-settings).

Without a reserved domain the ngrok URL changes every time ngrok restarts, so update `PUBLIC_BASE_URL` and rerun `uv run python -m app.channels.twilio_setup` (which sets the webhooks in step 3) when it does.

## Repo layout

```
app/
  main.py, config.py, db.py, models.py
  core/        handle_turn, identity/PIN, intent router, channel style rules
  engines/     form engine, document engine, status engine
  memory/      canonical profile facts with provenance and freshness
  pdf/         fill + verify
  llm/         Anthropic or OpenAI client, tool definitions, prompts
  channels/    Twilio messaging webhook, voice (ConversationRelay)
  dashboard/   live partner dashboard (and the browser agent's Agent tab)
  agent/       browser agent: call controller, decision loop, policy, prompts
  browser/     extension pairing, websocket hub, page sanitizing, PDF reading
extension/     Chrome extension (Manifest V3): page snapshot, constrained actions, pairing popup
demo_sites/    fake sites the agent is tested on (Riverbend, library, Reelbox, privacy testbench)
forms/<form_id>/   form.pdf, schema.json, meta.json
scripts/       simulate.py, call_sim.py, e2e_golden_path.py, extension_smoke.py, seed_demo.py, reset_demo.py, ...
tests/
docs/          CALL_THE_INTERNET.md, TRADEOFFS.md, PRIVACY.md, DEMO_SCRIPT.md
```

## Build status

- All lane plans are merged: Lane A (#22, #23), B1–B6, C1–C6 and D1–D6.
- The "call the internet" pivot (Chrome extension + browser agent) landed in #24.
- Open: outgoing calls and texts wait on [Twilio account settings](docs/CALL_THE_INTERNET.md#twilio-account-settings), and the browser-agent code (`app/agent/`, `app/browser/`, `extension/`, `demo_sites/`) has no lane owner yet.
