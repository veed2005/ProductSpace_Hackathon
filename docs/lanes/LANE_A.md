# Lane A brief: Conversation brain

**Owner:** @aryavsaigal · **Stage plan:** [docs/PLAN.md](../PLAN.md) · **Full spec:** [docs/PROJECT_BRIEF.md](../PROJECT_BRIEF.md) (read Sections 1, 4, 6, 7, 8, 10, 13)

You are building the brain of Formline: the single `handle_turn` function that both phone channels call, and everything it decides. You turn a person's messy messages into a filled form, using memory to skip questions, and you tell them exactly what you did. You are on the critical path: every checkpoint demo runs through your code.

**Key principle:** deterministic code controls the workflow; the LLM handles language. The LLM interprets answers and phrases replies through `llm.structured(...)`; your code decides what to ask next, when a form is complete, and what gets saved or sent.

## You own

`app/core/turn.py`, `app/core/router.py`, `app/core/style.py`, `app/core/referrals*` (new), `app/engines/form_engine.py` (new), `app/engines/conversation*.py` (new), `app/llm/prompts/conversation*` (new), `scripts/simulate.py`, `tests/test_brain_*.py`.

## You call (other lanes' APIs; don't edit their files)

| Need | Function | Lane |
|---|---|---|
| Session, profiles, PIN | `core.identity.get_session / save_session / note_channel / profiles_for_phone / create_profile / update_profile / set_pin / check_pin / pin_verified` | B |
| Texts outside the reply | return them in `TurnResult.followup_sms` (preferred), or `channels.outbound.send_sms` | B |
| Reminders | `reminders.create_reminder(profile_id, due_at, message, document_id=...)` | B |
| Forms | `engines.form_library.list_forms / match_form / load_schema / load_meta / pdf_path` | C |
| PDF | `pdf.fill.fill_pdf(template, values, out)` · `pdf.verify.verify_pdf(path, expected)` | C |
| Documents | `engines.document_engine.explain_document(media_paths, language=...)` | C |
| Memory | `memory.profile.get_facts / set_fact / confirm_fact / forget_profile` | D |
| Status | `engines.status_engine.recent_activity(profile_id)` | D |
| Logging | `events.log_activity / log_event / publish` (names and payloads in `docs/TEAM.md` and `app/events.py`) | D |
| LLM | `llm.client.structured(Model, system=..., messages=...)`, `llm.client.text(...)` | shared |

**Others depend on you for:** `Task.answers` in the agreed format plus `field_filled`, `verification`, and `document_explained` events (D's dashboard), metrics events (D's metrics panel), and short voice-styled replies (B's voice channel).

## Phases

### A1: Onboarding, identity flow, intent router *(Stage 1)*
- Replace `_placeholder_brain` with a real state machine, with state stored in `Session.state` / `Session.pending` (not in memory) so a person can continue on another channel or after a restart.
- **New number:** ask language → short consent message → set a 4-digit PIN (`identity.set_pin`) → menu.
- **Known number:** greet by name. If `profiles_for_phone` returns more than one, ask who's calling.
- **Intent router:** classify with `llm.structured` into `fill_form | explain_document | status | forget_me | help`, keeping the keyword version as a fallback when the LLM fails.
- Reply in the person's language. Apply `style.style_for(channel)` to every LLM-phrased reply.
- **Done when:** onboarding plus the menu works in the simulator in English and Spanish, with tests in `tests/test_brain_onboarding.py` (LLM mocked).

### A2: Generic form engine *(Stage 1)*
- `app/engines/form_engine.py`, driven only by `FormSchema` (no form-specific code). Run it on `forms/sample_benefits`.
- Ask one question at a time, honoring `condition` and skipping fields already answered.
- **Normalize messy answers** with `llm.structured`. For example, "300 a week, it changes" becomes about $1,300 a month (4.33 weeks per month), `varies: true`, confirmed back to the person.
- Allow "I don't know" or skip for non-required fields (`source: "unknown"` / `"skipped"`). Explain a question on request, then re-ask.
- SSN: only `ssn_last4`. Never echo a `sensitive` value over SMS.
- **Read-back** in short chunks by `group`, with corrections (`source: "corrected"`, log `readback_correction`).
- Write `Task.answers` in the format in `docs/TEAM.md`. `publish("field_filled", ...)` on every answer. Log `form_started` and `field_answered`.
- **Done when:** the sample form completes in the simulator in English and Spanish, including a messy-income answer and one correction.

### A3: Completion, verification, run-back *(Stage 1 → Checkpoint M1)*
- Map answers to `{pdf_field: value}`, using `pdf_values` for checkboxes and formatting dates and money. `fill_pdf` to `data/filled/task_<id>.pdf`, then `verify_pdf`.
- **Only report success if verification passes.** Otherwise set `Task.status = "needs_attention"` and tell the person what still needs attention.
- Store `Task.verification` and `Task.output_pdf_path`. `publish("verification", ...)`, log `verification` and `form_completed` (with `duration_s`, `turns`, `fields_total`, `fields_from_memory`), and `log_activity`.
- **Run-back:** a spoken or texted summary as the reply, plus a masked text receipt in `followup_sms` on both channels. Example: "Filled your application with 8 answers. Ready for your caseworker."
- **Done when:** Checkpoint M1 passes with B and D.

### A4: Memory in the conversation *(Stage 2)*
- At form start, prefill from `memory.get_facts` via each field's `profile_key`. **Require a verified PIN** (`identity.pin_verified`) before reading back or reusing stored info.
- **Fresh** values: confirm in quick batches ("I have your name, birthday, and address from last time. Should I use those?"). **Stale** values: re-ask with the old value as a hint. Mark reused answers `source: "memory"`.
- Write new answers and corrections back with `memory.set_fact(source_type="form", source_ref=task_id)`.
- **"What have you done for me?"** uses `recent_activity`. **"Where was I?"** resumes the active task. **"Forget me"** confirms, then calls `forget_profile`.
- **Done when:** the second form for the same simulator persona asks less than half as many questions, and stale income is re-asked.

### A5: Document flow *(Stage 2 → Checkpoint M2)*
- Media arrives → `explain_document(media_paths, language=...)` → explain in the person's language and channel style. On a call with no media, offer "I'll text you right now. Just reply with a photo," via `followup_sms`.
- Low confidence or unreadable parts: ask for a retake instead of guessing.
- **High-stakes documents** (eviction, court, immigration): always add the referral line. The referral list is configurable in `app/core/referrals.json`.
- Deadlines: ask permission, then `reminders.create_reminder`. Save reference numbers and dates with `set_fact(source_type="document")`.
- If `related_form_id` is set, offer to fill that form now and start A2/A4 (log `form_started` with `from_document_id`).
- Store the `Document` row, `publish("document_explained", ...)`, and log `document_explained`.
- **Done when:** Checkpoint M2 passes.

### A6: Voice-ready brain and scenario tests *(Stage 3 → Checkpoint M3)*
- Keep voice replies short: one or two sentences, no lists, numbers said naturally. Use the fast model and keep prompts tight. Log `turn` with `latency_ms`.
- **Channel-switch greeting** (when `note_channel` returns True): "Welcome back. We were on question 6 of your SNAP application."
- Language switch mid-conversation.
- Add scripted pytest scenarios from Section 13 of the brief: full fill, messy answers, read-back corrections, mid-task channel switch, language switch, second form from memory, stale re-ask, shared phone, document with deadline, failed verification.

### A7: Demo hardening *(Stage 4)*
- If the LLM call fails or times out, give a graceful fallback reply. Never leave dead air or show a stack trace to a caller.
- Tune latency and polish the wording of the demo flows with D.

## How to work

- Start a Claude Code session in this repo and say: **"Start Phase A1."** The session hook tells Claude you're Lane A and points it at this file.
- Use the simulator constantly: `uv run python scripts/simulate.py --channel sms` (or `--channel voice`).
- After each phase: tests pass → PR → merge → post a summary in chat.
- Need something changed in another lane? Ask its owner. Don't edit their files. Until it lands, work against the stub.
