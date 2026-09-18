# services/stt/stt_service.py
"""Speech settings compatibility plus the managed Whisper executor internals.

Server-side transcription is dispatched by ``src.openclank.modality_facade``.
The public methods here fail closed for stale local/endpoint settings; only the
private fixed-recipe local primitive is retained for the managed host broker.
Browser Web Speech remains client-side and performs no server model inference.
"""

import logging
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class STTService:
    """Legacy speech-settings facade with managed-executor internals.

    Reads provider config from data/settings.json on each call.
    Providers:
      "disabled"        — no STT
      "browser"         — client-side Web Speech API (no server transcription)
      "local"           — retired public selector; broker recipe owns execution
      "endpoint:<id>"   — retired direct-provider selector
    """

    def __init__(self):
        self._whisper_model = None  # lazy-init

    # ── Settings ──

    def _load_settings(self, owner: str | None = None) -> dict:
        from src.settings import get_user_setting
        return {
            "stt_enabled": get_user_setting("stt_enabled", owner or "", False),
            "stt_provider": get_user_setting("stt_provider", owner or "", "disabled"),
            "stt_model": get_user_setting("stt_model", owner or "", "base"),
            "stt_language": get_user_setting("stt_language", owner or "", ""),
        }

    @property
    def available(self) -> bool:
        return self.is_available()

    def is_available(self, owner: str | None = None) -> bool:
        settings = self._load_settings(owner)
        if settings.get("stt_enabled") is False:
            return False
        provider = settings["stt_provider"]
        if provider == "disabled":
            return False
        if provider == "browser":
            return True  # handled client-side
        if provider == "local":
            return False
        if isinstance(provider, str) and provider.startswith("endpoint:"):
            return False
        return False

    # ── Local Whisper ──

    def _get_whisper(self, owner: str | None = None):
        if self._whisper_model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                logger.warning("faster-whisper not installed. Install with: pip install faster-whisper")
                return None
            try:
                settings = self._load_settings(owner)
                model_size = settings.get("stt_model", "base")
                # faster-whisper runs on CTranslate2, not torch. torch is only
                # used (optionally) to detect a CUDA device for acceleration —
                # if it's missing or unusable we just run on CPU. Keeping this
                # probe separate (and tolerant of any failure, e.g. a broken
                # CUDA/torch install that raises OSError on import) means a
                # torch-less or torch-broken machine still does CPU
                # transcription instead of failing with a misleading
                # "faster-whisper not installed" error.
                try:
                    import torch
                    use_cuda = torch.cuda.is_available()
                except Exception:
                    use_cuda = False
                device = "cuda" if use_cuda else "cpu"
                compute_type = "float16" if device == "cuda" else "int8"
                self._whisper_model = WhisperModel(model_size, device=device, compute_type=compute_type)
                logger.info(f"faster-whisper model '{model_size}' loaded on {device}")
            except Exception as e:
                logger.error(f"Failed to load whisper model: {e}")
                return None
        return self._whisper_model

    def _transcribe_local(self, audio_bytes: bytes, language: str = "",
                          owner: str | None = None) -> Optional[str]:
        model = self._get_whisper(owner)
        if not model:
            return None
        tmp_path = None
        try:
            # Write to temp file (faster-whisper needs a file path or file-like)
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            kwargs = {}
            if language:
                kwargs["language"] = language

            segments, info = model.transcribe(tmp_path, **kwargs)
            text = " ".join(seg.text.strip() for seg in segments)

            logger.info(f"Local STT: {len(text)} chars, lang={info.language}, prob={info.language_probability:.2f}")
            return text
        except Exception as e:
            logger.error(f"Local STT transcription failed: {e}", exc_info=True)
            return None
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    # ── Public interface ──

    def transcribe(self, audio_bytes: bytes, owner: str | None = None) -> Optional[str]:
        """Fail closed when legacy callers bypass the managed operation router."""

        del audio_bytes
        settings = self._load_settings(owner)
        provider = settings.get("stt_provider")
        if settings.get("stt_enabled") is False or provider in ("disabled", "browser"):
            return None
        logger.warning(
            "Direct STT service execution is retired; use audio.transcribe"
        )
        return None

    def get_stats(self, owner: str | None = None) -> Dict[str, Any]:
        settings = self._load_settings(owner)
        provider = settings["stt_provider"]
        stt_enabled = settings.get("stt_enabled", False)
        # If toggle is off, report as disabled
        effective_provider = provider if stt_enabled else "disabled"

        stats = {
            "available": self.is_available(owner) and stt_enabled,
            "provider": effective_provider,
            "model": settings["stt_model"],
            "language": settings.get("stt_language", ""),
        }

        if provider == "local":
            stats["model_loaded"] = False
            stats["managed_executor"] = "faster-whisper"
        elif provider == "browser":
            stats["model"] = "Browser (Web Speech API)"
        elif isinstance(provider, str) and provider.startswith("endpoint:") and stats["available"]:
            stats["endpoint_id"] = provider.split(":", 1)[1]

        return stats


# Module-level singleton
_stt_service = None

def get_stt_service() -> STTService:
    global _stt_service
    if _stt_service is None:
        _stt_service = STTService()
    return _stt_service
