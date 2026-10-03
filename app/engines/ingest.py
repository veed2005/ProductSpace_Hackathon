"""Turn an official PDF into a draft form schema. Owner: Lane C.

Called by scripts/ingest_form.py (CLI) and by the dashboard's "Add a new form" upload (Lane D).
"""

from pathlib import Path

from app.contracts import FormMeta, FormSchema


def ingest_pdf(pdf_path: Path, *, form_id: str, name: str, aliases: list[str] | None = None) -> FormSchema:
    """Copy the PDF into forms/<form_id>/, generate schema.json (reviewed=false) and meta.json.

    TODO(Lane C, Phase C4): extract AcroForm fields + page text with PyMuPDF, ask the strong model
    for a draft FormSchema, write the files, and return the schema.
    """
    raise NotImplementedError("form ingestion is not built yet (Lane C, Phase C4)")


def draft_meta(form_id: str, name: str, aliases: list[str] | None = None) -> FormMeta:
    return FormMeta(form_id=form_id, name=name, aliases=aliases or [])
