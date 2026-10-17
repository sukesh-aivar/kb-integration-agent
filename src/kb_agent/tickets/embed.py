"""embed_texts: batched Bedrock embeddings with a local cache (spec 4.3).

The cache is keyed by ``sha256(text) + model_id`` so iterating on clustering
parameters does not re-pay for embeddings (spec 4.3). The embedding client is
injected (:class:`EmbeddingsClient`), so this runs offline with the fake and
unchanged against Bedrock.
"""

from __future__ import annotations

import hashlib

from kb_agent.aws.interfaces import EmbeddingsClient
from kb_agent.logging_setup import get_logger

logger = get_logger(__name__)


def _cache_key(text: str, model_id: str) -> str:
    return hashlib.sha256(f"{model_id}\x00{text}".encode()).hexdigest()


class EmbeddingCache:
    """In-memory embedding cache keyed by text hash + model id.

    Kept as an object (not a module global) so each run/test is isolated and the
    cache cannot leak between runs in a long-lived container.
    """

    def __init__(self) -> None:
        self._cache: dict[str, list[float]] = {}

    def get_or_compute(self, texts: list[str], client: EmbeddingsClient) -> list[list[float]]:
        """Return embeddings for ``texts``, computing only the cache misses.

        Order-preserving: the returned list aligns 1:1 with ``texts``. Only the
        uncached texts are sent to the client, batched in one call.
        """
        model_id = client.model_id
        keys = [_cache_key(t, model_id) for t in texts]

        # Collect UNIQUE missing keys (not just missing indices): the same text
        # can appear multiple times in one batch, and we must embed it only once.
        missing_unique: dict[str, str] = {}  # key -> text
        for key, text in zip(keys, texts, strict=False):
            if key not in self._cache and key not in missing_unique:
                missing_unique[key] = text

        if missing_unique:
            missing_keys = list(missing_unique)
            to_embed = [missing_unique[k] for k in missing_keys]
            vectors = client.embed(to_embed)
            if len(vectors) != len(to_embed):
                raise RuntimeError(
                    f"embedding client returned {len(vectors)} vectors for {len(to_embed)} texts"
                )
            for key, vec in zip(missing_keys, vectors, strict=False):
                self._cache[key] = vec
            logger.info(
                "embed_batch",
                requested=len(texts),
                computed=len(to_embed),
                cached=len(texts) - len(to_embed),
            )

        return [self._cache[k] for k in keys]


def embed_texts(
    texts: list[str], client: EmbeddingsClient, cache: EmbeddingCache | None = None
) -> list[list[float]]:
    """Convenience wrapper: embed ``texts`` using an optional shared cache."""
    cache = cache or EmbeddingCache()
    return cache.get_or_compute(texts, client)
