# Demo path review (2026-10-03)

A read of the whole repo at `ff27a6c`, plus a replay of the [DEMO_SCRIPT.md](DEMO_SCRIPT.md) lines through `handle_turn`.

**Reproduce:** `uv run python scripts/demo_replay.py`. It sends Rosa's scripted lines by text through the real brain, using a throwaway database and a fake LLM that always answers correctly. So every failure below comes from deterministic code, not model quality.

## Summary

- **Merged:** every phase in the build plan, with the conversation brain in #22 and #23.
- **Tests:** all 283 offline tests pass. The 6 live tests are skipped by default, and CI doesn't run them.
- **Gap:** the on-stage demo doesn't work yet. Most of the breakage is in the conversation brain, plus one bug in the shared contracts.

## What the replay shows

| Rosa says | What Formline does |
|---|---|
| "Sí, está bien." (consent) | Asks for consent again. It only accepts a bare "sí". |
| "Veintidós de julio de 1979…" | Replies in English: "Please enter a date like yyyy-mm-dd" |
| Most SNAP questions | "Por favor responda sobre gets mail at a different address." Spanish exists only for the 8 sample-form questions. |
| "Prefiero no darlo." (SSN) | "Please provide the last 4 digits" |
| "Somos tres: yo y mis dos hijos…" | Saved as the answer to *language spoken* and printed on the PDF. The script's order doesn't match the form's question order. |
| "Sí, en Lincoln Laundromat." | "Answer must be yes or no" |
| "La renta es novecientos cincuenta…" | Not understood. Only "cambiar X a Y" works. |
| Photo of the Medicaid letter | Explained in English |
| "Sí, recuérdame." / "Sí, llénala ahora." | "Responde sí o no." |
| "sí" to the related form | Skips the "use your saved info?" question and asks a question in English. Her next answer gets "Please reply yes to use these details." |

## Root causes, worst first

1. **Letter deadlines always come back without a date (shared: `app/contracts.py`).** In `class Deadline`, the field named `date` hides the `date` type, so its annotation becomes `Optional[None]`. `llm.structured` sends that schema to Claude as constrained output, so every real deadline date is `null`. As a result, no reminder is ever offered and the dashboard deadline countdown stays empty. `tests/test_brain_documents.py` works around it with `Deadline.model_construct`. The live test `test_live_medicaid_renewal_links_the_form` would catch it, but it never runs in CI. Fix: `import datetime` and annotate `Optional[datetime.date]`.
2. **The PIN is never checked again after setup (brain).** `turn.py` never calls `identity.verify_pin`. `start_form` prefills from memory only if the PIN was entered in the last 30 minutes. A returning user gets no prefill and is never asked for their PIN. The demo only works because Rosa sets her PIN minutes earlier.
3. **Exact-match parsing (brain).** Yes/no, consent, "skip" and reminder answers must match a fixed set exactly. Dates accept only numeric or English formats, and numbers need digits.
4. **Spanish is patchy (brain).** These come out in English: questions beyond the 8 hardcoded ids, read-back labels, validation errors, "Hi friend! I found the … form.", and letter explanations. The document engine returns English by design, and the brain never translates it.
5. **The related-form path skips the memory confirmation (brain).** `_document_followup_flow` replies with `field.question_hint` while the session is in `form_memory_confirm`.
6. **PDF values (brain).**
   - Pay normalized to a monthly amount is printed next to `pay_frequency` "weekly".
   - Phones print as `+12175550104`, because `_pdf_values` doesn't use `app/pdf/format.py` `pdf_value`.
7. **Smaller problems:**
   - `display_name` is never set from the applicant's name, so the greeting says "Hi friend!" and the dashboard shows "Unnamed".
   - `classify_intent` runs on every turn, even mid-form where its result is ignored. That's an extra LLM call per voice turn.
   - The receipt leaves out how many answers came from memory and what's still missing.
   - The brain doesn't use the identity helpers (`verify_pin`, `select_profile`, `match_profile`), so picking a person on a shared phone keeps the previous person's PIN verification.

## Docs drift

- The README "Build status" list is out of date.
- DEMO_SCRIPT step 5 uploads a LIHEAP form, but the team chose IL444-1893 (see TRADEOFFS).
- DEMO_SCRIPT's persona lines assume free-form Spanish that the brain doesn't accept yet.

## Asks by area

| Area | Ask |
|---|---|
| Brain | Causes 2–7. Re-run `scripts/demo_replay.py` after each fix. |
| Shared | One-line fix to `Deadline.date`, plus a test that `Deadline(date=date.today(), ...)` validates. |
| Docs and demo | Update the README status and DEMO_SCRIPT step 5. Match the persona card's order to the SNAP schema's question order. |
| Forms | Run the live tests (`uv run pytest -m live`) once the contract fix lands. |
