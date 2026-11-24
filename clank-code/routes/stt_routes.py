# routes/stt_routes.py
"""STT API routes — multi-provider (local Whisper, API endpoint, browser)."""

from fastapi import APIRouter, HTTPException, UploadFile, File, Request
import logging

from src.upload_limits import read_upload_limited, STT_MAX_AUDIO_BYTES
from src.auth_helpers import require_user
from src.openclank.modality_facade import managed_route_summary, transcribe_audio as managed_transcribe_audio

logger = logging.getLogger(__name__)


def _speech_owner(request: Request) -> tuple[str, str]:
    state = getattr(request, "state", None)
    if getattr(state, "api_token", False):
        if getattr(state, "api_token_client_kind", "api") != "api":
            raise HTTPException(403, "This client token cannot use speech routes")
        if "chat" not in set(getattr(state, "api_token_scopes", ()) or ()):
            raise HTTPException(403, "API token missing required scope: chat")
        settings_owner = str(getattr(state, "api_token_owner", "") or "").strip().lower()
        if not settings_owner:
            raise HTTPException(403, "API token has no provider owner")
    else:
        settings_owner = str(require_user(request) or "").strip().lower()
    return settings_owner or "local-installation", settings_owner


def setup_stt_routes(stt_service):
    """Setup STT routes with the provided STT service"""
    router = APIRouter(prefix="/api/stt", tags=["stt"])

    @router.get("/stats")
    async def get_stt_stats(request: Request):
        """Get STT service statistics"""
        try:
            owner, settings_owner = _speech_owner(request)
            settings = stt_service._load_settings(settings_owner)
            enabled = settings.get("stt_enabled") is not False
            browser = enabled and settings.get("stt_provider") == "browser"
            route = managed_route_summary(
                owner=owner,
                purpose="stt",
                operation="audio.transcribe",
            )
            available = bool(browser or (enabled and route))
            return {
                "available": available,
                "provider": "browser" if browser else ("managed" if enabled and route else "disabled"),
                "model": (
                    "Browser (Web Speech API)"
                    if browser
                    else ((route or {}).get("model_name") or (route or {}).get("model_id") or "")
                ),
                "language": settings.get("stt_language", ""),
                **({"model_route_id": route["model_route_id"]} if route else {}),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get STT stats", exc_info=True)
            raise HTTPException(status_code=503, detail="STT service unavailable") from e

    @router.post("/transcribe")
    async def transcribe_audio(request: Request, file: UploadFile = File(...)):
        """Transcribe uploaded audio file to text"""
        try:
            owner, settings_owner = _speech_owner(request)
            settings = stt_service._load_settings(settings_owner)
            if settings.get("stt_enabled") is False or settings.get("stt_provider") == "browser":
                raise HTTPException(
                    status_code=503,
                    detail={"message": "STT service not available or set to browser mode"}
                )

            route = managed_route_summary(
                owner=owner,
                purpose="stt",
                operation="audio.transcribe",
            )
            if route is None:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "STT service not available or set to browser mode"},
                )

            audio_bytes = await read_upload_limited(file, STT_MAX_AUDIO_BYTES, "Audio file")
            if not audio_bytes:
                raise HTTPException(status_code=400, detail={"message": "Empty audio file"})

            result = await managed_transcribe_audio(
                owner=owner,
                audio=audio_bytes,
                media_type=file.content_type or "application/octet-stream",
                language=str(settings.get("stt_language") or ""),
                model_route_id=route["model_route_id"],
            )
            text = result.output.get("text")
            if not isinstance(text, str):
                raise HTTPException(
                    status_code=502,
                    detail={"message": "Transcription failed"}
                )

            return {"text": text}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Managed STT transcription failed", exc_info=True)
            raise HTTPException(
                status_code=502,
                detail={"message": "Transcription failed"},
            ) from e

    return router
