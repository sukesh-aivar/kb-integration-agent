"""In-memory FAKE AWS implementations for ``--mode fake`` (offline, deterministic).

THESE ARE FAKES. They contain no network calls and must never be used in
production. They exist so the entire pipeline runs offline on the sample pack and
so unit tests are deterministic. Every class here satisfies a Protocol in
:mod:`interfaces`, so pipeline code cannot tell a fake from the real client.

Design choices that keep results meaningful rather than trivial:

* ``FakeEmbeddings`` produces a deterministic hashed-bag-of-words vector. Tickets
  that share vocabulary (the planted intents do) land near each other, so UMAP +
  HDBSCAN actually rediscover structure - the clustering metrics mean something
  in fake mode. It is NOT a real semantic model; it is a reproducible stand-in.
* ``FakeLlm`` is rule-based and returns schema-valid JSON for each of the five
  prompt kinds, detected by markers the prompt templates include. It never does
  real language understanding.
* ``FakeBda`` returns deterministic markdown derived from the input bytes.
* ``FakeGuardrail`` defers to the same regex layer used in production, so fake
  mode still catches the seeded PII (regex is deterministic and offline anyway).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict

from kb_agent.aws.interfaces import (
    BdaResult,
    GuardrailPiiFinding,
    RetrievedPassage,
)

_FAKE_EMBED_DIM = 64  # small but enough for UMAP/HDBSCAN on ~1.5k tickets

# Common English words carry no retrieval signal; dropped from the fake KB's
# lexical overlap score so topical matches are not drowned out by stopwords.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "with",
        "is", "are", "was", "were", "be", "been", "it", "this", "that", "these",
        "those", "i", "you", "my", "me", "we", "our", "your", "they", "them", "do",
        "does", "did", "can", "cannot", "could", "would", "should", "how", "what",
        "when", "where", "why", "which", "who", "if", "then", "so", "not", "no",
        "there", "here", "from", "by", "at", "as", "will", "want", "need", "please",
        "just", "about", "any", "some", "all", "get", "got", "have", "has", "had",
    }
)


class FakeObjectStore:
    """In-memory stand-in for S3. Keys -> bytes, with a fake ``s3://`` URI."""

    def __init__(self, bucket: str = "fake-bucket") -> None:
        self._bucket = bucket
        self._store: dict[str, bytes] = {}
        # Per-key metadata (mirrors S3 user metadata); content_hash powers the
        # skip-unchanged check (B3.2 step 8).
        self._meta: dict[str, dict[str, str]] = {}

    def _uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"

    def put_bytes(self, key: str, data: bytes) -> str:
        self._store[key] = data
        self._meta[key] = {"content_hash": hashlib.sha256(data).hexdigest()}
        return self._uri(key)

    def put_text(self, key: str, text: str) -> str:
        return self.put_bytes(key, text.encode("utf-8"))

    def get_bytes(self, key: str) -> bytes:
        return self._store[key]

    def get_text(self, key: str) -> str:
        return self._store[key].decode("utf-8")

    def exists(self, key: str) -> bool:
        return key in self._store

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self._store if k.startswith(prefix))

    # --- Part B additions ----------------------------------------------------

    def head(self, key: str) -> dict[str, str] | None:
        if key not in self._store:
            return None
        return dict(self._meta.get(key, {}))

    def copy(self, src_uri: str, dst_key: str) -> str:
        src_key = src_uri.split("/", 3)[-1] if src_uri.startswith("s3://") else src_uri
        self._store[dst_key] = self._store[src_key]
        self._meta[dst_key] = dict(self._meta.get(src_key, {}))
        return self._uri(dst_key)

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Conditional write: only succeeds if the key does not already exist."""
        if key in self._store:
            return False
        self.put_bytes(key, data)
        return True

    def list_uri(self, uri: str) -> list[str]:
        if uri.startswith("s3://"):
            _bucket, _, prefix = uri[len("s3://"):].partition("/")
        else:
            prefix = uri
        return [f"s3://{self._bucket}/{k}" for k in self.list_keys(prefix)]

    def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self._meta.pop(key, None)


class FakeBda:
    """Deterministic BDA stand-in: returns markdown derived from input bytes."""

    def __init__(self, store: FakeObjectStore) -> None:
        self._store = store

    def process_document(self, input_s3_uri: str, output_prefix: str) -> BdaResult:
        # Derive stable "extracted" markdown from the stored bytes so the same
        # input always yields the same output (content-hash cache can be tested).
        key = input_s3_uri.split("/", 3)[-1]
        raw = self._store.get_bytes(key) if self._store.exists(key) else input_s3_uri.encode()
        text = raw.decode("utf-8", errors="replace")
        pages = max(1, text.count("\f") + 1)
        markdown = f"# Extracted (fake BDA)\n\n{text.strip()}\n"
        out_key = f"{output_prefix}/output.md"
        out_uri = self._store.put_text(out_key, markdown)
        return BdaResult(
            markdown=markdown, pages_processed=pages, output_s3_uri=out_uri, cached=False
        )


class FakeEmbeddings:
    """Deterministic hashed bag-of-words embeddings (NOT a real model).

    Shared vocabulary -> similar vectors, which is enough for the planted intents
    to cluster. Vectors are L2-normalised so cosine behaves well.
    """

    _MODEL_ID = "fake-embeddings-v1"
    _token_re = re.compile(r"[a-z0-9]+")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * _FAKE_EMBED_DIM
        for token in self._token_re.findall(text.lower()):
            h = int(hashlib.sha256(token.encode()).hexdigest(), 16)
            idx = h % _FAKE_EMBED_DIM
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    @property
    def model_id(self) -> str:
        return self._MODEL_ID


class FakeLlm:
    """Rule-based LLM stand-in returning schema-valid JSON per prompt kind.

    The prompt templates embed a ``<<KIND:...>>`` marker (see config/prompts/);
    the fake reads it to decide the shape of the JSON to return. It performs no
    language understanding - it extracts the few fields it needs from the prompt
    deterministically so downstream schema validation and citation checks pass.
    """

    _MODEL_ID = "fake-llm-v1"
    _kind_re = re.compile(r"<<KIND:([a-z_]+)>>")
    _ticket_id_re = re.compile(r"\bT\d{5}\b")

    def invoke(
        self, prompt: str, *, max_tokens: int = 1024, temperature: float | None = None
    ) -> str:
        kind_match = self._kind_re.search(prompt)
        kind = kind_match.group(1) if kind_match else "unknown"
        handler = {
            "naming": self._naming,
            "categorise": self._categorise,
            "drafting": self._drafting,
            "coverage": self._coverage,
            "planner": self._planner,
        }.get(kind, self._unknown)
        return handler(prompt)

    def _naming(self, prompt: str) -> str:
        ids = self._ticket_id_re.findall(prompt)[:3]
        # Use the most frequent informative word as a readable name stand-in.
        name = _top_word(prompt) or "general_issue"
        return json.dumps(
            {
                "intent_name": name,
                "description": f"Tickets about {name.replace('_', ' ')}.",
                "coherence_flag": False,
                "example_ticket_ids": ids,
            }
        )

    def _categorise(self, prompt: str) -> str:
        # Return the first candidate label offered in the prompt, deterministically.
        m = re.search(r"candidates?:\s*\[([^\]]*)\]", prompt, re.IGNORECASE)
        label = "uncategorised"
        if m:
            first = m.group(1).split(",")[0].strip().strip("'\"")
            label = first or label
        return json.dumps({"intent": label})

    def _drafting(self, prompt: str) -> str:
        ids = self._ticket_id_re.findall(prompt)
        name = _top_word(prompt) or "issue"
        return json.dumps(
            {
                "title": f"How to resolve {name.replace('_', ' ')}",
                "problem": "Customers report this problem.",
                "applies_to": "All customers on the affected product.",
                "steps": ["Reproduce the issue.", "Apply the documented fix."],
                "resolution": "Follow the steps above to resolve the issue.",
                "source_ticket_ids": ids,
            }
        )

    def _coverage(self, prompt: str) -> str:
        # Covered only if the prompt carries actual retrieved passages. The empty
        # case renders "(no passages)"; the template's own instruction text
        # mentions "PASSAGES" so we must look for the real content marker
        # "PASSAGE: <text>" rather than the bare word.
        has_passages = "passage:" in prompt.lower() and "(no passages)" not in prompt.lower()
        return json.dumps(
            {
                "status": "covered" if has_passages else "gap",
                "reason": "Fake judge: passages present"
                if has_passages
                else "Fake judge: none",
            }
        )

    def _planner(self, prompt: str) -> str:
        return json.dumps({"action": "stop", "reason": "fake planner: nothing to do"})

    def _unknown(self, prompt: str) -> str:
        return json.dumps({"error": "unknown prompt kind"})

    @property
    def model_id(self) -> str:
        return self._MODEL_ID


class FakeGuardrail:
    """Guardrail stand-in that reuses the production regex PII layer (offline)."""

    def scan_pii(self, text: str) -> list[GuardrailPiiFinding]:
        from kb_agent.documents.pii import regex_pii_findings

        return [
            GuardrailPiiFinding(kind=f.kind, masked_sample=f.masked_sample)
            for f in regex_pii_findings(text)
        ]


class NoOpGuardrail:
    """Guardrail that finds nothing (freq-test: Guardrails disabled).

    Injected in --mode aws when ``guardrail_enabled`` is false so the Bedrock
    Guardrails API is never called. The offline regex PII layer in validate/
    drafting still runs independently of this, so PII is not entirely unchecked.
    """

    def scan_pii(self, text: str) -> list[GuardrailPiiFinding]:
        return []


class FakeKnowledgeBase:
    """In-memory KB: naive lexical retrieval over ingested passages.

    Good enough to exercise kb_create/ingest/retrieve, recall@k and the coverage
    check offline. Retrieval is Jaccard overlap on word sets - deterministic and
    dependency-free. NOT a vector store.
    """

    def __init__(self, store: FakeObjectStore) -> None:
        self._store = store
        self._passages: dict[str, list[RetrievedPassage]] = defaultdict(list)
        self._kbs: dict[str, str] = {}
        self._data_source_id: str | None = None
        self._last_ingestion_stats: dict | None = None
        # Part B provisioning state (create-or-verify).
        self._kb_info: dict[str, dict[str, str]] = {}
        self._ds_info: dict[str, dict[str, dict]] = {}
        self._tags: dict[str, dict[str, str]] = {}

    @property
    def data_source_id(self) -> str | None:
        return self._data_source_id

    @property
    def last_ingestion_stats(self) -> dict | None:
        return self._last_ingestion_stats

    def create(self, name: str, data_source_prefix: str, chunking_strategy: str) -> str:
        kb_id = f"fake-kb-{name}"
        self._kbs[kb_id] = data_source_prefix
        self._data_source_id = f"fake-ds-{name}"
        return kb_id

    def start_ingestion(self, kb_id: str) -> str:
        # Ingest = read every text object under the data-source prefix as a passage.
        prefix = self._kbs.get(kb_id, "")
        self._passages[kb_id] = []
        for key in self._store.list_keys(prefix):
            if key.endswith(".metadata.json"):
                continue
            try:
                text = self._store.get_text(key)
            except (UnicodeDecodeError, KeyError):
                continue
            self._passages[kb_id].append(
                RetrievedPassage(source_id=key, text=text, score=0.0)
            )
        return f"fake-job-{kb_id}"

    def wait_for_ingestion(self, kb_id: str, job_id: str, timeout_s: float = 1800.0) -> str:
        # Record deterministic document-level stats mirroring the real client's
        # last_ingestion_stats shape (spec v2 §5.5). No chunk count exists.
        indexed = len(self._passages.get(kb_id, []))
        self._last_ingestion_stats = {
            "scanned": indexed,
            "indexed": indexed,
            "modified": 0,
            "deleted": 0,
            "failed": 0,
        }
        return "COMPLETE"

    def sample_chunks(self, run_id: str, max_chunks: int = 5) -> dict:
        # Return a small sample of ingested passages for this run's KB. Post-filter
        # on run_id (the data-source prefix embeds it) so cross-run leakage cannot
        # happen even in fake mode (spec v2 §5.6).
        chunks: list[dict] = []
        for kb_id, prefix in self._kbs.items():
            if run_id not in prefix:
                continue
            for p in self._passages.get(kb_id, [])[:max_chunks]:
                chunks.append({"source_id": p.source_id, "text": p.text[:280]})
            break
        return {"chunks": chunks[:max_chunks]}

    def retrieve(self, kb_id: str, query: str, top_k: int) -> list[RetrievedPassage]:
        # Score = overlap coefficient on CONTENT words: fraction of the query's
        # content words that appear in the passage. This is a better lexical
        # stand-in for "does this passage answer the question" than Jaccard, which
        # unfairly penalises long documents (big union). Still NOT a semantic
        # model - it is a deterministic fake. (Chosen for retrieval fidelity, not
        # to hit any coverage threshold.)
        q = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _STOPWORDS}
        if not q:
            return []
        scored: list[RetrievedPassage] = []
        for p in self._passages.get(kb_id, []):
            words = {w for w in re.findall(r"[a-z0-9]+", p.text.lower()) if w not in _STOPWORDS}
            score = len(q & words) / len(q) if q else 0.0
            if score > 0:
                scored.append(p.model_copy(update={"score": round(score, 4)}))
        scored.sort(key=lambda p: p.score, reverse=True)
        return scored[:top_k]

    # --- Part B: create-or-verify provisioning (B4.2) ------------------------

    def find_knowledge_base(self, name: str) -> dict | None:
        for kb_id, info in self._kb_info.items():
            if info.get("name") == name:
                return {"id": kb_id, "name": name}
        return None

    def get_knowledge_base(self, kb_id: str) -> dict | None:
        info = self._kb_info.get(kb_id)
        if info is None:
            return None
        return {"id": kb_id, "status": "ACTIVE", **info}

    def list_data_sources(self, kb_id: str) -> list[dict]:
        return list(self._ds_info.get(kb_id, {}).values())

    def find_or_create_data_source(
        self, kb_id: str, name: str, *, inclusion_prefix: str,
        chunking_strategy: str, data_deletion_policy: str,
    ) -> dict:
        existing = self._ds_info.setdefault(kb_id, {})
        if name in existing:
            return {**existing[name], "_created": False}
        ds = {
            "id": f"fake-ds-{kb_id}-{name}", "name": name,
            "inclusion_prefix": inclusion_prefix,
            "chunking_strategy": chunking_strategy,
            "data_deletion_policy": data_deletion_policy,
        }
        existing[name] = ds
        self._data_source_id = ds["id"]
        self._kbs[kb_id] = inclusion_prefix
        return {**ds, "_created": True}

    def tag(self, arn: str, tags: dict[str, str]) -> None:
        self._tags.setdefault(arn, {}).update(tags)

    def delete_knowledge_base(self, kb_id: str) -> None:
        self._kb_info.pop(kb_id, None)
        self._ds_info.pop(kb_id, None)
        self._kbs.pop(kb_id, None)

    def delete_data_source(self, kb_id: str, data_source_id: str) -> None:
        dss = self._ds_info.get(kb_id, {})
        for name, ds in list(dss.items()):
            if ds["id"] == data_source_id:
                dss.pop(name)

    def create_kb_record(
        self, name: str, *, index_arn: str, embedding_model_arn: str
    ) -> str:
        """Create a KB (provisioning path) and remember its verifiable config."""
        kb_id = f"fake-kb-{name}"
        self._kb_info[kb_id] = {
            "name": name, "index_arn": index_arn,
            "embedding_model_arn": embedding_model_arn,
        }
        return kb_id


class FakeVectorStoreAdmin:
    """In-memory S3 Vectors control-plane. Enforces naming + immutability.

    Mirrors the real control plane just enough to exercise create-or-verify: a
    second ``create_index`` with different parameters is rejected (the real API
    cannot update them), and :meth:`get_index` returns the stored config so the
    provisioner can detect a mismatch and raise provisioning_conflict.
    """

    def __init__(self) -> None:
        self._buckets: set[str] = set()
        self._indexes: dict[tuple[str, str], dict] = {}
        self._tags: dict[str, dict[str, str]] = {}

    def ensure_vector_bucket(self, name: str) -> str:
        _assert_bucket_name(name)
        self._buckets.add(name)
        return f"arn:aws:s3vectors:::bucket/{name}"

    def get_index(self, bucket: str, name: str) -> dict | None:
        idx = self._indexes.get((bucket, name))
        return dict(idx) if idx else None

    def create_index(
        self, bucket: str, name: str, *, dimension: int, distance_metric: str,
        data_type: str, non_filterable_keys: list[str],
    ) -> dict:
        _assert_index_name(name)
        if bucket not in self._buckets:
            raise ValueError(f"vector bucket {bucket!r} does not exist")
        key = (bucket, name)
        config = {
            "arn": f"arn:aws:s3vectors:::bucket/{bucket}/index/{name}",
            "name": name, "dimension": dimension, "distance_metric": distance_metric,
            "data_type": data_type, "non_filterable_keys": list(non_filterable_keys),
        }
        existing = self._indexes.get(key)
        if existing is not None:
            # Immutable: a create against an existing index must match exactly.
            if any(existing[k] != config[k] for k in
                   ("dimension", "distance_metric", "data_type", "non_filterable_keys")):
                raise ValueError("index already exists with different immutable parameters")
            return dict(existing)
        self._indexes[key] = config
        return dict(config)

    def tag(self, arn: str, tags: dict[str, str]) -> None:
        self._tags.setdefault(arn, {}).update(tags)

    def delete_index(self, bucket: str, name: str) -> None:
        self._indexes.pop((bucket, name), None)


def _assert_bucket_name(name: str) -> None:
    import re as _re
    if not _re.match(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", name):
        raise ValueError(f"invalid vector bucket name {name!r}")


def _assert_index_name(name: str) -> None:
    import re as _re
    if not _re.match(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", name):
        raise ValueError(f"invalid vector index name {name!r}")


def _top_word(text: str) -> str:
    """Return the most frequent 'informative' token (len>=4) for naming stand-ins."""
    stop = {"this", "that", "with", "from", "your", "ticket", "tickets", "intent", "about"}
    counts: dict[str, int] = defaultdict(int)
    for tok in re.findall(r"[a-z]{4,}", text.lower()):
        if tok not in stop:
            counts[tok] += 1
    if not counts:
        return ""
    return max(counts, key=lambda k: counts[k])
