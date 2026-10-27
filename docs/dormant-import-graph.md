# Dormant import graph (evidence for the Part A move plan)

Generated at the Part A baseline (branch `orch-to-kb`, 159 active tests passing, ruff + mypy clean).
This records the ACTUAL import edges in `src/kb_agent/`, so the KEEP/MOVE/SPLIT decisions and the
`requirements-dormant.txt` split rest on evidence, not guesswork.

## Method

AST scan of every `src/kb_agent/**/*.py`: collected `import`/`from` targets, split into
internal (`kb_agent.*`) and the split-relevant third-party packages. Cross-checked with grep.

## Third-party imports, by module (split-relevant packages only)

| Module | Packages imported |
|---|---|
| `aws/clients.py` | botocore |
| `aws/clientset.py` | boto3 |
| `aws/interfaces.py` | pydantic |
| `config.py` | pydantic, yaml |
| `contract.py` | pydantic |
| `documents/features.py` | docx, fitz (PyMuPDF), magic, pdfplumber, pydantic |
| `documents/pii.py` | pydantic |
| `documents/validate.py` | langdetect |
| `logging_setup.py` | structlog |
| `models.py` | pydantic |
| `obs/audit.py` | pydantic |
| `obs/tracing.py` | opentelemetry |
| `pipeline.py` | **numpy, sklearn** |
| `planner/tools.py` | pydantic |
| `review/state.py` | pydantic |
| `tickets/cluster.py` | **hdbscan, numpy, umap** |
| `tickets/loaders.py` | **openpyxl** |
| `tickets/naming.py` | **numpy** |

## Heavy ML / ticket-only stack — where it is used

| Package | Imported by | Active or dormant? |
|---|---|---|
| `umap` | `tickets/cluster.py` | dormant (cluster) |
| `hdbscan` | `tickets/cluster.py` | dormant (cluster) |
| `openpyxl` | `tickets/loaders.py` | dormant (ticket XLSX loader) |
| `numpy` | `pipeline.py` (×2), `tickets/cluster.py`, `tickets/naming.py` | **dormant AFTER the two pipeline uses move** (see below) |
| `sklearn` | `pipeline.py` (`_silhouette_excluding_noise`) | **dormant AFTER that helper moves** (see below) |
| `numba`, `llvmlite` | (none directly) | transitive deps of umap/hdbscan → follow them |
| `pandas` | (none) | not imported anywhere in `src/` |

### The two `pipeline.py` uses of numpy/sklearn are both ticket-clustering code

- `pipeline.py:401` `import numpy as np; state.reduced = np.zeros(...)` — inside the `<500` clustering
  fallback branch of `_stage_intent_cluster` (a dormant stage).
- `pipeline.py:761` `import numpy as np; from sklearn.metrics import silhouette_score` — inside
  `_silhouette_excluding_noise`, a module-level helper only ever called from `_stage_intent_cluster`.

Therefore both numpy and sklearn become **dormant-only** once `_stage_intent_cluster` and
`_silhouette_excluding_noise` move to `dormant/`. They must NOT be removed from `requirements.txt`
until that move is done and verified.

## Internal imports of `llm_json` and `prompts` (the spec's conditional)

Both are imported **only** by dormant modules:

| Importer | Imports |
|---|---|
| `join/drafting.py` | `llm_json.extract_json`, `prompts.{prompt_version,render_prompt}` |
| `join/coverage.py` | `llm_json.extract_json`, `prompts.{prompt_version,render_prompt}` |
| `planner/planner.py` | `prompts.{prompt_version,render_prompt}` |
| `tickets/categorise.py` | `llm_json.extract_json`, `prompts.{prompt_version,render_prompt}` |
| `tickets/naming.py` | `llm_json.extract_json`, `prompts.{prompt_version,render_prompt}` |

No ACTIVE module imports `llm_json` or `prompts`. → both MOVE to `dormant/` (spec conditional satisfied).

## `pipeline.py` top-level `kb_agent` imports (lines 27–51)

Active (KEEP): `aws.clientset`, `config`, `contract`, `documents.bda`, `documents.features`,
`documents.knowledge_base` (KnowledgeBaseStage — but NOT `recall_at_5_from_golden`),
`documents.normalize`, `documents.router`, `documents.staging`, `join.report` (build_report/render_markdown
— the KB-only builder survives), `logging_setup`, `models`, `obs.audit`, `obs.runstate`, `obs.timing`,
`obs.tracing`.

To be commented out as `# DORMANT:` (moved): `join.coverage.coverage_check`,
`join.drafting.draft_articles`, `review.state.ReviewRegistry`, `tickets.adapters.sample.SampleAdapter`,
`tickets.clean.{clean_tickets,embedding_texts}`, `tickets.cluster.{cluster_embeddings,should_use_density_clustering}`,
`tickets.embed.EmbeddingCache`, `tickets.loaders.load_tickets_uri`, `tickets.naming.name_clusters`,
and `documents.knowledge_base.recall_at_5_from_golden`.

Lazy imports inside functions also to handle: `pipeline.py:492 PageCapExceededError` (removed in Part B,
untouched in Part A), `pipeline.py:618 models.CoverageStatus` (dormant stage), `pipeline.py:117` exceptions block.

## Consequences for `requirements-dormant.txt` (evidence-backed)

Move to `requirements-dormant.txt` (with this evidence): **umap-learn, hdbscan, numba, llvmlite, numpy,
scikit-learn, openpyxl**. Keep everything the active code or the fakes still use (pydantic, boto3/botocore,
pyyaml, structlog, python-magic, PyMuPDF, pdfplumber, python-docx, langdetect, opentelemetry, pydantic-settings).

> CAVEAT to verify during execution: `numpy` is a near-universal transitive dependency (PyMuPDF,
> scikit-learn, pandas, many others may pull it). It can only LEAVE the active image if nothing in the
> active import closure pulls it transitively. The move plan treats numpy as "move the direct dependency
> declaration, but expect it to remain installed transitively" and verifies via a clean active-only image build.
