"""Optional LLM-backed ticket generator for HARDER synthetic tickets (spec step 11).

The templated generator (``generate_tickets.py``) is the deterministic smoke-test
fixture. This script produces a harder set using Bedrock (``--mode aws``): varied
phrasing, overlapping intents, vague and multi-issue tickets - closer to real
helpdesk data, to stress clustering/coverage. It keeps the SAME hidden-label
separation: labels are written only to ``labels.csv``, never to ``tickets.csv``
(spec 2.4).

Design:
* The seeded intent for each ticket is chosen by CODE (so ground truth is known),
  then the LLM is asked to WRITE a ticket for that intent with a requested
  difficulty (vague / multi-issue / overlapping). The intent name is used only to
  steer generation; it is NOT written into tickets.csv.
* ``--mode fake`` is supported for a dry run (uses the rule-based fake LLM), but
  the point of this script is ``--mode aws`` for genuinely varied text.

Output mirrors the templated generator: tickets.csv (no labels), labels.csv
(hidden), ticket_profile.json. Writes to a separate ``--out`` dir by default so it
never clobbers the committed smoke-test pack unless asked.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))  # so `sample_pack` is importable

from kb_agent.aws.clientset import build_client_set  # noqa: E402
from kb_agent.config import get_settings, load_config  # noqa: E402

# Reuse the 20 planted intents so the hidden-label vocabulary matches the pack.
from sample_pack.generate_tickets import INTENTS  # noqa: E402

DIFFICULTIES = ("plain", "vague", "multi_issue", "overlapping")


def _prompt_for(intent_label: str, difficulty: str, rng: random.Random) -> str:
    """Build an untrusted-data-safe generation prompt (spec 7.4)."""
    topic = intent_label.replace("_", " ")
    style = {
        "plain": "a clear, specific ticket",
        "vague": "a vague, under-specified ticket that only hints at the problem",
        "multi_issue": "a ticket that mixes this problem with one unrelated minor problem",
        "overlapping": "a ticket that could plausibly belong to a neighbouring intent",
    }[difficulty]
    return (
        f"Write {style} from a customer about: {topic}. "
        "One short paragraph, first person, no greeting line, no signature, no PII. "
        "Return ONLY the ticket body text, nothing else."
    )


def generate_llm_tickets(n: int, mode: str, seed: int) -> list[dict]:
    settings = get_settings()
    config = load_config()
    clients = build_client_set(mode, settings, config)  # type: ignore[arg-type]
    rng = random.Random(seed)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    rows: list[dict] = []
    for i in range(1, n + 1):
        intent = rng.choice(INTENTS)
        difficulty = rng.choice(DIFFICULTIES)
        prompt = _prompt_for(intent.label, difficulty, rng)
        body = clients.llm.invoke(prompt, max_tokens=200).strip() or rng.choice(intent.problems)
        created = now - timedelta(days=rng.randint(1, 365))
        resolved = rng.random() > 0.1
        rows.append(
            {
                "ticket_id": f"L{i:05d}",
                "subject": rng.choice(intent.subjects),
                "description": body,
                "resolution": rng.choice(intent.resolutions) if resolved else "",
                "status": "solved" if resolved else "open",
                "created_at": created.isoformat(),
                "resolved_at": (created + timedelta(hours=rng.randint(1, 72))).isoformat()
                if resolved
                else "",
                "tags": intent.label.split("_")[0],
                "source_platform": "sample_llm",
                "_seeded_intent": intent.label,  # goes to labels.csv ONLY
                "_difficulty": difficulty,
            }
        )
    return rows


def write(rows: list[dict], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ticket_cols = [
        "ticket_id", "subject", "description", "resolution", "status",
        "created_at", "resolved_at", "tags", "source_platform",
    ]
    with (out / "tickets.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ticket_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with (out / "labels.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ticket_id", "seeded_intent", "difficulty"])
        for r in rows:
            w.writerow([r["ticket_id"], r["_seeded_intent"], r["_difficulty"]])
    with (out / "ticket_profile.json").open("w", encoding="utf-8") as fh:
        json.dump(
            {"schema": "ticket_profile v0.1 (llm)", "total_tickets": len(rows), "synthetic": True,
             "generator": "generate_tickets_llm.py", "labels_file": "labels.csv (hidden)"},
            fh, indent=2,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate harder LLM tickets (same hidden-label split)."
    )
    parser.add_argument("--mode", choices=["fake", "aws"], default="aws")
    parser.add_argument("--count", type=int, default=600)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out", type=Path, default=HERE / "tickets_llm")
    args = parser.parse_args()

    seed = args.seed if args.seed is not None else load_config().seed
    rows = generate_llm_tickets(args.count, args.mode, seed)
    write(rows, args.out)
    # Leak guard: assert no seeded label leaked into tickets.csv.
    tickets_text = (args.out / "tickets.csv").read_text(encoding="utf-8")
    leaked = [i.label for i in INTENTS if i.label in tickets_text]
    if leaked:
        print(f"ABORT: seeded labels leaked into tickets.csv: {leaked}", file=sys.stderr)
        return 1
    print(f"Wrote {len(rows)} LLM tickets ({args.mode}) to {args.out}; labels hidden; no leak.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
