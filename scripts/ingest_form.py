"""Add an official fillable PDF to the form library with a generated question flow.

Writes forms/<id>/form.pdf, schema.json (reviewed=false) and meta.json. Review the schema on
the dashboard (or by hand) before using it with people.
Run: uv run python scripts/ingest_form.py path/to/form.pdf --id il_medicaid --name "Illinois Medicaid" \
         --alias "medicaid" --alias "medical card"
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.engines.ingest import ingest_pdf, prepare  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--id", required=True, dest="form_id", help="e.g. il_medicaid")
    parser.add_argument("--name", required=True, help='e.g. "Illinois Medicaid Application"')
    parser.add_argument("--alias", action="append", default=[], help="what people call it (repeatable)")
    parser.add_argument("--model", help="override the model (default: FORMLINE_INGEST_MODEL, else STRONG)")
    parser.add_argument("--dry-run", action="store_true",
                        help="only read the PDF and show what the model would get (no API call, nothing written)")
    args = parser.parse_args()

    if args.dry_run:
        try:
            prep = prepare(args.pdf)
        except ValueError as e:
            sys.exit(f"error: {e}")
        print(f"{args.pdf}: {len(prep.fields)} fillable fields, {len(prep.field_lines)} lines shown to the model "
              f"({prep.rows_left_out} repeated rows left out), {prep.chars} chars (~{prep.chars // 4} tokens), "
              f"read in {prep.seconds:.2f}s")
        print("\n".join(prep.field_lines))
        return

    start = time.monotonic()
    try:
        schema = ingest_pdf(args.pdf, form_id=args.form_id, name=args.name, aliases=args.alias, model=args.model)
    except ValueError as e:
        sys.exit(f"error: {e}")
    mapped = sum(1 for f in schema.fields if f.profile_key)
    print(f"{schema.form_id}: {len(schema.fields)} questions ({mapped} from memory) "
          f"in {time.monotonic() - start:.0f}s. Review forms/{schema.form_id}/schema.json before use.")
    for f in schema.fields:
        cond = f"  [if {f.condition.field} = {f.condition.equals}]" if f.condition else ""
        print(f"  {f.id:28} {f.type:9} {f.profile_key or '':28} {f.question_hint}{cond}")


if __name__ == "__main__":
    main()
