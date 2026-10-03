# Lane D brief: Memory, dashboard, metrics, demo

**Owner:** @veed2005 · **Stage plan:** [docs/PLAN.md](../PLAN.md) · **Full spec:** [docs/PROJECT_BRIEF.md](../PROJECT_BRIEF.md) (read Sections 3, 7, 11, 12, 14, 15 "Phase 10")

You own what makes Formline smarter and what the judges see. That covers the person's memory (canonical facts with provenance and freshness), the live dashboard that is the centerpiece of the on-stage demo, the metrics that prove memory works, and the demo itself. You're also the **integrator**: you review PRs that touch shared files and run the checkpoint demos.

## You own

`app/memory/*`, `app/engines/status_engine.py`, `app/dashboard/*` (routes, templates, static), `app/metrics.py` (new), `scripts/seed_demo.py` and `scripts/reset_demo.py` (new), `docs/PRIVACY.md`, `docs/DEMO_SCRIPT.md`, `tests/test_memory_*.py`, `tests/test_dashboard_*.py`, `tests/test_metrics_*.py`. As integrator you also look after the shared files (`app/contracts.py`, `app/models.py`, `app/events.py`).

## You call

| Need | Function | Lane |
|---|---|---|
| Live events | `events.subscribe / unsubscribe` (A, B, and C publish) | yours |
| Forms | `engines.form_library.list_forms / load_schema / save_schema / pdf_path` | C |
| Upload a new form | `engines.ingest.ingest_pdf(path, form_id=..., name=..., aliases=...)` | C |
| Demo controls | `reminders.send_now / pending_reminders`, `identity.reset_pin` | B |

**Others depend on you for:** the canonical profile key list (C2, C4, A2: **deliver first**), `memory.*` (A4, C3), `recent_activity` (A4), and the dashboard to watch checkpoint demos.

## Phases

### D1: Canonical keys + demo data *(Stage 1, first hour; C and A need the key list)*
- Finalize the canonical keys and freshness policy in `FRESHNESS_POLICY` (`app/memory/profile.py`): `full_name`, `date_of_birth`, `address`, `phone`, `household_size`, `household_members[i].{name,relationship,dob}`, `employment.{employer,pay,frequency,varies}`, `monthly_income`, `housing_cost`, `utilities`, `disability_in_household`, `case_numbers`, …
- Post the list in the team chat so C maps schemas to it.
- `scripts/seed_demo.py`: fake personas only:
  - a brand-new number;
  - a returning person with a full profile, a PIN, and one **stale** fact (income confirmed 45 days ago);
  - a shared phone with two profiles.
- `scripts/reset_demo.py`: wipe `data/` (DB, media, filled PDFs) and reseed.
- **Done when:** `reset_demo.py` gives a clean, seeded database in one command.

### D2: Dashboard v1 *(Stage 1 → Checkpoint M1)*
- **JSON endpoints:**
  - `/api/people` (profiles with current activity, channel, and language)
  - `/api/phones/{phone}/messages` (interleaved transcript), `/api/phones/{phone}/task` (active or latest task)
  - `/api/tasks/{id}` (schema fields joined with `Task.answers`)
  - `/api/tasks/{id}/pdf` (download `output_pdf_path`)
- **Page `/dashboard`:** people list → live session view with the transcript labeled by channel, and the form filling in field by field, **color-coded by source** (memory / asked / corrected / unknown).
- Live updates from `/dashboard/events` (SSE is already wired).
- Make it projector-friendly: big type, high contrast, readable from the back of a room. A single HTML page with vanilla JS or a CDN library is fine; no build step.
- **Done when:** during the M1 run, the dashboard updates live without a refresh.

### D3: Memory hardening + profile view *(Stage 2)*
- `forget_profile` must delete or anonymize tasks, documents, messages, reminders, and activity for the profile (foreign keys are enforced), then confirm.
- **Profile view:** each fact with its value, source (conversation / form / document), confirmed date, and a fresh or stale badge. Mask sensitive values.
- Tests: freshness boundaries, nested keys, forget cascade.

### D4: Documents, verification, form library *(Stage 2 → Checkpoint M2)*
- **Document view:** the uploaded photo (serve `data/media` files) next to the structured explanation, deadlines, and related form.
- **Verification status** on completed tasks: a pass/fail badge, mismatches and truncations listed, and a Download PDF button.
- **Form library view:** list forms, view a schema, and "Mark reviewed" (sets `reviewed: true` via `form_library.save_schema`).
- **"Add a new form" upload:** call `ingest_pdf` and show progress. Until C4 lands, it shows the "not built yet" error.

### D5: Metrics + demo controls *(Stage 3 → Checkpoint M3)*
- `app/metrics.py` computes Section 12 metrics from the `events` table using the event names in `docs/TEAM.md`:
  - **Headline:** time for the first form vs. later forms for the same person.
  - Percentage of fields filled from memory.
  - Turns per form.
  - Correction rate.
  - Drop-off field.
  - Documents explained and how many led to a form.
  - Voice latency (average and p90).
  - Channel usage and switches.
  - Verification pass rate.
- A metrics panel on the dashboard, with the before/after form time shown big.
- **Demo controls:** Seed demo persona, Reset demo, Send reminder now, Reset PIN.

### D6: Demo *(Stage 4)*
- `docs/DEMO_SCRIPT.md`: the five-step on-stage flow from Section 15 of the brief, with exact phrases to say and type, who holds which phone, and what to point at on the dashboard.
- **Backup plan:** a pre-recorded video, plus the simulator on screen next to the dashboard.
- `docs/PRIVACY.md`: what we store and why, the PIN model and its limits (from B), and what production would need (encryption at rest, retention limits, consent script, partner audit access).
- Run two full rehearsals with the team and fix what breaks.

## As integrator

- Review any PR that touches `app/contracts.py`, `app/models.py`, or `app/events.py`, and merge those quickly so others aren't blocked.
- Run each checkpoint demo (M1, M2, M3) with the whole team, and keep the checkboxes in the README's build status up to date.

## How to work

- Start a Claude Code session in this repo and say: **"Start Phase D1."** The session hook tells Claude you're Lane D and points it at this file.
- Try the dashboard without phones: run the server with `FORMLINE_DEV_ENDPOINTS=true uv run uvicorn app.main:app --reload`, open `/dashboard`, and in another terminal run `uv run python scripts/simulate.py --server http://localhost:8000`. Events show up live.
- After each phase: tests pass → PR → merge → post a summary in chat.
