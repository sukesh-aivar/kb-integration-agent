"""Typed configuration loading (spec 7.4: no hard-coded values).

Two layers of configuration, deliberately separated:

* **Secrets / environment** come from environment variables (or a git-ignored
  ``.env``) via :class:`Settings` (pydantic-settings). These are per-deployment
  values a developer fills in: region, bucket, model IDs, guardrail IDs.
* **Tunable behaviour** (thresholds, seeds, limits) comes from
  ``config/default.yaml`` via :func:`load_config`. A reviewer must be able to
  retune these without touching code or redeploying.

Keeping them apart means the YAML can be committed and reviewed while the real
secrets never are.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root = three parents up from this file (src/kb_agent/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


class Settings(BaseSettings):
    """Environment-backed settings (secrets and per-deployment values).

    Every field maps to a ``KB_AGENT_*`` environment variable. Placeholder
    defaults are intentionally obvious (``REPLACE_ME_*``) so an unconfigured run
    fails loudly rather than silently pointing at the wrong resource. Several
    defaults are ``TODO(confirm)`` values tracked in OPEN_QUESTIONS.md.
    """

    model_config = SettingsConfigDict(
        env_prefix="KB_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Default Region. The run payload's `region` (Part B) takes precedence; this
    # is only the fallback when the payload omits it. Set to the deployment
    # Region (us-east-1), never the old us-west-2, so a missing payload field
    # never silently provisions in the wrong place.
    aws_region: str = "us-east-1"
    s3_bucket: str = "REPLACE_ME_placeholder-bucket"
    s3_prefix: str = "kb-agent"

    embedding_model_id: str = "amazon.titan-embed-text-v2:0"  # TODO(confirm) Q11.
    llm_model_id: str = "anthropic.claude-3-5-sonnet-20240620-v1:0"  # TODO(confirm) Q11.

    guardrail_id: str = "REPLACE_ME_placeholder-guardrail"  # TODO(confirm).
    guardrail_version: str = "DRAFT"
    # freq-test: disable the Guardrails call (a no-op guardrail is injected).
    # The offline regex PII layer stays on regardless of this flag.
    guardrail_enabled: bool = True

    # Real AWS resource ARNs for a KB-with-S3-Vectors run (freq-test). Empty
    # defaults so an unconfigured run fails loudly rather than hitting placeholders.
    kb_role_arn: str = ""  # IAM role the KB service assumes (not the agent)
    vector_index_arn: str = ""  # S3 Vectors index ARN (s3VectorsConfiguration.indexArn)
    bda_project_arn: str = ""  # only needed for the BDA path (not Path A)
    # BDA now requires a data-automation PROFILE arn on invoke. If left empty it
    # is derived as arn:aws:bedrock:<region>:<account>:data-automation-profile/
    # us.data-automation-v1 (the standard cross-region profile).
    bda_profile_arn: str = ""

    otel_endpoint: str = ""  # empty -> local no-op exporter
    log_level: str = "INFO"


# --- Tunable config models (mirror config/default.yaml) ----------------------


class EscalationConfig(BaseModel):
    min_chars_per_page_floor: int = 50
    max_garbled_char_ratio: float = 0.15


class RoutingConfig(BaseModel):
    min_chars_per_page: int = 100
    max_image_area_ratio: float = 0.5
    max_tables: int = 3
    max_file_size_mb: int = 50
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)


class ValidationConfig(BaseModel):
    non_english_action: str = "flag"  # TODO(confirm) Q5. flag | reject | ignore
    pii_regex_enabled: bool = True


class KnowledgeBaseConfig(BaseModel):
    vector_store: str = "s3_vectors"  # TODO(confirm) Q7.
    chunking_strategy: str = "hierarchical"
    retrieval_top_k: int = 5


# --- Part B config blocks (source handling, provisioning, vector store, etc.) ---


class SourceConfig(BaseModel):
    """Input-source handling limits (B3, B6)."""

    max_files: int = 100  # TODO(confirm) B-Q1; KB ingestion job supports up to 1000 (BDA)
    max_file_mb: int = 500  # hard ceiling (over it -> REJECT, rule 16)
    kb_path_max_mb: int = 50  # Bedrock KB per-file limit (verified)
    bda_max_pages: int = 3000  # BDA's native per-document page limit (verified 2025-04)


class ProcessingConfig(BaseModel):
    """Multi-file processing concurrency + scratch (B3, B6)."""

    max_workers: int = 4  # local pool
    bda_concurrency: int = 3  # TODO(confirm) B-Q7 account quota
    temp_dir: str = "/tmp"


class ProvisioningConfig(BaseModel):
    """Create-or-verify provisioning settings (B4, B6)."""

    enabled: bool = True
    vector_bucket_name: str = "kb-agent-vectors"
    name_prefix: str = "kb-"
    ds_prefix: str = "ds-"
    kb_source_root: str = "projects"
    lock_ttl_seconds: int = 300
    lock_wait_seconds: int = 900
    data_deletion_policy: str = "DELETE"  # DELETE | RETAIN (verified default DELETE)


class VectorStoreConfig(BaseModel):
    """S3 Vectors index parameters (B4.2, B6). Immutable after index creation."""

    type: str = "s3_vectors"
    dimension: int = 1024  # must equal the embedding model's output (Titan v2 = 1024)
    distance_metric: str = "cosine"
    data_type: str = "float32"
    non_filterable_keys: list[str] = Field(
        default_factory=lambda: [
            "AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA", "source", "source_type",
            "route", "content_hash", "run_id", "ingested_at", "language",
        ]
    )


class IngestionConfig(BaseModel):
    """Ingestion-job polling (B5, B6)."""

    poll_seconds: int = 10
    timeout_seconds: int = 3600


class JsonConfig(BaseModel):
    """Structured-conversion limits (B2.6, B6)."""

    max_depth: int = 5  # dotted-key flatten depth cap
    max_input_mb: int = 50


class UmapConfig(BaseModel):
    n_components: int = 10
    metric: str = "cosine"


class HdbscanConfig(BaseModel):
    min_cluster_size: int = 15


class ClusteringConfig(BaseModel):
    cluster_threshold: int = 500
    umap: UmapConfig = Field(default_factory=UmapConfig)
    hdbscan: HdbscanConfig = Field(default_factory=HdbscanConfig)


class DraftingConfig(BaseModel):
    min_source_tickets: int = 5  # TODO(confirm) Q3.


class CoverageConfig(BaseModel):
    retrieval_score_threshold: float = 0.5
    top_intents_to_test: int = 10


class WorkflowConfig(BaseModel):
    """Which workflow the agent runs (v3 KB-only reorg, A4.3).

    ``mode`` must be ``kb_only`` for now; any other value fails at load so an
    unknown mode never runs silently. ``legacy_stage_placeholders`` makes the
    reported stage order the original 13 (with the nine dormant stages marked
    skipped) for an unchanged 13-step consumer (A4.2). ``legacy_status_needs_review``
    is a Part-B flag (kept here so the schema is stable) that re-emits
    ``completed`` as ``needs_review``; default false.
    """

    mode: str = "kb_only"
    legacy_stage_placeholders: bool = False
    legacy_status_needs_review: bool = False

    def _validate_mode(self) -> WorkflowConfig:
        if self.mode != "kb_only":
            raise ValueError(
                f"unknown workflow.mode {self.mode!r}; only 'kb_only' is supported"
            )
        return self


class PlannerConfig(BaseModel):  # DORMANT (v3): planner is out of the active workflow
    enabled: bool = False  # OFF by default pending OPEN_QUESTIONS Q12
    max_planner_steps: int = 20
    max_retune_attempts_per_stage: int = 2


class VersionsConfig(BaseModel):
    prompt_pack: str = "v0.1"


class AppConfig(BaseModel):
    """Fully-typed view of ``config/default.yaml``.

    Validation here means a malformed config fails at load time with a clear
    pydantic error, rather than deep inside a pipeline stage.
    """

    # Allow the ``json`` YAML key to populate ``json_convert`` by alias, and also
    # allow constructing AppConfig() by field name in code/tests.
    model_config = ConfigDict(populate_by_name=True)

    seed: int = 42
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    knowledge_base: KnowledgeBaseConfig = Field(default_factory=KnowledgeBaseConfig)
    # --- Part B active blocks ---
    source: SourceConfig = Field(default_factory=SourceConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    provisioning: ProvisioningConfig = Field(default_factory=ProvisioningConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    # Attribute is ``json_convert`` to avoid shadowing pydantic's BaseModel.json;
    # the YAML/validation key stays ``json`` via the alias.
    json_convert: JsonConfig = Field(default_factory=JsonConfig, alias="json")
    # --- DORMANT config blocks (v3): kept for when the ticket/join/planner stages
    # return; not read by the active KB-only workflow. Do not delete (rule 5). ---
    clustering: ClusteringConfig = Field(default_factory=ClusteringConfig)  # DORMANT
    drafting: DraftingConfig = Field(default_factory=DraftingConfig)  # DORMANT
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)  # DORMANT
    planner: PlannerConfig = Field(default_factory=PlannerConfig)  # DORMANT
    versions: VersionsConfig = Field(default_factory=VersionsConfig)

    def model_post_init(self, __context: Any) -> None:
        # Fail at load on an unknown workflow mode (A4.3).
        self.workflow._validate_mode()


def load_config(path: Path | None = None) -> AppConfig:
    """Load and validate the tunable config from YAML.

    Args:
        path: Optional explicit path to a config file. Defaults to
            ``config/default.yaml`` at the repository root.

    Returns:
        A validated :class:`AppConfig`.
    """
    config_path = path or (CONFIG_DIR / "default.yaml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    return AppConfig.model_validate(raw)


def load_routing_rules(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the routing rule table (data, not code; spec 4.2).

    Returns the ordered list of rule dicts. Evaluation lives in the router
    module; this function only reads and shape-checks the file.
    """
    rules_path = path or (CONFIG_DIR / "routing_rules.yaml")
    with rules_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError(f"routing_rules.yaml 'rules' must be a list, got {type(rules)!r}")
    return rules


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached environment settings."""
    return Settings()
