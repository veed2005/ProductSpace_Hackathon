# Tradeoffs and design decisions

Format: decision, alternatives considered, why.

## Phase 0

**SQLite + SQLModel, with JSON columns for form answers and document results.**
- Alternatives: Postgres; fully normalized answer tables.
- Why: zero setup for a 24–36h build. Forms are schema-driven, so storing answers as JSON keyed by field id means a new form needs no migration.

**Added `Session` and `Message` tables beyond the brief's model list.**
- Alternatives: keep conversation state in memory; derive the transcript from `Event`.
- Why: `Session` holds per-phone conversation state in the database, so a person can hang up a call and continue by text (channel switching) and state survives server restarts. `Message` gives the dashboard a clean interleaved transcript labeled by channel.

**Form library on disk is the source of truth; the `Form` table is an index.**
- Alternatives: store schemas only in the database.
- Why: schemas are generated and then human-reviewed, so they belong in git where they can be diffed. The table speeds up matching and backs the dashboard.

**Freshness stored per fact (`freshness_days`), defaulted from a per-key policy in code.**
- Alternatives: compute freshness only from a code table.
- Why: the brief wants each value to carry its policy, and storing it per fact lets a document or partner override it for one person.

**Default models: `claude-haiku-4-5` for conversation turns, `claude-opus-5-5` for documents and schema generation.**
- Alternatives: one model for everything; Sonnet for turns.
- Why: voice latency is the tightest constraint, and Haiku is the fastest. Document understanding and schema generation happen less often and benefit most from the strongest model. Both are set through environment variables, so we can swap them after measuring.

**Python pinned to 3.11–3.13.**
- Why: 3.14 is the machine default, but some compiled dependencies (PyMuPDF, uvloop) lag new Python releases. 3.11 is installed and known good.

## Team setup

**Four lanes with frozen contracts and working stubs, instead of splitting by phase.**
- Alternatives: everyone works through the phases in order; split by feature without interfaces.
- Why: four people over ~30 hours. Phases depend on each other (the dashboard needs forms, forms need the brain), so splitting by phase leaves people blocked. Lanes own disjoint files, so merges rarely conflict. Every cross-lane call already has a stub that returns realistic data, so each lane can build and test on its own from hour one.

**A hand-written placeholder form (`forms/sample_benefits`) with a generated fillable PDF.**
- Why: lets Lanes A and C work on the full fill → verify → receipt path before the real PDFs are chosen and ingested.

**Server-side refusal fallback on for Opus/Sonnet 5.5 calls in `llm/client.py`.**
- Why: if the strong model declines a document (for example a medical bill or court notice it misreads as sensitive), the API retries on another model in the same call instead of failing the person's request.

## Lane D

**Profile keys are paths into a small set of top-level facts (`address.city`, `household_members[0].first_name`), and freshness is tracked per top-level fact.**
- Alternatives: one flat fact per form-field-sized value (`address_city`, `member1_first_name`); freshness per sub-field.
- Why: forms split the same data differently (one "address" box vs. street/city/zip boxes; three household rows vs. five), and paths let every form map onto one shape. Re-confirming a whole address when one part changes matches how people answer ("yes, that's still my address").

**`full_name` is a virtual key over `name.{first,middle,last}`.**
- Why: some forms want one name box and others want three. Splitting "Ana Maria de la Cruz" by first/last token is imperfect for multi-word surnames; the read-back gives the person a chance to correct it.

**Demo reset drops and recreates tables instead of deleting the SQLite file.**
- Why: it works while the server is running, so the dashboard's "Reset demo" button can call the same function between rehearsals.

**Seeded persona includes a completed first form 45 days ago, with income and employment deliberately stale.**
- Why: the on-stage memory metric needs a "first form" baseline, and the stale values show the re-ask-with-hint behavior.

**Dashboard: vanilla JS + SSE-triggered refetch, plus a 4-second poll.**
- Alternatives: a React/Vue build; pushing full state over the event stream.
- Why: no build step for a 30-hour project, and nothing loaded from a CDN, because venue Wi-Fi is unreliable. Events only say "something changed" and the page refetches, so missing an event can't leave the screen wrong. The poll covers anything published from another process (e.g. the simulator run in-process).

**Light theme, large type, phone numbers masked to the last 4 digits.**
- Why: projectors wash out dark themes, judges read from the back of the room, and team phones appear on screen.

**Transcript hides inbound replies that are just 4 digits.**
- Alternatives: a `sensitive` flag on each logged message, set by the brain.
- Why: PINs and SSN last-4 are typed as bare digits, and this needs no cross-lane change. It misses longer messages like "my pin is 1234". A per-message flag from Lane A would be exact; we can add it later.
