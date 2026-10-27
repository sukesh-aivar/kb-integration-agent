"""Create-or-verify provisioning for a project's knowledge resources (Part B B4).

Given a ``project_id`` the agent must end up with: a vector bucket, an S3 Vectors
index, a knowledge base, and an S3 data source - with deterministic names, reused
across runs, and created **only if missing**. Nothing is ever mutated: an index's
dimension / metric / non-filterable keys are immutable, so a returning project
whose stored index differs from the configured one is a hard
``provisioning_conflict`` (change nothing, report the differing fields) rather
than an update.

The agent creates **no IAM roles** (B4.5): the knowledge base service role is
platform-owned and passed in via settings. This module only creates the
knowledge resources and tags them so the cleanup script can find them.

Everything here is pure orchestration over the injected ``VectorStoreAdmin`` +
``KnowledgeBaseClient`` + ``ObjectStore``, so it runs identically under the fakes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kb_agent import project as naming
from kb_agent.aws.interfaces import KnowledgeBaseClient, ObjectStore, VectorStoreAdmin
from kb_agent.config import AppConfig, Settings
from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)

# Tag keys applied to every managed resource (B4.2). Cleanup is tag-gated.
TAG_MANAGED_BY = "kb-agent:managed-by"
TAG_PROJECT_ID = "kb-agent:project-id"
TAG_CREATED_BY_RUN = "kb-agent:created-by-run"
MANAGED_BY_VALUE = "kb-integration-agent"


class ProvisioningConflictError(Exception):
    """A returning project's resource differs on an IMMUTABLE field (B4.2).

    Carries the differing fields so the caller can report them. The run fails
    with error code ``provisioning_conflict`` and NOTHING is mutated.
    """

    def __init__(self, resource: str, mismatches: dict[str, tuple[Any, Any]]) -> None:
        self.resource = resource
        self.mismatches = mismatches
        detail = ", ".join(
            f"{k}: expected {exp!r}, found {got!r}" for k, (exp, got) in mismatches.items()
        )
        super().__init__(f"{resource} already exists with different settings ({detail})")


class ProvisioningError(Exception):
    """Provisioning could not complete (e.g. record present but KB missing)."""


@dataclass
class ProvisioningResult:
    """The outcome of provisioning one project's resources."""

    project_id: str
    knowledge_base_id: str
    data_source_id: str
    vector_bucket: str
    index_name: str
    index_arn: str
    dimension: int
    distance_metric: str
    steps: list[dict] = field(default_factory=list)  # [{resource, name, action}]


class Provisioner:
    """Create-or-verify the vector bucket, index, KB and data source for a project."""

    def __init__(
        self,
        store: ObjectStore,
        vector_admin: VectorStoreAdmin,
        kb_client: KnowledgeBaseClient,
        config: AppConfig,
        settings: Settings,
        *,
        emit: Callable[[str], None] | None = None,
        emit_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._store = store
        self._va = vector_admin
        self._kb = kb_client
        self._config = config
        self._settings = settings
        self._emit = emit or (lambda _m: None)
        self._emit_event = emit_event or (lambda _t, _d: None)

    def provision(
        self, project_id: str, run_id: str, *, chunking_strategy: str
    ) -> ProvisioningResult:
        """Ensure all four resources exist (or match); return their ids.

        When ``provisioning.enabled`` is false, skip creation entirely and bind to
        the hand-built index ARN from settings (the proven fallback, B4.2).
        """
        p = self._config.provisioning
        v = self._config.vector_store
        steps: list[dict] = []
        tags = {
            TAG_MANAGED_BY: MANAGED_BY_VALUE,
            TAG_PROJECT_ID: project_id,
            TAG_CREATED_BY_RUN: run_id,
        }

        if not p.enabled:
            return self._fallback(project_id, run_id, chunking_strategy)

        vbucket = p.vector_bucket_name
        iname = naming.index_name(project_id, prefix=p.name_prefix)
        kbname = naming.kb_name(project_id, prefix=p.name_prefix)
        dsname = naming.data_source_name(project_id, prefix=p.ds_prefix)
        src_prefix = naming.source_prefix(project_id, root=p.kb_source_root)

        # 1) Vector bucket (create-if-missing; concurrent-create tolerated).
        bucket_arn = self._va.ensure_vector_bucket(vbucket)
        self._va.tag(bucket_arn, tags)
        self._step(steps, "vector_bucket", vbucket, "verified")

        # 2) Index (create-or-verify; IMMUTABLE on mismatch).
        index = self._va.get_index(vbucket, iname)
        if index is None:
            index = self._va.create_index(
                vbucket, iname,
                dimension=v.dimension, distance_metric=v.distance_metric,
                data_type=v.data_type, non_filterable_keys=list(v.non_filterable_keys),
            )
            self._va.tag(index["arn"], tags)
            self._step(steps, "index", iname, "created")
        else:
            self._verify_index(index, v)
            self._step(steps, "index", iname, "verified")
        index_arn = index["arn"]

        # 3) Knowledge base (lookup by project record first, then by name).
        embedding_arn = self._embedding_model_arn()
        kb_id = self._resolve_kb(project_id, kbname, index_arn, embedding_arn, steps)

        # 4) Data source (create-or-verify inclusion prefix + chunking).
        ds = self._kb.find_or_create_data_source(
            kb_id, dsname,
            inclusion_prefix=src_prefix,
            chunking_strategy=chunking_strategy,
            data_deletion_policy=p.data_deletion_policy,
        )
        ds_id = ds["id"]
        self._step(steps, "data_source", dsname,
                   "created" if ds.get("_created") else "verified")

        # 5) Project record (ids + settings) for fast lookup next run.
        self._write_project_record(project_id, run_id, {
            "project_id": project_id,
            "knowledge_base_id": kb_id,
            "data_source_id": ds_id,
            "vector_bucket": vbucket,
            "index_name": iname,
            "index_arn": index_arn,
            "dimension": v.dimension,
            "distance_metric": v.distance_metric,
        })

        return ProvisioningResult(
            project_id=project_id, knowledge_base_id=kb_id, data_source_id=ds_id,
            vector_bucket=vbucket, index_name=iname, index_arn=index_arn,
            dimension=v.dimension, distance_metric=v.distance_metric, steps=steps,
        )

    # --- steps ---------------------------------------------------------------

    def _resolve_kb(
        self, project_id: str, kbname: str, index_arn: str, embedding_arn: str,
        steps: list[dict],
    ) -> str:
        """Find the KB by project record then by name; create if truly absent.

        If the project record names a KB that no longer exists, STOP with a
        conflict rather than silently recreating (vectors may remain in the
        index) - B4.2.
        """
        record = self._read_project_record(project_id)
        if record and record.get("knowledge_base_id"):
            rec_id = record["knowledge_base_id"]
            kb = self._kb.get_knowledge_base(rec_id)
            if kb is None:
                raise ProvisioningError(
                    f"project record names knowledge base {rec_id!r} but it does not "
                    f"exist; refusing to recreate (vectors may remain). Clean up first."
                )
            self._verify_kb(kb, index_arn, embedding_arn)
            self._step(steps, "knowledge_base", kbname, "verified")
            return rec_id

        found = self._kb.find_knowledge_base(kbname)
        if found:
            kb = self._kb.get_knowledge_base(found["id"])
            if kb is not None:
                self._verify_kb(kb, index_arn, embedding_arn)
            self._step(steps, "knowledge_base", kbname, "verified")
            return found["id"]

        # Truly absent -> create. The real client binds the KB to the index ARN.
        creator = getattr(self._kb, "create_kb_record", None)
        if callable(creator):
            kb_id = creator(kbname, index_arn=index_arn, embedding_model_arn=embedding_arn)
        else:  # pragma: no cover - every client we ship implements it
            kb_id = self._kb.create(kbname, "", self._config.knowledge_base.chunking_strategy)
        self._kb.tag(kb_id, {
            TAG_MANAGED_BY: MANAGED_BY_VALUE, TAG_PROJECT_ID: project_id,
        })
        self._step(steps, "knowledge_base", kbname, "created")
        return kb_id

    def _verify_index(self, index: dict, v: Any) -> None:
        mism: dict[str, tuple[Any, Any]] = {}
        if index.get("dimension") != v.dimension:
            mism["dimension"] = (v.dimension, index.get("dimension"))
        if index.get("distance_metric") != v.distance_metric:
            mism["distance_metric"] = (v.distance_metric, index.get("distance_metric"))
        if index.get("data_type") != v.data_type:
            mism["data_type"] = (v.data_type, index.get("data_type"))
        if set(index.get("non_filterable_keys") or []) != set(v.non_filterable_keys):
            mism["non_filterable_keys"] = (list(v.non_filterable_keys),
                                           index.get("non_filterable_keys"))
        if mism:
            raise ProvisioningConflictError("index", mism)

    def _verify_kb(self, kb: dict, index_arn: str, embedding_arn: str) -> None:
        mism: dict[str, tuple[Any, Any]] = {}
        if kb.get("index_arn") not in (None, index_arn):
            mism["index_arn"] = (index_arn, kb.get("index_arn"))
        if kb.get("embedding_model_arn") not in (None, embedding_arn):
            mism["embedding_model_arn"] = (embedding_arn, kb.get("embedding_model_arn"))
        if mism:
            raise ProvisioningConflictError("knowledge_base", mism)

    def _fallback(self, project_id: str, run_id: str, chunking_strategy: str) -> ProvisioningResult:
        """provisioning.enabled=false: bind to the hand-built index ARN (B4.2)."""
        index_arn = self._settings.vector_index_arn or "REPLACE_ME_vector_index_arn"
        v = self._config.vector_store
        kbname = naming.kb_name(project_id, prefix=self._config.provisioning.name_prefix)
        kb_id = self._kb.create(
            kbname, naming.source_prefix(project_id, root=self._config.provisioning.kb_source_root),
            chunking_strategy,
        )
        ds_id = getattr(self._kb, "data_source_id", None) or ""
        self._emit("Provisioning disabled; using the pre-provisioned index")
        return ProvisioningResult(
            project_id=project_id, knowledge_base_id=kb_id, data_source_id=ds_id,
            vector_bucket=self._config.provisioning.vector_bucket_name,
            index_name=naming.index_name(project_id, prefix=self._config.provisioning.name_prefix),
            index_arn=index_arn, dimension=v.dimension, distance_metric=v.distance_metric,
            steps=[{"resource": "index", "name": "(pre-provisioned)", "action": "fallback"}],
        )

    def _embedding_model_arn(self) -> str:
        region = self._settings.aws_region
        model = self._settings.embedding_model_id
        return f"arn:aws:bedrock:{region}::foundation-model/{model}"

    def _step(self, steps: list[dict], resource: str, name: str, action: str) -> None:
        steps.append({"resource": resource, "name": name, "action": action})
        event = "resource_created" if action == "created" else "resource_verified"
        self._emit_event(event, {"resource_type": resource, "name": name})
        self._emit(f"{action.capitalize()} {resource} {name}")

    # --- project record ------------------------------------------------------

    def _record_key(self, project_id: str) -> str:
        return naming.project_record_key(project_id, root=self._config.provisioning.kb_source_root)

    def _read_project_record(self, project_id: str) -> dict | None:
        key = self._record_key(project_id)
        try:
            if not self._store.exists(key):
                return None
            return json.loads(self._store.get_text(key))
        except Exception:  # noqa: BLE001 - a corrupt record reads as absent
            return None

    def _write_project_record(self, project_id: str, run_id: str, record: dict) -> None:
        record = {**record, "last_run_id": run_id}
        self._store.put_text(self._record_key(project_id), json.dumps(record, indent=2))
