# Formline: full project overview (as of 2026-10-03)

> **Superseded in part.** This describes the repo before the "call the internet" pivot. For the browser agent, see [CALL_THE_INTERNET.md](CALL_THE_INTERNET.md). The `Deadline.date` bug in section 10 is fixed.

This document describes the Formline repository in enough detail that someone who hasn't seen the code can write precise instructions for a coding agent (Claude Code) working in it. It covers what the product is meant to be, how the code is organized, what each part does today, what's broken, and the rules the agent works under.

---

## 1. The product

**Formline: "any form, any phone."** A single phone number that anyone can **call or text** for help with government paperwork. It works from a basic flip phone, with no app or internet. Voice and SMS are equally important.

It does two things:

1. **Fill out a form.** Benefits applications such as SNAP (food stamps), Medicaid and free school meals. It asks one simple question at a time and fills in the real official PDF.
2. **Explain a letter.** The person texts a photo of a letter (benefits notice, medical bill, eviction notice). Formline explains it in plain language and in their language: what it is, what it wants, any deadline, and what to do next. If the letter needs a form, it offers to fill it.

Three differentiators:

- **Memory.** It builds a profile (name, address, household, income, etc.) and reuses it, so the second form is much faster. It confirms ("I still have your address as 412 Elm St. Is that right?") rather than re-asking. Values go stale on a schedule (income after 30 days, address after 180, date of birth never) and are re-asked with the old value as a hint.
- **Run-back and verification.** After filling a PDF, the backend re-opens it and checks every field before claiming success. It then tells the person what it did: a spoken summary on calls, plus a text receipt on both channels.
- **Any form.** New forms are added by uploading the official fillable PDF. An LLM drafts the question flow (`schema.json`), a human reviews it, and the form becomes callable in about a minute.

A **live web dashboard** (the "partner view", as a food bank or legal-aid worker would use it) shows forms filling in field by field, color-coded by source (from memory / newly asked / corrected). It also shows letter explanations next to the photo, a transcript labeled by channel, verification status and metrics. It is the centerpiece of the on-stage demo.

**Context:** this is a hackathon prototype (~30 hours, four people). Judging criteria are product sense, prioritization, execution/AI usage, and the pitch. A demo that works reliably beats feature count. Fake personas only. Nothing is submitted to real government systems; completed forms end as verified, downloadable PDFs.

**Target users:** people without smartphones or internet, with limited literacy (voice path), limited English, or who are deaf or hard of hearing (SMS path), or who are overwhelmed by paperwork.

---

## 2. Tech stack

- **Python 3.11–3.13, FastAPI**, run with uvicorn. Dependencies are managed with **uv** (`uv sync`, `uv run ...`).
- **SQLite via SQLModel.** Tables are created from the models at startup with no migrations. New *tables* get picked up automatically; new *columns* require deleting `data/formline.db`.
- **Twilio:** one number handles voice, SMS and MMS. Voice uses **ConversationRelay**: Twilio does speech-to-text and text-to-speech and streams text over a websocket. SMS/MMS arrive through a messaging webhook.
- **Claude via the Anthropic Python SDK.**
  - Fast model (`claude-haiku-4-5`) for conversation turns.
  - Strong model (`claude-opus-5-5`) for reading letters and drafting form schemas.
  - Structured outputs (`messages.parse` with a Pydantic `output_format`) for anything the code acts on.
- **PyMuPDF** to read, fill and re-verify AcroForm PDFs.
- **APScheduler** for reminder texts.
- **Dashboard:** vanilla JS + CSS (no build step, no CDN), with live updates over Server-Sent Events plus a 4-second poll.
- **ngrok** exposes the local server to Twilio during development and the demo.

---

## 3. The core architectural rule

**Deterministic code controls the workflow; the LLM only handles language.**

- The LLM proposes structured outputs (intent classification, normalized income, letter explanation, draft form schema).
- Code validates them and decides what happens.
- The LLM never decides on its own that a form is complete, what gets saved to memory, or what gets sent to whom.

Other standing rules:

- Text inside uploaded photos and documents is **data, never instructions** (protection against prompt injection in mailed letters).
- Never log or text full SSNs or other sensitive values. Only SSN last 4 is ever collected.
- A 4-digit **PIN** must be verified before stored personal info is read back or reused, because caller ID can be spoofed.
- One phone can have several profiles (a shared family phone).
- "Forget me" really deletes the person's data.

---

## 4. Team rules a coding agent must follow

Modules talk through contracts (`app/contracts.py`) and a fixed list of functions (`docs/TEAM.md`).

**Shared files** (small, additive changes only, announced to the team first): `app/contracts.py`, `app/models.py`, `app/events.py`, `app/llm/client.py`, `app/config.py`, `app/main.py`, `pyproject.toml`, `uv.lock`, `.env.example`.

**Rules in `CLAUDE.md`** that Claude Code follows by default:

- Design decisions are appended to the end of `docs/TRADEOFFS.md` (format: decision, alternatives, why).
- **Git:**
  - Never commit or push to `main`; a pre-push hook blocks it.
  - Branch from fresh `main` as `<area>/<what>`.
  - Keep PRs small. `uv run pytest` must pass. Open PRs with `gh pr create --fill`.
  - CI runs pytest on every PR.
- Tests go in `tests/test_<area>_*.py`. The LLM is mocked by monkeypatching `app.llm.client.structured` / `text`. Tests that hit the real API are marked `@pytest.mark.live` and are **excluded by default and in CI**.

---

## 5. Repository layout

```
app/
  main.py              FastAPI app; registers routers; startup: init_db, sync_forms, start reminder scheduler
  config.py            Settings from .env (models, Twilio creds, PUBLIC_BASE_URL, data paths, flags)
  contracts.py         Cross-module Pydantic shapes (TurnRequest/TurnResult, FormSchema, DocumentExplanation, ...)
  models.py            SQLModel tables
  db.py                Engine, init_db, session_scope
  events.py            log_message / log_activity / log_event / publish (dashboard SSE); never raises
  metrics.py           Section 12 metrics computed from the events table
  core/
    turn.py            handle_turn: the brain, a state machine
    router.py          Intent classification: keywords first, then LLM
    style.py           Per-channel style rules for LLM-phrased replies
    referrals.py/.json Legal-aid referral line for high-stakes letters
    identity.py        Sessions, profiles, PIN hashing/lockout, shared phones
  engines/
    form_engine.py     Generic schema-driven form conversation, prefill, read-back, fill+verify
    form_library.py    Load forms/<id>/ schema+meta, match_form by alias then LLM
    document_engine.py Explain letter photos with the vision model
    ingest.py          PDF -> draft schema.json via LLM, with validation and repair
    status_engine.py   "What have you done for me?" from the activity log
  memory/profile.py    Canonical profile facts with source and freshness; path API; forget_profile
  pdf/                 fields.py (inspect), fill.py, verify.py, format.py (pdf_value)
  llm/client.py        structured(), text(), image_block(), model selection, refusal fallback (shared)
  llm/prompts/         document.py, ingest.py
  channels/
    messaging.py       POST /twilio/messaging: SMS/MMS webhook
    voice.py           POST /twilio/voice + WS /twilio/voice/relay: ConversationRelay
    outbound.py        send_sms (logs only when Twilio isn't configured)
    twilio_setup.py    CLI that points the Twilio number's webhooks at the current ngrok URL
  reminders.py         create_reminder, send_now, APScheduler job
  dashboard/           routes.py (page, static, SSE), api.py, control_api.py, forms_api.py, demo.py, static/
forms/<form_id>/       form.pdf, schema.json, meta.json
  il_snap/             Illinois SNAP application IL444-0683 (27 hand-picked fields of 367)
  il_medicaid/         Illinois HFS 2378H medical benefits application (31 fields), standing in for "Medicaid renewal"
  il_school_meals/     Illinois ISBE 68-06 free/reduced school meals (21 fields)
  sample_benefits/     Small generated placeholder form (8 fields)
  _new_form_demo/      IL444-1893 SNAP Redetermination PDF kept for the "upload a new form live" finale (no meta.json, so it isn't loaded)
scripts/               simulate.py, seed_demo.py, reset_demo.py, demo_preflight.py, make_demo_letter.py,
                       ingest_form.py, inspect_pdf.py, make_sample_form.py, setup.sh, demo_replay.py
tests/                 283 offline tests + 6 live; fixtures/documents/ holds fake letters (Medicaid renewal,
                       medical bill, eviction notice, blurry photo, prompt-injection letter)
docs/                  PROJECT_BRIEF.md (full spec), TEAM.md, TRADEOFFS.md, PRIVACY.md,
                       DEMO_SCRIPT.md, REVIEW_DEMO_PATH.md, OVERVIEW.md (this file)
```

---

## 6. Data model (`app/models.py`)

| Table | Purpose |
|---|---|
| `Profile` | One person. `phone` (not unique: shared phones), `display_name`, `preferred_language`, `pin_hash`, `consent_at`. |
| `ProfileFact` | One canonical fact per (profile, key): `value` (JSON), `source_type` (conversation/form/document/seed), `source_ref`, `confirmed_at`, `freshness_days`, `sensitive`. |
| `Session` | One per phone, shared across voice and SMS. `profile_id`, `state` (state-machine position), `pending` (JSON), `active_task_id`, `pin_verified_at`, `last_channel`. This is what lets someone hang up a call and continue by text. |
| `Task` | Filling one form or explaining one document. `status` (active/readback/completed/needs_attention/abandoned), `answers` (JSON: `{field_id: {value, source, updated_at}}`), `current_field`, `turn_count`, `output_pdf_path`, `verification` (JSON). |
| `Form` | Index of the on-disk form library (disk is the source of truth). |
| `Message` | Transcript line (direction, channel, text, media) for the dashboard. |
| `Document` | A photographed letter plus its structured explanation (`result` JSON), `related_form_id`, `confidence`. |
| `Reminder` | `due_at`, `message`, `status` (pending/sending/sent/failed/canceled). |
| `PinGuard` | Wrong-PIN counts and lock time per profile (a separate table, so no DB reset was needed). |
| `Activity` | Human-readable log ("Completed Illinois SNAP Application"). Powers run-backs and the dashboard. |
| `Event` | Machine-readable metrics events (`turn`, `form_started`, `field_answered`, `readback_correction`, `form_completed`, `verification`, `document_explained`, `channel_switch`, `voice_latency`, `reminder_sent`, ...). |

**Canonical memory keys** (`app/memory/profile.py`) use paths into a small set of top-level facts:

- `name` {first, middle, last}, with `full_name` as a virtual key over it
- `date_of_birth`, `phone`, `email`, `preferred_language`
- `address` {street, apt, city, state, zip}, `mailing_address`
- `household_size`
- `household_members[i]` {first_name, last_name, relationship, date_of_birth, is_student, school, has_income}
- `employment` {status, employer, gross_pay, pay_frequency, hours_per_week, varies}
- `monthly_income`, `other_income[]`, `housing_cost`, `utilities`, `disability_in_household`
- `case_numbers` {snap, medicaid, tanf, school_meals, other}
- `ssn_last4` (sensitive)

Each key has a freshness policy in days.

---

## 7. Key contracts (`app/contracts.py`)

- `TurnRequest(phone, channel: "sms"|"voice", text, media_paths)` → **`handle_turn`** → `TurnResult(reply, end_call, followup_sms[], language)`. Both channels call this one function.
- `FormSchema(form_id, name, reviewed, fields[])`. Each `FormField` has:
  - `id`, `label`, `type` (text/number/money/date/phone/yes_no/choice/address/ssn_last4), `required`
  - `question_hint`, `pdf_field`, `pdf_values`, `profile_key`
  - `options`, `condition` {field, equals}, `validation`, `sensitive`, `group`, `max_length`
- `FormMeta(form_id, name, aliases[], agency, description)`.
- `DocumentExplanation`:
  - `document_type`, `sender`, `plain_summary` (in English), `action_required`
  - `deadlines[]` (`Deadline{date, description}`), `amounts[]`, `reference_numbers[]`
  - `related_form_id`, `high_stakes`, `confidence`, `unreadable_parts[]`
- `VerificationResult(ok, fields_checked, mismatches[], truncated[], missing_required[])`.

---

## 8. How a conversation flows today (`app/core/turn.py`)

`handle_turn`:

1. Loads the phone's `Session` and records the channel. A channel change logs `channel_switch`, and the reply gets a "Welcome back. We were on question N of M" prefix.
2. Logs the inbound message, masking PINs and sensitive answers.
3. Runs the state machine.
4. Logs the `turn` event with latency, and the outbound message.

State machine (stored in `Session.state` + `Session.pending`):

- **New phone:**
  - `awaiting_language` ("English or Español")
  - → `awaiting_consent` (yes/no)
  - → profile created → `awaiting_pin_setup` (4 digits)
  - → `menu`
- **Shared phone with several profiles:** `awaiting_profile` ("Who is speaking? Reply 1: James 2: Denise").
- **Menu:** `classify_intent` returns one of fill_form / explain_document / status / forget_me / help. It tries keywords first, then the fast LLM.
- **fill_form:**
  - `match_form(text)` matches by whole-word alias first, then falls back to the LLM.
  - `start_form` creates a `Task`. If the PIN was verified in the last 30 minutes, it prefills from memory: fresh values go into `form_memory_confirm` ("I have your name, address… from last time. Should I use them?"), and stale values become hints on re-asks.
  - Then one question per turn (`answer_field`), honoring conditions. Answers are normalized (dates, phone, money with weekly→monthly via LLM, yes/no) and written back to memory.
  - When every field is answered, the task enters read-back (`readback_text`, grouped). Corrections use the form "change X to Y". "yes" triggers fill + verify.
  - Then a reply plus an SMS receipt, or a "needs attention" message listing the problems.
- **explain_document** (photo attached):
  - `explain_document` produces the structured explanation, saved as a `Document`. Reference numbers are saved to memory.
  - Low confidence leads to "please send a clearer photo". High-stakes letters get a legal-aid referral line.
  - If there's a dated deadline, the session moves to `awaiting_reminder_consent`, which calls `create_reminder`.
  - If there's a related form, the session moves to `awaiting_related_form`, which calls `start_form`.
  - On a call with no photo, Formline texts the caller asking for one.
- **status:** "what have you done for me?" lists recent activity; "where was I?" resumes the active form.
- **forget_me:** confirms first, then `forget_profile` hard-deletes the person's data (anonymized metrics are kept).
- Explaining a question ("what does this mean?") and switching language mid-form ("en español") are supported.

---

## 9. State of each area

### Channels + identity. All merged; in good shape.

- **SMS/MMS webhook** (`/twilio/messaging`):
  - Validates `X-Twilio-Signature`, rebuilding the URL from `PUBLIC_BASE_URL` because ngrok rewrites the host. Can be turned off for local testing.
  - Downloads MMS media with basic auth to `data/media/<MessageSid>_<i>.<ext>`.
  - Dedupes Twilio retries by `MessageSid` (in memory).
  - Never returns a 500: a bad signature gets a bare 403, and any other failure gets a 200 with an apology text.
  - Splits replies over 1,500 characters, and sends `followup_sms` in the background.
- **Voice** (`/twilio/voice` → ConversationRelay websocket `/twilio/voice/relay`):
  - The websocket is authorized by a one-time token issued by the signed webhook.
  - Twilio speaks an instant greeting (Spanish for Spanish profiles) while the brain gets an empty "caller connected" turn.
  - Each `prompt` message calls `handle_turn` in a threadpool. The TTS/STT language switches with `TurnResult.language`.
  - Says "One moment." after 2.5 s and apologizes at 25 s.
  - Keypad digits are buffered (sent on `#`, at 4 digits, or after a 2 s pause) so the PIN can be typed.
  - `end_call` waits roughly as long as the reply takes to say before hanging up.
  - Logs `voice_latency`. Call recording is opt-in and announced.
  - There is a fallback TwiML, plus a Twilio-hosted TwiML Bin for when the laptop is down.
- **Identity:**
  - PBKDF2-hashed PIN, valid for 30 minutes per session.
  - 3 wrong attempts lock the profile until a partner resets it from the dashboard.
  - Shared-phone helpers: `profiles_for_phone`, `match_profile`, `select_profile` (switching drops PIN verification and the active task).
  - `verify_pin` returns ok / wrong / locked / no_pin.
  - `note_channel` logs `channel_switch`.
- **Reminders:**
  - `create_reminder`; `send_now` atomically claims the row, so the scheduler and the dashboard button never double-send.
  - Failures are marked `failed` and not retried. SSN-shaped text is scrubbed.
  - An APScheduler job runs every minute, and there's a CLI.
- **`twilio_setup.py`:** reads the live ngrok URL, repoints both webhooks, and reports missing capabilities and unverified trial phones.
- **Not yet done:** real-phone verification on this machine. This Windows machine has no `uv` installed and no `.env`, so the project hasn't been run here with real credentials.

### Forms, PDFs, documents. All merged; strong.

- `fill_pdf`:
  - Maps checkbox/radio values to each widget's real on-state, and writes raw PDF names.
  - Strips the XFA layer and sets NeedAppearances so values show in every viewer.
  - Shrinks text down to 6pt to fit tight boxes.
- `verify_pdf` re-reads the PDF and reports mismatches, truncation (measured with font widths against the box size) and missing required fields.
- `pdf/format.py` `pdf_value()` formats stored values the way paper forms expect: (202) 555-0101, MM/DD/YYYY, money without "$".
- Three real Illinois forms with hand-checked schemas, all marked reviewed. Tests fill all three from the seeded persona's memory.
- **Document engine:**
  - One strong-model vision call over all photos (downscaled to 2000 px; PDFs rendered, up to 5 pages).
  - Code then enforces the rules: `related_form_id` must exist, SSNs are stripped, confidence is clamped, and a keyword backstop sets `high_stakes`.
  - Raises on API failure rather than faking a "blurry" result.
  - Output is always in English by design (the brain is expected to translate).
- **Ingestion** (`ingest_pdf`, also the dashboard upload):
  - The model sees short field handles plus tooltips or printed labels, and picks 12–40 questions.
  - Code fills in field names, Yes/No states and max lengths, then validates the draft.
  - It retries once if the draft is badly broken, otherwise repairs it. Output is always `reviewed: false`.
  - Prompt size was cut 31–64%; model time is still unmeasured.
- `match_form`: whole-word, accent-insensitive aliases (longest first), with a fast-model fallback.

### Memory, dashboard, metrics, demo. All merged; strong.

- **Memory:** path-based API (`get_value` / `set_value` / `confirm_fact` / `get_facts`), per-fact freshness, and `forget_profile` with a full cascade (files included).
- **Dashboard** at `/dashboard`, only reachable from localhost (ngrok requests are rejected):
  - **Live page:** people list, transcript labeled by channel, form fields color-coded by source, a Letter tab (photo + explanation + deadline countdown), a Memory tab, verification badge, Download PDF, and an "x% faster" banner.
  - **Forms page:** library, schema view, Mark reviewed, "Add a new form" upload with a timer.
  - **Metrics page:** first form vs. later forms time, % from memory, corrections, latency p90, channel switches, verification pass rate.
  - **Demo controls:** seed, reset, send reminder now, reset PIN.
- **Seeded personas:**
  - Maria Garcia: returning user, Spanish, with a first form 45 days ago and stale income.
  - James and Denise Walker: two profiles on one shared phone.
  - Rosa: the live new user in the demo.
- **Docs:** `DEMO_SCRIPT.md` (5-step on-stage flow with Rosa's exact Spanish lines, cast, backup plans B/C/D), `PRIVACY.md`, a pre-flight check script, and a demo letter generator.

### Conversation brain. Merged in two large PRs (#22, #23); the weak point.

The structure above exists and the unit tests pass. However, it doesn't survive the actual demo script (see section 10).

---

## 10. Known problems (verified by replaying the demo script)

`scripts/demo_replay.py` sends Rosa's scripted lines from `DEMO_SCRIPT.md` by text through the real `handle_turn`, with a throwaway database and a fake LLM that always answers correctly. Every failure below is therefore deterministic code. Full details are in `docs/REVIEW_DEMO_PATH.md`.

### What breaks on the demo path

| Rosa says | What Formline does |
|---|---|
| "Sí, está bien." (consent) | Asks for consent again; it only accepts a bare "sí". |
| "Veintidós de julio de 1979…" | Replies in English: "Please enter a date like yyyy-mm-dd" |
| Most SNAP questions | "Por favor responda sobre gets mail at a different address." Spanish exists only for 8 hardcoded sample-form field ids. |
| "Prefiero no darlo." (SSN) | "Please provide the last 4 digits" |
| "Somos tres: yo y mis dos hijos…" | Saved as the answer to *language spoken* and printed on the PDF. The script's order doesn't match the form's question order. |
| "Sí, en Lincoln Laundromat." | "Answer must be yes or no" |
| "La renta es novecientos cincuenta…" (read-back correction) | Not understood; only "cambiar X a Y" works. |
| Photo of the Medicaid letter | Explained in English |
| "Sí, recuérdame." / "Sí, llénala ahora." | "Responde sí o no." |
| "sí" to the related Medicaid form | Skips the "use your saved info?" batch confirmation and asks a question in English. Her answer then gets "Please reply yes to use these details." |

### Root causes, worst first

1. **Letter deadlines never have a date (shared file `app/contracts.py`).**
   - In `class Deadline`, the field named `date` hides the `date` type, so pydantic reads the annotation as `Optional[None]`.
   - `llm.structured` sends that schema to Claude as constrained output, so every real deadline date comes back `null`.
   - Result: no reminder is ever offered, and the dashboard deadline countdown stays empty.
   - A brain test works around it with `Deadline.model_construct`. The live test that would catch it never runs in CI.
   - Fix: one line (`Optional[datetime.date]`).
2. **The PIN is never checked again after setup.**
   - `turn.py` never calls `identity.verify_pin`, and `start_form` prefills from memory only within 30 minutes of PIN entry.
   - So returning users never get prefill and are never asked for their PIN. The demo only works because Rosa set her PIN minutes earlier.
3. **Exact-match parsing.** Yes/no, consent, skip and reminder answers must match a fixed set exactly. Dates accept only numeric or English formats, and numbers need digits. There is no LLM fallback for any of these.
4. **Spanish is patchy.** These come out in English: questions beyond 8 hardcoded ids, read-back labels, validation errors, "Hi friend! I found the … form.", and letter explanations (the engine returns English, and the brain never translates).
5. **Related-form path skips the memory confirmation.** `_document_followup_flow` replies with the first question while the session is in `form_memory_confirm`.
6. **PDF value errors.**
   - Pay normalized to a monthly amount is printed next to pay frequency "weekly".
   - Phones print as `+12175550104`, because the brain's `_pdf_values` doesn't use `pdf_value()` from `app/pdf/format.py`.
7. **Smaller problems:**
   - `display_name` is never set from the applicant's name, so the greeting says "Hi friend!" and the dashboard shows "Unnamed".
   - `classify_intent` runs on every turn, even mid-form where the result is ignored, adding an LLM call to every voice turn.
   - The receipt doesn't say how many answers came from memory or what's still missing.
   - Read-back is one long message even on voice.
   - The brain doesn't use identity's `verify_pin` / `select_profile` / `match_profile`, so choosing a person on a shared phone keeps the previous person's PIN verification.

### Docs drift

- The README "Build status" checklist is out of date.
- `DEMO_SCRIPT.md` step 5 uploads a LIHEAP form, but the team chose IL444-1893.
- The persona card's lines assume free-form Spanish the brain doesn't accept, and their order doesn't match the SNAP schema.

---

## 11. The on-stage demo (`docs/DEMO_SCRIPT.md`)

About 4½ minutes, with Rosa as a live new user on a flip phone, speaking Spanish:

1. **Call, start SNAP in Spanish, hang up, finish by text.** Formline should say "welcome back, we were on question N".
2. **Verified PDF and receipt.** The dashboard shows Verified, and the PDF downloads.
3. **Text a photo of a Medicaid renewal letter.** Formline explains it in Spanish with the deadline, offers a reminder, then offers to fill the renewal.
4. **The renewal fills mostly from memory.** Fields light up green ("From memory") and the "x% faster" banner and Metrics page show the speedup.
5. **Optional finale:** upload a brand-new form on the dashboard, then fill it by phone a minute later.

Backup plans:

- B: text only.
- C: simulator on screen (`scripts/simulate.py --server`).
- D: a pre-recorded video.
- Seeded Maria if memory reuse fails.

Today steps 1, 3 and 4 break as described in section 10.

---

## 12. Developer tooling and commands

```bash
uv sync                                        # install
uv run pytest                                  # 283 offline tests (LLM mocked); all pass
uv run pytest -m live                          # real-API tests (need ANTHROPIC_API_KEY)
uv run uvicorn app.main:app --reload           # server on :8000; /health, /dashboard
uv run python scripts/simulate.py --phone +15550001111 --channel sms [--image letter.png]
uv run python scripts/demo_replay.py           # replays the demo script; shows the failures above
uv run python scripts/demo_preflight.py        # checks ngrok/Twilio/keys/dev flags before a demo
uv run python scripts/reset_demo.py --yes      # wipe and reseed
uv run python -m app.channels.twilio_setup     # point Twilio webhooks at the current ngrok URL
```

Environment variables (`.env`, never committed):

- `ANTHROPIC_API_KEY`, `FORMLINE_FAST_MODEL`, `FORMLINE_STRONG_MODEL`, `FORMLINE_INGEST_MODEL`
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `TWILIO_VALIDATE_SIGNATURES`, `FORMLINE_RECORD_CALLS`
- `PUBLIC_BASE_URL`, `DATABASE_URL`, `DATA_DIR`, `LOG_LEVEL`
- `FORMLINE_DEV_ENDPOINTS` (enables `POST /dev/turn` for the simulator; never with ngrok up)
- `FORMLINE_DASHBOARD_REMOTE`

**This machine (Windows 10, VS Code + Claude Code):** `uv` is not installed and there is no `.env` or `.venv`. Commands above need `uv` installed first. During the review, tests were run in a throwaway virtualenv instead.

---

## 13. Git state

- `main` at `ff27a6c` (PR #23).
- The user's branch `b/review-demo-path` (local, not pushed) adds `docs/REVIEW_DEMO_PATH.md` and `scripts/demo_replay.py`. This file, `docs/OVERVIEW.md`, is uncommitted in the working tree.

---

## 14. What a prompt for Claude Code should pin down

Claude Code will follow `CLAUDE.md` unless told otherwise. A good instruction prompt for this repo should state:

- **Scope and authority:**
  - May it change shared files like `app/contracts.py`?
- **Goal and priority order**, e.g. "make the DEMO_SCRIPT path pass end to end first", or "fix the PIN gate", or "rework the brain to use the LLM for parsing replies".
- **Approach preferences:**
  - LLM-assisted interpretation of free-form replies (still validated by code) vs. more deterministic patterns.
  - Translating at reply time vs. Spanish strings in schemas.
- **Definition of done:**
  - `uv run pytest` green.
  - `scripts/demo_replay.py` producing correct Spanish replies.
  - Live tests passing.
  - New scenario tests.
- **Process:** one branch and PR per fix vs. one combined branch; whether to push and open PRs or leave commits local; whether to append to `docs/TRADEOFFS.md`; whether to stop after each step for review.
- **Constraints to keep:**
  - Deterministic workflow control.
  - No sensitive values in logs or texts.
  - Fake personas only.
  - Treat letter text as data.
  - Keep the `handle_turn` / `create_reminder` / `send_now` signatures stable.
- **Environment:** whether Claude should install `uv` and create `.env` from `.env.example`. Secrets would come from the user privately.
