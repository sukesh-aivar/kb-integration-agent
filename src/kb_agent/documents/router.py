"""Document router: evaluate the YAML rule table (spec 4.2).

Deliberately deterministic: an LLM could judge "does this PDF need BDA?", but
routing must be reproducible and auditable (same file -> same route -> same
rule_id in the audit log), and every signal we need is locally measurable. See
docs/adr/0002-deterministic-routing.md. This is one of the stages the spec
explicitly says must NOT use a model (spec 2.1).

The rule table is data (config/routing_rules.yaml); this module is the pure
interpreter. First match wins. Thresholds referenced by dotted name (e.g.
``routing.min_chars_per_page``) resolve against AppConfig so numbers live in one
place.
"""

from __future__ import annotations

from typing import Any

from kb_agent.config import AppConfig, load_routing_rules
from kb_agent.documents.features import DocumentFeatures, normalise_mime
from kb_agent.exceptions import RoutingError
from kb_agent.models import Route, RoutingDecision


def _resolve_threshold(value: Any, config: AppConfig) -> Any:
    """Resolve a dotted config reference (e.g. 'routing.max_tables') or literal."""
    if isinstance(value, str) and "." in value:
        node: Any = config
        for part in value.split("."):
            node = getattr(node, part)
        return node
    return value


def _condition_matches(when: dict[str, Any], f: DocumentFeatures, config: AppConfig) -> bool:
    """Evaluate one rule's ``when`` clause against the features.

    Supported predicate keys mirror routing_rules.yaml. All listed predicates
    must hold (AND), except ``any_of`` which is an OR of sub-predicates.
    """
    short_mime = normalise_mime(f.detected_mime)

    for key, raw in when.items():
        expected = _resolve_threshold(raw, config)
        if key == "always":
            if not expected:
                return False
        elif key == "mime":
            if short_mime != expected:
                return False
        elif key == "mime_in":
            allowed = {normalise_mime(m) for m in expected}
            if short_mime not in allowed and f.detected_mime not in expected:
                return False
        elif key == "pdf_encrypted_or_unreadable":
            if f.pdf_encrypted_or_unreadable != expected:
                return False
        elif key == "any_page_chars_lt":
            if f.min_page_chars is None or not (f.min_page_chars < expected):
                return False
        elif key == "any_page_image_area_ratio_gt":
            if not (f.max_page_image_area_ratio > expected):
                return False
        elif key == "detected_tables_gte":
            if not (f.detected_tables >= expected):
                return False
        elif key == "multi_column":
            if f.multi_column != expected:
                return False
        elif key == "embedded_images_gt":
            if not (f.embedded_images > expected):
                return False
        elif key == "complex_tables_gt":
            if not (f.complex_tables > expected):
                return False
        elif key == "any_of":
            if not any(_condition_matches(sub, f, config) for sub in expected):
                return False
        else:
            raise RoutingError(f"Unknown routing predicate: {key!r}")
    return True


def route_document(
    features: DocumentFeatures,
    config: AppConfig,
    rules: list[dict[str, Any]] | None = None,
) -> RoutingDecision:
    """Return the routing decision for one document (first matching rule wins).

    Args:
        features: Locally-measured features for the file.
        config: App config supplying thresholds.
        rules: Optional pre-loaded rule list (defaults to routing_rules.yaml).

    Returns:
        A :class:`RoutingDecision` carrying the winning ``rule_id`` and reason.

    Raises:
        RoutingError: If no rule matches (the table must end with a catch-all).
    """
    rules = rules if rules is not None else load_routing_rules()
    feature_dump: dict[str, float | int | bool | str] = {
        "mime": features.detected_mime,
        "min_page_chars": features.min_page_chars if features.min_page_chars is not None else -1,
        "max_image_area_ratio": features.max_page_image_area_ratio,
        "detected_tables": features.detected_tables,
        "multi_column": features.multi_column,
        "embedded_images": features.embedded_images,
        "complex_tables": features.complex_tables,
    }

    for rule in rules:
        when = rule.get("when", {})
        if _condition_matches(when, features, config):
            return RoutingDecision(
                file_id=features.file_id,
                route=Route(rule["route"]),
                rule_id=int(rule["id"]),
                features=feature_dump,
                reason=rule.get("description", ""),
            )

    raise RoutingError(
        f"No routing rule matched {features.file_id}; the table must end with a catch-all"
    )
