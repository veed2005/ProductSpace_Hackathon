# Phone-only form filling

Someone with only a telephone (a landline or flip phone, no computer, no paired browser) calls Formline and says "I need help applying for SNAP", in English or Spanish. Formline asks the application's questions one at a time in their language. It checks spellings and important values, takes corrections in natural words, explains confusing parts with quotes it has verified against the form, and points out consequential language before the caller agrees to anything. It fills and verifies the PDF only after an explicit yes, then emails a receipt of exactly what it entered.

It runs alongside "call the internet" ([CALL_THE_INTERNET.md](CALL_THE_INTERNET.md)): callers with a paired browser get the browser agent, everyone else gets this. Both share identity, PINs, memory and the consent rules.

## Try it

```bash
uv run python scripts/make_demo_snap_form.py          # (re)builds forms/demo_snap, a FAKE county SNAP form
TWILIO_VALIDATE_SIGNATURES=false FORMLINE_PHONE_FORMS=true uv run uvicorn app.main:app --port 8000
uv run python scripts/formcall_demo.py                 # the Spanish demo caller, over the real call path
```

`scripts/formcall_demo.py` calls through the voice webhook and relay websocket exactly as Twilio does, with the real model, and answers whatever Formline asks. A live run on 2026-10-03 (OpenAI `gpt-4.1-mini` for interpretation) took 18 seconds. Watch it at `/dashboard`: open the person, and the form shows a **phone session** panel with state, language, what Formline is waiting for, each answer's provenance and verification, the notices and how the caller responded, and the receipt.

On a real phone, call the number with no paired browser and say "Quiero solicitar beneficios de SNAP" (the fake form) or "I need help with food stamps" (the real Illinois form).

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `FORMLINE_PHONE_FORMS` | `true` | Use this workflow. `false` uses the original form flow (`app/engines/form_engine.py`). |
| `FORMLINE_VOICE_AUTODETECT` | `true` | ConversationRelay recognizes English and Spanish on its own (`transcriptionLanguage="multi"`, Deepgram nova-3). Each utterance's detected language is passed to the brain as `TurnRequest.language_hint`. |
| `FORMLINE_EMAIL_PROVIDER` | `outbox` | `outbox` writes `.eml` files to `data/outbox/`, and nothing leaves the laptop; the caller is told the receipt was *saved*, not emailed. `smtp` really sends. |
| `FORMLINE_EMAIL_FROM` | `Formline <receipts@formline.local>` | Sender. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS` | `587`, STARTTLS on | For `smtp`. For Gmail, use `smtp.gmail.com` and an app password. Never commit them. |

## How a turn works

| Step | Code | Notes |
|---|---|---|
| Language | `app/formcall/language.py` | An explicit request ("Can you explain that in English?", "Háblame en español") always switches. Otherwise, clear words or the recognizer's hint switch. Answers to name, address and number questions never switch, because "412 Elm Street" from a Spanish speaker isn't English. New callers get a bilingual greeting and no language menu, unless the language really is unclear. |
| Waiting for something? | `controller.py` `on_*` | Read-back, spelling, double entry, notice, review, final yes, receipt. Consent uses a strict yes/no (`app.agent.policy.classify_reply`). |
| Interpret | `interpret.py` | One fast-model call returns `TurnInterpretation`: intent, candidate value, amount and period, target field, confidence, whether spelling is uncertain, and a clarifying question. A keyword fallback is used when there's no model. |
| Normalize | `values.py` | Code turns the words into the form's value: ISO dates (English or Spanish), amounts, **weekly→monthly ×4.33**, phones, spelled letters ("D as in David", "de, o, u"), spoken emails ("arroba", "punto"). Proper names come from the caller's own words, so a model's "John" for "Juan" is ignored. |
| Verify | `verify.py` | `none` / `read_back` / `spell` / `double_entry`, chosen by field type, sensitivity, confidence, channel and provenance. Typed (SMS) answers aren't read back unless converted. Sensitive digits are asked twice and never echoed. |
| Store | `store.py` | Each answer keeps `value`/`source` (for existing code) plus `provenance`, `heard`, `normalized`, `form_value`, `translated`, `transformed`, `verification`, `language`, `history`. Sensitive ones are masked. |
| Next question | `controller.advance` | Conditions come from the schema, never from the model. Saved facts are used only after the PIN: fresh ones are offered in one batch, and stale ones are re-asked with the old value as a hint. |

**Interruptions at any point:** "what does that mean?" and "why do they need that?" (explanation, then "Would you like me to continue?"), "what are you putting down?", "go back", "change my address", "actually it's 315 a week, not 300" (a correction, with the target field picked by the model and checked by code), "skip", "start over" (confirmed first), "read everything", "read my income information", and "stop" (pauses; "continue" resumes).

## Explanations and notices

- **Questions about the form** (`explain.py`): the model gets the relevant paragraphs of the PDF and proposes a quote plus a plain explanation. Formline says "The form says, '…'" **only if code finds the quote in the PDF** (`doctext.find_quote`). Otherwise it says the form doesn't say it directly. Questions about eligibility, law, money or health add "not legal advice".
- **Important information** (`notices.py`): findings come from a fixed set of categories (deadline, perjury certification, third-party contact, data sharing, repayment, waiver, arbitration, fee, …), with severity and an `unusual` flag. Each finding's quote is checked against the PDF; findings whose quote doesn't check out are dropped. A reviewed list can ship with the form (`forms/<id>/notices.json`); otherwise the strong model analyzes the form once per version, in the background at form start, and caches the result. Medium, high and unusual findings are presented before the final review: "There's something important…" or, for unusual ones, "I want to point something out before you agree to this…". Then "Would you like me to read the exact wording?" The caller's response (read, OK, explain, I don't agree, skip) is stored per task in `FormNotice`.

## Final review, consent, and what "done" means

`validate.py` blocks completion until four conditions hold: every required question is answered, every exact field the caller didn't type is confirmed, every important notice has been presented, and contradictions are resolved. Then comes a short grouped summary: "your name and contact information, people in household 3, monthly income from work $1,364 and monthly rent $850".

"Is that all correct?" is answered with "that's correct", "read everything", or a change. Formline then asks "Should I prepare your application now? I won't send it to anyone." **Only a plain yes, given for the same answers, runs it.** A fingerprint of the answers is stored with the question, so if anything changes, Formline asks again. "Uh-huh", "ajá", "I guess" and silence never count.

`FormRun.state` is moved only by code:

```
in_progress → ready_for_review → awaiting_confirmation → prepared
                                                       ↘ needs_attention    (the filled PDF failed its check)
prepared → submitting → submitted | submission_failed | submission_unverified   (only with a submission provider)
```

There's no submission provider by default: Formline prepares the PDF, and a partner submits it. `app/formcall/submit.py` is the hook for a supported integration. The caller hears "submitted" only when the provider returns a verified reference.

## Receipts

`receipt.py` builds the receipt from stored task data, never from a model summary. It's sent as readable HTML and plain text with a PDF attached, and contains:
- the form, date, reference (`FL-00001-261003`) and state: "Prepared, not submitted" unless a submission was verified;
- every entered value, with where it came from ("you said", "from your saved information", "corrected by you; you said $315 each week");
- corrections, unanswered questions, and the notices with the caller's response.

SSNs appear as `***-**-1234` and other sensitive values as `****4321`. PINs, passwords, codes and tokens never appear (`privacy.py`). The email address is read back letter by letter and needs a yes before sending. The caller hears "I emailed it" only if the provider delivered it.

## Tests

```bash
uv run pytest tests/test_formcall_flow.py tests/test_formcall_values.py tests/test_formcall_dashboard.py
```

Coverage: an English form in English; the same form entirely in Spanish (the demo script); language detected and switched mid-call; proper names not translated; spelling asked for and rebuilt; amounts and weekly→monthly conversion done by code; corrections; questions answered and then resumed; "what are you putting down"; notices with verified quotes and fake quotes dropped; a missing required field blocking completion; ambiguous consent refused; a yes for changed answers refused; receipts with real values, masking and no PIN; prepared, submitted, failed and unverified states; outbox vs. delivered vs. failed email; nothing sensitive in transcripts; PIN needed for saved info and again after switching person; and "forget me" deleting runs, notices and receipts.

## Data and privacy

There are three new tables: `FormRun` (workflow state), `FormNotice` (notices and the caller's responses) and `Receipt` (status, address, provider, PDF). "Forget me" deletes all three, along with receipt PDFs. The PIN and sensitive digits are written to the transcript as `[PIN]` and `[sensitive answer]`. Receipt PDFs are saved under `data/receipts/` and outbox mail under `data/outbox/`, which is local runtime data and never committed.
