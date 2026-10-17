"""The ClientSet: the single bundle of injected AWS capabilities (spec 7.4).

Pipeline stages receive one :class:`ClientSet` and read the protocol-typed
clients from it. :func:`build_client_set` is the only place that decides between
fake and real implementations, so ``--mode fake`` and ``--mode aws`` differ in
exactly one function and share all pipeline code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kb_agent.aws.interfaces import (
    BdaClient,
    EmbeddingsClient,
    GuardrailClient,
    KnowledgeBaseClient,
    LlmClient,
    ObjectStore,
)
from kb_agent.config import AppConfig, Settings

Mode = Literal["fake", "aws"]


@dataclass
class ClientSet:
    """All AWS-backed capabilities a run needs, as injected protocol objects."""

    mode: Mode
    store: ObjectStore
    bda: BdaClient
    embeddings: EmbeddingsClient
    llm: LlmClient
    guardrail: GuardrailClient
    knowledge_base: KnowledgeBaseClient


def build_fake_client_set(bucket: str = "fake-bucket") -> ClientSet:
    """Build an all-fakes client set for offline, deterministic runs/tests."""
    from kb_agent.aws.fakes import (
        FakeBda,
        FakeEmbeddings,
        FakeGuardrail,
        FakeKnowledgeBase,
        FakeLlm,
        FakeObjectStore,
    )

    store = FakeObjectStore(bucket=bucket)
    return ClientSet(
        mode="fake",
        store=store,
        bda=FakeBda(store),
        embeddings=FakeEmbeddings(),
        llm=FakeLlm(),
        guardrail=FakeGuardrail(),
        knowledge_base=FakeKnowledgeBase(store),
    )


def build_aws_client_set(settings: Settings, config: AppConfig) -> ClientSet:  # pragma: no cover
    """Build a real boto3-backed client set from environment settings.

    Several inputs are ``TODO(confirm)`` placeholders (BDA project ARN, KB role
    ARN, embedding model ARN); they come from settings/config and are flagged in
    OPEN_QUESTIONS.md. This function does no network I/O itself - it only wires
    clients - so importing it is cheap.
    """
    import boto3

    region = settings.aws_region
    session = boto3.Session(region_name=region)

    from kb_agent.aws.clients import (
        BedrockDataAutomation,
        BedrockEmbeddings,
        BedrockGuardrail,
        BedrockKnowledgeBase,
        BedrockLlm,
        S3ObjectStore,
    )

    store = S3ObjectStore(session.client("s3"), settings.s3_bucket)
    bedrock_runtime = session.client("bedrock-runtime")

    # TODO(confirm) Q6/Q11: BDA project ARN + KB service role ARN are not yet
    # provisioned; read from settings so nothing is hard-coded. A real run fails
    # loudly if these remain placeholders.
    bda = BedrockDataAutomation(
        session.client("bedrock-data-automation-runtime"),
        store,
        project_arn=getattr(settings, "bda_project_arn", "REPLACE_ME_bda_project_arn"),
    )
    embeddings = BedrockEmbeddings(bedrock_runtime, settings.embedding_model_id)
    llm = BedrockLlm(bedrock_runtime, settings.llm_model_id)

    # freq-test: when guardrails are disabled, inject a no-op so the Guardrails
    # API is never called. The offline regex PII layer still runs in validate/draft.
    if settings.guardrail_enabled:
        guardrail: GuardrailClient = BedrockGuardrail(
            bedrock_runtime, settings.guardrail_id, settings.guardrail_version
        )
    else:
        from kb_agent.aws.fakes import NoOpGuardrail

        guardrail = NoOpGuardrail()

    kb = BedrockKnowledgeBase(
        session.client("bedrock-agent"),
        session.client("bedrock-agent-runtime"),
        bucket=settings.s3_bucket,
        role_arn=settings.kb_role_arn or "REPLACE_ME_kb_role_arn",
        embedding_model_arn=(
            f"arn:aws:bedrock:{region}::foundation-model/{settings.embedding_model_id}"
        ),
        vector_store_config=_vector_store_config(settings, config),
    )
    return ClientSet(
        mode="aws",
        store=store,
        bda=bda,
        embeddings=embeddings,
        llm=llm,
        guardrail=guardrail,
        knowledge_base=kb,
    )


def _vector_store_config(settings: Settings, config: AppConfig) -> dict:  # pragma: no cover
    """Build the KB storageConfiguration for the configured vector store (Q7)."""
    if config.knowledge_base.vector_store == "s3_vectors":
        # Verified 2026-10-15 against the Bedrock Agent API: s3VectorsConfiguration
        # accepts indexArn | indexName | vectorBucketArn. We pass the index ARN
        # directly since that is what was provisioned.
        return {
            "type": "S3_VECTORS",
            "s3VectorsConfiguration": {
                "indexArn": settings.vector_index_arn or "REPLACE_ME_vector_index_arn",
            },
        }
    # OpenSearch Serverless path (conditional, Q7).
    return {
        "type": "OPENSEARCH_SERVERLESS",
        "opensearchServerlessConfiguration": {
            "collectionArn": getattr(settings, "oss_collection_arn", "REPLACE_ME_oss_arn"),
            "vectorIndexName": "kb-index",
            "fieldMapping": {
                "vectorField": "vector",
                "textField": "text",
                "metadataField": "metadata",
            },
        },
    }


def build_client_set(mode: Mode, settings: Settings, config: AppConfig) -> ClientSet:
    """Build the client set for the requested run mode."""
    if mode == "fake":
        return build_fake_client_set(bucket=settings.s3_bucket)
    return build_aws_client_set(settings, config)
