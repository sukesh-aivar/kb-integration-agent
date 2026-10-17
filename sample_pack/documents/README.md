# Sample Pack - Documents

Drop the open-source product documents the knowledge base will be built from
here. This directory is **intentionally empty** of content: the build agent does
not fabricate document text (that would make the KB and recall metrics
meaningless, and risks licence problems).

## What to place here

Per the spec's Monday deliverable (section 1.4):

- Open-source product documentation - e.g. Nextcloud / Rocket.Chat admin and
  user manuals, FAQs, policies.
- Roughly **60-150 pages** in total.
- A deliberate mix of formats to exercise the router (spec 4.2):
  - clean `.txt` / `.md` / `.html` -> should route `DEFAULT`
  - normal `.pdf` / `.docx` -> `DEFAULT`
  - **some scanned-to-PDF / image-heavy PDFs** -> should route `BDA`
  - a password-protected / corrupt PDF -> should route `REJECT`
- Optionally a few files that trip specific router rules (multi-column PDF,
  table-heavy PDF, DOCX with embedded images) so each rule has a fixture.

## Required for every file you add

1. Record its **licence and source URL** in `../LICENSES.md`.
2. Confirm the licence permits redistribution in this demonstration pilot.
3. Note any conversion (e.g. "rendered to a scanned PDF for the OCR edge case").

## Why empty now

Steps 1-3 of the build (scaffold, sample-pack generator, evals) do not require
document content - no pipeline or KB code exists yet. Document fixtures are
consumed starting with the red-pill stages (build-order step 4). Keeping this
directory empty-but-documented avoids inventing content and keeps `make test`
offline and deterministic.
