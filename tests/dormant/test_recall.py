"""DORMANT: recall@5-from-golden test, split out of test_bda_normalize_kb.py.

``recall_at_5_from_golden`` moved to ``kb_agent.dormant.documents_extras`` (it is
only used by the dormant kb_retrieve stage and the evals harness), so its test
lives here. The KnowledgeBaseStage it exercises is still active.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.dormant

from kb_agent.aws.clientset import build_fake_client_set
from kb_agent.config import load_config
from kb_agent.documents.knowledge_base import KnowledgeBaseStage
from kb_agent.dormant.documents_extras import recall_at_5_from_golden

CONFIG = load_config()


def test_recall_at_5_from_golden_scores() -> None:
    cs = build_fake_client_set()
    prefix = "kb/run-1/docs"
    cs.store.put_text(f"{prefix}/reset.md", "reset your password from account settings")
    stage = KnowledgeBaseStage(cs.knowledge_base, CONFIG)
    stage.create("kb", prefix)
    stage.ingest()

    golden = [
        {"id": "q1", "question": "how to reset my password", "expected_source_ids": ["reset.md"]},
        {
            "id": "q2",
            "question": "unrelated nonsense query zzz",
            "expected_source_ids": ["missing.md"],
        },
    ]
    recall, detail = recall_at_5_from_golden(stage, golden, k=5)
    assert recall == 0.5  # one hit, one miss
    assert detail[0]["hit"] is True
    assert detail[1]["hit"] is False
