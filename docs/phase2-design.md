# Phase 2 (Part B) — Design (Checkpoint B1)

**Branch:** `orch-to-kb` · **Status:** design for approval. **No Part B code written yet.**
This document is the file-level plan, the interface changes, the config, the risks, and
every *verify* item I could settle by reading current AWS documentation (with URLs) plus
the read-only backend status-mapping finding the spec required. Part A is complete and
verified (Checkpoint A2); the four-stage KB-only workflow and the legacy-placeholder flag
are in place.

Terminology: "customer" throughout (never "client"). Content is never logged.

---

## Decisions locked with the engagement lead (supersede any conflicting spec text)

These were confirmed in review and take precedence over the spec draft where they differ.

1. **Region and chunking strategy arrive in the payload.** `AgentInput` gains `region` and
   `chunking_strategy` (the UI dropdowns send `source_uri`, `chunking_strategy`,
   `vector_store`, and `region`). The agent provisions in the payload's region. If `region`
   is omitted, fall back to `us-east-1` (never the old `us-west-2`). The payload is
   authoritative; config is only the fallback.
2. **No human-review gate.** Status goes straight to `completed` (never `needs_review` by
   default). BDA markdown is reviewable in S3 under the project prefix; files on the
   KB-parser path have no stored extraction, and the contract says so plainly. The
   `legacy_status_needs_review` flag is retained for other 13-step consumers only.
3. **Structured formats (CSV, JSON/JSONL, XLSX) → CONVERT (Python extract → KB parser),**
   not BDA and not raw. Rationale: BDA does not accept CSV/JSON/XLSX at all; and a
   spreadsheet/CSV/JSON is already machine-readable, so Python extraction to clean Markdown
   is lossless and gives the KB parser predictable, well-structured text. BDA is reserved for
   *visual/scanned* documents (image-only or layout-heavy PDFs, standalone images,
   image-heavy DOCX) where the text is hidden in pixels. The two paths never compete for the
   same file. If Python extraction of a structured file fails or is degenerate (e.g. an
   "xlsx" that is really an embedded image, a corrupt file), that file is **rejected with a
   clear reason** — no silent fallback to a path that cannot work.
   - **XLSX/XLSM** via `openpyxl` (already a project dependency — moved to dormant in Part A;
     brought back to active here; it does **not** pull pandas). Each sheet → its own Markdown
     section; merged cells forward-filled; formulas rendered as cached values.
   - **CSV** via stdlib `csv`; **JSON/JSONL** via stdlib `json` (B2.6). Zero new dependencies.
   - **Legacy `.xls` (BIFF)** is **rejected with a clear reason** (openpyxl cannot read it;
     `xlrd` deliberately NOT added — keeps the dependency set lean).
4. **No agent-imposed caps on BDA pages or cost.** Remove `bda_page_cap_per_run`,
   `bda_budget_usd_per_run`, `options.max_pages` enforcement, and `PageCapExceededError`. The
   only ceiling is BDA's own native 3,000-page limit; a file over it is rejected with a clear
   reason. Keep a `pages_processed` metric for visibility (not a limit).
5. **Multi-document handling: optimal route.** Bounded parallelism (stream each object to a
   temp file, process, delete) with a safe sequential fallback. Determinism is preserved by
   collecting results in sorted key order regardless of completion order.

**Deliverable framing:** this is a self-contained module to be zipped and handed to a peer who
adapts it to his UI and payload schema. The integration contract, JSON schemas, and examples
are first-class deliverables; the input/output payloads are the stable boundary; the S3
run-state files (`_started`/`_events`/`_state`/`_finished`) are the progress markers the
orchestrator polls to populate the UI.

---

## 0. Corrections / disagreements with the instruction (report-first rule)

None of these block the work; they change *how* I implement, so they need your eyes.

1. **DOC/DOCX is now natively supported by BDA (2025-07).** The spec's B2.1 says BDA
   converts DOCX to PDF (breaking page mapping). That is still true for the PDF-conversion
   path, but BDA added native DOC/DOCX document processing. It does not change our routing
   (DOCX with images/complex tables still → BDA; plain DOCX → DEFAULT), so I will keep the
   spec's table as written and just note the capability. Evidence below.

2. **BDA document page limit is 3,000, not 1,500** (doubled 2025-04). The spec references a
   "page ceiling with the splitter"; I will read 3,000 into config (`source.bda_max_pages`)
   rather than hard-code, as the spec asks.

3. **Per-document ingestion-failure mapping is not available from the API.** B5.1 asks to
   "map failures back to files by key where the service reports them." The
   `StartIngestionJob`/`GetIngestionJob` response gives aggregate counts
   (`numberOfDocumentsScanned/Failed/...`) and a **list** of `failureReasons` strings — not
   a per-document-key structure. Per-file detail is only in CloudWatch Logs. So I will record
   the aggregate stats + the reasons list verbatim in the binding, and mark each staged file
   `ingested` unless the reasons list names it; files are otherwise left `ingested` with a
   run-level warning when `numberOfDocumentsFailed > 0`. This is a documented limitation, not
   a gap in our code. (OPEN_QUESTIONS Q8.)

4. **The status flip to `completed` is SAFE for the current frontend** (read-only finding,
   §2). The `legacy_status_needs_review` flag is therefore not strictly required for *this*
   UI, but I will keep it exactly as the spec mandates for any other 13-step consumer.

5. **`project_id` regex in the spec rejects 1–2-char and 40-char-exactly edge values.**
   `^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$` means min length 3, max length 40, no leading/trailing
   hyphen, no uppercase. I will implement it verbatim; noting only that the derivation
   fallback (from `run_id`) must guarantee a result that satisfies it (lowercasing, invalid→`-`,
   collapse repeats, trim hyphens, pad short, truncate long with an 8-char hash). Property
   tests will assert the fallback always produces a valid id.

---

## 1. *verify* findings (current AWS documentation, with URLs)

Content below is paraphrased from AWS docs for licensing compliance.

### 1.1 S3 Vectors
- **Immutability:** after an index is created you cannot change its name, dimension,
  distance metric, or non-filterable metadata keys; you must create a new index.
  → drives B4.2 `provisioning_conflict` (change nothing on mismatch).
  [create-index](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-create-index.html),
  [indexes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-indexes.html)
- **Vector bucket name:** 3–63 chars, lowercase letters/digits/hyphens, begin and end with a
  letter or number, unique per account **per Region** (not globally).
  [getting-started](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-getting-started.html)
- **Index name:** 3–63 chars, lowercase letters/digits/hyphens **and dots**, begin/end
  alphanumeric, unique within the bucket.
- **Dimension:** 1–4096 (we use 1024 = Titan Embed Text v2 default). **Non-filterable keys:**
  1–10 allowed, each 1–63 chars (we use the current nine). **Total metadata per vector:** 40 KB.
  [limitations](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html),
  [metadata-config (CFN)](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-s3vectors-index-metadataconfiguration.html)
- **boto3 client `s3vectors`:** `create_vector_bucket`, `get_vector_bucket`, `create_index`,
  `get_index`, `list_indexes`, `delete_index`, `put_vectors`, `tag_resource`.
  IAM actions mirror these (`s3vectors:CreateIndex`, `s3vectors:TagResource`, …).
  [boto3 s3vectors](https://docs.aws.amazon.com/boto3/latest/reference/services/s3vectors.html)

### 1.2 Bedrock Data Automation (documents)
- **Accepted for documents:** PDF, TIFF, JPEG, PNG, DOCX (and now DOC/DOCX natively). It does
  **not** accept CSV, JSON, or XLSX — so those never route to BDA.
  [doc/docx support](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-bedrock-data-automation/),
  [output-documents](https://docs.aws.amazon.com/bedrock/latest/userguide/bda-output-documents.html)
- **Page limit 3,000** (doubled from 1,500, 2025-04); auto-splitting supports up to 3,000
  pages total and 20 pages per split document.
  [larger documents](https://aws.amazon.com/about-aws/whats-new/2025/04/amazon-bedrock-data-automation-modality-controls-hyperlinks-larger-documents),
  [splitting](https://docs.aws.amazon.com/bedrock/latest/userguide/bda-document-splitting.html)
- **PDF constraints:** cannot be password-protected; max page size 40 inches / 9000 points;
  images ≤ 10000 px per side. BDA image files (JPEG/PNG) max 5 MB, up to 8k.
  [bda-limits](https://docs.aws.amazon.com/bedrock/latest/userguide/bda-limits.html)
- **DOCX page mapping** does not work when BDA converts DOCX→PDF internally (cosmetic for us;
  we don't rely on DOCX page numbers).
- **Concurrency quota** (`bda_concurrency` default 3) is account-specific and not stated as a
  fixed public number → OPEN_QUESTIONS Q7 (human/console check).

### 1.3 Bedrock Knowledge Base (S3 data source)
- **Per-file limit 50 MB.** Formats: PDF, MD, TXT, DOC, DOCX, HTML, CSV, XLS, XLSX. **JSON is
  not supported** → it must be converted (B2.6).
  [knowledge-base-ds](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-ds.html)
- **Files per ingestion job:** up to 1,000 (BDA parser) / 15,000 (Nova). Informs `source.max_files`.
  [multimodal troubleshooting](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-multimodal-troubleshooting.html)
- **CSV metadata customization:** the CSV is parsed one row at a time; there is **one**
  content field; the other columns become string metadata. Metadata sidecars are capped at
  10 KB and are filterable. → B2.7: CSV/XLSX go DEFAULT (KB parser), and whether row-level
  retrieval is usable is confirmed by the human-run integration script, not assumed.
  [kb-metadata](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-metadata.html)
- **KB name** max length 128; **data source name** max length 200 → `kb-{project_id}` and
  `ds-{project_id}` are safely within bounds (project_id ≤ 40). KB name pattern is permissive;
  our deterministic names use only `[a-z0-9-]`.
  [CreateKnowledgeBase](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_CreateKnowledgeBase.html)
- **Data deletion policy:** default `DELETE` (deleting the data source/KB removes its vectors;
  the vector store bucket itself is **not** deleted); `RETAIN` keeps them. A failed KB delete
  is resolved by switching to `RETAIN` then retrying — informs the cleanup script's ordering
  and its fallback note.
  [kb-delete](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-delete.html),
  [kb-ds-delete](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-ds-delete.html)
- **Ingestion failure reporting:** aggregate `IngestionJobStatistics`
  (`numberOfDocumentsScanned/Indexed/Failed/...`) + an `IngestionJob.failureReasons` **list**;
  no per-document-key map via the API (CloudWatch only). See §0.3.
  [IngestionJobStatistics](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_IngestionJobStatistics.html),
  [StartIngestionJob](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_StartIngestionJob.html)

### 1.4 S3 conditional writes (the project lock)
- `PutObject` with header `If-None-Match: *` succeeds only if the key does not already exist
  (GA Aug 2024); a conflict returns **412 PreconditionFailed**. boto3:
  `put_object(..., IfNoneMatch="*")`. This is the lock primitive for B4.4.
  [conditional-writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- Cross-bucket: the source bucket may differ from the agent's data bucket; `ObjectStore` must
  accept full `s3://bucket/key` URIs, not just data-bucket-relative keys (B7).

### 1.5 Still open (cannot settle by docs — go to OPEN_QUESTIONS / integration script)
- **Retrieval usability of the CONVERTed Markdown** for CSV/JSON/XLSX (decision 3) — Q5,
  integration script. (We no longer rely on native CSV/XLSX parsing; we hand the KB parser
  our extracted Markdown. The script confirms retrieval quality end-to-end.)
- BDA **account concurrency** quota and runtime **ephemeral disk** — Q7.
- Exact `vector_store` strings the consumer sends — Q2 (normalization covers variants).
- TIFF wanted? — Q4 (accepted by BDA, not routed per spec).

---

## 2. Backend status-mapping finding (read-only; the reason the legacy flag exists)

`web/api.js` → `adaptStatus()` (≈ line 234):

```js
if (finished) status = finished.status === "failed" ? "failed" : "complete";
```

The frontend maps **any** non-`failed` `_finished.json.status` to its own `"complete"`.
So today `needs_review` already renders as "complete"; changing the agent to emit
`completed` renders identically. **The flip is safe — a finished run will not look stuck.**

The only place the raw status surfaces is `getOutputs()` (≈ line 300):
`status: o.status || report.status || "complete"`, a display-only summary field; it gates
nothing. One mock preview string in `app.js` hardcodes `Status: needs_review`, which is a
demo sample, not live behavior.

**Conclusion:** emit `completed` by default. Keep `workflow.legacy_status_needs_review`
(default false) to re-emit `completed` as `needs_review` for any *other* 13-step consumer,
exactly as the spec requires. There is no backend "status Lambda" that re-maps statuses; the
translation lives entirely in `api.js`. (OPEN_QUESTIONS Q3 answered: yes, `completed` is
accepted.)

---

## 3. File-level plan (what changes, by file)

Legend: **NEW** file · **EDIT** · **EDIT(additive)** keeps every existing field/behavior.

### 3.1 Contract & HTTP (B1)
| File | Change |
|---|---|
| `src/kb_agent/contract.py` | EDIT(additive): add `contract_version`, `project_id`, `source_uri`, `vector_store` to `AgentInput`; keep `document_uris` (deprecated), `ticket_uri`/`options.max_pages` (accepted+ignored). Add `project_id`, `knowledge_base_id`, `knowledge_binding_uri`, `warnings` to `AgentOutput`; add the new `RunMetrics` fields. Status Literal → `completed/running/failed` (+ legacy flag re-emit). |
| `src/kb_agent/project.py` | **NEW**: pure `validate_project_id`, `derive_project_id(run_id)`, `normalise_vector_store`, and the S3-key/name sanitisers (B3.3, B4.1). Property-tested. |
| `src/kb_agent/entrypoint.py` | EDIT: add the synchronous pre-flight inside `POST /invocations` (B1.2) — normalise/reject `vector_store` **before** anything; parse the URI; `ListObjectsV2 MaxKeys=1` existence check; required-config presence (KB role ARN, region, data bucket). Map failures to the B1.3 error codes with plain messages. Keep the async-start behavior. |

### 3.2 Formats & routing (B2)
| File | Change |
|---|---|
| `src/kb_agent/documents/features.py` | EDIT: add `format` + `size_bytes` to `DocumentFeatures`; add `detect_format(name, head_bytes)` (libmagic MIME first, extension tie-break for the text/CSV/JSON family); make PDF geometry **lazy + memoised per file** (compute per predicate) so an oversize file is never parsed. |
| `src/kb_agent/documents/router.py` | EDIT: add predicates `format_in`, `size_bytes_eq`, `size_mb_gt`, `size_mb_lte`; add the `CONVERT` route; keep first-match-wins + catch-all check; keep dotted-threshold resolution. |
| `config/routing_rules.yaml` | EDIT: rewrite to the revised ordered table (§3.2.1) — CSV/JSON/XLSX → CONVERT, catch-all `99 → REJECT`, narrow `10` to `pdf`. |
| `src/kb_agent/documents/convert.py` | **NEW**: deterministic structured→Markdown extraction. JSON/JSONL via stdlib `json` (dotted-key flatten to `json.max_depth`, record-boundary split at the KB size cap, byte-stable). CSV via stdlib `csv` → one Markdown table. XLSX/XLSM via `openpyxl` → one Markdown section per sheet (merged cells forward-filled, formulas as cached values). `.xls` → raise a clear "unsupported legacy format" so the router/stage reject it. |
| `src/kb_agent/models.py` | EDIT(additive): add `Route.CONVERT`; extend the per-file outcome model (B3.5). |
| `requirements.txt` | EDIT: add `openpyxl` back to active (dormant→active); verified it does NOT pull pandas. |

#### 3.2.1 Revised routing table (decision 3: structured formats → CONVERT)

First match wins; existing IDs keep meaning; new IDs 11–18 + 99. `size_mb` compared to
`source.kb_path_max_mb` (50) for the KB path and `source.max_file_mb` (500) for the hard ceiling.

| Order | ID | Condition | Route | Reason |
|---|---|---|---|---|
| 1 | 15 | `size_bytes == 0` | REJECT | Empty file |
| 2 | 16 | `size_mb > source.max_file_mb` | REJECT | Over the maximum supported size |
| 3 | 2 | format in `jpeg, png` | BDA | Image; BDA extracts its text |
| 4 | 11 | format in `json, jsonl` | CONVERT | KB parser cannot read JSON; extract to Markdown |
| 5 | 12 | format `csv` | CONVERT | Extract to a clean Markdown table for predictable retrieval |
| 6 | 13 | format in `xlsx, xlsm` | CONVERT | Extract each sheet to Markdown (BDA cannot take XLSX) |
| 7 | 19 | format `xls` | REJECT | Legacy .xls unsupported (no xlrd); re-save as .xlsx |
| 8 | 1 | format in `txt, md, html` and `size_mb ≤ kb_path_max_mb` | DEFAULT | Plain text or markup |
| 9 | 14 | format in `txt, md, html` and `size_mb > kb_path_max_mb` | REJECT | Over the knowledge base per-file limit |
| 10 | 3 | `docx` with embedded images or complex tables | BDA | Needs layout-aware parsing |
| 11 | 17 | `docx` with `size_mb > kb_path_max_mb` | BDA | Too large for the KB parser; BDA output is smaller |
| 12 | 4 | `docx` or `doc`, otherwise | DEFAULT | Plain Word document |
| 13 | 5 | `pdf` encrypted or unreadable | REJECT | Cannot be processed |
| 14 | 18 | `pdf` with `size_mb > kb_path_max_mb` | BDA | Too large for the KB parser |
| 15 | 6 | `pdf` any page chars `< routing.min_chars_per_page` | BDA | Scanned page |
| 16 | 7 | `pdf` any page image area `> routing.max_image_area_ratio` | BDA | OCR-grade parsing needed |
| 17 | 8 | `pdf` tables `≥ routing.max_tables` | BDA | Default parser loses table structure |
| 18 | 9 | `pdf` multi-column | BDA | Default parser mis-orders text |
| 19 | 10 | `pdf` (narrowed from `always`) | DEFAULT | Simple text PDF |
| 20 | 99 | `always` | REJECT | Unsupported format |

Changes from the spec's B2.5 table: CSV (12) and XLSX (13) move from DEFAULT to **CONVERT**
per decision 3; XLS gets its own REJECT rule (19); the oversize-text REJECT is split so each
family (text / docx / pdf) has its own size handling. The `always → REJECT` catch-all (99)
makes unknown formats fail visibly instead of being silently skipped by the KB.

### 3.3 Many-files normalisation (B3)
| File | Change |
|---|---|
| `src/kb_agent/documents/staging.py` | EDIT: extend the denylist with `__MACOSX/`, `Thumbs.db`, `.DS_Store`, dotfiles; keep README/LICENSE/.gitkeep. |
| `src/kb_agent/pipeline.py` | EDIT: rewrite `_stage_normalize_docs` to the B3.2 algorithm (discover→limits→route→dedupe→bounded parallel→collect sorted→skip-unchanged→retry). Remove the BDA page cap / cost caps / `max_pages` enforcement (B3.4); keep a `pages_processed` metric; keep the content-hash cache and the full-bucket BDA URI (regression-tested). |
| `src/kb_agent/documents/outcomes.py` | **NEW**: the per-file `FileOutcome` record + aggregation to run-level status (`no_documents_ingested` / partial `completed` + warnings). |
| `src/kb_agent/exceptions.py` | EDIT: remove `PageCapExceededError`; add nothing new (error codes are data, not exceptions). |

### 3.4 Provisioning (B4)
| File | Change |
|---|---|
| `src/kb_agent/aws/vector_admin.py` | **NEW**: `S3VectorsAdmin` (real) implementing `VectorStoreAdmin`. |
| `src/kb_agent/provisioning.py` | **NEW**: create-or-verify orchestration for vector bucket / index / KB / data source; the project record (`project.json`); tagging; `provisioning_conflict` on any immutable mismatch; `provisioning.enabled=false` fallback to `KB_AGENT_VECTOR_INDEX_ARN`. |
| `src/kb_agent/lock.py` | **NEW**: the S3 conditional-write project lock (acquire/renew/steal-stale/release), `project_busy` after `lock_wait_seconds`. |
| `src/kb_agent/aws/clients.py` | EDIT(additive): extend `S3ObjectStore` (head, server-side copy, conditional put, list/stream by full URI); extend the KB client (find/get/list_data_sources/find_or_create_data_source/tag/delete_*, ingestion status with failure reasons). |
| `src/kb_agent/aws/fakes.py` | EDIT(additive): `FakeVectorStoreAdmin` (enforces naming + immutability), extend `FakeObjectStore` + `FakeKnowledgeBase` to match. |
| `src/kb_agent/aws/interfaces.py` | EDIT(additive): new `VectorStoreAdmin` Protocol; extend `ObjectStore` + `KnowledgeBaseClient`. |
| `scripts/cleanup_project.py` | **NEW**: dry-run-by-default cleanup (DS→KB→index; tag-gated; never the bucket). |

### 3.5 Ingestion, outputs, binding (B5)
| File | Change |
|---|---|
| `src/kb_agent/pipeline.py` | EDIT: `_stage_kb_create` → create-or-verify + lock acquire; `_stage_kb_ingest` → single job, wait-if-running, poll, read stats; release lock in `finally`. |
| `src/kb_agent/obs/runstate.py` | EDIT(additive): `_started` gains `project_id/source_uri/vector_store`; `_state` gains the B5.2 block; new event types `resource_created/resource_verified/file_outcome`. |
| `src/kb_agent/binding.py` | **NEW**: build + write `knowledge_binding.json` (run + project copies), schema-validated. |
| `docs/schemas/*.json` | **NEW**: JSON Schemas (input, binding, `_started/_state/_finished`, per-file outcome). |

### 3.6 Config (B6)
| File | Change |
|---|---|
| `src/kb_agent/config.py` | EDIT: add `source/processing/provisioning/vector_store/ingestion/json/convert` models; **`Settings.aws_region` default `us-east-1`** (decision 1: payload region wins; config is fallback); keep `KB_AGENT_VECTOR_INDEX_ARN` (used only when provisioning disabled); drop the cost/page-cap enforcement entirely (decision 4) — remove `CostConfig` usage and `PageCapExceededError`. |
| `config/default.yaml` | EDIT: add the B6 blocks. |

### 3.7 Docs & scripts (B9, B8)
`docs/integration-contract.md`, `docs/formats.md`, `docs/provisioning.md`,
`docs/agent-permissions.md`, `docs/CHANGELOG-contract.md`, `docs/examples/`,
`scripts/integration_mixed_formats.py` (dry-run default). All **NEW**.

---

## 4. Interface changes (exact, additive; fakes kept in step)

**`ObjectStore`** (add): `list_uri(uri)`, `stream_to_file(uri, path)`, `head(key)->dict|None`,
`copy(src_uri, dst_key)`, `put_if_absent(key, data)->bool` (If-None-Match). Existing methods
unchanged. The URI-taking methods accept `s3://bucket/key` across buckets.

**`VectorStoreAdmin`** (new): `ensure_vector_bucket(name)`, `get_index(bucket,name)->Index|None`,
`create_index(bucket,name,dimension,metric,data_type,non_filterable_keys)`, `tag(arn,tags)`,
`delete_index(bucket,name)`. `FakeVectorStoreAdmin` enforces the naming rules and rejects any
post-create change (returns the stored config so the verifier sees mismatches).

**`KnowledgeBaseClient`** (add): `find_knowledge_base(name)`, `get_knowledge_base(id)`,
`list_data_sources(kb_id)`, `find_or_create_data_source(...)`, `tag(arn,tags)`,
`delete_knowledge_base(id)`, `delete_data_source(...)`, `get_ingestion_job(...)->stats+reasons`.
LLM client untouched.

---

## 5. Config additions (B6) — exact YAML

```yaml
workflow:    {mode: kb_only, legacy_stage_placeholders: false, legacy_status_needs_review: false}
source:      {max_files: 100, max_file_mb: 500, kb_path_max_mb: 50, bda_max_pages: 3000}  # TODO(confirm) Q1
processing:  {max_workers: 4, bda_concurrency: 3, temp_dir: /tmp}                          # TODO(confirm) Q7
provisioning: {enabled: true, vector_bucket_name: kb-agent-vectors, name_prefix: kb-,
               ds_prefix: ds-, kb_source_root: projects, lock_ttl_seconds: 300,
               lock_wait_seconds: 900, data_deletion_policy: DELETE}
vector_store: {type: s3_vectors, dimension: 1024, distance_metric: cosine, data_type: float32,
               non_filterable_keys: [AMAZON_BEDROCK_TEXT, AMAZON_BEDROCK_METADATA, source,
                                     source_type, route, content_hash, run_id, ingested_at, language]}
ingestion:   {poll_seconds: 10, timeout_seconds: 3600}
json:        {max_depth: 5, max_input_mb: 50}
```

`Settings.aws_region`: the current code default is `us-west-2` but the deployment runs in
`us-east-1`. I will **require** it (no silent default) or default to `us-east-1` and fail
loudly if provisioning is enabled and the region is unset — a wrong region would provision in
the wrong place. (Your call on "require" vs "default `us-east-1`"; I recommend require.)

---

## 6. Implementation order (maps to the spec's checkpoints)

- **B2 (Checkpoint B2):** router predicates + CONVERT, `detect_format`, JSON conversion,
  multi-file `normalize_docs`, remove page cap. All verifiable offline in fakes.
- **B3 (Checkpoint B3):** `VectorStoreAdmin`, create-or-verify provisioning, lock, fakes.
- **B4 (Checkpoint B4):** ingestion, outputs, binding, contract + status.
- **B5 (Checkpoint B5):** docs package, integration script, cleanup script, permissions doc.

(The spec interleaves B-section numbers and checkpoint numbers; I've mapped them so each
checkpoint is a self-contained, test-green increment, matching Part A's batch discipline.)

---

## 7. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| CSV/XLSX retrieval through the KB parser is unusable | Medium | B2.7: integration script decides; if unusable, STOP and report — do not silently convert |
| Per-document ingestion failures not available via API | Certain | §0.3: record aggregate stats + reasons list; run-level warning; documented limit (Q8) |
| `s3vectors` boto3 shape differs from assumption | Low | verified client methods exist (§1.1); thin `VectorStoreAdmin` isolates it; fakes mirror |
| Removing `PageCapExceededError` breaks a test/import | Low | grep + update tests; keep `CostConfig` field inert (additive, rule 5) rather than deleting the key |
| Bounded parallelism exhausts ephemeral disk on huge sets | Medium | stream to temp file, delete per task, cap concurrency; Q7 confirms disk |
| Cross-bucket source read lacks permission | Medium | `permission_denied` error code surfaced at pre-flight; documented in agent-permissions.md |
| Lock renewal races / stale takeover | Medium | conditional write + `expires_at`; steal only when expired; release in `finally`; tests for contend/steal |
| Wrong region default provisions in the wrong place | High if unaddressed | §5: require region / fail loudly |
| Index created with wrong dimension vs embedding model | Medium | dimension in config = Titan v2 1024; verify at create; mismatch → `provisioning_conflict`, nothing mutated |

---

## 8. Open questions (to log in OPEN_QUESTIONS.md)

| # | Question | Owner | Current stance |
|---|---|---|---|
| 1 | Default `source.max_files` / total-size cap | Manager | 100 files; KB job supports up to 1,000 (BDA) |
| 2 | Exact `vector_store` strings the consumer sends | Integrator | normalisation covers variants; only `s3_vectors` accepted |
| 3 | Does the backend accept `completed`? | Kiro | **Answered: yes** — `api.js` maps non-failed→complete (§2) |
| 4 | Is TIFF wanted? | Manager | BDA accepts it; not routed per spec; leave out |
| 5 | CSV/XLSX retrieval behavior | Human (integration script) | DEFAULT route; usability unconfirmed |
| 6 | KB name pattern; tagging per resource; delete order & vector behavior | Kiro (docs) | KB≤128, DS≤200; delete DS→KB→index; default DELETE (§1.3) |
| 7 | BDA concurrency quota; runtime ephemeral disk | Kiro/Human | defaults 3 / stream+delete; confirm |
| 8 | Per-document ingestion failure reporting | Kiro (docs) | **Answered: aggregate + reasons list only** (§0.3) |
| 9 | Re-uploads: should removing a source file remove it from the KB? | Manager | additive by default; sync is incremental |
| 10 | Is `iam:PassRole` on a single shared role acceptable to security? | Human | one KB role, Bedrock-conditioned; no role creation |

---

## 9. Definition of done for Checkpoint B1

This document exists, answers every *verify* item that documentation can settle (§1), records
the backend status-mapping finding (§2), lays out the file-level plan (§3), the exact interface
and config changes (§4–§5), the implementation order (§6), risks (§7), and open questions (§8).
**No Part B code has been written.** Awaiting your approval to proceed to Checkpoint B2.
