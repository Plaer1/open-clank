# routes/embedding_routes.py
"""Cache management for registered local FastEmbed executor assets.

Provider endpoints and credentials are managed exclusively through
``/api/v1/providers/**``.  The retired ``/api/embeddings/endpoint`` route is
intentionally absent.
"""
import os
import shutil
import logging
import asyncio
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends
from core.constants import FASTEMBED_CACHE_DIR
from core.middleware import require_admin
from src.runtime_paths import get_app_root

logger = logging.getLogger(__name__)

# Track in-progress downloads
_downloading: dict = {}

# Curated recommendations — good coverage of size/quality tiers
RECOMMENDED_MODELS = {
    "sentence-transformers/all-MiniLM-L6-v2",     # 384d, 90MB  — fast & tiny, good default
    "BAAI/bge-small-en-v1.5",                      # 384d, 67MB  — smallest, solid quality
    "nomic-ai/nomic-embed-text-v1.5-Q",            # 768d, 130MB — quantized, great bang/buck
    "BAAI/bge-base-en-v1.5",                       # 768d, 210MB — balanced mid-range
    "snowflake/snowflake-arctic-embed-m",          # 768d, 430MB — strong performer
    "BAAI/bge-large-en-v1.5",                      # 1024d, 1.2GB — highest quality
}


def _cache_dir() -> str:
    """Get the fastembed cache directory.

    Defaults to a persistent path under the repo's data/ dir. The old
    default lived in /tmp, which many systems wipe on reboot — forcing a
    full re-download of the embedding model after every restart.
    """
    return FASTEMBED_CACHE_DIR


def _model_cache_name(hf_source: str) -> str:
    """Convert HF source like 'qdrant/all-MiniLM-L6-v2-onnx' to cache dir name."""
    return "models--" + hf_source.replace("/", "--")


def _model_cache_path(hf_source: str) -> Path:
    """Return a confined cache path for a fastembed HF source."""
    root = Path(_cache_dir()).expanduser().resolve()
    raw_path = root / _model_cache_name(hf_source)
    if raw_path.is_symlink():
        raise ValueError("Model cache path must not be a symlink")
    path = raw_path.resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError("Model cache path escapes cache root")
    return path


def _is_downloaded(hf_source: str) -> bool:
    """Check if a model is already cached."""
    try:
        model_dir = _model_cache_path(hf_source)
    except ValueError:
        return False
    if not model_dir.is_dir():
        return False
    # Check for actual model files (not just empty dir)
    snapshots = model_dir / "snapshots"
    if snapshots.is_dir():
        return any(snapshots.iterdir())
    # Also check for blobs (older cache format)
    blobs = model_dir / "blobs"
    return blobs.is_dir() and any(blobs.iterdir())


def _active_model() -> str:
    """Return the fixed model behind the ``fastembed/default`` route."""
    return "sentence-transformers/all-MiniLM-L6-v2"


def _dir_size_mb(path: str) -> float:
    """Get directory size in MB."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return round(total / (1024 * 1024), 1)


def setup_embedding_routes():
    router = APIRouter(prefix="/api/embeddings", dependencies=[Depends(require_admin)])

    @router.get("/models")
    def list_models():
        """List all available fastembed models with download status."""
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise HTTPException(503, "fastembed is not installed")

        active = _active_model()
        catalog = TextEmbedding.list_supported_models()
        result = []

        for m in catalog:
            hf_src = m.get("sources", {}).get("hf", "")
            downloaded = _is_downloaded(hf_src) if hf_src else False

            cached_size = None
            if downloaded and hf_src:
                try:
                    cached_size = _dir_size_mb(str(_model_cache_path(hf_src)))
                except ValueError:
                    cached_size = None

            result.append({
                "model": m["model"],
                "dim": m.get("dim"),
                "size_gb": m.get("size_in_GB", 0),
                "description": m.get("description", ""),
                "downloaded": downloaded,
                "downloading": m["model"] in _downloading,
                "active": m["model"] == active,
                "recommended": m["model"] in RECOMMENDED_MODELS,
                "cached_size_mb": cached_size,
            })

        # Sort: active first, then downloaded, then by size
        result.sort(key=lambda x: (not x["active"], not x["downloaded"], x["size_gb"]))
        return result

    @router.post("/models/{model_name:path}/download")
    async def download_model(model_name: str):
        """Download a fastembed model. Returns when complete."""
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise HTTPException(503, "fastembed is not installed")

        # Validate model exists
        catalog = {m["model"]: m for m in TextEmbedding.list_supported_models()}
        if model_name not in catalog:
            raise HTTPException(404, f"Unknown model: {model_name}")

        hf_src = catalog[model_name].get("sources", {}).get("hf", "")
        if hf_src and _is_downloaded(hf_src):
            return {"status": "already_downloaded", "model": model_name}

        if model_name in _downloading:
            return {"status": "already_downloading", "model": model_name}

        _downloading[model_name] = True
        try:
            # Run in thread to not block the event loop
            loop = asyncio.get_running_loop()
            cache = _cache_dir()
            await loop.run_in_executor(
                None,
                lambda: TextEmbedding(model_name=model_name, cache_dir=cache),
            )
            return {"status": "downloaded", "model": model_name}
        except Exception as e:
            logger.error(f"Failed to download {model_name}: {e}")
            raise HTTPException(500, f"Download failed: {str(e)}")
        finally:
            _downloading.pop(model_name, None)

    @router.get("/models/{model_name:path}/status")
    def download_status(model_name: str):
        """Check download status of a model."""
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise HTTPException(503, "fastembed is not installed")

        catalog = {m["model"]: m for m in TextEmbedding.list_supported_models()}
        if model_name not in catalog:
            raise HTTPException(404, f"Unknown model: {model_name}")

        hf_src = catalog[model_name].get("sources", {}).get("hf", "")
        downloaded = _is_downloaded(hf_src) if hf_src else False

        return {
            "model": model_name,
            "downloaded": downloaded,
            "downloading": model_name in _downloading,
        }

    @router.delete("/models/{model_name:path}")
    def delete_model(model_name: str):
        """Delete a cached model."""
        if model_name == _active_model():
            raise HTTPException(400, "Cannot delete the active embedding model")

        if model_name in _downloading:
            raise HTTPException(400, "Model is currently downloading")

        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise HTTPException(503, "fastembed is not installed")

        catalog = {m["model"]: m for m in TextEmbedding.list_supported_models()}
        if model_name not in catalog:
            raise HTTPException(404, f"Unknown model: {model_name}")

        hf_src = catalog[model_name].get("sources", {}).get("hf", "")
        if not hf_src:
            raise HTTPException(400, "No cache source for this model")

        try:
            model_path = _model_cache_path(hf_src)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not model_path.is_dir():
            return {"deleted": False, "message": "Model not cached"}

        shutil.rmtree(model_path)
        logger.info(f"Deleted cached model: {model_name} ({model_path})")
        return {"deleted": True, "model": model_name}

    return router
