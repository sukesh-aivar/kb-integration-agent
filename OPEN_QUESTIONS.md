# Open Questions

Every `TODO(confirm)` from the build spec and every placeholder the code leaves,
with an owner who can answer it and what it blocks. **Nothing here has been
silently invented** (spec rule 1): where a value was needed to make the code run,
an obviously-placeholder default was used and recorded below.

Status legend: OPEN = unanswered · PLACEHOLDER = code runs on a flagged default.

## Questions from the spec (section 9)

| # | Question | Owner | Blocks | Status | Placeholder in code |
|---|----------|-------|--------|--------|---------------------|
| 1 | How are inputs delivered - console upload to S3, pasted JSON, S3 URI typed in chat? (We assume S3 references.) | Manager / console owner | Integration only; prototype safe | OPEN | `AgentInput` takes S3 URIs (`contract.py`) |
| 2 | Ticket export format and field names for the sample | Manager / Delivery Lead | `ticket_profile`, adapter | OPEN | `Ticket` schema + `sample` platform assumed (`models.py`) |
| 3 | Is ">=5 source tickets" per article the intended reading? | Manager | `draft_article` | OPEN | `drafting.min_source_tickets: 5` (`default.yaml`) |
| 4 | What does the "70/30 time-split" of sample tickets mean? | Manager | Sample pack | OPEN | 70/30 older/newer split implemented in generator; interpretation noted |
| 5 | Non-English tickets: flag, reject, or ignore? (SOW English-only) | Product Owner | `validate_uploads` | PLACEHOLDER | `validation.non_english_action: flag` (`default.yaml`) |
| 6 | Confirmed Region, and BDA available there with quota? | IT / Environment Owner | Tuesday | PLACEHOLDER | `aws_region = us-west-2` (`config.py`, `.env.example`) |
| 7 | Vector store: S3 Vectors confirmed, or OpenSearch Serverless by Day 10? | Product Owner | `kb_create` config | PLACEHOLDER | `knowledge_base.vector_store: s3_vectors`, behind a config flag |
| 8 | KB location - pilot account or sandbox? (SOW sections disagree) | Solution Architect | `kb_create` | OPEN | not yet wired (no AWS code this pass) |
| 9 | AgentCore Runtime max invocation duration; polling or callback? | Solution Architect | `entrypoint`, resumability | OPEN | contract supports both `run()`/`step()` via `resume_from` |
| 10 | Is DynamoDB approved for run/review state, or S3-only? | Solution Architect / InfoSec | `review/state.py` | OPEN | not yet wired (no state store this pass) |
| 11 | Exact embedding + Claude model IDs to pin | Solution Architect | config | PLACEHOLDER | `embedding_model_id`, `llm_model_id` defaults in `config.py`/`.env.example` |
| 12 | How much autonomy - is the thin planner wanted, or deterministic workflow enough? | Manager | section 6 scope | OPEN | **planner intentionally NOT built this pass; awaiting answer** |
| 13 | Is PyMuPDF's AGPL licence acceptable in a commercial deliverable? | Engagement Lead / legal | dependency choice | RESOLVED (Part B) | AGPL rejected; `fitz` removed from the runtime. PDF reading consolidated onto `pdfplumber` (MIT); `pymupdf` moved to `requirements-dev.txt` (dev fixture generator only). See the "PDF library licensing" section below. |

## Part B (Phase 2) open questions — logged at Checkpoint B1 (see docs/phase2-design.md §8)

| # | Question | Owner | Status | Notes |
|---|---|---|---|---|
| B1 | Default `source.max_files` and any total-size cap | Manager | OPEN | default 100; KB ingestion job supports up to 1,000 files (BDA parser) |
| B2 | Exact `vector_store` strings the consumer's dropdown sends | Integrator | OPEN | normalisation (lowercase, non-alnum→`_`) covers variants; only `s3_vectors` accepted, else HTTP 400 `unsupported_vector_store` |
| B3 | Does the backend status mapping accept `completed`? | Kiro (read-only) | RESOLVED | YES. `web/api.js` `adaptStatus` maps any non-`failed` finished status to the UI's `complete`; `needs_review` and `completed` render identically. The flip is safe; the legacy flag stays for other 13-step consumers. |
| B4 | Is TIFF wanted? (BDA accepts it; not currently routed) | Manager | OPEN | leave out per spec; add a routing rule later if wanted |
| B5 | CSV and XLSX retrieval behaviour through the KB parser | Human (integration script) | OPEN | CSV parsed one row at a time, one content field, other columns → string metadata; usability confirmed by `scripts/integration_mixed_formats.py`, not assumed |
| B6 | KB name pattern/length; tagging support per resource; delete order & vector behaviour | Kiro (docs) | RESOLVED | KB name ≤128, data source name ≤200; cleanup deletes DS→KB→index; default data deletion policy `DELETE` removes vectors (bucket itself retained); on a failed KB delete switch the DS to `RETAIN` then retry |
| B7 | BDA account concurrency quota; runtime ephemeral disk size | Kiro / Human | OPEN | defaults: `bda_concurrency=3`, stream-to-temp + delete per task; confirm the account quota and AgentCore ephemeral disk |
| B8 | Per-document ingestion-failure reporting for S3 data sources | Kiro (docs) | RESOLVED (limitation) | API gives aggregate `IngestionJobStatistics` + a `failureReasons` LIST only; no per-document-key map (CloudWatch only). Record stats + reasons; run-level warning when `numberOfDocumentsFailed > 0` |
| B9 | Re-uploads: should removing a file from the source remove it from the KB? | Manager | OPEN | additive by default; KB sync is incremental (adds/modifies/deletes since last sync) |
| B10 | Is `iam:PassRole` on a single shared KB role acceptable to security? | Human | OPEN | one KB service role, Bedrock-conditioned; the agent creates NO IAM roles; shared role = weaker per-project isolation (documented trade-off) |

## Additional placeholders introduced during scaffolding

These are not in section 9 but were needed to make the scaffold run. Flagged here
per spec rule 1 and 2.3.

| Item | Where | Why it is a placeholder | Owner |
|------|-------|-------------------------|-------|
| Python version 3.11 | `requirements.txt`, `pyproject.toml` | 3.11 verified to resolve umap-learn wheels + build hdbscan; 3.13/3.14 wheels lag. 3.12 expected OK but unverified. | Build/Eng |
| Metadata sidecar naming/format | `models.py DocumentMetadata` | RESOLVED 2026-10-15: `<filename>.metadata.json` with `metadataAttributes`. See verification log item 2. | Solution Architect |
| Guardrail ID + version | `.env.example`, `config.py` | No guardrail provisioned yet for the pilot. | Solution Architect / InfoSec |
| Embedding dimensionality assumption (~1024) | spec 4.3 reasoning | Depends on confirmed embedding model (Q11). | Solution Architect |
| BDA invocation / project setup specifics | not yet coded | To be verified against current AWS docs before the BDA stage is built (spec 4.2). URLs to be recorded then. | Build/Eng |

## AWS detail verification log

Verified against current official AWS docs on **2026-10-15** (Part A of the full
build). Findings drive the AWS client interfaces. Where a doc did not pin an exact
value (BDA Region list, S3 Vectors preview/GA status), that is stated explicitly
and deferred to in-region confirmation rather than invented (spec rule 1).

### 1. Bedrock Data Automation (BDA) for documents

- **Three-step API workflow:** create a project, invoke analysis, retrieve
  results. Invoking with a project ARN processes the file using the project's
  configuration.
  Source: <https://docs.aws.amazon.com/bedrock/latest/userguide/bda-using-api.html>,
  <https://docs.aws.amazon.com/bedrock/latest/userguide/bda-projects.html>
- **Async invocation:** `InvokeDataAutomationAsync` processes asynchronously and
  writes output to the S3 bucket you specify; **check completion with
  `GetDataAutomationStatus`** (poll). Async JSON output is **S3-only**.
  Source: <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_data-automation-runtime_InvokeDataAutomationAsync.html>
- **Output format (matches spec's "standard output, not blueprints"):** standard
  document output supports page/element/word granularity and text formats
  Plaintext, **Text with markdown (the default)**, HTML, and CSV for tables. The
  **JSON+files** setting (async only) emits a **markdown file** plus a text file,
  per-table CSVs, and figure crops under
  `standard_output/logical_doc_id/assets/`. This is what we ingest.
  Source: <https://docs.aws.amazon.com/bedrock/latest/userguide/bda-output-documents.html>
- **File requirements relevant to routing:** images max **5 MB**, formats **JPEG,
  PNG**. DOCX is converted to PDF internally (so page-number mapping is lost for
  DOCX). DOCX is **not** supported by the *sync* API - another reason we use async.
  Source: <https://docs.aws.amazon.com/bedrock/latest/userguide/bda-limits.html>
- **Document (PDF) limits VERIFIED 2026-10-15 (approval condition 9):**
  - Overall document page limit **3,000 pages**; **500 MB per API request**.
    Sources: <https://aws.amazon.com/about-aws/whats-new/2025/04/amazon-bedrock-data-automation-modality-controls-hyperlinks-larger-documents>,
    <https://aws.amazon.com/blogs/machine-learning/from-pdfs-to-insights-architecting-an-intelligent-document-processing-pipeline-with-aws-generative-ai-services/>
  - With automatic document splitting enabled: up to **3,000 pages** per file and
    **20 pages per individual (split) document**.
    Source: <https://docs.aws.amazon.com/bedrock/latest/userguide/bda-document-splitting.html>
  - PDFs **cannot be password-protected**; max page dimensions **40 inches /
    9000 points**; JPEG 2000 images allowed.
    Source: <https://docs.aws.amazon.com/bedrock/latest/userguide/bda-limits.html>
  - BDA can be selected as a Knowledge Base **advanced parser** (per-page cost).
    Source: <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-advanced-parsing.html>
  - **Implication for our fixtures:** our scanned-PDF sample fixtures are 1-2
    pages and a few MB - comfortably within 3,000 pages / 500 MB. The per-run page
    cap in `config/default.yaml` (`cost.bda_page_cap_per_run: 2000`) is below the
    3,000 hard limit, which is the intended cost guard, not a limit violation.
    BDA GA Regions confirmed to include **us-west-2** and **us-east-1** (our
    placeholder region us-west-2 is covered); exact full Region list still Q6.
- **Supported Regions:** BDA went GA with cross-Region inference endpoints
  (Feb 2025 announcement). The docs did **not** give me a single authoritative
  enumerated Region list on the pages fetched. **NOT INVENTED** - Q6 remains: the
  confirmed Region and BDA availability/quota there must be verified in-region at
  provisioning (owner: IT / Environment Owner).
  Source: <https://aws.amazon.com/blogs/aws/get-insights-from-multimodal-content-with-amazon-bedrock-data-automation-now-generally-available/>

### 2. Bedrock Knowledge Bases with S3 Vectors

- **S3 as data source + S3 Vectors as vector store:** Bedrock fetches from the S3
  data source, chunks, embeds, and stores vectors in an S3 vector index. KB
  created via `CreateKnowledgeBase` (`storageConfiguration` for S3 vectors), data
  source via `CreateDataSource`, chunking via `vectorIngestionConfiguration`.
  Source: <https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html>,
  <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_StorageConfiguration.html>,
  <https://docs.aws.amazon.com/bedrock/latest/userguide/s3-data-source-connector.html>
- **Hierarchical chunking is a supported strategy** (fixed-size, hierarchical,
  semantic, none), configured in `vectorIngestionConfiguration` on the data source.
  Source: <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-chunking.html>,
  <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-data-source-customize-ingestion.html>
- **Metadata sidecar format CONFIRMED** (resolves the prior placeholder): for any
  document type in an S3 data source, a sidecar file named
  **`<filename>.metadata.json`** carries `{"metadataAttributes": { ... }}` with
  STRING / NUMBER / BOOLEAN / STRING_LIST values and an `includeForEmbedding`
  option. (CSV record-based metadata is a separate mechanism we do not need.)
  Source: <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-metadata.html>
- **Ingestion / completion:** ingestion is an async job started with
  `StartIngestionJob`; status is polled (there is no native push "completion
  event" in the KB API itself - a run would poll job status, or wrap with an
  EventBridge rule if an event is wanted).
  Source: <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-create.html>,
  <https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-ingest.html>
- **S3 Vectors status:** launched as **preview on 2025-07-15**; the current S3
  user-guide page no longer carries a "preview" banner, but GA status and
  **Region availability are NOT confirmed** here - deferred to Q7 / in-region
  check. **NOT INVENTED.**
  Source: <https://aws.amazon.com/blogs/machine-learning/building-cost-effective-rag-applications-with-amazon-bedrock-knowledge-bases-and-amazon-s3-vectors/>

### 3. Direct ingestion API for approved articles (Week 2 seam)

- **`IngestKnowledgeBaseDocuments`** (the KnowledgeBaseDocuments API) indexes
  documents **directly** into the vector store of a KB connected to a **CUSTOM**
  data source - no S3 round-trip. Up to **25 documents per API call** (10 via
  console). This is the right mechanism for Week 2 `ingest_approved`: approved
  articles are pushed directly rather than re-synced from S3.
  Source: <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-direct-ingestion.html>,
  <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_IngestKnowledgeBaseDocuments.html>,
  <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-direct-ingestion-add.html>
- **Implication:** direct ingestion requires a CUSTOM data source. Our main doc
  pipeline uses an S3 data source. For Week 2 we will likely want a **second,
  CUSTOM data source** on the same KB for approved articles. Left as a seam this
  pass; noted for the KB client interface.

### 4. AgentCore Runtime container requirements

- **ARM64 is REQUIRED** for all deployed agents.
  Source: <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/getting-started-custom.html>
- **Invocation contract CONFIRMED:** the container must expose a **`POST
  /invocations`** endpoint and a **`GET /ping`** health endpoint, packaged as a
  Docker container.
  Source: <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/getting-started-custom.html>
- **Invocation duration (resolves part of Q9):** microVM sessions run **up to 8
  hours (28800 s)**; Instance (capacity-provider) sessions up to **14 days
  (1209600 s)**. Exceeding the max for the compute type fails with a
  `ValidationException`.
  Source: <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html>,
  <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html>
- **Container build VERIFIED (2026-10-15):** the ARM64 image builds successfully
  (`docker build --platform linux/arm64`), with all native wheels resolving on
  arm64 - hdbscan builds from source, umap-learn/numba/llvmlite/numpy/pymupdf/
  scipy install cleanly - and `libmagic1` baked into the runtime stage. The
  running container passed a contract test: `GET /ping` returns healthy and
  `POST /invocations` runs the full pipeline and returns the AgentOutput shape
  (via `scripts/stub_orchestrator.py`). Image size ~1.35 GB. This closes the
  "most likely containerisation failure" risk flagged in the spec (7.6).

### Conflicts with the spec

**None that block building.** All findings are consistent with the spec's
assumptions. Three clarifications that refine (not contradict) the spec:

1. KB ingestion completion is **polled** (`GetIngestionJob`/`StartIngestionJob`
   status), not a native push event. The spec's "wait on completion events, do
   not sleep-poll blindly" is satisfied by polling job status with backoff (and
   optionally an EventBridge wrapper later) rather than a fixed sleep. The KB
   client interface exposes a `wait_for_ingestion` method so the mechanism is
   swappable. This refines Q9 for the KB stage.
2. The **metadata sidecar placeholder is now resolved** to
   `<filename>.metadata.json` - the `DocumentMetadata` model will serialise to
   that shape.
3. Direct ingestion (item 3) needs a **CUSTOM data source**, distinct from the S3
   data source used for the document pipeline. Recorded as a Week 2 seam.

Because there are no blocking conflicts, I will proceed to build per the plan.
Items genuinely unconfirmed (BDA Region list, S3 Vectors GA/Region) stay OPEN
under Q6/Q7 and are NOT invented.

---

## POC frontend (UI only) — build spec section 13

Logged while building `web/` on branch `kbagent-poc`. The UI is mock-first; none
of these block a mock-mode review, but they do block the live wiring.

| # | Question | Owner | Blocks | Status | Notes / placeholder in code |
|---|----------|-------|--------|--------|------------------------------|
| P1 | Official brand palette and font, or keep the values sampled from screenshots? | Manager | `web/styles.css` section 2 | OPEN | Sampled tokens in `:root`; one swap point if an official palette arrives |
| P2 | Is the supplied logo PNG suitable for the top bar? | Human | `web/assets/logo.png` | RESOLVED | Now using the wide transparent wordmark `Ingram_micro_logo.png` (791×157, RGBA, transparent background). Displayed height-constrained (26px, width auto). Renders cleanly on light and dark bars — verified both. (Earlier square 840×859 white-background icon rendered tiny and showed a white box in dark mode; replaced.) |
| P3 | Does a normal run skip `kb_retrieve`, and does the status response report the skip set? | Kiro, from backend | step list skip rules (section 4) | ANSWERED (from code) — see note | In `pipeline.py`, `_stage_kb_retrieve` is **not skipped**; it runs but no-ops unless a golden question set is supplied (recall@5 eval). So in a normal run it completes quickly rather than being "skipped". The status API has no golden concept. **UI decision:** treat `kb_retrieve` as a fast `done` step by default; only show it `skipped` if the backend's status response explicitly says so. |
| P4 | Confirmed per-run limits: file count, per-file size, total size? | Manager + cost owner | upload validation (section 3.3) | PLACEHOLDER | Using spec defaults in `config.example.js` LIMITS: 10 files / 50 MB each / 150 MB total; ticket 1 file / 50 MB |
| P5 | Should the "View an example run" path be available before sign-in, or only after? | Manager | sign-in screen (section 3.2) | PLACEHOLDER | Built as available **before** sign-in (fastest reliable demo path); trivially gated later if required |
| P6 | Real `STALE_AFTER` value — 60s or longer? | Kiro, from observed run times | "still working" callout (section 3.4) | ANSWERED (from observed runs) — see note | 60s is **too low**: observed `normalize_docs` (BDA) ran ~48–77s and `kb_ingest` ~12–60s on real runs, so 60s would false-alarm on a normal run. Set `STALE_AFTER: 120` in `config.example.js`. Revisit if BDA on larger scans runs longer. |
| P7 | Will reviewers have AWS console access (should "Open in AWS console" links appear beside S3 objects)? | Manager | outputs screen (section 3.5) | OPEN | Not built; S3 URIs shown with a copy button only. Add console deep-links if confirmed yes. |

### Agent change requested from the POC frontend review (item A4)

The UI shows `kb_retrieve` as **skipped** ("No question set supplied") in a normal
run, because the step no-ops without a golden question set and showing it "Done"
would claim a check that did not happen. For the live build this requires the
**backend status response to report `kb_retrieve` with `state: "skipped"` and a
`skip_reason`** in a normal run (the UI already prefers the backend's skip set and
only falls back to its own rules). Logged here as an agent-side change to make;
the mock models the intended behaviour now.

---

## Console-deploy spec v2 — answered from code / to verify

### Answered from code (Phase 1)

- **Q8 (chunk count "1,736"):** NOT computed in the pipeline. It was a hard-coded demo
  string in the old mock and a log line. Bedrock KB `get_ingestion_job` returns
  **document-level** statistics (documents scanned / indexed / failed / modified), **not a
  chunk count**. Decision: persist the document-level ingestion stats in `_state.json` and
  leave `chunks_written: null` (the UI hides the chip). We do NOT estimate.
- **Q10 (auth token):** `web/auth.js getToken()` returns the **access token**
  (`web/auth.js` ~L84–L106). The backend needs the **ID token** (that is where `email` /
  `cognito:groups` live). FLAGGED for the Phase-4 frontend change: `getToken()` must return
  the id_token for API calls (or the API Gateway JWT authorizer must accept the access token
  and the backend must not rely on email). Will reconcile in Phase 4.
- **Q10 (mock event `t`):** `web/mock.js` emits event `t` as **whole seconds** since run
  start (virtual clock). The backend `status` endpoint will use the same (whole seconds since
  `started_at`). No frontend change needed for `t`.
- **entrypoint server:** currently `wsgiref.simple_server.make_server` — **single-threaded**
  (`src/kb_agent/entrypoint.py` ~L117–L121). Phase 1 replaces it with a threaded server.

### Verification log (to confirm against live AWS docs — spec rule 8)

| # | Item | Status |
|---|------|--------|
| V1 | AgentCore `/ping` health strings + casing (believed `Healthy` / `HealthyBusy`; agent returns `{"status":"healthy"}` today) and how idle timeout is measured with a background thread | OPEN — needs docs |
| V2 | IAM resource ARN format for `bedrock-agentcore:InvokeAgentRuntime` (runtime ARN, possibly with endpoint sub-resource) | OPEN — needs docs |
| V3 | S3 Vectors: is the reserved data-source-id metadata key filterable in a query? | OPEN — needs read-only test |
| V4 | KB `dataDeletionPolicy` param name + whether a KB can be deleted while data sources exist + delete order + account KB quota | OPEN — needs docs / Service Quotas |
| V5 | Presigned POST URL host form in this account (virtual-hosted vs path style) — drives CSP connect-src | OPEN — determined at deploy |
| V6 | Cognito default email sending limit (cited ~50/day) | OPEN — needs docs |
| V7 | S3 console object URL format for us-east-1 (outputs console_url) | OPEN — needs docs |
| V8 | Bedrock `get_ingestion_job` `statistics` key names (assumed `numberOfDocumentsScanned` / `numberOfNewDocumentsIndexed` / `numberOfModifiedDocumentsIndexed` / `numberOfDocumentsDeleted` / `numberOfDocumentsFailed`) — `_map_ingestion_stats` tolerates missing keys (defaults 0) | OPEN — needs docs / live ingestion |

### Phase 1 — Checkpoint 1 build record (console-deploy v2)
- **Scope delivered:** agent is now asynchronous and self-reporting to S3 under
  `runs/{run_id}/` (`_started` / `_events` / `_heartbeat` / `_state` / `vectors_sample` /
  `_finished`). No AWS resource was created, changed or deleted by the agent — the image is
  built locally only and the human performs the ECR push + runtime update.
- **Tests:** full offline suite green — **158 passed** (`.venv/bin/python -m pytest -q`),
  including the 5 async-contract tests (`test_entrypoint_contract.py`) and the 10 new §5.10
  run-state tests (`test_runstate.py`). `ruff check` clean on all changed files;
  `mypy src/kb_agent` clean (48 files).
- **Image:** `kb-integration-agent:v3` built ARM64 single-arch
  (`docker buildx build --platform linux/arm64 --provenance=false --sbom=false
  -t kb-integration-agent:v3 --load .`), loaded locally (NOT pushed). Verified:
  `arch=arm64 os=linux` (~318 MB); in-container `kb_agent.entrypoint` import OK; a running
  container answered `GET /ping` → `200 {"status":"Healthy","run_id":null}`.
- **Next (human):** tag + push `:v3` to ECR, then update the AgentCore runtime to the new
  image. `scripts/update_runtime.py` previews the change (dry-run default) and prints the
  console steps; `--apply` performs it (human only).

### Phase 1 — live verification on the deployed `:v3` runtime (2026-10-22)
Runtime `kb_integration_agent-LQ8ettAIsg` updated v2 → v3 (role + 9 env vars + PUBLIC/HTTP
carried over unchanged), status returned to **READY**. Async smoke run
`smoke-v3-1792648824` (input `s3://kb-integration-agent-test/sample-doc-runtime.pdf`, no
tickets, no question set):
- `POST /invocations` → `202 {status: accepted}` immediately; S3 run-state streamed as
  expected; `_finished.json` → `status: needs_review`, `error: null`, 20.87s.
- **V1 (ping strings) — effectively RESOLVED:** the runtime stayed READY and served the
  invocation to completion with the new `Healthy`/`HealthyBusy` strings, so AgentCore's
  health check accepts them. (AgentCore proxies `/ping` internally; not probed directly.)
- **V8 (ingestion stat keys) — RESOLVED:** `_state.json` reported
  `ingestion: {scanned:1, indexed:1, modified:0, failed:0, chunks_written:null}` from a real
  `get_ingestion_job`, confirming the assumed `numberOf*` key names. `chunks_written` null by
  design.
- **Known Phase-1 placeholder (expected):** `vectors_sample.json` =
  `{"chunks":[],"error":"unavailable","method":"unimplemented"}` — the real S3 Vectors
  chunk-sample query is deferred to a later phase; it never fails the run (spec §5.6).
- Skip set correct for a docs-only run (8 ticket/join stages "No ticket export uploaded";
  `kb_retrieve` "Runs only when a question set is supplied"). Red pill routed the PDF to BDA
  (rule 8, 50 pages, cache hit), KB created + ingested 1 document; report + manifest written.


### Bug fix — BDA input URI dropped the bucket name (:v4, 2026-10-22)
Symptom: a real uploaded doc (`simple-finance-document.pdf`) failed at `normalize_docs`
with `ValidationException ... InvokeDataAutomationAsync ... Unable to read file from
given S3 location`. Root cause: `pipeline._stage_normalize_docs` reconstructed the BDA
input uri as `f"s3://{key}"` from the BARE key (bucket already stripped), yielding
`s3://uploads/<run>/docs/<file>` — so BDA tried to read a bucket named `uploads`.
Earlier smoke runs used `sample-doc-runtime.pdf`, which was a BDA **cache hit**, so the
malformed uri was never exercised; the first uncached BDA call exposed it.
Fix: carry the caller's ORIGINAL full `s3://bucket/key` uri through to the BDA call
(new `_store_uri()` helper builds it from the store's bucket for bare-key/CLI inputs;
never `f"s3://{key}"`). Added regression test
`tests/unit/test_runstate.py::test_bda_input_uri_keeps_the_bucket_name`. Rebuilt
`:v4` (ARM64), pushed to ECR, updated runtime; re-ran the exact failing input →
`needs_review`, BDA parsed 1 page. 158+1 tests pass, ruff+mypy clean.


## PDF library licensing — PyMuPDF (AGPL) replacement (decided at Part A / Checkpoint A1 review)

**Finding (verify item, rule 10):** `documents/features.py` currently extracts PDF geometry with
**PyMuPDF (`fitz`)**, which is **AGPL-3.0** (also sold under a commercial PyMuPDF license by Artifex).
AGPL is not a permissible license for this product, so `fitz` must be removed.

**Decision — consolidate onto `pdfplumber`:**
- `pdfplumber` is **MIT** (its engine `pdfminer.six` is MIT/X), **already a project dependency**, and
  already performs the table counting in `_count_pdf_tables`. Its rendering dep `pypdfium2` is
  **Apache-2.0 / BSD-3-Clause** (Google PDFium) — fully permissive.
- Verified from the installed environment that pdfplumber exposes everything `features.py` needs:
  `len(pdf.pages)` (page count), `page.extract_text()` (per-page chars), `page.images` with bounding
  boxes (image-area ratio), `page.extract_words()` / `page.chars` bounding boxes (multi-column
  heuristic, replacing fitz `get_text("blocks")`), and a `password=` parameter + raised exceptions on
  encrypted/corrupt PDFs (the `pdf_encrypted_or_unreadable` signal).
- Net: **drop `fitz`/PyMuPDF entirely; no new library required.** pypdfium2 arrives transitively via
  pdfplumber.

Sources (rephrased for licensing compliance):
- pdfplumber (MIT, low-level char/rect/image + table API): https://pypi.org/project/pdfplumber/0.11.8/
- pypdfium2 (Apache-2.0 / BSD-3-Clause, PyMuPDF replacement): https://www.pypi.org/project/pypdfium2 ,
  https://github.com/pypdfium2-team

**Scope / status: RESOLVED in Part B (2026-10-15).** Q13 is closed. What shipped:
1. `_extract_pdf_features` and `_looks_multi_column` were rewritten onto pdfplumber in a single parse
   (page count, per-page chars, image-area ratio from `page.images` bboxes, `find_tables()`, and a
   `page.extract_words()` x-position multi-column heuristic). The `import fitz` path and the
   `pymupdf_unavailable` branch are gone; `features.py` no longer imports `fitz`.
2. `pymupdf` was removed from `requirements.txt`. pdfplumber (+ its transitive pdfminer.six / Pillow /
   pypdfium2) remain and are all permissively licensed. The active ARM64 image was rebuilt and verified
   to NOT install pymupdf.
3. Tests: the PDF feature/routing fixtures (`tests/unit/test_features_and_router.py`,
   `tests/unit/test_runstate.py`) no longer use `fitz`. A tiny pure-Python PDF writer
   (`tests/pdf_fixtures.py`) builds the text/scanned fixtures (no AGPL, no new dependency), and the same
   routing assertions (DEFAULT for a text PDF, BDA rule 6 for a text-free/scanned PDF) pass unchanged.
4. The only remaining PyMuPDF user is the DEV-ONLY sample-pack generator
   (`sample_pack/fetch_documents.py`), which rasterises scanned/degraded PDFs — genuine image synthesis
   the read-only runtime stack cannot do. `pymupdf` now lives in `requirements-dev.txt` for that purpose;
   it is never installed in the runtime image, and the agent never imports `fitz`.
