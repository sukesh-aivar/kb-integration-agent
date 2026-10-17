# Kiro Build Specification — Knowledge Base Integration Agent (Week 1 Prototype)

**Project:** Conversational AI Demonstration Pilot — Ingram Micro
**Supplier:** Aivar Innovations Pvt. Ltd.
**Component:** Knowledge Base Integration Agent (sub-agent #1 of 4)
**Target:** Locally runnable prototype, container-ready, destined for Amazon Bedrock AgentCore Runtime
**Spec status:** v1.0, for Kiro. Placeholders are marked `TODO(confirm)` and must NOT be silently invented.

---

## 0. How to read this document

This is a build brief for an AI coding agent. It is deliberately verbose about *why*, because
most of the design decisions here were argued out and rejected alternatives matter — if you
re-derive a decision from first principles you will likely pick the wrong one.

**Non-negotiable rules for Kiro:**

1. **Do not invent values marked `TODO(confirm)`.** Leave the placeholder, raise it in a
   `OPEN_QUESTIONS.md`, and make the code work with a configured default that is obviously a
   placeholder (e.g. `REGION = "us-west-2"  # TODO(confirm) InfoSec-approved region`).
2. **Do not add an LLM call where this spec says "deterministic".** The single most important
   property of this agent is reproducibility. Every deviation must be justified in a comment
   and flagged in `OPEN_QUESTIONS.md`.
3. **Do not delete, modify or overwrite any file outside the project directory.** Describe
   intended changes before making them where they are destructive.
4. **Use the term "customer"**, never "client", in all code comments, docstrings, variable
   names and documentation. (Organisation-wide standard.)
5. **Write code that runs with no AWS credentials** for everything except the explicitly
   AWS-backed stages. A developer must be able to run the unit tests on a laptop offline.

---

## 1. Context — what is being built and why

### 1.1 The wider system

Ingram Micro delivers a **customer support chatbot** repeatedly, each time adapted to a
different industry and to a different customer's content. Today those deliveries are largely
manual. The pilot builds **four agents that speed up that delivery**, sitting behind a single
"Solution Builder" chat with an **orchestrator** that calls each in turn:

| # | Agent | Produces |
|---|---|---|
| 1 | **Knowledge Base Integration** *(this build)* | The knowledge base + intent analysis + draft articles |
| 2 | Vertical Adaptation | Industry context pack (ontology, persona, prompts, guardrails) |
| 3 | Observability and Evaluation | Test set, eval scores, go-live gate |
| 4 | Solution Packaging and Deploy | Terraform, deployment to a sandbox AWS account |

The **output of the whole system is a chatbot**. The four agents are the factory, not the
product. The chatbot itself is deployed into a separate sandbox account by agent #4.

**Critical scheduling fact:** agents #2 and #3 consume this agent's knowledge base. The
Wednesday checkpoint ("KB live for B and C") is a hard dependency for other engineers.

### 1.2 What this agent does

> **Goal (manager's words):** Upload docs + ticket export → Bedrock KB (S3 Vectors) built,
> intent view with volumes, 3 cited draft articles, coverage gaps — all reviewable.

Two input streams, two processing paths, then a join:

- **Documents** (FAQs, manuals, policies — PDF/DOCX/HTML/TXT) → parsed → ingested into a
  Bedrock Knowledge Base. This is *what the company has officially documented*.
- **Ticket export** (CSV/JSON/XLSX from a helpdesk) → intents discovered and counted → draft
  articles written from resolved tickets. This is *what customers actually ask, and how humans
  actually fixed it*.
- **Join:** check whether the KB can answer the frequent intents. Where it cannot, that is a
  **coverage gap**, and a gap backed by enough resolved tickets becomes a drafted article.

### 1.3 Hard constraints from the SOW

These are contractual. Do not design around them.

- **Demonstration pilot on sample, masked or synthetic data.** No production data, no
  production systems, no live helpdesk/CRM connections.
- **No autonomous actions.** *Every* output is a draft or recommendation that a **named
  reviewer** approves. Nothing enters the knowledge base without approval.
- **English only.** Non-English content is an edge case to be detected and flagged, not
  supported.
- **No model training or fine-tuning**, and no customer data used to train models.
- **Everything auditable:** inputs, prompts, model versions, tool calls, outputs and decisions
  are logged for every run, with export.
- **PII masked before upload**, and Bedrock Guardrails PII filters applied to inputs and
  outputs.
- **Native-first:** where AWS offers a managed service that does the job, configure it rather
  than building a custom component. Aivar builds only the glue, the agent logic, the review
  steps and the measurements.

### 1.4 Week 1 scope (manager's plan) — what Kiro must deliver

| Day | Deliverable |
|---|---|
| Mon | Sample pack v0.1: OSS product docs (Nextcloud/Rocket.Chat, 60–150 pages, some scanned-to-PDF) + 1,500 Claude-generated tickets across 20 intents with hidden labels, seeded edge cases (PII rows, non-English, duplicates, no-resolution), 70/30 time-split. `LICENSES.md`, `ticket_profile`. |
| Tue | `validate_uploads` (Guardrail PII + regex + language + encrypted-PDF), `bda_process` with content-hash cache, `normalize_docs` (dedupe, metadata sidecars), retrieval golden set. |
| Wed | `kb_create` (S3 Vectors, hierarchical chunking) + `kb_ingest` (async job, event completion) + `kb_retrieve` · recall@5 baseline. **Checkpoint: KB live for B and C.** |
| Thu | `embed_texts` · `intent_cluster` (UMAP+HDBSCAN ≥500 tickets, direct LLM categorisation <500) · cluster naming prompt · purity/silhouette report · chunking comparison → ADR. |
| Fri | `draft_article` (template, ≥5 source tickets, citations, PII re-scan) · `coverage_check` v1 · `write_report` · contract + golden tests · stitch. |

**Deferred to Week 2 — do not build, but leave seams for:** `score_automation`,
`ingest_approved` + `KB_CHANGED` re-evaluation trigger, conflict detection, Ingram-data swap
path.

### 1.5 Validation targets (the definition of done for Week 1)

| Metric | Target | What it proves |
|---|---|---|
| Intent purity vs seeded labels | ≥ 0.80 | Clustering rediscovers the planted intents |
| Retrieval recall@5 on a 30-question golden set | ≥ 0.80 | The KB returns the right passage |
| Seeded PII file rejected | 1 of 1 | Intake validation works |
| Undocumented intents flagged by coverage check | 3 of 3 | Coverage check works |
| Handling time per stage | Recorded | Feeds the SOW "savings gauge" |
| *(added)* Routing accuracy on labelled fixtures | Report it | The BDA router is correct and cheap |
| *(added)* Coverage-check false-positive rate | Report it | Prevents "flag everything" gaming the 3/3 target |
| *(added)* Silhouette, cluster count vs 20, noise share | Report it | Detects over-splitting / merging |

All of these run on **synthetic data**. They validate the *pipeline*, not real-world quality.
Say so explicitly in the generated report.

---

## 2. Design principles — the reasoning you must preserve

### 2.1 Deterministic code first; LLM only where judgement is required

An LLM is used in exactly five places. **Nowhere else.**

| # | LLM use | Why a model is genuinely needed |
|---|---|---|
| 1 | Cluster naming | Turning a cluster of tickets into a human-readable intent name requires language understanding. ~20 calls per run, not 1,500. |
| 2 | Direct ticket categorisation (<500 tickets only) | Below ~500 tickets, density clustering is unstable, so classification falls back to the model. |
| 3 | Article drafting | Synthesising several resolutions into one coherent article is generative work. |
| 4 | Coverage judgement | "Do these retrieved passages actually answer this question?" Retrieval score alone is a weak signal — passages are often on-topic but non-answering. |
| 5 | Planner (thin agentic layer, §6) | Interpreting free-text reviewer feedback and deciding which stage to re-run. |

**Explicitly NOT LLM work — these were considered and rejected:**

- **Parser routing (default vs BDA).** Rejected because routing must be reproducible and
  auditable, and every signal it needs is locally measurable. Use a rule table (§4.2).
- **Ticket field mapping (the adapter).** Rejected because a schema mapping is deterministic
  work; an LLM per record is costly, non-deterministic, and can place a value in the wrong
  field while still passing schema validation. An LLM may *propose* a mapping for an unknown
  format **once**, for a human to approve — but it never runs per record.
- **The ≥500 / <500 threshold.** A row count does not need a model.
- **The PII re-scan after drafting.** Guardrails + regex, in code.
- **Volume counting and ranking.** Arithmetic.

### 2.2 Everything is a draft until approved

Enforce in code, not in prompts:

- Generated articles are written to `drafts/{run_id}/` — a prefix the knowledge base's data
  source **cannot read**. There must be no path by which unapproved content is indexed.
- Every artefact carries `status: pending_review | approved | rejected | rerun_requested`.
- Every state transition records reviewer identity, timestamp, free-text feedback, and the
  model + prompt version that produced the artefact.

### 2.3 Reproducibility

- Fix random seeds for UMAP and HDBSCAN. Expose the seed in config.
- Content-hash every input file; the hash is part of cache keys and of chunk provenance.
- Pin model IDs and prompt versions in config and record them in every run manifest.
- The same inputs + same config must produce the same routing decisions and the same clusters.

### 2.4 Never leak ground truth into the pipeline

Seeded intent labels exist **only** to score clustering. They must never appear in any prompt,
embedding input, or feature. **Write an automated test that asserts this** (scan all prompt
payloads for the seeded label strings and fail if found). Without that discipline the purity
metric measures nothing.

### 2.5 Inputs arrive as references, not payloads

The console stores uploads in S3 and hands the orchestrator **pointers**. The agent reads S3
itself. Rationale: file contents must never travel through the chat/conversation path; the
agent's IAM role can then be scoped to the run's prefix; and this interface survives whatever
the console's upload UX turns out to be (`TODO(confirm)` — see §9).

---

## 3. Target architecture and the agent's place in it

```
Delivery engineer
      │ HTTPS
      ▼
CloudFront + Cognito  ──►  Console back end (EKS)
                                 │ invoke
                                 ▼
                     Orchestrator (AgentCore Runtime)
                                 │ invoke(run_id, s3 refs, options)
                                 ▼
        ┌──────── KB Integration Agent (THIS BUILD) ────────┐
        │  AgentCore Runtime container                      │
        │                                                   │
        │  reads/writes S3 ── calls Bedrock Data Automation  │
        │  creates/queries Bedrock Knowledge Base (S3 Vectors)│
        │  calls Bedrock models (embeddings + Claude)        │
        │  emits OTEL traces → CloudWatch                    │
        └───────────────────────────────────────────────────┘
```

**Implications for how you write the code:**

- **Stateless container.** Do not assume local disk persists between invocations. All durable
  state goes to S3 (and optionally a state table — `TODO(confirm)` whether DynamoDB is
  approved for the pilot) keyed by `run_id`.
- **Idempotent.** A retried invocation must not double-ingest. Content hash + `run_id` +
  per-stage completion markers make this possible.
- **Long-running work is asynchronous.** BDA jobs and KB ingestion jobs are async, and
  clustering 1,500 tickets takes real time. Design every stage to be resumable: write a stage
  completion marker, and on re-entry skip completed stages unless forced.
  `TODO(confirm)` — AgentCore Runtime's maximum invocation duration and whether the
  orchestrator polls for status or receives a callback. Until confirmed, implement **both**:
  a synchronous `run()` for local testing and a stage-wise `step()` that returns
  `{"status": "running", "stage": ..., "next": ...}`.
- **ARM64.** AgentCore Runtime container images are, to the best of current knowledge, ARM64.
  `TODO(confirm)` against current AWS docs. Build the image `--platform linux/arm64` and make
  sure native wheels (UMAP, HDBSCAN, numpy, pymupdf) resolve for that architecture — this is
  the single most likely containerisation failure.

---

## 4. Pipeline specification

### 4.1 Overview

```
RED PILL (documents)                      BLUE PILL (tickets)
──────────────────────                    ──────────────────────
validate_uploads                          load_tickets
      ▼                                         ▼
route_documents (rule table)              adapt_tickets (schema mapping)
      ▼                                         ▼
bda_process  ──or──  (keep original)      clean_tickets
      ▼                                         ▼
normalize_docs                            embed_texts
      ▼                                         ▼
kb_create → kb_ingest                     intent_cluster  (≥500: UMAP+HDBSCAN
      ▼                                                    <500: LLM categorise)
kb_retrieve (recall@5 baseline)                 ▼
      │                                   name_clusters (LLM)
      │                                         ▼
      │                                   intent view  ──► REVIEW GATE
      │                                         │
      └──────────────┬──────────────────────────┘
                     ▼
              coverage_check
                     ▼
              draft_article (LLM) → PII re-scan → drafts/{run_id}/
                     ▼
              write_report  ──► REVIEW GATE
                     ▼
         [Week 2] ingest_approved → KB_CHANGED → re-run coverage
```

### 4.2 Red pill — documents

#### `validate_uploads`

Two stages, because some checks need bytes and some need text.

**Stage A — pre-parse, on raw bytes (no parsing cost):**
- True MIME type via `python-magic` (never trust the extension — a renamed file must be caught).
- File size against a configured cap.
- Encrypted / password-protected PDF → reject.
- Corrupt / unreadable → reject.

**Stage B — post-extraction, on text:**
- PII detection: Bedrock Guardrails PII filter **plus** a regex layer (emails, phones, card-like
  numbers, national IDs). Both, because regex catches format-obvious cases cheaply and
  Guardrails catches contextual ones.
- Language detection → anything not English is flagged (see `TODO(confirm)` in §9: flag,
  reject or ignore).

**Ordering subtlety you must handle:** a **scanned** PDF has no text layer, so there is nothing
to PII-scan until BDA has run. Therefore the rule is *not* "validate then parse" but:

> **No document reaches the knowledge base until its extracted text has passed Stage B** —
> whether that text came from local extraction or from BDA output.

Cheap local extraction (`pdfplumber`/`PyMuPDF`, `python-docx`) covers most files, so BDA is
never invoked merely to validate.

Outputs: `ValidationResult(file_id, passed, failures[], detected_mime, language, pii_findings[])`.
Rejections are **flagged for review**, never silently dropped, and are counted in the report.

#### `route_documents` — the BDA router

**Why this exists:** BDA is billed per page and is overkill for a `.txt` or a clean `.html`.
The manager's guidance is explicit: BDA might be overkill, structured/text data would not need
it. But Bedrock Knowledge Bases fix the parsing strategy **per data source**, so KB-managed
parsing cannot choose per file. Hence: **the agent calls BDA directly for complex files**,
stores the resulting Markdown in S3, and the knowledge base ingests clean text from a single
data source.

**Deterministic rule table, first match wins.** Thresholds in config, never hard-coded.

| # | Condition | Route |
|---|---|---|
| 1 | MIME in {text/plain, text/markdown, text/html, text/csv} | `DEFAULT` |
| 2 | MIME in {image/jpeg, image/png} | `BDA` |
| 3 | DOCX with embedded images > 0 **or** nested/merged-cell tables > 0 | `BDA` |
| 4 | DOCX / XLSX otherwise | `DEFAULT` |
| 5 | PDF encrypted or unreadable | `REJECT` |
| 6 | PDF where any page's extracted chars < `MIN_CHARS_PER_PAGE` | `BDA` (scan or image page) |
| 7 | PDF where any page's image-area ratio > `MAX_IMAGE_AREA_RATIO` | `BDA` |
| 8 | PDF with detected tables ≥ `MAX_TABLES` | `BDA` |
| 9 | PDF with multi-column layout detected | `BDA` |
| 10 | Otherwise | `DEFAULT` |

**Safety net — deterministic escalation.** After a `DEFAULT` parse, compute quality metrics and
escalate to BDA if any fail: chars-per-page below a floor; share of garbled/non-printable
characters above a ceiling; table-like whitespace patterns in the output. This lets the rule
table err toward `DEFAULT` (cheap) with a backstop.

Every decision returns `RoutingDecision(file_id, route, rule_id, features, reason)` and is
written to the audit log. `rule_id` is what makes a misroute debuggable.

#### `bda_process`

- **Standard output, not blueprints.** Blueprints (a structured JSON schema of fields to
  extract) are for form-like documents — invoices, IDs, claims. Support content is narrative
  prose for retrieval, so standard output (page text, structure, tables, figure descriptions,
  returned as JSON/Markdown) is correct. `TODO(confirm)` — exact BDA project setup and
  invocation requirements against current AWS docs before Tuesday.
- **Content-hash cache.** Key = `sha256(file_bytes) + parser_version + parser_config_hash`.
  On hit, reuse the stored Markdown from S3 and skip the paid call. On a config change the key
  changes, so the cache invalidates correctly.
- Async invocation → poll or event completion. Record pages processed for the cost report.
- **Cost guards:** per-run page cap and a per-run BDA budget counter; stop and escalate when
  exceeded rather than silently spending.

#### `normalize_docs`

- Deduplicate by content hash.
- Write **metadata sidecars** alongside each document for the KB to pick up:
  `source`, `source_type` (`uploaded_doc` | `ticket_derived`), `route` (`default`|`bda`),
  `content_hash`, `run_id`, `ingested_at`, `language`.
- `source_type` matters downstream: groundedness evaluation and traces must be able to tell
  official content from ticket-derived drafted content.
- `TODO(confirm)` — exact sidecar file naming/format expected by Bedrock Knowledge Bases;
  verify in the docs, do not guess.

#### `kb_create` / `kb_ingest` / `kb_retrieve`

- Vector store: **S3 Vectors** (SOW default). OpenSearch Serverless is **conditional** on
  Ingram Micro requesting it by Day 10 — keep the vector store behind a config flag and an
  interface so switching is a configuration change, not a rewrite.
- Chunking: **hierarchical** (small child chunks matched, larger parent returned), which
  generally performs best on long manuals and policies. Thursday's deliverable includes a
  **chunking comparison → ADR**: measure fixed-size vs hierarchical vs semantic on recall@5 and
  record the decision and the numbers in `docs/adr/0001-chunking-strategy.md`.
- Embedding model: `TODO(confirm)` the exact model ID. Default to a current Titan Text
  Embeddings V2 configuration. **The query and the documents must use the same model** —
  changing it later means re-embedding everything, so pin it in config and record it in the
  run manifest.
- Ingestion is an **async job**; wait on completion events, do not sleep-poll blindly.
- `kb_retrieve` runs the 30-question golden set and reports **recall@5** (is the known-correct
  passage in the top 5?).

**Why the S3 Markdown is kept even though the KB indexes it:** S3 holds the *source of truth*
as files; the knowledge base holds a *derived index* (chunks + embeddings + metadata). If
chunking or the embedding model changes, you re-ingest from S3 — re-parsing through BDA would
cost money again. S3 also gives provenance for every chunk.

### 4.3 Blue pill — tickets

#### `load_tickets`

A **ticket** is one customer support case: the customer's problem, the conversation, and how a
human resolved it. It is structured conversational data, **not** a document — so it never goes
near BDA. Read CSV/JSON/XLSX with pandas/json. Format is `TODO(confirm)`; build for CSV and
JSON first, XLSX via the same loader.

#### `adapt_tickets` — the adapter

Different helpdesk platforms name fields differently (`short_description` vs `subject`). The
adapter is a **declarative per-platform mapping** to one uniform internal schema, behind a
common interface so a new platform is one new mapping, not a pipeline change.

Uniform schema (this is `ticket_profile`, Monday's deliverable — treat this as the draft and
refine it against the sample data):

```python
class Ticket(BaseModel):
    ticket_id: str
    subject: str
    description: str            # the customer's problem, as stated
    resolution: str | None      # what the human agent did; None = unresolved
    status: Literal["solved", "open", "pending", "closed", "unknown"]
    created_at: datetime
    resolved_at: datetime | None
    tags: list[str] = []
    language: str | None = None          # filled by validation
    source_platform: str                 # "sample", "zendesk", ...
    # Ground truth, test data ONLY. MUST NOT reach any prompt or embedding.
    _seeded_intent: str | None = None
```

- Validate every record against the schema. Emit a **mapping report**: mapped / skipped /
  failed counts with reasons. Never fail the whole run on one bad row.
- Keep `_seeded_intent` physically separate from the object passed to prompts if that is
  cleaner — a separate `labels.csv` keyed by `ticket_id` is the safest design.

#### `clean_tickets`

- Strip signatures, quoted reply chains, disclaimers, boilerplate.
- Deduplicate (seeded duplicates must be caught).
- Validate masking/PII and language on ticket text.
- Build the text to embed: **`subject + description` only** — the customer's problem.
  **Do not** embed the resolution or metadata: intent is what the customer asked for, and
  including resolutions pulls clusters toward "how it was fixed", which is a different
  partition of the data.

#### `embed_texts`

Bedrock embedding model, batched, with a local cache keyed by text hash + model ID (so
iterating on clustering parameters does not re-pay for embeddings).

#### `intent_cluster`

**≥ 500 tickets → UMAP + HDBSCAN.** Write the threshold in config; 500 is a heuristic, not a
law. Validate it by subsampling (200 / 500 / 1,500) and reporting where purity degrades.

- **Why reduce dimensions at all:** embeddings are ~1,024-dimensional. In very high dimensions
  the gap between nearest and farthest neighbour shrinks, so density contrast — the thing
  HDBSCAN relies on — fades. Embeddings also encode phrasing, length and style, which is noise
  for intent. UMAP preserves *local neighbourhood structure* while discarding much of the rest.
  Speed is a side benefit, not the justification (at 1,500 tickets it is negligible).
- `n_components`: **5–15** (default 10). This is the number of *axes*, and has nothing to do
  with the number of intents — 2 dimensions can hold 20 groups. Do **not** set it to 20.
  Never use 2–3 for clustering (that is for visualisation only; run a separate 2D UMAP for
  plots).
- `metric="cosine"` for text embeddings. Fix `random_state`.
- **HDBSCAN** because: it does not require the cluster count up front (you do not know how many
  intents exist); it handles very uneven cluster sizes (one intent may have 300 tickets,
  another 15); and it labels sparse points as **noise (-1)** rather than forcing them into a
  cluster — which is exactly where the seeded non-English/vague tickets should land.
  K-means was rejected: it needs *k*, assumes similar-sized round clusters, and assigns
  everything including noise.
- Key parameter `min_cluster_size` = "the smallest group I would call an intent". Tune it, and
  report the result.

**< 500 tickets → direct LLM categorisation.** Against a provided intent list or an
LLM-proposed one. **The provided list must never be the seeded labels** or the test is
circular.

#### `name_clusters`

- One call per cluster (~20 calls, not 1,500).
- Prompt input: a sample of each cluster — the most central tickets **plus some random
  members**, so a mixed cluster is visible rather than hidden behind its centroid.
- Structured output: `{intent_name, description, coherence_flag, example_ticket_ids}`.
  `coherence_flag` lets the model say "this cluster mixes several intents", which routes
  straight to the reviewer for a split.
- Every ticket in a cluster inherits that cluster's label. **Therefore cluster impurity
  propagates into volume counts and into drafted articles** — which is precisely why the
  review gate sits before drafting.
- Post-step: flag near-identical cluster names as merge candidates.

#### Intent view (reviewable output)

Table of: intent name, description, volume, share of resolved tickets, example ticket IDs,
coherence flag, plus a separate **noise bucket** of unclustered tickets. Status
`pending_review`. Reviewer may approve / rename / merge / split.

### 4.4 Join — coverage check and drafting

#### `coverage_check`

1. Select intents to test: top N by volume, or above a volume threshold.
2. Build probe questions per intent from the actual customer wording of that cluster's central
   tickets (not LLM-invented questions where real ones exist).
3. `kb_retrieve` top-k for each probe.
4. Judge `covered | partial | gap`: retrieval score threshold **plus** an LLM judge with
   structured output and a reason. Score alone is insufficient — retrieval routinely returns
   passages that are on-topic but do not answer.
5. Output per intent: status, volume, evidence passages, and a priority (volume × gap).
6. **Report the false-positive rate** (covered intents wrongly flagged). The "3 of 3 gaps
   flagged" target is trivially gamed by flagging everything; the FP rate is the guard.

#### `draft_article`

- Only for intents with **≥ 5 resolved source tickets** and reasonably consistent resolutions.
  `TODO(confirm)` with the manager that ≥5 is the intended reading of his plan.
  Rationale for a minimum: one ticket is a one-off fix, not documented practice; several give
  the model enough evidence to find the *consistent* resolution.
- Prompt input: the **content** of the resolved tickets (problem + resolution), plus the intent
  name and description to fix topic and scope. Volume is **not** a prompt input — frequency is
  used by *code* to decide which intents to draft, not by the model when writing.
- Fixed template: Title / Problem / Applies to / Steps / Resolution / Source tickets.
- **Citations are mandatory**: every article lists the `ticket_id`s it was built from. Validate
  in code that each cited ID exists and belongs to that intent. Reject the draft otherwise.
- **PII re-scan in code** (Guardrails + regex) on the generated text. A draft failing the scan
  is blocked and flagged, never stored as approvable.
- Write to `drafts/{run_id}/` with a sidecar record: status, source ticket IDs, model ID,
  prompt version, PII scan result, generated_at.

#### `write_report`

One package: KB status + recall baseline, intent view with volumes, cited draft articles,
coverage gaps, run metrics, handling time per stage, and the explicit note that all metrics are
measured on synthetic data. Markdown + JSON. Status `pending_review`.

---

## 5. Measurement and the audit trail

### 5.1 Handling time (SOW "savings gauge")

Record wall-clock duration **per stage** and for the whole run, into the run manifest. The SOW
compares this against a baseline Ingram Micro supplies. Make it a decorator so no stage can
forget.

### 5.2 Audit record (per run)

The SOW requires "inputs, prompts, model versions, tool calls, outputs and decisions for every
run, with export". Write a single JSON manifest per run containing:

- `run_id`, start/end, config snapshot, library versions
- every input file: name, content hash, size, detected MIME, validation result
- every routing decision: `rule_id`, features, route, reason
- every model call: model ID, prompt name + version, token counts, latency (**not** raw
  customer content where avoidable)
- every stage: status, duration, output location
- every artefact: status and any review decision

### 5.3 Observability

Emit OpenTelemetry spans per stage and per tool call, with `run_id` as a trace attribute, so
CloudWatch / AgentCore Observability can show them. Keep the exporter configurable and a no-op
locally.

---

## 6. The agentic layer (thin, bounded, last)

Build the deterministic pipeline first and make it pass its tests. Then add a **thin planner**.

**The planner chooses which registered tool/stage to call next. It does not do the work.**

| Decision | Who decides |
|---|---|
| Which parser (default vs BDA) | **Code** — rule table |
| Which adapter | **Code** — format signature detection |
| Cluster vs direct categorise | **Code** — the count threshold |
| Order of the happy path | **Code** — fixed sequence |
| What to do after poor quality metrics (noise share high, purity low) | **Planner** — retune and re-run, or escalate |
| What to do with free-text reviewer feedback | **Planner** — which stage to re-run, with what change |
| How to respond to a failure | **Planner**, within allowed options: retry / skip-with-flag / escalate |
| Which inputs were supplied (docs only, tickets only, both) | **Planner** — skip inapplicable stages |

**Guardrails, all enforced in code, not in the prompt:**

- Tool allowlist; the planner can call nothing else.
- **Preconditions enforced in the tool**, not assumed from the plan: `kb_ingest` refuses to run
  on a file whose validation did not pass, regardless of what the planner asked for.
- `MAX_PLANNER_STEPS`, `MAX_RETUNE_ATTEMPTS_PER_STAGE`, per-run cost budget, per-call timeouts.
- On limit breach: stop, escalate to the reviewer with the evidence gathered so far.
- Log every planner decision with its stated reason.

**Tool design standards** (these shape how well the planner works):

- One clear purpose per tool; name and description say *when to use it and when not to*.
- Typed, minimal parameters; `Literal`/enum for constrained values; examples of valid values in
  the docstring. The docstring **is** the prompt the model reads.
- Context like `run_id` is injected by the runtime from the session — **never** supplied by the
  model.
- Concise structured outputs (facts and metrics), not giant payloads.
- Errors returned as actionable `status: "error"` tool results so the planner can recover,
  rather than raising and killing the run.
- Validate every tool-call argument against the schema (Pydantic) **before** execution; on
  failure the function is never called and a clear error is returned for the model to correct.

---

## 7. Repository layout, environment and coding standards

### 7.1 Layout

```
kb-integration-agent/
├── README.md
├── OPEN_QUESTIONS.md              # every TODO(confirm), with who can answer it
├── pyproject.toml                 # or requirements.txt + requirements-dev.txt
├── .env.example                   # never commit a real .env
├── Makefile                       # setup / test / lint / eval / run / docker-build
├── Dockerfile                     # ARM64, for AgentCore Runtime
├── config/
│   ├── default.yaml               # thresholds, model IDs, limits, seeds
│   ├── routing_rules.yaml         # the rule table — data, not code
│   └── prompts/                   # versioned prompt templates (*.md or *.jinja)
├── src/kb_agent/
│   ├── __init__.py
│   ├── entrypoint.py              # AgentCore handler + local CLI
│   ├── contract.py                # AgentInput / AgentOutput / StageStatus models
│   ├── config.py                  # typed config loading (pydantic-settings)
│   ├── models.py                  # Ticket, Document, IntentCluster, Article, ...
│   ├── pipeline.py                # orchestration of stages; resumable
│   ├── planner/
│   │   ├── planner.py             # thin LLM planner
│   │   └── tools.py               # tool registry: specs + functions
│   ├── documents/
│   │   ├── validate.py            # validate_uploads
│   │   ├── features.py            # pure feature extraction (no I/O decisions)
│   │   ├── router.py              # rule table evaluation
│   │   ├── bda.py                 # bda_process + content-hash cache
│   │   ├── normalize.py           # normalize_docs
│   │   └── knowledge_base.py      # kb_create / kb_ingest / kb_retrieve
│   ├── tickets/
│   │   ├── loaders.py             # csv / json / xlsx readers
│   │   ├── adapters/
│   │   │   ├── base.py            # TicketAdapter interface
│   │   │   └── sample.py          # the one adapter built this week
│   │   ├── clean.py
│   │   ├── embed.py
│   │   ├── cluster.py             # UMAP + HDBSCAN
│   │   ├── categorise.py          # <500 LLM path
│   │   └── naming.py              # cluster naming
│   ├── join/
│   │   ├── coverage.py            # coverage_check
│   │   ├── drafting.py            # draft_article
│   │   └── report.py              # write_report
│   ├── review/
│   │   └── state.py               # artefact status + decision records
│   ├── aws/
│   │   ├── s3.py
│   │   ├── bedrock.py             # model + embedding calls, retries
│   │   └── guardrails.py
│   └── obs/
│       ├── tracing.py             # OTEL spans
│       ├── audit.py               # run manifest
│       └── timing.py              # @timed decorator
├── evals/
│   ├── golden_set.jsonl           # 30 retrieval questions + expected sources
│   ├── routing_fixtures.yaml      # file → expected route
│   ├── metrics.py                 # purity, silhouette, recall@k, FP rate
│   └── run_evals.py
├── sample_pack/                   # Monday's deliverable
│   ├── LICENSES.md                # provenance + licence of every OSS doc used
│   ├── documents/
│   ├── tickets/tickets.csv
│   ├── tickets/labels.csv         # hidden seeded labels — NEVER read by src/
│   └── generate_tickets.py        # the generator, so the pack is reproducible
├── scripts/
│   ├── run_local.py
│   └── bootstrap_aws.py           # create buckets / KB for local testing
└── tests/
    ├── unit/
    ├── component/                 # hits AWS; marked and skippable
    └── golden/
```

### 7.2 Environment setup

Kiro must produce a `README.md` whose setup section actually works from a clean machine.

```bash
# Python 3.11 or 3.12 (3.13 wheels for umap-learn/hdbscan may lag — TODO(confirm) at build time)
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip

pip install -r requirements.txt
pip install -r requirements-dev.txt

# System dependency for python-magic (not a pip package):
#   macOS:        brew install libmagic
#   Debian/Ubuntu: sudo apt-get install -y libmagic1
#   (In the container, install it in the image — see Dockerfile.)

cp .env.example .env               # fill in region, bucket, model IDs
make test                          # unit tests, no AWS needed
make eval                          # scored runs (needs AWS)
make run                           # full local pipeline on the sample pack
```

### 7.3 Dependencies

Pin versions in `requirements.txt`. Group with comments explaining *why each one is here*.

```
# --- AWS ---
boto3                     # S3, Bedrock, Bedrock Agent (KB), Data Automation
botocore

# --- Config / validation ---
pydantic                  # schemas for tickets, tool args, agent contract
pydantic-settings         # typed config + env loading
pyyaml                    # routing rules + config files

# --- Document feature extraction (local, free — keeps BDA calls down) ---
python-magic              # TRUE mime type from bytes; needs libmagic system lib
pymupdf                   # fast PDF text/image/page geometry  (licence: AGPL — see note)
pdfplumber                # table detection + per-page char counts
python-docx               # docx images/tables inspection

# --- Tickets ---
pandas                    # csv / xlsx loading
openpyxl                  # xlsx engine for pandas

# --- Clustering ---
numpy
scikit-learn              # silhouette, utilities, (HDBSCAN also available here)
umap-learn                # dimensionality reduction
hdbscan                   # density clustering  (or sklearn.cluster.HDBSCAN)

# --- Language detection ---
langdetect                # or lingua-py; pick one and note why in a comment

# --- Observability ---
opentelemetry-api
opentelemetry-sdk
opentelemetry-exporter-otlp
structlog                 # structured JSON logs with run_id bound

# --- dev (requirements-dev.txt) ---
pytest, pytest-cov, pytest-mock, moto (AWS mocking), ruff, black, mypy, pre-commit
```

> **Licence note for Kiro:** PyMuPDF is AGPL-licensed. Flag this in `OPEN_QUESTIONS.md` for
> legal review, and keep the PDF feature extraction behind a small interface so it can be
> swapped for `pypdf` + `pdfplumber` if AGPL is unacceptable for a commercial deliverable.
> Also populate `sample_pack/LICENSES.md` with the licence and source URL of every OSS document
> used — Monday's deliverable names this file explicitly.

### 7.4 Coding standards

**Style and typing**
- Python 3.11+, full type hints on every public function. `mypy` clean.
- `ruff` + `black`, enforced via `pre-commit`. Line length 100.
- Module, class and function docstrings. For anything an LLM will read as a tool, the docstring
  is the prompt — write it as such.

**Structure**
- **Pure functions for logic, thin adapters for I/O.** Feature extraction, the rule table,
  purity and recall calculations must be pure and testable with no AWS and no network.
- No business logic in `entrypoint.py`. It parses input, calls `pipeline`, formats output.
- **No hard-coded values.** Thresholds, model IDs, limits, seeds, prefixes → `config/`.
  A reviewer must be able to retune without a redeploy.
- Dependency-inject AWS clients so tests can substitute fakes/`moto`.
- One stage per module; each stage has one public entry function with a typed input and output.

**Error handling**
- Custom exception hierarchy (`KBAgentError` → `ValidationError`, `ParsingError`,
  `IngestionError`, `BudgetExceededError`, ...).
- A single bad file or row must never kill a run: collect, flag, continue, report counts.
- Retries with exponential backoff + jitter on throttling, on AWS calls only. Cap them.
- Never swallow an exception silently — log with `run_id` and stage.

**Comments — what good looks like here**
Code review for this pilot will be done by people who were not in the design discussions.
Comment the *why*, especially where a cheaper or more obvious approach was deliberately rejected:

```python
# We route per FILE, not per data source, because Bedrock Knowledge Bases fix the parsing
# strategy per data source — so KB-managed parsing cannot choose BDA for one PDF and the
# default parser for a .txt. Calling BDA directly here and ingesting its markdown keeps a
# single KB data source while still paying BDA only for files that need it.
```

```python
# Deliberately deterministic: an LLM could judge "does this PDF need BDA?", but routing must
# be reproducible and auditable (same file -> same route -> same rule_id in the audit log),
# and every signal we need is locally measurable. See docs/adr/0002-deterministic-routing.md.
```

Do **not** write comments that restate the code (`# increment counter`).

**Security**
- No secrets in code or logs. `.env` is git-ignored; `.env.example` holds keys with empty values.
- Never log raw customer/ticket content at INFO. Log IDs, counts and hashes.
- Scope the IAM policy to the run's S3 prefix and the specific Bedrock resources; write the
  least-privilege policy JSON into `docs/iam-policy.json` as a starting point for review.
- Treat **all** file and ticket content as untrusted data, never as instructions. A ticket body
  containing "ignore previous instructions" must not influence any prompt — note this in the
  prompt templates and add a test case using a prompt-injection string in a ticket.

### 7.5 Testing

```
make test        # unit only, offline, fast — must pass with no AWS credentials
make test-all    # + component tests (marked @pytest.mark.aws)
make eval        # scored evaluation run, writes evals/results/{timestamp}.json
```

- **Unit:** feature extraction, every routing rule (one fixture file per rule), adapter mapping,
  schema validation, PII regex, cache keying, purity/recall/silhouette maths, planner limit
  enforcement, precondition enforcement.
- **Component (`@pytest.mark.aws`, skippable):** BDA on 2–3 scanned files, KB create/ingest/
  retrieve, embeddings, clustering on the real sample pack.
- **Golden:** stored expected outputs for fixtures, diffed on every change.
- **LLM steps:** assert on *structure* — valid schema, citations present, cited IDs exist and
  belong to the intent, PII scan passed. Never assert exact wording. Run multi-sample and
  report pass rate.
- **Mock the LLM in unit tests** with canned responses so pipeline logic is tested
  deterministically.
- **Failure/safety tests:** encrypted PDF, corrupt file, non-English tickets, empty input,
  oversized input, unknown ticket format, planner step-limit breach, attempted ingest before
  validation (must be refused), prompt-injection string inside a ticket.
- **Ground-truth leak test:** assert no seeded label string appears in any prompt payload.
- Fix seeds; cache BDA and embedding results while iterating to control cost.

### 7.6 Containerisation

Build the container **after** local tests pass, but before integration.

- `--platform linux/arm64` (`TODO(confirm)` against current AgentCore Runtime requirements).
- Install `libmagic1` in the image — python-magic will fail at import without it, and this is
  the classic "works locally, breaks in the container" failure.
- Verify `umap-learn`, `hdbscan`, `numpy` and `pymupdf` resolve to ARM64 wheels; if any builds
  from source, pin a version that does not, or add the build toolchain.
- Multi-stage build to keep the image small. Non-root user. No secrets baked in.
- **Contract test:** run the container locally, POST the exact payload the orchestrator will
  send, and assert the response shape. Use a stub orchestrator script in `scripts/`.

---

## 8. The agent contract (define it now; it is the integration surface)

The SOW does not specify the orchestrator↔agent interface. Define it in `contract.py` and make
it the stable boundary — everything else can change behind it.

```python
class AgentInput(BaseModel):
    run_id: str
    document_uris: list[str] = []       # s3://.../uploads/{run_id}/docs/...
    ticket_uri: str | None = None       # s3://.../uploads/{run_id}/tickets.csv
    industry: str | None = None
    options: Options = Options()        # kb_required, vector_store, thresholds overrides
    reviewer_feedback: ReviewerFeedback | None = None   # drives a re-run
    resume_from: str | None = None      # stage name, for resumable execution

class AgentOutput(BaseModel):
    run_id: str
    status: Literal["completed", "running", "needs_review", "failed"]
    stage: str
    artifacts: dict[str, str]           # name -> s3 uri (report, intent view, drafts, manifest)
    metrics: RunMetrics                 # recall@5, purity, counts, durations, costs
    pending_review: list[ArtifactRef]
    errors: list[ErrorRecord]
```

`TODO(confirm)` with the orchestrator owner: synchronous vs polling, and how reviewer decisions
are delivered back to the agent.

---

## 9. Open questions — put these in `OPEN_QUESTIONS.md`

| # | Question | Who answers | Blocks |
|---|---|---|---|
| 1 | How are inputs delivered — console upload to S3, pasted JSON, S3 URI typed in chat? (We assume S3 references.) | Manager / console owner | Integration only; prototype is safe |
| 2 | Ticket export format and field names for the sample | Manager / Delivery Lead | `ticket_profile`, adapter |
| 3 | Is "≥5 source tickets" per article the intended reading? | Manager | `draft_article` |
| 4 | What does the "70/30 time-split" of the sample tickets mean (older/newer, to simulate coverage gaps or KB change)? | Manager | Sample pack |
| 5 | Non-English tickets: flag, reject, or ignore? (SOW is English-only) | Product Owner | `validate_uploads` |
| 6 | Confirmed Region, and is Bedrock Data Automation available there with sufficient quota? | IT / Environment Owner | Tuesday |
| 7 | Vector store: S3 Vectors confirmed, or will OpenSearch Serverless be requested by Day 10? | Product Owner | `kb_create` config |
| 8 | KB location — pilot account or sandbox? (SOW §4.7.4 and §4.10/diagram disagree) | Solution Architect | `kb_create` |
| 9 | AgentCore Runtime max invocation duration; polling or callback for async stages? | Solution Architect | `entrypoint`, resumability |
| 10 | Is DynamoDB (or equivalent) approved for run/review state, or is S3-only required? | Solution Architect / InfoSec | `review/state.py` |
| 11 | Exact embedding model ID and Claude model IDs to pin | Solution Architect | config |
| 12 | How much autonomy is expected inside this agent — is the thin planner wanted for the demo, or is a deterministic workflow sufficient? | Manager | §6 scope |
| 13 | Is PyMuPDF's AGPL licence acceptable in a commercial deliverable? | Engagement Lead / legal | dependency choice |

---

## 10. Build order for Kiro

Do it in this order. Do not start the planner until the pipeline passes its tests.

1. **Scaffold:** repo layout, venv, `requirements*.txt`, Makefile, config loading, structlog +
   `run_id` binding, `contract.py`, `models.py`, `OPEN_QUESTIONS.md`, README.
2. **Sample pack generator:** `generate_tickets.py` (20 intents × ~75 tickets, hidden labels in
   a separate `labels.csv`, seeded PII/non-English/duplicate/no-resolution rows), `LICENSES.md`
   scaffold, document fixtures placeholder directory with a README explaining what to drop in.
3. **Evals first:** `metrics.py` (purity, silhouette, recall@k, FP rate), `golden_set.jsonl`
   with ~5 placeholder entries and the schema documented, `routing_fixtures.yaml`.
4. **Red pill:** features → router (rule table from YAML) → validate → BDA client with cache →
   normalize → KB create/ingest/retrieve. Unit tests at each step.
5. **Blue pill:** loaders → adapter → clean → embed (cached) → cluster → naming → intent view.
6. **Join:** coverage_check → draft_article (+ citation validation + PII re-scan) →
   write_report → review state.
7. **Stitch:** `pipeline.py` end-to-end, resumable, timed, audited. `run_local.py`.
8. **Planner (only if §9 Q12 says yes):** tool registry, planner loop, limits, precondition
   enforcement, decision logging.
9. **Container:** Dockerfile, ARM64 build, contract test with a stub orchestrator.
10. **Docs:** README (setup, run, test, architecture diagram), ADRs for chunking strategy and
    deterministic routing, `docs/iam-policy.json`.

### Definition of done for the week

- `make test` passes offline on a clean clone.
- `make run` completes end-to-end on the sample pack and produces: a built KB, an intent view
  with volumes, ≥3 cited draft articles, a coverage-gap list, a run manifest and a timing
  report — all in `pending_review`.
- `make eval` reports: purity, silhouette, cluster count, noise share, recall@5, PII rejection,
  gaps flagged with FP rate, routing accuracy.
- Every `TODO(confirm)` appears in `OPEN_QUESTIONS.md` with an owner.
- The report states plainly that all metrics are measured on synthetic data.
