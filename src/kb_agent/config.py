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
from pydantic import BaseModel, Field
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

    aws_region: str = "us-west-2"  # TODO(confirm) Q6: InfoSec-approved region.
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


class PlannerConfig(BaseModel):
    enabled: bool = False  # OFF by default pending OPEN_QUESTIONS Q12
    max_planner_steps: int = 20
    max_retune_attempts_per_stage: int = 2


class CostConfig(BaseModel):
    bda_page_cap_per_run: int = 2000
    bda_budget_usd_per_run: float = 50.0


class VersionsConfig(BaseModel):
    prompt_pack: str = "v0.1"


class AppConfig(BaseModel):
    """Fully-typed view of ``config/default.yaml``.

    Validation here means a malformed config fails at load time with a clear
    pydantic error, rather than deep inside a pipeline stage.
    """

    seed: int = 42
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    knowledge_base: KnowledgeBaseConfig = Field(default_factory=KnowledgeBaseConfig)
    clustering: ClusteringConfig = Field(default_factory=ClusteringConfig)
    drafting: DraftingConfig = Field(default_factory=DraftingConfig)
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    versions: VersionsConfig = Field(default_factory=VersionsConfig)


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
