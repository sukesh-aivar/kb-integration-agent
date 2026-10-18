"""Offline dry-run checker for a synthetic ticket CSV (no AWS, no spend).

Runs the REAL pipeline front half against a local CSV so you can see exactly how
many rows the agent would keep, drop, or flag BEFORE a real run:

  loader (load_tickets_bytes)  -> raw rows
  adapter (SampleAdapter.adapt) -> mapped / skipped / failed  (+ per-row reasons)
  clean   (clean_tickets)       -> duplicates removed
  regex PII (regex_pii_findings on subject+description)        -> PII-flagged count
  language (detect_language)    -> non-English count

This is the authoritative pre-check: it uses the same code the agent runs, so a
row that passes here maps in a real run. The regex PII / language layers mirror
validate.py's Stage B (Guardrails is the extra online layer, not exercised here).

Usage:
    .venv/bin/python scripts/check_tickets.py path/to/tickets.csv
    .venv/bin/python scripts/check_tickets.py path/to/tickets.csv --show-reasons 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kb_agent.documents.pii import regex_pii_findings  # noqa: E402
from kb_agent.documents.validate import detect_language  # noqa: E402
from kb_agent.tickets.adapters.base import FORBIDDEN_SOURCE_FIELDS  # noqa: E402
from kb_agent.tickets.adapters.sample import SampleAdapter  # noqa: E402
from kb_agent.tickets.clean import clean_tickets  # noqa: E402
from kb_agent.tickets.loaders import load_tickets_bytes  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline dry-run check of a ticket CSV.")
    parser.add_argument("csv_path", help="Path to the CSV (or .json/.xlsx) to check.")
    parser.add_argument(
        "--show-reasons", type=int, default=10, help="How many skip/fail reasons to print."
    )
    args = parser.parse_args()

    path = Path(args.csv_path)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    fmt = path.suffix.lstrip(".").lower() or "csv"
    data = path.read_bytes()

    # 1) loader
    try:
        rows = load_tickets_bytes(data, fmt)
    except Exception as exc:  # noqa: BLE001
        print(f"LOADER FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"rows read by loader        : {len(rows)}")

    # header / forbidden-column check
    header = set(rows[0].keys()) if rows else set()
    leaked = sorted(FORBIDDEN_SOURCE_FIELDS & header)
    if leaked:
        print(f"WARNING forbidden label columns present (ignored, but a LEAK): {leaked}")
    print(f"columns seen               : {sorted(header)}")

    # 2) adapter
    tickets, report = SampleAdapter().adapt(rows)
    print(f"adapter mapped             : {report.mapped}")
    print(
        f"adapter skipped            : {report.skipped}  "
        "(missing id / subject+description, or bad/absent created_at)"
    )
    print(f"adapter failed             : {report.failed}")
    if report.reasons and args.show_reasons:
        print(f"--- first {args.show_reasons} skip/fail reasons ---")
        for r in report.reasons[: args.show_reasons]:
            print(f"  {r}")

    # 3) clean / dedupe
    cleaned, duplicates = clean_tickets(tickets)
    print(
        f"duplicates removed (clean) : {duplicates}  "
        "(by cleaned subject+description, case-insensitive)"
    )
    print(f"unique tickets after clean : {len(cleaned)}")

    # 4) PII (regex layer) + language, on subject+description (what validate scans)
    pii_flagged = 0
    non_english = 0
    lang_counts: dict[str, int] = {}
    for t in cleaned:
        text = f"{t.subject}\n{t.description}"
        if regex_pii_findings(text):
            pii_flagged += 1
        lang = detect_language(text)
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
        if lang != "en":
            non_english += 1
    print(f"PII-flagged (regex layer)  : {pii_flagged}  (in subject+description)")
    print(f"non-English (flagged)      : {non_english}")
    ordered = dict(sorted(lang_counts.items(), key=lambda kv: -kv[1]))
    print(f"language breakdown         : {ordered}")

    # clustering-path note
    threshold = 500
    path_note = (
        "UMAP+HDBSCAN (>=500)" if len(cleaned) >= threshold
        else "LLM-categorise fallback (<500)"
    )
    print(f"clustering path (by count) : {path_note}")

    print(
        f"\nSUMMARY: of {len(rows)} rows -> {report.mapped} mapped -> "
        f"{len(cleaned)} unique after dedupe."
    )
    if report.skipped or report.failed:
        print(
            "NOTE: skipped/failed rows are SILENTLY DROPPED in a real run - "
            "fix them if unintended."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
