"""Form library: forms/<form_id>/{form.pdf, schema.json, meta.json}. Owner: Lane C."""

import json
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.contracts import FormMeta, FormSchema


def _forms_dir() -> Path:
    return get_settings().forms_dir


def form_dir(form_id: str) -> Path:
    return _forms_dir() / form_id


def pdf_path(form_id: str) -> Path:
    return form_dir(form_id) / "form.pdf"


def list_forms() -> list[FormMeta]:
    metas = []
    for meta_file in sorted(_forms_dir().glob("*/meta.json")):
        if (meta_file.parent / "schema.json").exists():
            metas.append(FormMeta.model_validate_json(meta_file.read_text()))
    return metas


def load_meta(form_id: str) -> FormMeta:
    return FormMeta.model_validate_json((form_dir(form_id) / "meta.json").read_text())


def load_schema(form_id: str) -> FormSchema:
    return FormSchema.model_validate_json((form_dir(form_id) / "schema.json").read_text())


def save_schema(schema: FormSchema) -> None:
    path = form_dir(schema.form_id) / "schema.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n")


def match_form(text: str) -> Optional[str]:
    """Cheap alias match. TODO(Lane C): LLM-assisted matching for fuzzy requests."""
    t = text.lower()
    for meta in list_forms():
        for name in [meta.name, meta.form_id, *meta.aliases]:
            if name.lower() in t:
                return meta.form_id
    return None
