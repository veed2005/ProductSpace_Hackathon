# Finale: add a brand-new form on stage

`IL444-1893_snap_renewal.pdf` is the official IDHS SNAP Redetermination (renewal) form, R-09-17.
It isn't in the form library (no `meta.json` here, so `list_forms` skips this folder), so it can be
uploaded live and filled by phone a minute later, mostly from Maria's memory.

Why this one: 3 pages and 89 fillable fields (fast to ingest, ~3k-token prompt), a clean AcroForm
with descriptive tooltips, the same household/income questions as the SNAP application, and a
"Yes [ ] No [ ]" layout that ingestion reads correctly.

## Before the demo

```bash
# Check what the model will get (no API call):
uv run python scripts/ingest_form.py forms/_new_form_demo/IL444-1893_snap_renewal.pdf \
    --id il_snap_renewal --name "Illinois SNAP Renewal" --dry-run

# Dry run of the real thing on the backup laptop (needs ANTHROPIC_API_KEY), then delete it:
uv run python scripts/ingest_form.py forms/_new_form_demo/IL444-1893_snap_renewal.pdf \
    --id il_snap_renewal --name "Illinois SNAP Renewal" --alias "SNAP renewal" --alias "renew my food stamps"
rm -r forms/il_snap_renewal
```

## On stage

Dashboard -> Form library -> "Add a new form": upload this PDF, name "Illinois SNAP Renewal",
aliases "SNAP renewal, renew my food stamps, redetermination". Then text the number:
"I need to renew my food stamps."

If the upload fails or runs long, say so and move on; the three main forms don't depend on it.
