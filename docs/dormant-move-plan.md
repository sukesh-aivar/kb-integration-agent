# Part A — Dormant Move Plan (Checkpoint A1)

**Branch:** `orch-to-kb` · **Baseline:** 159 active tests pass, ruff clean, mypy clean (48 files).
**Rule reminder:** Part A moves code only (`git mv`), changes no logic, deletes nothing. This document
is for approval. **No moves have been made yet.** Awaiting Checkpoint A1 sign-off.

Evidence base: `docs/dormant-import-graph.md` (AST import scan) and the golden baseline under
`tests/golden/phase1_baseline/`.

---

## 0. Corrections to the instruction (rule 0.2.1 — report where code disagrees)

These are places the v3 spec's A2 inventory or assumptions differ from the actual repository. None are
blockers; they change *how* I execute, so they need your eyes before I move anything.

1. **Dispatch is already a dynamic registry-by-convention — no new registry needed.**
   The spec (A3) says "If stages are dispatched from a hard-coded sequence, introduce the smallest
   possible registry … Report this in the plan first." They are **not** hard-coded. `Pipeline._run_stage`
   does `handler = getattr(self, f"_stage_{stage}")` and the driver loops `for stage in STAGE_ORDER[...]`.
   **Consequence:** simply reducing `STAGE_ORDER` to the four active IDs stops every dormant `_stage_*`
   method from ever being called — even if the methods physically remain on the class. So I propose
   **NOT** introducing a new registry (nothing to add), and **NOT** physically extracting the nine
   dormant `_stage_*` methods into `dormant/pipeline_stages.py` in Part A. See §3 for the recommended
   handling and the trade-off. **This is the single biggest deviation from the spec and needs your call.**

2. **The spec missed `numpy`; it is an ACTIVE-module import.** A2 lists only "possibly scikit-learn,
   pandas, openpyxl" as ML deps to consider. In fact `pipeline.py` imports **both numpy and sklearn**
   (lines 401 and 761), but only inside ticket-clustering code (`_stage_intent_cluster` and
   `_silhouette_excluding_noise`). They become dormant-only once that code moves. See §4.

3. **`pandas` is not imported anywhere in `src/`.** A2 lists it as a candidate; the scan finds zero uses.
   It will not appear in `requirements-dormant.txt` unless it's a pure transitive dependency (then it
   belongs to neither list explicitly).

4. **`categorise.py` is already unwired.** The `<500`-ticket LLM fallback (`tickets/categorise.py`) is
   built and tested but **not called** by `_stage_intent_cluster` (that branch just assigns label 0 and
   `np.zeros`). It still MOVES (it's dormant), but note it was never in the active path.

5. **Current finished status is `needs_review`, not `completed`.** D10/`completed` is a **Part B** change
   (B1.3). Part A must NOT change the status, or the golden diff (A6) would show an unintended delta.
   The baseline golden captures `needs_review`.

6. **`report.json` already contains the ticket/coverage/draft keys.** `intent_view`, `coverage`,
   `draft_articles`, `drafting_summary` are always present (the builder emits them from empty state on a
   docs-only run). So A4.4 "set them to `[]`/`{}`" is partly already true for a docs-only run; the real
   change is making the KB-only `write_report` path emit them empty *by construction* and add
   `"workflow": "kb_only"`. See §6.

7. **`llm_json.py` and `prompts.py` are provably dormant-only** (imported only by drafting/coverage/
   planner/categorise/naming) — the spec's conditional ("MOVE only if the import graph shows
   dormant-only use") resolves to **MOVE both**.

8. **pytest already excludes aws via `addopts = "-m 'not aws'"`**; the spec's A6 "default addopts excludes
   aws and dormant" needs `addopts` changed to `-m "not aws and not dormant"` plus a registered `dormant`
   marker. (The `slow` marker already exists and should keep running by default.)

---

## 1. Final file classification (verified line by line)

Legend: **MOVE** = whole file `git mv` into `dormant/`; **SPLIT** = part moves; **ANNOTATE** = stays,
add `# DORMANT-OWNED:`/`# DORMANT` comment, no move; **KEEP** = untouched; **EDIT** = active file changed
(logic-free where possible).

### Ticket track → `src/kb_agent/dormant/tickets/`
| Path | Class | Notes |
|---|---|---|
| `tickets/__init__.py` | MOVE | |
| `tickets/loaders.py` | MOVE | sole `openpyxl` user |
| `tickets/adapters/__init__.py` | MOVE | |
| `tickets/adapters/base.py` | MOVE | |
| `tickets/adapters/sample.py` | MOVE | |
| `tickets/clean.py` | MOVE | |
| `tickets/embed.py` | MOVE | |
| `tickets/cluster.py` | MOVE | sole `umap`/`hdbscan` user |
| `tickets/categorise.py` | MOVE | already unwired (§0.4) |
| `tickets/naming.py` | MOVE | uses numpy |

### Join → `src/kb_agent/dormant/join/`
| Path | Class | Notes |
|---|---|---|
| `join/__init__.py` | KEEP a thin one active; the dormant one gets its own | `join/report.py` stays active (SPLIT), so `join/` package stays |
| `join/coverage.py` | MOVE | |
| `join/drafting.py` | MOVE | |
| `join/report.py` | **SPLIT** | active KB-only builder stays in `join/report.py`; ticket/coverage/drafting rendering moves to `dormant/join/report_sections.py` |

### Review / Planner → `dormant/`
| Path | Class | Notes |
|---|---|---|
| `review/__init__.py`, `review/state.py` | MOVE | → `dormant/review/` |
| `planner/__init__.py`, `planner/planner.py`, `planner/tools.py` | MOVE | → `dormant/planner/` |

### Shared-but-dormant-only
| Path | Class | Notes |
|---|---|---|
| `llm_json.py` | MOVE | → `dormant/llm_json.py` (dormant-only, §0.7) |
| `prompts.py` | MOVE | → `dormant/prompts.py`; update its `PROMPTS_DIR` to `config/dormant/prompts/` |
| `config/prompts/*.v1.md` (naming, categorise, coverage, drafting, planner) | MOVE | → `config/dormant/prompts/` |

### Documents — mostly KEEP, one SPLIT
| Path | Class | Notes |
|---|---|---|
| `documents/features.py` | KEEP | active (router needs it) |
| `documents/router.py` | KEEP | active |
| `documents/staging.py` | KEEP | active |
| `documents/validate.py` | KEEP, untouched | rule 0 D9: masking/detection left as-is |
| `documents/pii.py` | KEEP, untouched | |
| `documents/bda.py` | KEEP | active |
| `documents/normalize.py` | KEEP | active |
| `documents/knowledge_base.py` | **SPLIT** | `KnowledgeBaseStage` (incl. `retrieve`) stays active; **`recall_at_5_from_golden` moves** → `dormant/documents_extras.py` |

### Contract / models / config — ANNOTATE only (never move classes)
| Path | Class | Notes |
|---|---|---|
| `models.py` | ANNOTATE | `# DORMANT-OWNED:` above `Ticket`, `MappingReport`, `IntentCluster`, `Article`, `CoverageItem` (and `CoverageStatus`, `ReviewStatus` if only dormant uses them — **verify**: `ReviewStatus` is referenced by `IntentCluster`/`Article` and `review/state`; `SourceType`/`Route` are active, keep unannotated) |
| `contract.py` | ANNOTATE | `reviewer_feedback` (ReviewerFeedback) is dormant-owned; `pending_review`/`ArtifactRef` dormant-owned. Annotate, do not remove (rule 5 additive-only) |
| `config.py` (`AppConfig`) | ANNOTATE | `# DORMANT` on `ClusteringConfig`, `DraftingConfig`, `CoverageConfig`, `PlannerConfig` and their `AppConfig` fields — do NOT delete (A4/rule 5) |
| `config/default.yaml` | ANNOTATE | `# DORMANT` comments over `clustering`, `drafting`, `coverage`, `planner` blocks; keep values |

### aws/* — KEEP (shared infra, incl. the LLM client)
`aws/interfaces.py`, `aws/clients.py`, `aws/fakes.py`, `aws/clientset.py` — KEEP. The `LlmClient`
protocol and `BedrockLlm`/`FakeLlm` stay (rule: LLM client is shared infrastructure even though no active
stage calls it). **Verify** `FakeKnowledgeBase`/`FakeObjectStore` have no dormant-only dependency (they
don't; they're used by active tests).

### Pipeline / runstate — EDIT (see §3, §5)
`pipeline.py` SPLIT/EDIT; `obs/runstate.py` EDIT (stage order + skip set). `obs/audit.py`,
`obs/timing.py`, `obs/tracing.py` KEEP.

### Scripts / sample_pack / evals / tests
| Path | Class | Notes |
|---|---|---|
| `scripts/check_tickets.py` | MOVE → `dormant/scripts/` | |
| ticket generators in `sample_pack/` (e.g. `generate_tickets.py`) | MOVE → `dormant/scripts/` | **verify exact filenames at execution** |
| `sample_pack/tickets/`, `sample_pack/labels.csv`, `sample_pack/ticket_profile.json` | MOVE → `dormant/sample_pack/tickets/` | `sample_pack/documents/` STAYS |
| `evals/` | SPLIT | purity, silhouette, coverage-FP, planted-gap scoring → `dormant/evals/`; routing-accuracy + PII-adversarial stay. **Verify** `evals/metrics.py` function boundaries before splitting |
| `tests/unit/test_blue_pill.py`, `test_join.py`, `test_clustering_purity.py`, `test_planner.py`, `test_ground_truth_leak.py`(?) | MOVE → `tests/dormant/` | **verify** each test's imports; `test_ground_truth_leak` may assert active behavior — classify at execution |
| `tests/unit/test_pipeline.py` | EDIT | update 13→4 stage expectations; add parametrised legacy-13 test |
| `web/`, `deploy/`, backend Lambda source | UNTOUCHED | rule 4 |

---

## 2. Target layout
```
src/kb_agent/dormant/
  README.md  __init__.py
  tickets/ (loaders, adapters/, clean, embed, cluster, categorise, naming)
  join/ (coverage, drafting, report_sections, __init__)
  review/ (state, __init__)
  planner/ (planner, tools, __init__)
  llm_json.py  prompts.py
  documents_extras.py          # recall_at_5_from_golden
config/dormant/prompts/        # the 5 .v1.md templates
dormant/scripts/               # check_tickets + ticket generators
dormant/sample_pack/tickets/   # tickets, labels.csv, ticket_profile.json
dormant/evals/                 # purity/silhouette/coverage-fp/planted-gap
tests/dormant/                 # moved dormant tests
```
Note: there are TWO dormant roots — `src/kb_agent/dormant/` (importable package code, so it stays
testable) and a top-level `dormant/` (non-package assets: scripts, sample data, evals). This matches the
spec's A3 layout.

---

## 3. `pipeline.py` changes (recommended approach — needs your decision)

The spec (A3) wants the nine dormant `_stage_*` methods **extracted verbatim** into
`dormant/pipeline_stages.py`, leaving a commented re-registration block.

**Because dispatch is `getattr(self, f"_stage_{stage}")` driven by `STAGE_ORDER` (§0.1), I propose a
smaller, lower-risk change and want your ruling:**

- **Option A (spec-literal):** physically cut the nine `_stage_*` methods out of `Pipeline` into
  `dormant/pipeline_stages.py` as standalone functions, and leave a commented re-registration block.
  *Cost:* these methods call `self._clients`, `self._config`, `self._rs`, `self._mark_done`, etc., so
  "verbatim" extraction is impossible without converting them to take those as parameters — that is a
  **logic change**, which violates rule 2 ("never combine a move with a logic change") and rule A3
  ("verbatim"). This option is internally contradictory for these particular methods.
- **Option B (recommended):** leave the nine `_stage_*` methods physically on `Pipeline` but **remove
  them from `STAGE_ORDER`** so they are never dispatched, and **comment out the top-level dormant
  imports** (replacing with `# DORMANT: from kb_agent.dormant...`). The methods become dead-but-present
  code. To satisfy the spec's intent ("don't leave hundreds of lines of commented code" and "kept,
  importable"), I would **move the method *bodies*' dormant dependencies** by having each dormant
  `_stage_*` method's body replaced with a one-line guard that raises `NotImplementedError("dormant
  stage; see kb_agent.dormant")` — **but that is a logic change too.**

**The honest resolution:** the only way to both (a) keep the active module import-clean of dormant deps
and (b) not rewrite logic, is:
- Comment out the dormant **imports** in `pipeline.py` (→ `# DORMANT:`), AND
- Move the nine `_stage_*` methods into a **mixin** `dormant/pipeline_stages.py: DormantStagesMixin`
  **by `git mv`-ing the method source verbatim** into that mixin class, and have `Pipeline` **not**
  inherit it in the active path. The methods reference `self.*`, which resolves fine on the mixin when
  (in a dormant test) a `Pipeline`-with-mixin is constructed. This is a *move* (verbatim method bodies),
  not a logic change, and keeps them importable/testable.

This mixin approach is my recommendation. It is still more than "shrink STAGE_ORDER," so **I will not do
it without your explicit approval of Option B-mixin.** If you prefer the absolute-minimum change
(just shrink `STAGE_ORDER` + comment imports, leave methods on `Pipeline` unused), say so — it's less
clean but lower risk, and I'll note the dead code in `dormant/README.md`.

Other `pipeline.py` edits (common to any option):
- `STAGE_ORDER` → one constant `KB_ONLY_STAGE_ORDER = ["normalize_docs","kb_create","kb_ingest","write_report"]`; keep the original as `LEGACY_STAGE_ORDER_13` (for the legacy placeholder flag, A4.2).
- The `<500` branch + `_silhouette_excluding_noise` move out with the clustering code (frees numpy/sklearn).
- Comment out the `ReviewRegistry` call in `_finalise`; `AgentOutput.pending_review = []`.
- Lazy imports: `models.CoverageStatus` (618) moves with the dormant stage; `PageCapExceededError` (492) is a Part B concern — **leave untouched in Part A**.

---

## 4. `requirements-dormant.txt` split (evidence per package)

| Package | Move to dormant? | Evidence |
|---|---|---|
| `umap-learn` | YES | only `tickets/cluster.py` |
| `hdbscan` | YES | only `tickets/cluster.py` |
| `numba` | YES | transitive dep of umap/hdbscan; no direct active use |
| `llvmlite` | YES | transitive dep of numba |
| `scikit-learn` | YES | only `pipeline.py:_silhouette_excluding_noise` (moves) |
| `numpy` | DECLARE dormant; absence decided empirically | **Corrected at A1 review:** `pip show` proves PyMuPDF `Requires: (none)` and pdfplumber → pdfminer.six/Pillow/pypdfium2 (no numpy). The ONLY active-candidate puller was scikit-learn (`Requires: numpy`), which is itself moving to dormant. So numpy may genuinely leave the active image. Do NOT assume it stays; the active-only image build is the arbiter. Declaration moves to dormant; the image test checks active import+run, not numpy's presence either way. |
| `openpyxl` | YES | only `tickets/loaders.py` |
| everything else (pydantic, boto3, botocore, pyyaml, structlog, python-magic, PyMuPDF, pdfplumber, python-docx, langdetect, opentelemetry*, pydantic-settings) | KEEP | active or fakes use them |

**Verification at execution (not now):** after the split, build the active-only ARM64 image
(`.dockerignore` excludes `src/kb_agent/dormant/`, install only `requirements.txt`) and run
`python -c "import kb_agent.entrypoint"` + a fake run. If anything active imports a moved package
transitively in a way that breaks, that package returns to `requirements.txt` with a note.

---

## 5. `obs/runstate.py` changes
- Replace the hard-coded `TICKET_DEPENDENT_STAGES`-based `compute_skip_set` so that in `kb_only` mode it
  returns `{}` (no skips), and add the **legacy placeholder** behavior (A4.2): when
  `legacy_stage_placeholders` is true, `stage_order` = the 13-identifier constant and `skipped` = the
  nine dormant IDs each with reason `"Not part of this workflow"`.
- Keep the exact skip-reason strings for dormant modules only in the legacy path.
- `AGENT_VERSION` stays (bump is a Part B/deploy concern).

---

## 6. Contract-visible changes (A4) — exact list
1. Stage order constant → 4 IDs; `compute_skip_set` → `{}`; `manifest.stages` → 4 entries; `resume_from`
   naming a dormant stage → HTTP 400 `invalid_input` listing valid stages; old dormant `_markers/*.done`
   ignored (they simply aren't in `STAGE_ORDER`).
2. `workflow.legacy_stage_placeholders` (default false) → 13 identifiers + 9 skipped + `stage_skipped`
   events + manifest skipped entries.
3. Config `workflow: {mode: kb_only, legacy_stage_placeholders: false}`, validated by `AppConfig`;
   unknown mode fails at load.
4. `report.json`: keep every key; `intent_view/coverage/draft_articles=[]`, `drafting_summary={}`, add
   `"workflow": "kb_only"`; `report.md` drops those sections. (KB-only `build_report` path.)
5. `ticket_uri` stays on `AgentInput`, ignored, one `info` event at run start with the exact spec string;
   CLI `--ticket-uri` kept and ignored.

---

## 7. Risk list
| Risk | Likelihood | Mitigation |
|---|---|---|
| "Verbatim extraction" of `_stage_*` is impossible without a logic change (they use `self.*`) | Certain | §3: mixin-by-git-mv, or minimal shrink-STAGE_ORDER — **your decision** |
| A moved test asserts active behavior (e.g. `test_ground_truth_leak`) | Medium | classify each test at execution by reading its asserts; keep active-behavior tests active |
| `numpy` presence in the active image is uncertain (PyMuPDF/pdfplumber don't pull it; sklearn did but moves) | Medium | don't assert either way; the active-only image build decides. If active code breaks without numpy, return it to `requirements.txt` with a note |
| `ReviewStatus`/`CoverageStatus` enums annotated as dormant but referenced by active imports | Medium | verify each enum's active references before annotating; annotate only truly dormant-owned ones |
| `join/__init__.py` import side-effects when `report.py` stays but `coverage/drafting` move | Low | keep `join/__init__.py` import-free; verify no `from join import *` |
| evals split cuts a shared helper in `metrics.py` | Medium | read `metrics.py` boundaries before splitting; keep shared helpers active |
| Golden diff shows an unexpected delta (e.g. status) | Low | Part A must not touch status/logic; A6 golden diff asserts only the allowed deltas |
| AST guard false-positive on `# DORMANT:` commented imports | Low | guard scans parsed imports, not comments — commented imports are invisible to AST (good) |

---

## 8. Execution batches (after approval; active suite run after each)
1. scripts, sample-pack tickets, evals split
2. prompts templates + dormant `prompts.py`/`llm_json.py` loader path
3. `tickets/`, `join/` (coverage, drafting, report split), `review/`, `planner/` with rewritten imports
4. `pipeline.py` (STAGE_ORDER + dormant imports + chosen §3 option), `runstate.py`, config annotate
5. tests (move dormant, update active 13→4, add legacy-13 parametrised test, AST guard, smoke import test)
6. `requirements-dormant.txt`, `.dockerignore`, Makefile (`test-dormant`/`test-all`), pytest `addopts`

Then: A6 verification, `docs/phase1-changes.md`, report, **stop at Checkpoint A**.

---

## 9. Decisions (Checkpoint A1)

Resolved at A1 review:
- **§1 tests — RESOLVED (approved):** classify ambiguous tests (`test_ground_truth_leak`, evals helpers)
  at execution and report every move in `phase1-changes.md`.
- **numpy — RESOLVED:** declared dormant; absence decided empirically by the active-only image build.
  `pip show` at A1 shows PyMuPDF/pdfplumber do NOT pull numpy and scikit-learn (the only active-candidate
  puller) is moving to dormant, so numpy MAY leave the active image. Not force-removed; not asserted
  either way. (§4 corrected.)
- **§0 corrections — RESOLVED:** user disagrees with none.
- **PyMuPDF — CONFIRMED active:** `fitz` is used in `documents/features.py` (`_extract_pdf_features`),
  core to the document track. Stays in `requirements.txt`, untouched.

Resolved at A1 review (continued):
- **§3 pipeline option — RESOLVED: Option B-mixin (approved by user).** The nine dormant `_stage_*`
  method bodies are `git mv`'d verbatim into `dormant/pipeline_stages.py: DormantStagesMixin`; active
  `Pipeline` does not inherit it. `STAGE_ORDER` → `KB_ONLY_STAGE_ORDER` (4 ids); original kept as
  `LEGACY_STAGE_ORDER_13`. Dormant top-level imports in `pipeline.py` become `# DORMANT:` comments.
  (Noted: this is a move of method bodies, not a logic rewrite — the bodies keep using `self.*`, which
  resolves on the mixin when a dormant test composes `Pipeline`+mixin.)
- **PyMuPDF (AGPL) — RESOLVED as a DEFERRED item.** User confirmed PyMuPDF's license is not permissible.
  Replacement decided: **consolidate PDF feature extraction onto `pdfplumber` (MIT; already a dep; already
  does tables) with `pypdfium2` (Apache/BSD) as its engine; drop `fitz` entirely — no new library.**
  BUT the rewrite of `_extract_pdf_features` is a LOGIC CHANGE, so it is **out of scope for Part A**
  (rule 2). `features.py` stays untouched in Part A. Full finding + the 4-step Part-B work item recorded
  in `OPEN_QUESTIONS.md` ("PDF library licensing"). Flag for Claude: fold this into the Part B design.

No decisions remain open. Ready to execute Part A batches on Option B-mixin once you say go.
