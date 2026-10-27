# Phase 1 (Part A): KB-only reorganization — what changed for integrators

**Branch:** `orch-to-kb` · **Scope:** reorganization only. Part A moves non-KB code
into `dormant/` and shrinks the active workflow to the four knowledge-base stages.
**No business logic changed** — moves are moves (`git mv`), and the document-processing
core is byte-for-byte identical to the pre-reorg baseline (verified, see *Golden diff*).

This is a one-pager for whoever integrates with the agent. The deep rationale and the
file-by-file plan live in `docs/dormant-move-plan.md`; the import evidence is in
`docs/dormant-import-graph.md`.

---

## 1. The active workflow is now four stages

```
normalize_docs → kb_create → kb_ingest → write_report
```

The previous 13-stage order (ticket load/adapt/clean, embed, cluster, name, retrieve,
coverage, draft) is parked. Dispatch was already dynamic (`getattr(self, f"_stage_{stage}")`
driven by `STAGE_ORDER`), so shrinking `STAGE_ORDER` to these four is what stops the
dormant stages from running. Nothing is deleted.

- `pipeline.py`: `STAGE_ORDER = KB_ONLY_STAGE_ORDER` (the four above). The original
  order is kept verbatim as `LEGACY_STAGE_ORDER_13`.
- The nine dormant `_stage_*` method bodies moved verbatim into
  `src/kb_agent/dormant/pipeline_stages.py` (`DormantStagesMixin`). The active
  `Pipeline` does **not** inherit the mixin.

## 2. Contract: additive only — no field was removed

The orchestrator ↔ agent contract (`contract.py`) is unchanged in shape. Specifically:

- `AgentInput.ticket_uri` is **still accepted**. In the KB-only workflow it is
  **ignored**, and the run emits exactly one info event at start:
  *"A ticket export was supplied and ignored: ticket analysis is not part of this workflow."*
  The CLI `--ticket-uri` flag is likewise kept and ignored.
- `AgentOutput.pending_review` is now always `[]` (review is dormant).
- `resume_from` naming a dormant/unknown stage is **invalid input** → `ValueError`
  that the HTTP layer maps to `400 invalid_input`, listing the four valid stage names.
- Run status in Part A stays `needs_review` (the `completed` change is Part B).

## 3. What `report.json` looks like now

Every key is preserved (additive contract). On a KB-only run:

- `intent_view`, `coverage`, `draft_articles` → `[]`
- `drafting_summary` → `{}`
- new key `"workflow": "kb_only"`
- the clustering-specific caveat line is gone; the fake-mode and synthetic-data
  caveats remain.
- `report.md` drops the ticket/coverage/drafting sections.

Metrics that only the dormant track produced (`intent_purity`, `silhouette`,
`cluster_count`, `noise_share`, `gaps_flagged`, `coverage_fp_rate`, `ticket_count`,
`recall_at_5`) are still present on the metrics object but stay `null`.

## 4. Config

`config/default.yaml` gains a `workflow` block:

```yaml
workflow:
  mode: kb_only               # validated at load; any other value fails fast
  legacy_stage_placeholders: false
  legacy_status_needs_review: false   # Part-B flag, kept for schema stability
```

- `legacy_stage_placeholders: true` makes the **reported** `stage_order` the original
  13 identifiers with the nine dormant stages marked `skipped` ("Not part of this
  workflow"). This only changes what is reported in `_started.json` / `manifest.json`
  and the `stage_skipped` events — **execution is always the four active stages.**
  Use it for a consumer that still expects the 13-identifier shape.
- The `clustering`, `drafting`, `coverage`, `planner` config blocks are **kept** (marked
  `# DORMANT`) so the schema is stable; their values are unused by the active workflow.

## 5. Dependencies and the runtime image

- `requirements.txt` is now **active-only**. Removed: `pandas`, `openpyxl`, `numpy`,
  `scikit-learn`, `umap-learn`, `hdbscan`.
- Those packages (plus `numba`, `llvmlite`) moved to **`requirements-dormant.txt`**, with
  per-package evidence. Install it only for dormant dev work.
- `.dockerignore` now excludes `src/kb_agent/dormant/`, the top-level `dormant/`, and
  `config/dormant/` from the image.
- Result: the active ARM64 image shrank from **~1.36 GB → ~0.55 GB**. Verified in the
  built image that `kb_agent.entrypoint` imports, the dormant subtree is absent, the six
  dormant packages are not installed, and a fake 4-stage run completes with
  `status=needs_review`.

> **Note (deferred to Part B):** `pymupdf` (AGPL) is still listed because
> `documents/features.py` uses it. Swapping it for `pdfplumber`-only is a logic change,
> so it is out of Part A scope. Tracked in `OPEN_QUESTIONS.md`.

## 6. Where things moved

```
src/kb_agent/dormant/
  tickets/            loaders, adapters/, clean, embed, cluster, categorise, naming
  join/               coverage, drafting, report_sections
  review/             state
  planner/            planner, tools
  llm_json.py         prompts.py           (dormant-only shared helpers)
  documents_extras.py recall_at_5_from_golden (split out of documents/knowledge_base.py)
  pipeline_stages.py  DormantStagesMixin + _silhouette_excluding_noise
config/dormant/prompts/     the 5 ticket/coverage/drafting/planner prompt templates
dormant/scripts/            check_tickets + ticket generators
dormant/sample_pack/tickets/ tickets.csv, labels.csv, ticket_profile.json
dormant/evals/              purity/silhouette/coverage-fp/planted-gap scoring
tests/dormant/              the moved + new dormant tests
```

`report.py` was **split**: the active KB-only report builder stays in
`src/kb_agent/join/report.py`; the ticket/coverage/drafting rendering moved to
`dormant/join/report_sections.py`. `evals/metrics.py` was split: the active file keeps
only `routing_accuracy`; the six scoring functions moved to `dormant/evals/metrics.py`.

Untouched (per the brief): `web/`, `deploy/`, backend Lambda source,
`documents/validate.py`, `documents/pii.py`.

## 7. Tests, lint, types

- `pytest` default run excludes dormant and aws (`addopts = -m "not aws and not dormant"`).
- **Active suite: 100 passed** (84 dormant deselected).
- **Dormant suite** (`pytest -m dormant` / `make test-dormant`): **84 passed**.
- `ruff`: clean. `mypy`: clean (53 source files).
- An AST guard (`tests/unit/test_no_evals_import_in_src.py`) fails the build if any
  active module under `src/kb_agent` imports from `kb_agent.dormant`.

## 8. Golden diff (regression evidence)

The same fake docs-only run that produced `tests/golden/phase1_baseline/` (four
documents: `faq.md`, `installation.md`, `quota.md`, `conflicts.html`) was re-run after
the reorg. The document-processing core (`_state.json`: routing decisions + ingestion
counts) is **identical** to the baseline. The only differences vs the baseline are the
enumerated, intended Part-A deltas:

- `stage_order` 13 → 4; `skipped` → `{}`
- `manifest.stages` 13 → 4 (all `completed`)
- `report.json` dormant sections emptied + `workflow: kb_only` added
- the nine dormant `stage_skipped` events and their nine `_events/*.json` objects are gone
- `pending_review` → `[]`

No change to routing rules, parsing, KB creation/ingestion, metrics computation, PII, or
run status.
