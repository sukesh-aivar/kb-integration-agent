"""Protocols for every AWS-backed capability (spec 7.4 dependency injection).

Pipeline stages accept these Protocols, not concrete boto3 clients. That is what
lets ``--mode fake`` (in-memory, offline, deterministic) and ``--mode aws`` (real
services) share identical pipeline code - only the injected implementation
differs. Keeping the surface small and typed also keeps the IAM policy
least-privilege: the methods here map directly to the API actions we need.

All method signatures reflect the Part A verification (see OPEN_QUESTIONS.md
verification log): BDA async + poll, KB ingestion polled to completion, metadata
sidecars as ``<filename>.metadata.json``, embeddings + Claude via Bedrock.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    """Minimal S3-like object store.

    Inputs arrive as S3 references and all durable state goes to S3 keyed by
    ``run_id`` (spec 2.5, 3). Keys are plain strings; implementations map them to
    a bucket.
    """

    def put_bytes(self, key: str, data: bytes) -> str:
        """Store raw bytes at ``key``; return the resource URI (e.g. s3://...)."""
        ...

    def put_text(self, key: str, text: str) -> str:
        """Store UTF-8 text at ``key``; return the resource URI."""
        ...

    def get_bytes(self, key: str) -> bytes:
        """Fetch raw bytes stored at ``key``."""
        ...

    def get_text(self, key: str) -> str:
        """Fetch UTF-8 text stored at ``key``."""
        ...

    def exists(self, key: str) -> bool:
        """Return whether an object exists at ``key``."""
        ...

    def list_keys(self, prefix: str) -> list[str]:
        """List object keys under ``prefix``."""
        ...

    # --- Part B additions (cross-bucket, conditional, server-side copy) ------

    def head(self, key: str) -> dict[str, str] | None:
        """Return object metadata (incl. ``content_hash`` if stored), or None.

        Used for the skip-unchanged check (B3.2 step 8). None means no object.
        """
        ...

    def copy(self, src_uri: str, dst_key: str) -> str:
        """Server-side copy ``s3://bucket/key`` -> ``dst_key`` in the data bucket."""
        ...

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Conditional write (If-None-Match: *). True if written, False if the key
        already existed. The primitive behind the project lock (B4.4)."""
        ...

    def list_uri(self, uri: str) -> list[str]:
        """List full ``s3://bucket/key`` URIs under a prefix uri (cross-bucket)."""
        ...

    def delete(self, key: str) -> None:
        """Delete the object at ``key`` (used to release the project lock)."""
        ...


@runtime_checkable
class VectorStoreAdmin(Protocol):
    """S3 Vectors control-plane (Part B B4.2). Create-or-verify, never mutate.

    After an index is created its dimension, distance metric and non-filterable
    keys are IMMUTABLE; :meth:`get_index` lets the provisioner VERIFY a returning
    project's index matches and raise ``provisioning_conflict`` on a mismatch
    instead of trying (and failing) to update it.
    """

    def ensure_vector_bucket(self, name: str) -> str:
        """Create the vector bucket if missing (idempotent); return its name/arn."""
        ...

    def get_index(self, bucket: str, name: str) -> dict | None:
        """Return the index config (dimension/metric/data_type/non_filterable_keys
        /arn) or None if it does not exist."""
        ...

    def create_index(
        self,
        bucket: str,
        name: str,
        *,
        dimension: int,
        distance_metric: str,
        data_type: str,
        non_filterable_keys: list[str],
    ) -> dict:
        """Create a vector index; return its config incl. ``arn``."""
        ...

    def tag(self, arn: str, tags: dict[str, str]) -> None:
        """Tag a vector resource (best-effort; cleanup relies on tags)."""
        ...

    def delete_index(self, bucket: str, name: str) -> None:
        """Delete a vector index (cleanup script only)."""
        ...


@runtime_checkable
class BdaClient(Protocol):
    """Bedrock Data Automation (documents) - async invoke + poll (Part A item 1).

    The real client calls ``InvokeDataAutomationAsync`` and polls
    ``GetDataAutomationStatus``; output (markdown via the JSON+files setting) lands
    in S3. The fake returns deterministic markdown so routing/normalisation can be
    exercised offline.
    """

    def process_document(self, input_s3_uri: str, output_prefix: str) -> BdaResult:
        """Process one document; block until complete; return the result."""
        ...


@runtime_checkable
class EmbeddingsClient(Protocol):
    """Bedrock text-embeddings client (spec 4.3 embed_texts)."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text, order-preserving."""
        ...

    @property
    def model_id(self) -> str:
        """The embedding model id, recorded in the run manifest (spec 2.3)."""
        ...


@runtime_checkable
class LlmClient(Protocol):
    """Bedrock LLM (Claude) client for the five permitted LLM uses (spec 2.1).

    ``invoke`` returns raw text; callers parse structured output and validate it
    against a schema (spec 6). ``prompt_version`` is recorded per call.
    """

    def invoke(
        self, prompt: str, *, max_tokens: int = 1024, temperature: float | None = None
    ) -> str:
        """Run a single completion.

        ``temperature`` defaults to ``None`` (omitted from the request) because
        newer Claude models reject the parameter; pass a float only when a model
        still accepts it and you need a non-default value.
        """
        ...

    @property
    def model_id(self) -> str:
        """The LLM model id, recorded in the run manifest (spec 2.3)."""
        ...


@runtime_checkable
class GuardrailClient(Protocol):
    """Bedrock Guardrails PII filter (spec 4.2 validate, 4.4 drafting re-scan).

    Paired with a regex layer in code; this is the contextual detector.
    """

    def scan_pii(self, text: str) -> list[GuardrailPiiFinding]:
        """Return PII findings for ``text`` (empty list == clean)."""
        ...


@runtime_checkable
class KnowledgeBaseClient(Protocol):
    """Bedrock Knowledge Base lifecycle (spec 4.2 kb_create/ingest/retrieve).

    Mirrors the verified API shape: create KB + S3 data source with a chunking
    strategy, start an async ingestion job, poll it to completion, then retrieve.
    """

    def create(self, name: str, data_source_prefix: str, chunking_strategy: str) -> str:
        """Create (or reuse) a KB + data source; return the knowledge base id."""
        ...

    def start_ingestion(self, kb_id: str) -> str:
        """Start an async ingestion job; return the job id."""
        ...

    def wait_for_ingestion(self, kb_id: str, job_id: str, timeout_s: float = 1800.0) -> str:
        """Poll ingestion to completion; return terminal status (COMPLETE/FAILED).

        Polling (not a push event) is the verified mechanism (Part A item 2);
        kept behind this method so an EventBridge-based approach can replace it
        without touching pipeline code.
        """
        ...

    def retrieve(self, kb_id: str, query: str, top_k: int) -> list[RetrievedPassage]:
        """Return the top-k passages for ``query`` (recall@k uses this)."""
        ...

    # --- Part B: create-or-verify provisioning (B4.2) ------------------------

    def find_knowledge_base(self, name: str) -> dict | None:
        """Find a KB by exact name (paginated search); return its summary or None."""
        ...

    def get_knowledge_base(self, kb_id: str) -> dict | None:
        """Fetch a KB by id; return its config (storage index arn, embedding
        model, status) or None if it does not exist."""
        ...

    def list_data_sources(self, kb_id: str) -> list[dict]:
        """List the KB's data sources (id, name, inclusion prefixes, chunking)."""
        ...

    def find_or_create_data_source(
        self,
        kb_id: str,
        name: str,
        *,
        inclusion_prefix: str,
        chunking_strategy: str,
        data_deletion_policy: str,
    ) -> dict:
        """Return the matching data source, creating it if absent; verify config."""
        ...

    def tag(self, arn: str, tags: dict[str, str]) -> None:
        """Tag a KB/data-source resource (best-effort)."""
        ...

    def delete_knowledge_base(self, kb_id: str) -> None:
        """Delete a KB (cleanup script only)."""
        ...

    def delete_data_source(self, kb_id: str, data_source_id: str) -> None:
        """Delete a data source (cleanup script only)."""
        ...


# --- Lightweight result types (shared by fake + real) -----------------------
# Defined here (not models.py) because they are AWS-boundary DTOs, not domain
# objects. Importing lazily avoids a cycle with models.

from pydantic import BaseModel, Field  # noqa: E402


class BdaResult(BaseModel):
    """Result of a BDA document job."""

    markdown: str
    pages_processed: int
    output_s3_uri: str
    cached: bool = False


class GuardrailPiiFinding(BaseModel):
    """A PII hit reported by Guardrails."""

    kind: str
    masked_sample: str = ""


class RetrievedPassage(BaseModel):
    """One retrieved passage from the knowledge base."""

    source_id: str
    text: str
    score: float
    metadata: dict[str, str] = Field(default_factory=dict)
