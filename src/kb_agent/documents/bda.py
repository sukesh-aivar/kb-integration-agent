"""bda_process: BDA invocation with a content-hash cache (spec 4.2).

The cache is the point of this module. BDA is billed per page, so we never pay
twice for the same bytes under the same parser configuration. The cache key is
``sha256(file_bytes) + parser_version + parser_config_hash`` (spec 4.2): a config
change changes the key, so the cache invalidates correctly rather than serving
stale output.

The actual BDA call is injected (:class:`BdaClient`) so this logic is tested
offline with the fake and runs unchanged against real BDA.
"""

from __future__ import annotations

import hashlib
import json

from kb_agent.aws.interfaces import BdaClient, BdaResult, ObjectStore
from kb_agent.exceptions import BudgetExceededError
from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)

# Bump when BDA parsing behaviour changes so cached outputs invalidate.
PARSER_VERSION = "bda-standard-output-v1"


def content_cache_key(data: bytes, parser_config: dict) -> str:
    """Compute the cache key for a file under a parser configuration.

    Key = sha256(bytes) + parser_version + sha256(parser_config). Returned as a
    hex-ish string safe to use in an S3 key.
    """
    file_hash = hashlib.sha256(data).hexdigest()
    config_hash = hashlib.sha256(
        json.dumps(parser_config, sort_keys=True).encode()
    ).hexdigest()[:16]
    return f"{file_hash}.{PARSER_VERSION}.{config_hash}"


class BdaProcessor:
    """Processes documents through BDA with an S3-backed content-hash cache."""

    def __init__(
        self,
        bda: BdaClient,
        store: ObjectStore,
        *,
        cache_prefix: str = "bda-cache",
        page_cap: int = 2000,
        parser_config: dict | None = None,
    ) -> None:
        self._bda = bda
        self._store = store
        self._cache_prefix = cache_prefix
        self._page_cap = page_cap
        self._parser_config = parser_config or {"granularity": "page+element", "format": "markdown"}
        self._pages_this_run = 0

    @property
    def pages_processed(self) -> int:
        """Total BDA pages processed this run (fed to the cost report)."""
        return self._pages_this_run

    def process(self, file_id: str, data: bytes, input_s3_uri: str) -> BdaResult:
        """Process one document, using the cache on a content+config hit.

        Enforces the per-run page cap (spec 4.2 cost guards): exceeding it raises
        rather than silently spending.
        """
        key = content_cache_key(data, self._parser_config)
        cache_md_key = f"{self._cache_prefix}/{key}.md"

        if self._store.exists(cache_md_key):
            markdown = self._store.get_text(cache_md_key)
            logger.info("bda_cache_hit", file_id=file_id, cache_key=key)
            return BdaResult(
                markdown=markdown,
                pages_processed=0,  # cached: no new paid pages
                output_s3_uri=f"cache://{cache_md_key}",
                cached=True,
            )

        result = self._bda.process_document(input_s3_uri, f"{self._cache_prefix}/{key}")

        if self._pages_this_run + result.pages_processed > self._page_cap:
            raise BudgetExceededError(
                f"BDA page cap {self._page_cap} would be exceeded "
                f"(used {self._pages_this_run}, +{result.pages_processed})"
            )
        self._pages_this_run += result.pages_processed

        # Store the markdown under the content-hash key so the next identical
        # file + config hits the cache.
        self._store.put_text(cache_md_key, result.markdown)
        logger.info(
            "bda_cache_miss", file_id=file_id, cache_key=key, pages=result.pages_processed
        )
        return result
