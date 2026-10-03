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
**The demo uses a live new user (Rosa) whose memory is built on stage, with the seeded Maria only as a fallback.**
- Alternatives: demo memory reuse with a pre-seeded returning user.
- Why: judges see the profile being built in step 1 and reused in step 4, so the "x% faster" number is measured live, not staged. Maria stays seeded in case memory reuse fails.

**A pre-flight script checks the demo setup instead of a printed checklist alone.**
- Why: the failures that sink live demos are configuration (stale ngrok URL, Twilio webhook pointing at yesterday's tunnel, dev endpoint left on, key missing). Each is a few lines to check automatically, and the script prints the fix.

**Demo forms are all Illinois: IDHS IL444-0683 (SNAP), HFS 2378H (Medicaid), ISBE 68-06 (school meals).**
- Alternatives: the combined IL444-2378B (cash + medical + SNAP, 20 pages); USDA's prototype school meals form; other states' Medicaid renewals.
- Why: same state means the same household, income and address questions, so memory reuse is obvious on stage. All three are fillable AcroForms (two are XFA hybrids, which `fill_pdf` handles), and SNAP-only 0683 is half the length of 2378B. Illinois has no blank Medicaid renewal form (renewals are mailed pre-filled, and DC/Ohio renewals we found are flat scans), so the HFS 2378H medical benefits application stands in for "Medicaid renewal"; its aliases include "Medicaid renewal".

**The SNAP schema is 27 hand-picked fields, not the form's 367.**
- Why: a phone conversation can't ask 367 questions. We ask what decides eligibility and benefit size (household, income, rent) plus contact details, and leave the rest (immigration table, race, signature) for the caseworker. Yes/No answers map to on-states by button position, because this form names them inconsistently ("0" is Yes on most rows and No on others).

**Gaps in the SNAP schema we accepted for now.**
- The applicant's name is written once (page 1), not again in row 1 of the household table, because a schema field maps to one PDF field. An additive `also_pdf_fields` on `FormField` would fix it.
- The household member's name box isn't mapped to memory: it wants "Last, First" in one box, and memory stores first/last separately with no formatter for a list item's full name. Asked Lane D for one.
- The SSN box gets whatever Lane A writes for the last 4 (e.g. "XXX-XX-1234"); full SSNs are never collected.

**`match_form` matches whole words, longest alias first, accents ignored.**
- Why: substring matching made "EBT" match "medical debt" and "SNAP" match "snapshot". Longest-first lets a specific alias ("school lunch") beat a generic one, and accent folding lets "almuerzo gratis" match "almuerzo gratís" typed on a phone.

**`fill_pdf` writes button states as raw PDF names.**
- Why: pymupdf decodes escaped names when writing, so the school meals form's "Hispanic#2FLatino" became /Hispanic/Latino and the radio showed blank in every viewer. We set /AS and /V ourselves after pymupdf's update.

**Document engine: one strong-model call over all photos, then deterministic checks on the result.**
- Alternatives: OCR first and send text; one call per page; let the model's output stand.
- Why: vision reads layout (which date is the deadline, which number is the case number) better than OCR text, and one call keeps a multi-page letter coherent. Code then enforces what the model must not decide alone: `related_form_id` must exist in the library, Social Security numbers are dropped from reference numbers, confidence is clamped, and a keyword backstop sets `high_stakes` for eviction/court/immigration even if the model misses it (it never downgrades the model's `true`).

**Letter text is only ever inside the image; the system prompt says photos are data.**
- Why: prompt injection in a mailed letter ("ignore previous instructions") is a real risk for a tool that reads strangers' mail. Nothing from the photo is copied into a text block, so the only instructions the model sees come from us. A live test sends a letter with an injection attempt.

**Photos are downscaled to 2000 px and re-encoded as JPEG when needed; PDFs are rendered (first 5 pages).**
- Why: phone photos are often 4000 px and several MB, which is slower and costlier with no gain in readability, and MMS can deliver PDFs or formats the API doesn't take. Anything we can't open (e.g. HEIC) is reported in `unreadable_parts` so the brain can ask for a retake; with nothing readable, we skip the model call entirely.

**`explain_document` raises on API failure instead of returning a low-confidence result.**
- Why: a low-confidence result makes the brain say "the photo is blurry, please retake it", which is wrong when the real problem is the network. Lane A catches the error and apologizes instead.
