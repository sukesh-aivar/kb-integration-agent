# ADR 0002: Deterministic document routing (no LLM in the router)

Status: Accepted (Week 1)
Date: 2026-10-15

## Context

Each document must be routed to one of DEFAULT (cheap local parse), BDA
(layout-aware paid parse), or REJECT. BDA is billed per page and is overkill for a
clean ``.txt`` or ``.html``, but Bedrock Knowledge Bases fix the parsing strategy
**per data source**, so KB-managed parsing cannot choose per file. Hence the agent
routes per file and calls BDA directly for complex files, ingesting clean Markdown
from a single data source (spec 4.2).

The question for this ADR: should the routing decision be made by an LLM or by
deterministic code?

## Decision

**Deterministic code - a YAML rule table, first match wins
(``config/routing_rules.yaml``), interpreted by a pure function
(``documents/router.py``).** No LLM is involved in routing. This is one of the
stages the spec explicitly forbids using a model for (spec 2.1).

## Rationale

- **Reproducibility and auditability.** The same file must always produce the same
  route and the same ``rule_id`` in the audit log, so a misroute is debuggable.
  An LLM is non-deterministic and cannot guarantee this.
- **Every signal is locally measurable.** True MIME (python-magic), per-page char
  counts and image-area ratio (PyMuPDF), table counts (pdfplumber), multi-column
  layout, DOCX embedded images / merged cells (python-docx) - none of these need
  a model.
- **Cost and latency.** An LLM call per file is slow and billable for a decision
  arithmetic can make.

## Safety net

The rule table deliberately errs toward cheap DEFAULT, with a deterministic
escalation backstop (``documents/validate.py:needs_escalation``): after a DEFAULT
parse, escalate to BDA if the extracted text is too short per page or has too high
a share of garbled characters. This bounds the cost of an over-eager DEFAULT.

## Consequences

- Routing is fully testable offline: there is one fixture per rule
  (``evals/routing_fixtures.yaml``), all exercised in ``test_features_and_router``.
- Adding a signal means adding a predicate to the router interpreter and a rule to
  the YAML - the thresholds stay in ``config/`` (no hard-coded values, spec 7.4).
- An LLM may *propose* a routing rule change for human review offline, but never
  decides a route at runtime.
