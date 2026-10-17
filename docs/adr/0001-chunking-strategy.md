# ADR 0001: Knowledge base chunking strategy

Status: Proposed (Week 1)
Date: 2026-10-15

## Context

Bedrock Knowledge Bases support several chunking strategies, configured per data
source via ``vectorIngestionConfiguration`` (verified in Part A; see
OPEN_QUESTIONS.md log item 2): fixed-size, hierarchical, semantic, and none.
Thursday's deliverable is a chunking comparison measuring fixed-size vs
hierarchical vs semantic on recall@5, recording the decision and the numbers here
(spec 4.2).

The corpus is long product manuals and policies plus ticket-derived content.

## Decision

Default to **hierarchical** chunking (small child chunks are matched, larger
parent chunks are returned). It generally performs best on long manuals and
policies because the small child improves match precision while the returned
parent preserves enough surrounding context to answer. The strategy is a config
value (``knowledge_base.chunking_strategy``) so it can be changed without code.

## Comparison (to be completed with real documents)

The recall@5 comparison requires the real document pack (sample-pack documents are
not yet supplied - see OPEN_QUESTIONS.md and ``sample_pack/documents/README.md``)
and the real golden set (the current ``evals/golden_set.jsonl`` holds flagged
placeholder rows). The harness to run it exists:

- ``evals/metrics.py:recall_at_k`` computes the score.
- ``make eval`` (fake mode) exercises the plumbing; the real numbers need Bedrock
  embeddings + real documents (``--mode aws``).

| Strategy | recall@5 | notes |
|----------|----------|-------|
| fixed_size | _TBD_ | baseline |
| hierarchical | _TBD_ | expected best on long docs |
| semantic | _TBD_ | higher ingestion cost |

**This ADR will be updated with the measured numbers once real documents and a
real golden set are in place.** Until then, hierarchical is the reasoned default,
not a measured winner - stated honestly rather than fabricated.

## Consequences

- Changing the embedding model or chunking later means re-ingesting from S3 (the
  source of truth is kept as files; the KB holds a derived index), not re-paying
  BDA. This is why normalised Markdown is retained in S3 (spec 4.2).
- The config flag keeps the comparison cheap to re-run.
