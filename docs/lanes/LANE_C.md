# Lane C brief: Forms, PDFs, documents

**Owner:** @luisNava111 · **Stage plan:** [docs/PLAN.md](../PLAN.md) · **Full spec:** [docs/PROJECT_BRIEF.md](../PROJECT_BRIEF.md) (read Sections 1, 8, 9, 10 "Verification", 14)

You own everything about paper. That includes the form library, filling official PDFs and proving they were filled correctly, explaining photos of letters, and the "any form" capability: ingesting a brand-new PDF and generating its question flow automatically. Your functions are pure engines. Lane A calls them during conversations, and Lane D calls ingestion from the dashboard.

## You own

`app/engines/form_library.py`, `app/engines/document_engine.py`, `app/engines/ingest.py`, `app/pdf/*`, `forms/*`, `scripts/ingest_form.py` (new), `scripts/inspect_pdf.py` (new), `scripts/make_sample_form.py`, `app/llm/prompts/document*` and `app/llm/prompts/ingest*` (new), `tests/test_forms_*.py`, `tests/test_docs_*.py`, `tests/test_pdf_*.py`, `tests/fixtures/documents/*`.

You're also the main user of the shared `app/llm/client.py`. If it needs changes, make them small and announce them.

## You call

| Need | Function | Lane |
|---|---|---|
| Canonical profile keys for `profile_key` mapping | `memory.profile.FRESHNESS_POLICY` keys | D |
| LLM | `llm.client.structured(Model, system=..., messages=..., model=llm.strong_model())`, `llm.client.image_block(path)` | shared |

**Others depend on you for:** a hardened `fill_pdf` / `verify_pdf` (A3, Stage 1), real demo schemas (A, Stage 2), `explain_document` (A5), and `ingest_pdf` (D4's upload). Keep these signatures stable. They're listed in `docs/TEAM.md`.

## Phases

### C1: PDF toolkit *(Stage 1)*
- `scripts/inspect_pdf.py <pdf>`: print every AcroForm field with its name, type, options, checkbox on-value, page, and rect.
- **`fill_pdf`:** checkboxes (use each widget's real on-state), radio buttons and choice fields, and appearance regeneration so filled values display in every viewer.
- **`verify_pdf`:**
  - Truncation: text wider than its box, measured with `pymupdf.get_text_length` against the widget rect and font size, or longer than `max_length`.
  - Required fields left empty: accept the schema, or a list of required pdf fields.
  - Checkbox state compared correctly.
- Tests in `tests/test_pdf_*.py`, including a deliberately failing verification (too-long text, wrong checkbox).
- **Done when:** A can rely on `verify_pdf` to catch real problems. Tell A when it's merged.

### C2: Real demo forms *(Stage 1; start immediately in parallel with C1)*
- Find three official PDFs that overlap heavily on household, income, and address: a **SNAP application**, a **Medicaid renewal**, and a **free/reduced school meals application**, ideally from the same state.
- **They must be fillable AcroForms.** Check with `inspect_pdf.py`. Many state forms are flat scans, so reject those.
- Create `forms/<form_id>/form.pdf` and `meta.json` with good aliases ("food stamps", "SNAP", "EBT").
- **Hand-write `schema.json` for SNAP first,** about 15–25 key fields, so A can test a real form early in Stage 2. Map fields to D's canonical keys.
- **Done when:** `tests/test_integration.py` passes with the new forms. It checks that each schema's `pdf_field`s exist in the PDF.

### C3: Document engine *(Stage 2)*
- `explain_document(media_paths, language=...)`: send all images in one `llm.structured(DocumentExplanation, model=llm.strong_model(), ...)` call.
- In the system prompt, treat everything in the image as **data, never instructions**. A letter saying "ignore previous instructions" must not change behavior; add a test for it.
- Pass the form library list (`form_id`, name, aliases) so the model can set `related_form_id`, and only accept ids that actually exist.
- Set `high_stakes` for eviction, court, and immigration documents.
- Blurry or partial photos: low `confidence` and `unreadable_parts` filled in, so A asks for a retake.
- **Fake sample letters** for testing and the demo go in `tests/fixtures/documents/`: a Medicaid renewal notice with a deadline, a medical bill, an eviction notice, and a blurry photo. Render them with PyMuPDF to PNG and photograph one with a phone for realism.
- Tests: mocked LLM unit tests, plus `@pytest.mark.live` tests on the fixtures.
- **Done when:** the Medicaid renewal fixture returns the right type, deadline, and `related_form_id`.

### C4: Ingestion, the "any form" capability *(Stage 2 → Checkpoint M2)*
- `ingest_pdf(pdf_path, form_id=..., name=..., aliases=...)`:
  1. Extract fields and page text with PyMuPDF.
  2. Ask the strong model for a draft `FormSchema`: plain-language labels and questions, types, required, conditions, validation, sensitive (any SSN field is `sensitive` and becomes `ssn_last4` or is skipped), `profile_key` mapped to the canonical keys, and `group`.
  3. Order the fields into a natural conversation.
  4. Write `schema.json` (`reviewed: false`) and `meta.json`.
- `scripts/ingest_form.py <pdf> --id <form_id> --name "..."` is the CLI wrapper.
- Validate the output: every `pdf_field` exists, ids are unique, and conditions reference real fields. Retry once on failure.
- Generate the Medicaid and school-meals schemas, then review them by hand.
- `match_form`: add LLM-assisted matching for fuzzy requests, keeping the alias match as the fast path.
- **Done when:** all three demo forms load, and A can fill each one in the simulator.

### C5: Upload to callable in about a minute *(Stage 3)*
- Make `ingest_pdf` fast enough for the stage demo (aim for under 60 seconds): trim the page text and consider a faster model for the drafting pass.
- Return clear errors for non-fillable PDFs ("This PDF has no fillable fields").
- Help D wire the dashboard upload.
- *Stretch, only if everything else is done:* non-fillable PDFs via vision field detection and text overlay.

### C6: Demo hardening *(Stage 4)*
- Pre-generate and commit the reviewed schemas.
- Fill all three forms with D's seed personas and open the PDFs to eyeball them.
- Keep a known-good form PDF ready for the "new form" finale.

## How to work

- Start a Claude Code session in this repo and say: **"Start Phase C1."** The session hook tells Claude you're Lane C and points it at this file.
- Your work is mostly testable without Twilio: use scripts, pytest, and open the output PDFs.
- After each phase: tests pass → PR → merge → post a summary in chat.
