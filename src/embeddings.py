"""Embedding clients used by Open Clank indexes.

The application-facing client is deliberately only a synchronous adapter over
the managed MiMo operation router.  It never accepts a URL, API key, provider
environment variable, or a hidden local fallback.  Local FastEmbed execution
is kept as an implementation detail of the registered host executor at
``openclank.fastembed.v1``; callers reach it through the same normalized route
binding as every remote embedding model.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import threading

from src.constants import FASTEMBED_CACHE_DIR

# Windows: force HuggingFace/fastembed to COPY model files rather than symlink
# them. On a network-share/UNC cache dir Windows can't follow HF's symlinks
# ([WinError 1463] "symbolic link cannot be followed"), so ONNX fails to load the
# model and semantic memory dies. huggingface_hub reads this flag at import time,
# so it must be set before huggingface_hub is first imported — hence module-top.
# (app.py sets the same guard for the server entrypoint.)
if os.name == "nt":
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import logging
import numpy as np
from typing import Any, Coroutine, List, Optional, TypeVar

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "all-minilm:l6-v2"
_DEFAULT_FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_T = TypeVar("_T")


class ManagedEmbeddingError(RuntimeError):
    """Safe failure raised by the managed embedding compatibility client."""


def _run_managed(coroutine: Coroutine[Any, Any, _T]) -> _T:
    """Run one router coroutine from legacy synchronous index code.

    Most index mutations already run in FastAPI's worker pool.  A few call
    sites are synchronous helpers invoked from an event-loop thread, so the
    latter case uses one bounded helper thread instead of attempting a nested
    event loop.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="managed-embed") as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _route_identity(owner: str, model_route_id: str, connection_id: str) -> dict[str, Any]:
    """Reload the exact normalized route selected by the engine."""

    from core.database import SessionLocal
    from core.provider_models import ProviderConnection, ProviderModelRoute

    with SessionLocal() as db:
        row = (
            db.query(ProviderModelRoute, ProviderConnection)
            .join(
                ProviderConnection,
                ProviderConnection.id == ProviderModelRoute.connection_id,
            )
            .filter(
                ProviderModelRoute.id == model_route_id,
                ProviderModelRoute.owner == owner,
                ProviderModelRoute.connection_id == connection_id,
                ProviderModelRoute.enabled.is_(True),
                ProviderModelRoute.deleted_at.is_(None),
                ProviderConnection.owner == owner,
                ProviderConnection.enabled.is_(True),
                ProviderConnection.deleted_at.is_(None),
            )
            .first()
        )
        if row is None:
            raise ManagedEmbeddingError("managed embedding route is no longer available")
        route, connection = row
        if "embeddings.create" not in set(route.operations or ()):
            raise ManagedEmbeddingError("managed route does not support embeddings")
        return {
            "connection_id": connection.id,
            "adapter_id": str(connection.adapter_id),
            "kind": str(connection.kind),
            "billing_lane": str(connection.billing_lane),
            "model_route_id": route.id,
            "model_id": str(route.provider_model_id),
            "route_revision": int(route.revision),
            "catalog_revision": int(route.catalog_revision),
        }


class ManagedEmbeddingClient:
    """SentenceTransformer-shaped adapter for ``embeddings.create``.

    One instance becomes pinned to the first exact model route selected for
    it.  Account rotation remains engine-owned, while a later route/model/
    adapter/dimension change fails closed so vectors from incompatible
    generations can never be mixed.
    """

    url = "managed://openclank/embeddings"

    def __init__(
        self,
        *,
        owner: str,
        model_route_id: Optional[str] = None,
        content_purpose: str = "index",
        root_operation_id: Optional[str] = None,
    ) -> None:
        self.owner = str(owner or "").strip().lower()
        if not self.owner:
            raise ManagedEmbeddingError("embedding owner is required")
        self.model_route_id = str(model_route_id or "").strip() or None
        self.content_purpose = str(content_purpose or "index").strip() or "index"
        self.root_operation_id = str(root_operation_id or "").strip() or None
        self.model = self.model_route_id or "managed-route-binding"
        self.adapter_id = ""
        self.billing_lane = ""
        self.endpoint_class = "managed"
        self._dim: Optional[int] = None
        self._provider_ref = ""
        self._engine_fingerprint = ""
        self._lock = threading.RLock()

    @property
    def provider_ref(self) -> str:
        if not self._provider_ref:
            self.get_sentence_embedding_dimension()
        return self._provider_ref

    @property
    def model_fingerprint(self) -> str:
        return self.provider_ref

    def for_root_operation(self, root_operation_id: str) -> "ManagedEmbeddingClient":
        """Create a child client bound to an explicit canonical root ID."""

        root = str(root_operation_id or "").strip()
        if not root:
            raise ManagedEmbeddingError("embedding root operation ID is required")
        return ManagedEmbeddingClient(
            owner=self.owner,
            model_route_id=self.model_route_id,
            content_purpose=self.content_purpose,
            root_operation_id=root,
        )

    def get_sentence_embedding_dimension(self) -> int:
        if self._dim is None:
            self.encode(["dimension probe"], normalize_embeddings=True)
        if self._dim is None:
            raise ManagedEmbeddingError("managed embedding dimension is unavailable")
        return self._dim

    def _accept_result(self, result: Any) -> list[list[float]]:
        if result.state != "complete":
            raise ManagedEmbeddingError("managed embedding operation did not complete")
        vectors = result.output.get("embeddings")
        if not isinstance(vectors, list) or not vectors:
            raise ManagedEmbeddingError("managed embedding operation returned no vectors")
        try:
            dimension = int(result.dimension)
        except (TypeError, ValueError):
            raise ManagedEmbeddingError("managed embedding dimension is invalid") from None
        engine_fingerprint = str(result.model_fingerprint or "").strip()
        if not engine_fingerprint:
            raise ManagedEmbeddingError("managed embedding fingerprint is missing")
        identity = _route_identity(
            self.owner,
            str(result.model_route_id),
            str(result.connection_id),
        )
        material = {
            **identity,
            "dimension": dimension,
            "engine_model_fingerprint": engine_fingerprint,
        }
        provider_ref = "managed-embedding:" + hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._lock:
            if self.model_route_id and self.model_route_id != identity["model_route_id"]:
                raise ManagedEmbeddingError("managed embedding route changed during a generation")
            if self._dim is not None and self._dim != dimension:
                raise ManagedEmbeddingError("managed embedding dimension changed during a generation")
            if self._provider_ref and self._provider_ref != provider_ref:
                raise ManagedEmbeddingError("managed embedding fingerprint changed during a generation")
            self.model_route_id = identity["model_route_id"]
            self.model = identity["model_id"]
            self.adapter_id = identity["adapter_id"]
            self.billing_lane = identity["billing_lane"]
            self.endpoint_class = (
                "managed-local-executor"
                if identity["kind"] in {"local", "local_executor"}
                else "managed-provider"
            )
            self._dim = dimension
            self._provider_ref = provider_ref
            self._engine_fingerprint = engine_fingerprint
        return vectors

    def encode(
        self,
        texts: List[str],
        normalize_embeddings: bool = True,
    ) -> np.ndarray:
        if not texts:
            return np.array([], dtype="float32")
        if len(texts) > 256 or any(not isinstance(value, str) for value in texts):
            raise ManagedEmbeddingError("managed embeddings require 1 to 256 text values")
        from src.openclank.modality_facade import create_embeddings

        request = {
            "owner": self.owner,
            "texts": texts,
            "content_purpose": self.content_purpose,
            "model_route_id": self.model_route_id,
        }
        if self.root_operation_id:
            request["root_operation_id"] = self.root_operation_id
        result = _run_managed(create_embeddings(**request))
        vectors = np.asarray(self._accept_result(result), dtype="float32")
        if vectors.ndim != 2 or vectors.shape[0] != len(texts):
            raise ManagedEmbeddingError("managed embedding row count is invalid")
        if not np.isfinite(vectors).all():
            raise ManagedEmbeddingError("managed embedding result contains non-finite values")
        if normalize_embeddings:
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if np.any(~np.isfinite(norms)) or np.any(norms <= 0):
                raise ManagedEmbeddingError("managed embedding result has an invalid norm")
            vectors = vectors / norms
        return vectors


class ManagedEmbeddingClientFactory:
    """Owner-keyed client cache for the singleton document RAG projection."""

    def __init__(self) -> None:
        self._clients: dict[str, ManagedEmbeddingClient] = {}
        self._lock = threading.Lock()

    def for_owner(self, owner: str) -> ManagedEmbeddingClient:
        normalized = str(owner or "").strip().lower()
        if not normalized:
            raise ManagedEmbeddingError("embedding owner is required")
        with self._lock:
            client = self._clients.get(normalized)
            if client is None:
                client = ManagedEmbeddingClient(owner=normalized)
                self._clients[normalized] = client
            return client


class EmbeddingClient(ManagedEmbeddingClient):
    """Compatibility name for the managed client; direct HTTP is retired."""

    def __init__(self, *, owner: str, model_route_id: Optional[str] = None) -> None:
        super().__init__(owner=owner, model_route_id=model_route_id)


class FastEmbedClient:
    """Local embedding client using fastembed (ONNX). No external service needed."""

    def __init__(self, model: Optional[str] = None):
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise RuntimeError(
                "Local fastembed is not installed. Either install it "
                "(pip install fastembed) or point the app at a remote "
                "embeddings server."
            ) from e

        # The model is selected by a registered local-executor recipe.  A host
        # environment variable must never silently change an existing vector
        # generation's model or dimension.
        self.model = model or _DEFAULT_FASTEMBED_MODEL
        # Persistent cache under data/ so the model survives reboots and so
        # the download lands exactly where the admin panel's _is_downloaded()
        # check looks (both default to this same path).
        cache_dir = FASTEMBED_CACHE_DIR
        os.makedirs(cache_dir, exist_ok=True)
        # Windows self-heal: the HuggingFace-hub cache stores model files as
        # symlinks (snapshots/<rev>/model.onnx -> ../../blobs/<hash>). On a
        # network-share / UNC data dir Windows refuses to follow them
        # ([WinError 1463] "symbolic link cannot be followed because its type is
        # disabled"), and a cache copied between machines can carry dead symlinks
        # too. Either way fastembed tries to load a broken symlink and fails
        # *without* re-downloading, leaving semantic memory degraded. Detect a
        # broken-symlink model in the cache and drop the contaminated hub dir so
        # fastembed re-fetches (it falls back to its CDN tarball of real files,
        # which load fine). Best-effort; only ever removes a verifiably dead link.
        if os.name == "nt":
            try:
                import glob, shutil
                for _onnx in glob.glob(os.path.join(cache_dir, "**", "*.onnx"), recursive=True):
                    if os.path.islink(_onnx) and not os.path.exists(_onnx):
                        _root = _onnx
                        while os.path.basename(_root) and not os.path.basename(_root).startswith("models--"):
                            _parent = os.path.dirname(_root)
                            if _parent == _root:
                                break
                            _root = _parent
                        if os.path.basename(_root).startswith("models--"):
                            logger.warning(
                                "Embedding cache has a broken symlink (%s); clearing %s "
                                "so fastembed re-downloads real files", _onnx, _root,
                            )
                            shutil.rmtree(_root, ignore_errors=True)
            except Exception as _e:
                logger.debug("embedding cache symlink-heal skipped: %s", _e)
        kwargs = {"model_name": self.model, "cache_dir": cache_dir}
        self._embedding = TextEmbedding(**kwargs)
        self._dim: Optional[int] = None
        self.url = "local://fastembed"
        logger.info(f"FastEmbed loaded model={self.model}")

    def get_sentence_embedding_dimension(self) -> int:
        if self._dim is not None:
            return self._dim
        vec = self.encode(["hello"])
        self._dim = vec.shape[1]
        logger.info(f"Embedding dimension: {self._dim} (model={self.model})")
        return self._dim

    def encode(
        self, texts: List[str], normalize_embeddings: bool = True
    ) -> np.ndarray:
        """Encode texts locally. Returns (N, dim) float32 array."""
        if not texts:
            return np.array([], dtype="float32")

        vecs = np.array(list(self._embedding.embed(texts)), dtype="float32")

        if normalize_embeddings and vecs.size > 0:
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1, norms)
            vecs = vecs / norms

        if self._dim is None and vecs.size > 0:
            self._dim = vecs.shape[1]

        return vecs


def _load_persisted_endpoint() -> dict:
    """Retired compatibility hook.

    Provider cutover imports the old file before normal application startup;
    runtime code must never revive it as provider authority.
    """

    return {}


def reset_http_embed_state():
    """Retired no-op retained for import compatibility."""


_managed_factory = ManagedEmbeddingClientFactory()


def get_embedding_client(owner: Optional[str] = None):
    """Return only the managed client factory or one exact owner client.

    Missing normalized routes are surfaced when that owner first requests an
    embedding.  There is intentionally no HTTP-to-local fallback.
    """

    if owner is None:
        return _managed_factory
    return _managed_factory.for_owner(owner)


__all__ = [
    "EmbeddingClient",
    "FastEmbedClient",
    "ManagedEmbeddingClient",
    "ManagedEmbeddingClientFactory",
    "ManagedEmbeddingError",
    "get_embedding_client",
]
