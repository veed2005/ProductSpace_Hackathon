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
