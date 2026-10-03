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

**"Forget me" hard-deletes the person's data but keeps anonymized metrics events.**
- Alternatives: soft delete (a `deleted_at` flag); delete metrics events too.
- Why: a deletion the person asked for should be real, including their filled PDFs and letter photos. Metrics events keep only numbers (durations, counts) once identity is stripped, so the aggregate metrics stay honest. On a shared phone, transcript lines that were never tied to anyone are kept while another profile remains, because they may belong to that person.

**Form upload runs ingestion inside the request (no job queue), with a live elapsed-time counter in the dialog.**
- Alternatives: background job + polling.
- Why: ingestion targets under a minute and happens a handful of times, mostly on stage. A synchronous request is one moving part instead of three. A failed ingestion removes the half-written form folder so the upload can be retried.

**The form library flags problems instead of blocking: PDF fields that don't exist, unknown memory keys, broken conditions, SSN fields not marked sensitive.**
- Why: generated schemas are drafts. The reviewer sees exactly what to fix before pressing "Mark reviewed".

**The dashboard and its API only answer requests made on the machine running Formline.**
- Alternatives: a password; leave it open.
- Why: during the demo ngrok exposes the whole server so Twilio can reach the webhooks, which would also put personal data and the "Reset everything" button on a public URL. ngrok forwards from localhost but adds `X-Forwarded-For`, so the check rejects any request that's non-local or carries proxy headers. `/twilio/*` is unaffected. `FORMLINE_DASHBOARD_REMOTE=true` turns it off for a trusted network. A password would be the production answer.

**Metrics come from the `events` table; the per-form "x% faster" banner comes from task timestamps.**
- Why: metrics follow the brief's event contract and keep working after "forget me" strips identity. The live banner only needs this person's previous task, which the tasks table answers directly. Both use the same definition: time from form start to completion.

**Transcript hides a 4-digit reply only when Formline's previous message asked for a PIN or SSN digits.**
- Why: the first version hid every bare 4-digit reply, which also hid income answers like "1450" during testing.

**PDF fill: checkbox and radio values map to the widget's real on-state; unrecognized values are left unset.**
- Alternatives: write the schema's `pdf_values` string as-is; treat any non-empty value as "checked".
- Why: real forms use "On", "1", "Y" or custom names instead of "Yes", and writing the wrong name leaves the box blank in most viewers. Mapping yes/true/on/x and the on-state itself covers schemas written without opening the PDF. A value like "sometimes" isn't guessed: it stays unchecked and `verify_pdf` reports a mismatch, so the brain re-asks instead of shipping a wrong box.

**PDF fill strips the XFA layer and sets NeedAppearances.**
- Why: many government PDFs are XFA hybrids, and Acrobat shows the (empty) XFA data instead of the AcroForm values we wrote. Regenerated appearances plus NeedAppearances make values show in Preview, Chrome and Acrobat. The cost is that XFA-only scripting (dynamic sections, calculations) no longer runs, which we don't use.

**Truncation is measured, not guessed: base-14 font widths against the widget rect.**
- Alternatives: rely on `max_length` only; render and OCR the result.
- Why: most boxes have no MaxLen, and long names and addresses are the realistic failure. Unknown embedded fonts are measured as Helvetica, which can be off by a few percent; auto-size fields count as truncated only when the text would shrink below 6pt. Multiline uses greedy word wrap at 1.15 line height.

**Required-field check skips checkboxes and conditional fields whose condition can't be read from the PDF.**
- Why: unchecked means "no", which is a valid answer; and flagging a conditional field we can't evaluate would block completion on a false alarm. A caller that wants strict checks can pass an explicit list of pdf fields instead of the schema.
