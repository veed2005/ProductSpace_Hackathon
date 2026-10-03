# Team workflow: four people, no merge pain

**Build order:** [docs/PLAN.md](PLAN.md) (stages, checkpoints, handoffs). **Your phases:** [Lane A](lanes/LANE_A.md) · [Lane B](lanes/LANE_B.md) · [Lane C](lanes/LANE_C.md) · [Lane D](lanes/LANE_D.md).

The code is split into **four lanes**. Each lane owns its own files, and lanes talk only through the contracts in `app/contracts.py` and the small APIs listed below. Every cross-lane function already exists as a working stub, so nobody waits on anybody: build your lane against the stubs, and replace your own stubs as you go.

## Lanes

| Lane | Owner | Owns (edit freely) | Phases |
|---|---|---|---|
| **A: Conversation brain** | @aryavsaigal | `app/core/turn.py`, `app/core/router.py`, `app/core/style.py`, `app/core/referrals*` (new), `app/engines/form_engine.py` (new), `app/llm/prompts/conversation*`, `scripts/simulate.py`, `tests/test_brain_*.py` | 1, conversation side of 4 (read-back, receipt text) and 5 (prefill, batch confirm, stale re-ask, PIN prompts, "forget me" flow) |
| **B: Channels + identity** | @Edoubek1024 | `app/channels/*`, `app/core/identity.py`, `app/reminders.py` (new), Twilio console, ngrok, `tests/test_channels_*.py` | 2, 7, 9, storage side of 5 (PIN, shared phones) |
| **C: Forms, PDFs, documents** | @luisNava111 | `app/engines/form_library.py`, `app/engines/document_engine.py`, `app/engines/ingest.py`, `app/pdf/*`, `forms/*`, `scripts/ingest_form.py`, `scripts/inspect_pdf.py`, `scripts/make_sample_form.py`, `app/llm/prompts/document*`, `app/llm/prompts/ingest*`, `tests/test_forms_*.py`, `tests/test_docs_*.py` | 3, fill + verify side of 4, 6 |
| **D: Memory, dashboard, metrics, demo** | @veed2005 | `app/memory/*`, `app/engines/status_engine.py`, `app/dashboard/*`, `app/metrics.py` (new), `scripts/seed_demo.py`, `scripts/reset_demo.py`, `docs/PRIVACY.md`, `docs/DEMO_SCRIPT.md`, `tests/test_memory_*.py`, `tests/test_dashboard_*.py` | storage side of 5, 8, 10 |

The source of truth for file ownership is `scripts/lane.py` (CODEOWNERS mirrors it). Check any file with `python3 scripts/lane.py owner <path>`.

### How lanes are enforced

- **Claude Code knows your lane.** When anyone opens Claude Code in this repo, a session hook (`.claude/settings.json` → `scripts/lane.py session-start`) works out who you are from your GitHub login (`gh`) or git config, and tells Claude your lane, your plan file, and which files it may edit. If it can't tell, Claude asks you. Pin it yourself with `python3 scripts/lane.py set <A|B|C|D>`.
- **Git hook.** `.githooks/pre-push` blocks direct pushes to `main` and warns when your branch edits another lane's files or shared files. `sh scripts/setup.sh` turns it on (so does the Claude session hook). If the hook's script ever breaks, it warns and lets the push through rather than blocking you.
- **CODEOWNERS.** A PR that touches another lane's files automatically requests that owner's review.
- **Self-check any time:** `python3 scripts/lane.py check`.

## Shared files: announce before editing

These cause conflicts if two people edit them at once. Post in the team chat before you change them, keep the change small and **additive**, and merge it quickly.

- `app/contracts.py`: data shapes that cross lanes. Add optional fields; never rename or remove one without agreement.
- `app/models.py`: database tables. Prefer storing new per-task or per-document data in the existing JSON columns (`Task.answers`, `Task.verification`, `Document.result`, `Session.pending`, `Event.data`) over adding columns.
- `app/events.py`, `app/llm/client.py`, `app/config.py`, `.env.example`
- `app/main.py`: every router is already registered. You shouldn't need to touch it.
- `pyproject.toml` / `uv.lock`: add dependencies with `uv add <pkg>` and say so in chat.

The database is recreated from the models, not migrated. After someone changes `models.py`, delete `data/formline.db` and restart.

## Cross-lane APIs (already stubbed)

| Caller → Provider | Function | Status |
|---|---|---|
| B → A | `core.turn.handle_turn(TurnRequest) -> TurnResult` | placeholder brain |
| A → B | `core.identity.get_session / save_session / note_channel / profiles_for_phone / create_profile / update_profile / set_pin / check_pin / pin_verified` | working |
| D → B | `core.identity.reset_pin`, `reminders.send_now / pending_reminders` | working |
| A → B | `reminders.create_reminder(profile_id, due_at, message, ...)` | working (scheduler in B4) |
| D → C | `engines.ingest.ingest_pdf(path, form_id=..., name=..., aliases=...)` | stub (C4) |
| A, D → B | `channels.outbound.send_sms(to, body)` | working (logs only when Twilio isn't configured) |
| A → C | `engines.form_library.list_forms / load_schema / load_meta / match_form / pdf_path` | working (alias match only) |
| A → C | `pdf.fill.fill_pdf(template, {pdf_field: value}, out_path)` and `pdf.verify.verify_pdf(path, expected) -> VerificationResult` | working (no truncation check yet) |
| A → C | `engines.document_engine.explain_document(media_paths, language) -> DocumentExplanation` | placeholder |
| A, C → D | `memory.profile.get_facts / get_fact / set_fact / confirm_fact / forget_profile` | working |
| A → D | `engines.status_engine.recent_activity(profile_id)` | working |
| everyone → D | `events.log_message / log_activity / log_event / publish` | working |
| everyone → C | `llm.client.structured(PydanticModel, system=..., messages=...)`, `llm.client.text(...)`, `llm.client.image_block(path)` | working (needs `ANTHROPIC_API_KEY`) |

### Conventions that make the lanes fit together

**`Task.answers`** (written by A, read by D's dashboard and A's receipts):
```json
{"applicant_name": {"value": "Ana Lopez", "source": "memory", "updated_at": "2026-10-03T14:02:11Z"}}
```
`source` is one of `memory | asked | corrected | document | unknown | skipped` (`AnswerSource` in contracts).

**Dashboard events:** call `events.publish(type, **payload)`. The types and payloads are listed at the top of `app/events.py`. D renders them; A, B, and C just publish.

**Metrics events:** call `events.log_event(name, ...)` with these names so D's metrics panel can compute Section 12:

| Event | Who logs it | Data |
|---|---|---|
| `turn` | A | `channel`, `latency_ms` |
| `form_started` | A | `form_id`, `from_document_id` (optional) |
| `field_answered` | A | `field_id`, `source` |
| `readback_correction` | A | `field_id` |
| `form_completed` | A | `form_id`, `duration_s`, `turns`, `fields_total`, `fields_from_memory` |
| `form_abandoned` | A | `form_id`, `last_field` |
| `verification` | A | `ok`, `mismatches` |
| `document_explained` | A | `document_type`, `related_form_id` |
| `channel_switch` | B | `from`, `to` |
| `voice_latency` | B | `ms` |
| `reminder_sent` | B | `reminder_id` |

**Tests:** put yours in `tests/test_<lane-area>_*.py` so test files never conflict. Mock the LLM by monkeypatching `app.llm.client.structured` / `text`. Mark tests that hit the real API with `@pytest.mark.live`. `tests/test_integration.py` checks the cross-lane wiring; if you break it, fix it before merging.

## Git workflow

1. **`main` always runs.** Never push broken code to it. CI runs `pytest` on every PR.
2. **One short-lived branch per task**, named `<lane>/<what>`: `a/form-engine`, `b/mms-download`, `c/ingest`, `d/dashboard-live`.
3. **Merge small and often**, at least every 2–3 hours. A 300-line PR merges cleanly; a 3,000-line PR at hour 20 doesn't.
4. **Start every task, and update before every PR, with:**
   ```bash
   git checkout main && git pull
   git checkout -b a/my-task          # or: git rebase main (on an existing branch)
   ```
5. **Open a PR, let CI pass, squash-merge it yourself.** No review is required for changes inside your own lane. Get a quick look from the affected lane's owner if you touched a shared file.
6. **Delete the branch after merging.**

**If `uv.lock` conflicts:** take main's version and regenerate.
```bash
git checkout --theirs uv.lock && uv lock && git add uv.lock
```

## Checkpoints

Short sync every ~3 hours: what merged, what's blocked, and whether anything in the contracts needs to change. The checkpoints (M1, M2, M3, Demo) and what each lane delivers for them are in [docs/PLAN.md](PLAN.md).

## Running things

```bash
uv sync                                   # install
uv run uvicorn app.main:app --reload      # server on :8000
uv run python scripts/simulate.py         # chat with the brain in the terminal
python3 scripts/lane.py whoami            # which lane am I?
uv run pytest                             # tests
```
