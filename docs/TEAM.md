# Team workflow: four people, no merge pain

Modules talk to each other through the contracts in `app/contracts.py` and the small APIs listed below. Keep those stable and the rest of the code can change freely.

## Shared files: announce before editing

These cause conflicts if two people edit them at once. Post in the team chat before you change them, keep the change small and **additive**, and merge it quickly.

- `app/contracts.py`: data shapes that cross modules. Add optional fields; never rename or remove one without agreement.
- `app/models.py`: database tables. Prefer storing new per-task or per-document data in the existing JSON columns (`Task.answers`, `Task.verification`, `Document.result`, `Session.pending`, `Event.data`) over adding columns.
- `app/events.py`, `app/llm/client.py`, `app/config.py`, `.env.example`
- `app/main.py`: every router is already registered. You shouldn't need to touch it.
- `pyproject.toml` / `uv.lock`: add dependencies with `uv add <pkg>` and say so in chat.

The database is recreated from the models, not migrated. After someone changes `models.py`, delete `data/formline.db` and restart.

## Module APIs

| Used by | Function |
|---|---|
| channels → brain | `core.turn.handle_turn(TurnRequest) -> TurnResult` |
| brain → identity | `core.identity.get_session / save_session / note_channel / profiles_for_phone / create_profile / update_profile / set_pin / check_pin / pin_verified` |
| dashboard → identity, reminders | `core.identity.reset_pin`, `reminders.send_now / pending_reminders` |
| brain → reminders | `reminders.create_reminder(profile_id, due_at, message, ...)` |
| dashboard → ingestion | `engines.ingest.ingest_pdf(path, form_id=..., name=..., aliases=...)` |
| brain, dashboard → channels | `channels.outbound.send_sms(to, body)` (logs only when Twilio isn't configured) |
| brain → form library | `engines.form_library.list_forms / load_schema / load_meta / match_form / pdf_path` |
| brain → PDFs | `pdf.fill.fill_pdf(template, {pdf_field: value}, out_path)` and `pdf.verify.verify_pdf(path, expected) -> VerificationResult` |
| brain → documents | `engines.document_engine.explain_document(media_paths, language) -> DocumentExplanation` |
| brain, forms → memory | `memory.profile.get_facts / get_fact / set_fact / confirm_fact / forget_profile` |
| brain → status | `engines.status_engine.recent_activity(profile_id)` |
| everyone → events | `events.log_message / log_activity / log_event / publish` |
| everyone → LLM | `llm.client.structured(PydanticModel, system=..., messages=...)`, `llm.client.text(...)`, `llm.client.image_block(path)` (needs `ANTHROPIC_API_KEY`) |

### Conventions that keep the pieces fitting together

**`Task.answers`** (written by the brain, read by the dashboard and the receipts):
```json
{"applicant_name": {"value": "Ana Lopez", "source": "memory", "updated_at": "2026-10-03T14:02:11Z"}}
```
`source` is one of `memory | asked | corrected | document | unknown | skipped` (`AnswerSource` in contracts).

**Dashboard events:** call `events.publish(type, **payload)`. The types and payloads are listed at the top of `app/events.py`. The dashboard renders them; everything else just publishes.

**Metrics events:** call `events.log_event(name, ...)` with these names so the dashboard's metrics panel can compute Section 12:

| Event | Who logs it | Data |
|---|---|---|
| `turn` | brain | `channel`, `latency_ms` |
| `form_started` | brain | `form_id`, `from_document_id` (optional) |
| `field_answered` | brain | `field_id`, `source` |
| `readback_correction` | brain | `field_id` |
| `form_completed` | brain | `form_id`, `duration_s`, `turns`, `fields_total`, `fields_from_memory` |
| `form_abandoned` | brain | `form_id`, `last_field` |
| `verification` | brain | `ok`, `mismatches` |
| `document_explained` | brain | `document_type`, `related_form_id` |
| `channel_switch` | channels | `from`, `to` |
| `voice_latency` | channels | `ms` |
| `reminder_sent` | reminders | `reminder_id` |

**Tests:** put them in `tests/test_<area>_*.py` (e.g. `test_channels_voice.py`, `test_agent_tabs.py`). Mock the LLM by monkeypatching `app.llm.client.structured` / `text`. Mark tests that hit the real API with `@pytest.mark.live`. `tests/test_integration.py` checks the wiring between modules; if you break it, fix it before merging.

## Git workflow

1. **`main` always runs.** Never push broken code to it. CI runs `pytest` on every PR, and `.githooks/pre-push` blocks direct pushes to `main` (`sh scripts/setup.sh` turns it on).
2. **One short-lived branch per task**, named `<area>/<what>`: `agent/new-tab`, `voice/autodetect`, `docs/setup`.
3. **Merge small and often.** A 300-line PR merges cleanly; a 3,000-line PR doesn't.
4. **Start every task, and update before every PR, with:**
   ```bash
   git checkout main && git pull
   git checkout -b agent/my-task      # or: git rebase main (on an existing branch)
   ```
5. **Open a PR, let CI pass, squash-merge it.** Get a quick look from a teammate if you touched a shared file.
6. **Delete the branch after merging.**

**If `uv.lock` conflicts:** take main's version and regenerate.
```bash
git checkout --theirs uv.lock && uv lock && git add uv.lock
```

## Running things

```bash
uv sync                                   # install
uv run uvicorn app.main:app --reload      # server on :8000
uv run python scripts/simulate.py         # chat with the brain in the terminal
uv run pytest                             # tests
```
