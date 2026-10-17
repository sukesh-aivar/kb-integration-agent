# Knowledge Base Integration Agent

Sub-agent #1 of 4 in the Ingram Micro conversational-AI delivery pilot (supplier:
Aivar Innovations). It builds a Bedrock knowledge base from customer documents,
discovers intents from a customer ticket export, checks whether the knowledge
base answers the frequent intents, and drafts cited articles for the gaps.

Everything it produces is a **draft for a named reviewer** - nothing enters the
knowledge base without approval (spec section 2.2).

> Terminology: we always say **customer**, never "client" (organisation-wide
> standard).

## Status

Week 1 prototype, full pipeline built. The deterministic pipeline (red pill +
blue pill + join), both run modes, the audit/observability layer, the AgentCore
container, and the thin planner are all implemented and tested. The planner is
**disabled by default** (`planner.enabled: false`) pending OPEN_QUESTIONS Q12.

The sample-pack documents and the real golden set are the remaining inputs: the
`sample_pack/documents/` directory is intentionally empty (no fabricated content)
and `evals/golden_set.jsonl` holds flagged placeholder rows, so retrieval-quality
metrics (recall@5) require real documents before they mean anything. The pipeline
runs end-to-end today in fake mode on the synthetic ticket pack.

## Two run modes

| Mode | What it uses | Use for |
|------|--------------|---------|
| `--mode fake` | In-memory fakes for S3, BDA, KB, embeddings, LLM, guardrails (`src/kb_agent/aws/fakes.py`). Offline, deterministic. | Local dev, tests, `make run-fake` |
| `--mode aws` | Real boto3-backed Bedrock/S3 clients (`src/kb_agent/aws/clients.py`). | Real runs; needs confirmed config + credentials |

Both modes share one pipeline; only the injected client set differs
(`build_client_set` in `src/kb_agent/aws/clientset.py`).

## Requirements

- **Python 3.11** (3.12 expected to work; 3.13/3.14 not supported - `umap-learn`
  / `hdbscan` / `numba` wheels lag the newest CPython).
- System library for `python-magic`:
  - macOS: `brew install libmagic`
  - Debian/Ubuntu: `sudo apt-get install -y libmagic1`
  - In the container it is installed in the image (see `Dockerfile`).

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
cp .env.example .env               # fill in region, bucket, model IDs

make test                          # unit tests, no AWS needed
make run-fake                      # full local pipeline on the sample pack, offline
```

Or simply `make setup`.

## Make targets

| Target | What it does |
|--------|--------------|
| `make setup` | Create venv, install runtime + dev deps |
| `make test` | Unit tests only, **offline, no AWS credentials** |
| `make test-all` | Unit + component tests (`@pytest.mark.aws`) |
| `make eval` | Scored evaluation run (real scores in fake mode) |
| `make run-fake` | Full pipeline on the sample pack, in-memory fakes (offline) |
| `make run-aws` | Full pipeline against real AWS (needs credentials + confirmed config) |
| `make lint` / `make format` / `make typecheck` | ruff / black / mypy |
| `make docker-build` | Build the ARM64 container image |

## Architecture

```
AgentInput (S3 refs) ──► Pipeline (13 stages, resumable, timed, audited)
                              │
   RED PILL (documents)       │       BLUE PILL (tickets)
   validate → route → BDA     │       load → adapt → clean → embed → cluster
   → normalize → KB create    │       → name  (LLM only: naming, <500 categorise)
   /ingest/retrieve           │
                              ▼
                     JOIN: coverage_check (LLM judge) → draft_article
                     (≥5 tickets, citations validated, PII re-scan) → write_report
                              ▼
                     AgentOutput + run manifest, all pending_review
```

- **Deterministic by default; an LLM in exactly five places** (spec 2.1): cluster
  naming, <500-ticket categorisation, article drafting, coverage judgement, the
  planner. Everything else - routing, field mapping, counting - is code.
- **Drafts never reach the KB:** articles are written only to `drafts/{run_id}/`,
  a prefix the KB data source cannot read (spec 2.2).
- **Never leak ground truth:** seeded intent labels (`sample_pack/tickets/labels.csv`)
  exist only to score clustering; an automated test asserts they never enter a
  prompt or embedding (spec 2.4).
- **Reproducible & auditable:** fixed seeds, content-hash caching, pinned model
  IDs, and a per-run JSON manifest (`runs/{run_id}/manifest.json`).

## Sample data caveats

- **The ticket generator is a smoke-test fixture, not realistic data.**
  `sample_pack/generate_tickets.py` plants 20 intents with hidden labels. It was
  adjusted to compose each ticket body from varied opener/context/closer fragments
  so the ~1,500 tickets are lexically distinct (without that, only a handful of
  phrasings existed and dedup collapsed the set). The intent-specific vocabulary
  still dominates. `generate_tickets_llm.py` (`--mode aws`) produces harder,
  more realistic tickets.
- **Fake-mode metrics are smoke-test values only.** In `--mode fake`, embeddings
  are a hashed bag-of-words stand-in, so intent purity, cluster count, silhouette
  and recall validate the *pipeline wiring*, not real quality. Do not tune
  clustering parameters against them. The run report prints this caveat itself.
- **recall@5 in fake mode is not evidence the target is met.** The committed
  `evals/golden_set.jsonl` holds placeholder rows; real recall needs Bedrock
  embeddings and a human-approved golden set (see
  `evals/generate_golden_candidates.py`).

## Container (AgentCore Runtime)

The image targets Amazon Bedrock AgentCore Runtime (verified requirements in
`OPEN_QUESTIONS.md`): **ARM64**, exposing `POST /invocations` and `GET /ping`.

```bash
make docker-build                                  # builds --platform linux/arm64
# contract-test it locally:
docker run --rm -p 8080:8080 -e KB_AGENT_S3_BUCKET=fake-bucket \
  --entrypoint python kb-integration-agent:local \
  -m kb_agent.entrypoint --serve --mode fake --port 8080
python scripts/stub_orchestrator.py --base http://localhost:8080
```

## Layout

```
config/            default.yaml, routing_rules.yaml, prompts/*.v1.md
src/kb_agent/
  aws/             interfaces (Protocols), fakes, real clients, clientset factory
  documents/       validate, features, router, bda, normalize, knowledge_base, pii
  tickets/         loaders, adapters/, clean, embed, cluster, categorise, naming
  join/            coverage, drafting, report
  review/          state (status + decision records)
  obs/             timing, tracing (OTEL), audit (run manifest)
  planner/         planner, tools  (disabled by default)
  pipeline.py      13-stage resumable orchestration
  entrypoint.py    AgentCore handler + CLI
evals/             metrics.py, golden_set.jsonl, routing_fixtures.yaml, run_evals.py
sample_pack/       generate_tickets.py, LICENSES.md, documents/, tickets/
scripts/           run_local.py, bootstrap_aws.py, stub_orchestrator.py
docs/              adr/0001-chunking-strategy.md, adr/0002-deterministic-routing.md, iam-policy.json
tests/unit/        offline unit + contract tests
```

## Open questions

Every unconfirmed value is tracked in [`OPEN_QUESTIONS.md`](./OPEN_QUESTIONS.md)
with an owner, plus the AWS verification log. Placeholders in code are deliberately
obvious (`REPLACE_ME_*`) so an unconfigured `--mode aws` run fails loudly.
