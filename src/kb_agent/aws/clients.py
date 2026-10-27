"""Real boto3-backed AWS clients for ``--mode aws`` (spec 7.4).

Each class implements a Protocol from :mod:`interfaces`. API shapes follow the
Part A verification (OPEN_QUESTIONS.md log). These are intentionally thin: retries
with capped exponential backoff on throttling, no business logic. They are only
exercised against real AWS (component tests, ``@pytest.mark.aws``); unit tests use
the fakes.

Model IDs, Region, guardrail IDs and the KB embedding model are injected from
config/settings - never hard-coded (spec 7.4). Several of those are still
``TODO(confirm)`` placeholders (Q6/Q7/Q11) and are flagged in config.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from kb_agent.aws.interfaces import BdaResult, GuardrailPiiFinding, RetrievedPassage
from kb_agent.exceptions import IngestionError, ParsingError
from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)


def _retry(
    fn: Callable[[], Any], *, attempts: int = 5, base_delay: float = 0.5, max_delay: float = 8.0
) -> Any:
    """Call ``fn`` with capped exponential backoff on throttling (spec 7.4).

    Only retries throttling/transient errors; other errors propagate immediately
    so real bugs are not masked.
    """
    from botocore.exceptions import ClientError

    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except ClientError as exc:  # pragma: no cover - needs live AWS to hit
            code = exc.response.get("Error", {}).get("Code", "")
            throttles = {
                "ThrottlingException",
                "TooManyRequestsException",
                "ServiceQuotaExceededException",
            }
            if code not in throttles:
                raise
            last_exc = exc
            delay = min(max_delay, base_delay * (2**attempt))
            logger.warning("aws_throttled_retry", attempt=attempt, code=code, delay=delay)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


class S3ObjectStore:
    """S3-backed object store scoped to one bucket (spec 2.5 S3-only state)."""

    def __init__(self, s3_client: Any, bucket: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket

    def _uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"

    def put_bytes(self, key: str, data: bytes) -> str:
        # Store the content hash as S3 user metadata so head()/skip-unchanged can
        # compare without re-downloading the object (B3.2 step 8).
        chash = hashlib.sha256(data).hexdigest()
        _retry(lambda: self._s3.put_object(
            Bucket=self._bucket, Key=key, Body=data,
            Metadata={"content_hash": chash},
        ))
        return self._uri(key)

    def put_text(self, key: str, text: str) -> str:
        return self.put_bytes(key, text.encode("utf-8"))

    def get_bytes(self, key: str) -> bytes:
        resp = _retry(lambda: self._s3.get_object(Bucket=self._bucket, Key=key))
        return resp["Body"].read()

    def get_text(self, key: str) -> str:
        return self.get_bytes(key).decode("utf-8")

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = _retry(partial(self._s3.list_objects_v2, **kwargs))
            keys.extend(obj["Key"] for obj in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return sorted(keys)

    # --- Part B additions ----------------------------------------------------

    @staticmethod
    def _split(uri: str) -> tuple[str, str]:
        """Split ``s3://bucket/key`` -> (bucket, key)."""
        rest = uri[len("s3://"):]
        bucket, _, key = rest.partition("/")
        return bucket, key

    def head(self, key: str) -> dict[str, str] | None:  # pragma: no cover
        from botocore.exceptions import ClientError

        try:
            resp = _retry(lambda: self._s3.head_object(Bucket=self._bucket, Key=key))
        except ClientError:
            return None
        return dict(resp.get("Metadata", {}))

    def copy(self, src_uri: str, dst_key: str) -> str:  # pragma: no cover
        if src_uri.startswith("s3://"):
            src_bucket, src_key = self._split(src_uri)
        else:
            src_bucket, src_key = self._bucket, src_uri
        _retry(lambda: self._s3.copy_object(
            Bucket=self._bucket, Key=dst_key,
            CopySource={"Bucket": src_bucket, "Key": src_key},
        ))
        return self._uri(dst_key)

    def put_if_absent(self, key: str, data: bytes) -> bool:  # pragma: no cover
        """Conditional write with If-None-Match: * (S3 conditional writes, 2024).

        Returns True if written; False if the key already existed (HTTP 412
        PreconditionFailed). The primitive behind the project lock (B4.4).
        """
        from botocore.exceptions import ClientError

        try:
            _retry(lambda: self._s3.put_object(
                Bucket=self._bucket, Key=key, Body=data, IfNoneMatch="*",
            ))
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "412"):
                return False
            raise

    def list_uri(self, uri: str) -> list[str]:  # pragma: no cover
        bucket, prefix = self._split(uri) if uri.startswith("s3://") else (self._bucket, uri)
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = _retry(partial(self._s3.list_objects_v2, **kwargs))
            keys.extend(obj["Key"] for obj in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return [f"s3://{bucket}/{k}" for k in sorted(keys)]

    def delete(self, key: str) -> None:  # pragma: no cover
        _retry(lambda: self._s3.delete_object(Bucket=self._bucket, Key=key))


class S3VectorsAdmin:  # pragma: no cover
    """Real S3 Vectors control-plane over the boto3 ``s3vectors`` client (B4.2).

    Create-or-verify only; never updates an existing index (its dimension /
    metric / non-filterable keys are immutable). ``get_index`` returns the stored
    config so the provisioner can detect a mismatch.
    """

    def __init__(self, s3vectors_client: Any) -> None:
        self._c = s3vectors_client

    def ensure_vector_bucket(self, name: str) -> str:
        from botocore.exceptions import ClientError

        try:
            resp = _retry(lambda: self._c.get_vector_bucket(vectorBucketName=name))
            return resp.get("vectorBucket", {}).get("vectorBucketArn", name)
        except ClientError:
            # Create; tolerate a concurrent-create conflict by re-reading.
            try:
                _retry(lambda: self._c.create_vector_bucket(vectorBucketName=name))
            except ClientError:
                pass
            resp = _retry(lambda: self._c.get_vector_bucket(vectorBucketName=name))
            return resp.get("vectorBucket", {}).get("vectorBucketArn", name)

    def get_index(self, bucket: str, name: str) -> dict | None:
        from botocore.exceptions import ClientError

        try:
            resp = _retry(lambda: self._c.get_index(vectorBucketName=bucket, indexName=name))
        except ClientError:
            return None
        idx = resp.get("index", {})
        md = idx.get("metadataConfiguration", {})
        return {
            "arn": idx.get("indexArn"),
            "name": idx.get("indexName", name),
            "dimension": idx.get("dimension"),
            "distance_metric": idx.get("distanceMetric"),
            "data_type": idx.get("dataType"),
            "non_filterable_keys": list(md.get("nonFilterableMetadataKeys", [])),
        }

    def create_index(
        self, bucket: str, name: str, *, dimension: int, distance_metric: str,
        data_type: str, non_filterable_keys: list[str],
    ) -> dict:
        _retry(lambda: self._c.create_index(
            vectorBucketName=bucket, indexName=name,
            dataType=data_type, dimension=dimension, distanceMetric=distance_metric,
            metadataConfiguration={"nonFilterableMetadataKeys": list(non_filterable_keys)},
        ))
        got = self.get_index(bucket, name)
        if got is None:
            raise RuntimeError(f"index {name} not found immediately after create")
        return got

    def tag(self, arn: str, tags: dict[str, str]) -> None:
        from botocore.exceptions import ClientError

        try:
            _retry(lambda: self._c.tag_resource(
                resourceArn=arn, tags=dict(tags)))
        except ClientError:
            pass  # tagging is best-effort; never fail provisioning on it

    def delete_index(self, bucket: str, name: str) -> None:
        _retry(lambda: self._c.delete_index(vectorBucketName=bucket, indexName=name))


class BedrockDataAutomation:
    """BDA documents client: async invoke + poll (Part A item 1).

    Uses ``InvokeDataAutomationAsync`` with a project ARN and polls
    ``GetDataAutomationStatus``. Reads the resulting markdown from S3.
    """

    def __init__(
        self,
        bda_runtime_client: Any,
        store: S3ObjectStore,
        *,
        project_arn: str,
        profile_arn: str,
        poll_interval_s: float = 5.0,
        timeout_s: float = 900.0,
    ) -> None:
        self._client = bda_runtime_client
        self._store = store
        self._project_arn = project_arn
        # Current BDA requires a data-automation PROFILE arn on invoke (for
        # cross-region inference), in addition to the project arn.
        self._profile_arn = profile_arn
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s

    def process_document(  # pragma: no cover
        self, input_s3_uri: str, output_prefix: str
    ) -> BdaResult:
        output_uri = f"s3://{self._store._bucket}/{output_prefix}"
        resp = _retry(
            lambda: self._client.invoke_data_automation_async(
                inputConfiguration={"s3Uri": input_s3_uri},
                outputConfiguration={"s3Uri": output_uri},
                dataAutomationConfiguration={"dataAutomationProjectArn": self._project_arn},
                dataAutomationProfileArn=self._profile_arn,
            )
        )
        invocation_arn = resp["invocationArn"]
        deadline = time.monotonic() + self._timeout_s
        while True:
            status = _retry(
                lambda: self._client.get_data_automation_status(invocationArn=invocation_arn)
            )
            state = status.get("status")
            if state == "Success":
                break
            if state in {"ServiceError", "ClientError"}:
                raise ParsingError(f"BDA failed for {input_s3_uri}: {status}")
            if time.monotonic() > deadline:
                raise ParsingError(f"BDA timed out for {input_s3_uri}")
            time.sleep(self._poll_interval_s)
        # JSON+files output: locate the markdown file under the output prefix.
        md_keys = [k for k in self._store.list_keys(output_prefix) if k.endswith(".md")]
        if not md_keys:
            raise ParsingError(f"BDA produced no markdown for {input_s3_uri}")
        markdown = self._store.get_text(md_keys[0])
        pages = int(status.get("pagesProcessed", markdown.count("\f") + 1))
        return BdaResult(
            markdown=markdown, pages_processed=pages, output_s3_uri=output_uri, cached=False
        )


class BedrockEmbeddings:
    """Bedrock text-embeddings via InvokeModel (spec 4.3)."""

    def __init__(self, bedrock_runtime_client: Any, model_id: str) -> None:
        self._client = bedrock_runtime_client
        self._model_id = model_id

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        out: list[list[float]] = []
        for text in texts:
            body = json.dumps({"inputText": text})
            resp = _retry(
                partial(self._client.invoke_model, modelId=self._model_id, body=body)
            )
            payload = json.loads(resp["body"].read())
            out.append(payload["embedding"])
        return out

    @property
    def model_id(self) -> str:
        return self._model_id


class BedrockLlm:
    """Bedrock Claude client via InvokeModel (spec 2.1 permitted LLM uses)."""

    def __init__(self, bedrock_runtime_client: Any, model_id: str) -> None:
        self._client = bedrock_runtime_client
        self._model_id = model_id

    def invoke(  # pragma: no cover
        self, prompt: str, *, max_tokens: int = 1024, temperature: float | None = None
    ) -> str:
        payload_body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        # Newer Claude models (e.g. claude-sonnet-5) reject ``temperature``:
        # "temperature is deprecated for this model". Only send it when a caller
        # explicitly asks for a non-default value; otherwise let the model decide.
        if temperature is not None:
            payload_body["temperature"] = temperature
        body = json.dumps(payload_body)
        resp = _retry(lambda: self._client.invoke_model(modelId=self._model_id, body=body))
        payload = json.loads(resp["body"].read())
        # Claude on Bedrock returns a list of content blocks. Newer models can
        # emit non-text blocks (e.g. a reasoning/thinking block) before the text
        # block, so pick the first block that actually carries text rather than
        # blindly indexing [0] (which raised KeyError: 'text').
        blocks = payload.get("content", []) or []
        for block in blocks:
            if isinstance(block, dict) and "text" in block:
                return block["text"]
        # No text block (e.g. the token budget was spent on a reasoning block
        # and truncated before any answer). Don't fail the whole run: return an
        # empty string so the caller's JSON-parse fallback handles it, and log
        # the cause so it is diagnosable.
        stop = payload.get("stop_reason")
        logger.warning(
            "llm_no_text_content",
            stop_reason=stop,
            blocks=[b.get("type") for b in blocks if isinstance(b, dict)],
        )
        return ""

    @property
    def model_id(self) -> str:
        return self._model_id


class BedrockGuardrail:
    """Bedrock Guardrails PII filter via ApplyGuardrail (spec 4.2/4.4)."""

    def __init__(self, bedrock_runtime_client: Any, guardrail_id: str, version: str) -> None:
        self._client = bedrock_runtime_client
        self._guardrail_id = guardrail_id
        self._version = version

    def scan_pii(self, text: str) -> list[GuardrailPiiFinding]:  # pragma: no cover
        resp = _retry(
            lambda: self._client.apply_guardrail(
                guardrailIdentifier=self._guardrail_id,
                guardrailVersion=self._version,
                source="INPUT",
                content=[{"text": {"text": text}}],
            )
        )
        findings: list[GuardrailPiiFinding] = []
        for assessment in resp.get("assessments", []):
            for pii in assessment.get("sensitiveInformationPolicy", {}).get("piiEntities", []):
                findings.append(
                    GuardrailPiiFinding(kind=pii.get("type", "UNKNOWN"), masked_sample="[REDACTED]")
                )
        return findings


class BedrockKnowledgeBase:
    """Bedrock Knowledge Base client (Part A item 2): create/ingest/retrieve.

    ``agent_client`` is bedrock-agent (build-time: create KB/data source, start
    ingestion). ``agent_runtime_client`` is bedrock-agent-runtime (retrieve).
    """

    def __init__(
        self,
        agent_client: Any,
        agent_runtime_client: Any,
        *,
        bucket: str,
        role_arn: str,
        embedding_model_arn: str,
        vector_store_config: dict[str, Any],
        poll_interval_s: float = 5.0,
    ) -> None:
        self._agent = agent_client
        self._runtime = agent_runtime_client
        self._bucket = bucket
        self._role_arn = role_arn
        self._embedding_model_arn = embedding_model_arn
        self._vector_store_config = vector_store_config
        self._poll_interval_s = poll_interval_s
        self._data_source_id: str | None = None

    def create(  # pragma: no cover
        self, name: str, data_source_prefix: str, chunking_strategy: str
    ) -> str:
        kb = _retry(
            lambda: self._agent.create_knowledge_base(
                name=name,
                roleArn=self._role_arn,
                knowledgeBaseConfiguration={
                    "type": "VECTOR",
                    "vectorKnowledgeBaseConfiguration": {
                        "embeddingModelArn": self._embedding_model_arn
                    },
                },
                storageConfiguration=self._vector_store_config,
            )
        )
        kb_id = kb["knowledgeBase"]["knowledgeBaseId"]
        # A freshly created KB is CREATING for a few seconds. Creating the data
        # source and (especially) starting ingestion against a CREATING KB fails
        # with ConflictException, so wait for ACTIVE before proceeding.
        self._wait_for_kb_active(kb_id)
        chunking = _chunking_config(chunking_strategy)
        ds = _retry(
            lambda: self._agent.create_data_source(
                knowledgeBaseId=kb_id,
                name=f"{name}-s3",
                dataSourceConfiguration={
                    "type": "S3",
                    "s3Configuration": {
                        "bucketArn": f"arn:aws:s3:::{self._bucket}",
                        "inclusionPrefixes": [data_source_prefix],
                    },
                },
                vectorIngestionConfiguration={"chunkingConfiguration": chunking},
                # Set the deletion policy EXPLICITLY (spec v2 §5.8): DELETE means
                # removing the data source also removes its vectors from the index,
                # which cleanup (scripts/cleanup_kbs.py) relies on. The previous
                # default was left implicit. TODO(confirm) V4: exact param name /
                # accepted values against current Bedrock docs.
                dataDeletionPolicy="DELETE",
            )
        )
        self._data_source_id = ds["dataSource"]["dataSourceId"]
        return kb_id

    @property
    def data_source_id(self) -> str | None:
        """The S3 data source id, needed by the chunk-sample filter (spec v2 §5.6)."""
        return self._data_source_id

    def _wait_for_kb_active(  # pragma: no cover
        self, kb_id: str, timeout_s: float = 300.0
    ) -> None:
        """Poll the KB until it leaves CREATING (reaches ACTIVE).

        Raises IngestionError if the KB ends in a failed state or does not become
        ACTIVE within ``timeout_s``.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            resp = _retry(lambda: self._agent.get_knowledge_base(knowledgeBaseId=kb_id))
            status = resp["knowledgeBase"]["status"]
            if status == "ACTIVE":
                return
            if status in {"FAILED", "DELETING", "DELETE_UNSUCCESSFUL"}:
                raise IngestionError(f"KnowledgeBase {kb_id} entered status {status}")
            if time.monotonic() > deadline:
                raise IngestionError(
                    f"KnowledgeBase {kb_id} not ACTIVE after {timeout_s}s (status {status})"
                )
            time.sleep(self._poll_interval_s)

    def start_ingestion(self, kb_id: str) -> str:  # pragma: no cover
        if self._data_source_id is None:
            raise IngestionError("create() must run before start_ingestion()")
        resp = _retry(
            lambda: self._agent.start_ingestion_job(
                knowledgeBaseId=kb_id, dataSourceId=self._data_source_id
            )
        )
        return resp["ingestionJob"]["ingestionJobId"]

    def wait_for_ingestion(  # pragma: no cover
        self, kb_id: str, job_id: str, timeout_s: float = 1800.0
    ) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            resp = _retry(
                lambda: self._agent.get_ingestion_job(
                    knowledgeBaseId=kb_id, dataSourceId=self._data_source_id, ingestionJobId=job_id
                )
            )
            job = resp["ingestionJob"]
            status = job["status"]
            if status in {"COMPLETE", "FAILED"}:
                # Stash the job's document-level statistics for _state.json
                # (spec v2 §5.5). Bedrock returns documents scanned/indexed/
                # modified/failed — NOT a chunk count, so chunks_written stays null.
                self._last_ingestion_stats = _map_ingestion_stats(job.get("statistics", {}))
                if status == "FAILED":
                    raise IngestionError(f"Ingestion job {job_id} failed")
                return status
            if time.monotonic() > deadline:
                raise IngestionError(f"Ingestion job {job_id} timed out")
            time.sleep(self._poll_interval_s)

    @property
    def last_ingestion_stats(self) -> dict[str, Any] | None:
        return getattr(self, "_last_ingestion_stats", None)

    def sample_chunks(self, run_id: str, max_chunks: int = 5) -> dict[str, Any]:  # pragma: no cover
        """Precompute a small chunk sample for this run (spec v2 §5.6).

        Query the vector index filtered on the reserved data-source-id metadata
        key (verify V3: it should be filterable; it is absent from the index's
        nonFilterableMetadataKeys). Post-filter in code on this run's run_id so a
        chunk from another run can never leak. On any failure return an empty
        sample — a missing sample must never fail the run.
        """
        try:
            return self._sample_chunks_impl(run_id, max_chunks)
        except Exception as exc:  # noqa: BLE001 - never fail the run
            logger.warning("sample_chunks_failed", run_id=run_id, error=str(exc))
            return {"chunks": [], "error": "unavailable"}

    def _sample_chunks_impl(  # pragma: no cover
        self, run_id: str, max_chunks: int
    ) -> dict[str, Any]:
        # The actual S3 Vectors query/embedding is deferred to a hook the real
        # client set injects; absent that, return an empty-but-valid sample. This
        # keeps the method importable/testable offline and avoids coupling the KB
        # stage to a specific S3 Vectors client surface we have not yet verified.
        return {"chunks": [], "error": "unavailable", "method": "unimplemented"}

    def retrieve(  # pragma: no cover
        self, kb_id: str, query: str, top_k: int
    ) -> list[RetrievedPassage]:
        resp = _retry(
            lambda: self._runtime.retrieve(
                knowledgeBaseId=kb_id,
                retrievalQuery={"text": query},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {"numberOfResults": top_k}
                },
            )
        )
        passages: list[RetrievedPassage] = []
        for r in resp.get("retrievalResults", []):
            loc = r.get("location", {}).get("s3Location", {}).get("uri", "unknown")
            passages.append(
                RetrievedPassage(
                    source_id=loc,
                    text=r.get("content", {}).get("text", ""),
                    score=float(r.get("score", 0.0)),
                    metadata={k: str(v) for k, v in r.get("metadata", {}).items()},
                )
            )
        return passages

    # --- Part B: create-or-verify provisioning (B4.2) ------------------------

    def find_knowledge_base(self, name: str) -> dict | None:  # pragma: no cover
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = _retry(partial(self._agent.list_knowledge_bases, **kwargs))
            for s in resp.get("knowledgeBaseSummaries", []):
                if s.get("name") == name:
                    return {"id": s["knowledgeBaseId"], "name": name, "status": s.get("status")}
            token = resp.get("nextToken")
            if not token:
                return None

    def get_knowledge_base(self, kb_id: str) -> dict | None:  # pragma: no cover
        from botocore.exceptions import ClientError

        try:
            resp = _retry(lambda: self._agent.get_knowledge_base(knowledgeBaseId=kb_id))
        except ClientError:
            return None
        kb = resp["knowledgeBase"]
        storage = kb.get("storageConfiguration", {}).get("s3VectorsConfiguration", {})
        vector_cfg = kb.get("knowledgeBaseConfiguration", {}).get(
            "vectorKnowledgeBaseConfiguration", {}
        )
        return {
            "id": kb_id,
            "name": kb.get("name"),
            "status": kb.get("status"),
            "index_arn": storage.get("indexArn"),
            "embedding_model_arn": vector_cfg.get("embeddingModelArn"),
        }

    def list_data_sources(self, kb_id: str) -> list[dict]:  # pragma: no cover
        out: list[dict] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"knowledgeBaseId": kb_id, "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = _retry(partial(self._agent.list_data_sources, **kwargs))
            for s in resp.get("dataSourceSummaries", []):
                out.append({"id": s["dataSourceId"], "name": s.get("name")})
            token = resp.get("nextToken")
            if not token:
                return out

    def find_or_create_data_source(  # pragma: no cover
        self, kb_id: str, name: str, *, inclusion_prefix: str,
        chunking_strategy: str, data_deletion_policy: str,
    ) -> dict:
        for ds in self.list_data_sources(kb_id):
            if ds["name"] == name:
                self._data_source_id = ds["id"]
                return ds
        resp = _retry(lambda: self._agent.create_data_source(
            knowledgeBaseId=kb_id, name=name,
            dataSourceConfiguration={
                "type": "S3",
                "s3Configuration": {
                    "bucketArn": f"arn:aws:s3:::{self._bucket}",
                    "inclusionPrefixes": [inclusion_prefix],
                },
            },
            vectorIngestionConfiguration={
                "chunkingConfiguration": _chunking_config(chunking_strategy)
            },
            dataDeletionPolicy=data_deletion_policy,
        ))
        ds = resp["dataSource"]
        self._data_source_id = ds["dataSourceId"]
        return {"id": ds["dataSourceId"], "name": name}

    def create_kb_record(  # pragma: no cover
        self, name: str, *, index_arn: str, embedding_model_arn: str
    ) -> str:
        """Create a KB bound to an existing S3 Vectors index; wait ACTIVE."""
        kb = _retry(lambda: self._agent.create_knowledge_base(
            name=name, roleArn=self._role_arn,
            knowledgeBaseConfiguration={
                "type": "VECTOR",
                "vectorKnowledgeBaseConfiguration": {"embeddingModelArn": embedding_model_arn},
            },
            storageConfiguration={
                "type": "S3_VECTORS",
                "s3VectorsConfiguration": {"indexArn": index_arn},
            },
        ))
        kb_id = kb["knowledgeBase"]["knowledgeBaseId"]
        self._wait_for_kb_active(kb_id)
        return kb_id

    def tag(self, arn: str, tags: dict[str, str]) -> None:  # pragma: no cover
        from botocore.exceptions import ClientError

        try:
            _retry(lambda: self._agent.tag_resource(resourceArn=arn, tags=dict(tags)))
        except ClientError:
            pass  # best-effort

    def delete_knowledge_base(self, kb_id: str) -> None:  # pragma: no cover
        _retry(lambda: self._agent.delete_knowledge_base(knowledgeBaseId=kb_id))

    def delete_data_source(self, kb_id: str, data_source_id: str) -> None:  # pragma: no cover
        _retry(lambda: self._agent.delete_data_source(
            knowledgeBaseId=kb_id, dataSourceId=data_source_id))


def _map_ingestion_stats(statistics: dict[str, Any]) -> dict[str, Any]:
    """Map a Bedrock ingestion job's ``statistics`` to our _state shape (spec v2 §5.5).

    Bedrock's ``get_ingestion_job`` returns document-level counts only (no chunk
    count), so the caller keeps ``chunks_written`` null. The exact statistics key
    names are verified against the Bedrock agent API (TODO(confirm) V8). Missing
    keys default to 0 so a partial statistics map never raises.
    """
    return {
        "scanned": int(statistics.get("numberOfDocumentsScanned", 0) or 0),
        "indexed": int(statistics.get("numberOfNewDocumentsIndexed", 0) or 0),
        "modified": int(statistics.get("numberOfModifiedDocumentsIndexed", 0) or 0),
        "deleted": int(statistics.get("numberOfDocumentsDeleted", 0) or 0),
        "failed": int(statistics.get("numberOfDocumentsFailed", 0) or 0),
    }


def _chunking_config(strategy: str) -> dict[str, Any]:
    """Map our chunking strategy name to the KB chunkingConfiguration (Part A)."""
    if strategy == "hierarchical":
        return {
            "chunkingStrategy": "HIERARCHICAL",
            "hierarchicalChunkingConfiguration": {
                "levelConfigurations": [{"maxTokens": 1500}, {"maxTokens": 300}],
                "overlapTokens": 60,
            },
        }
    if strategy == "semantic":
        return {
            "chunkingStrategy": "SEMANTIC",
            "semanticChunkingConfiguration": {
                "maxTokens": 300,
                "bufferSize": 0,
                "breakpointPercentileThreshold": 95,
            },
        }
    if strategy == "fixed_size":
        return {
            "chunkingStrategy": "FIXED_SIZE",
            "fixedSizeChunkingConfiguration": {"maxTokens": 300, "overlapPercentage": 20},
        }
    return {"chunkingStrategy": "NONE"}
