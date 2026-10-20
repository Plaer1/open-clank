# routes/tts_routes.py
"""
TTS API routes — multi-provider (local Kokoro, API endpoint, browser).
"""

import base64

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
import logging

from src.auth_helpers import require_user
from src.openclank.modality_facade import managed_route_summary, synthesize_audio
from services.tts.tts_service import _safe_speed

logger = logging.getLogger(__name__)

class TTSRequest(BaseModel):
    text: str
    format: str = "audio"  # "audio" or "base64"


def _speech_owner(request: Request) -> tuple[str, str]:
    """Return normalized provider owner and settings owner with scope fencing."""

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

def setup_tts_routes(tts_service):
    """Setup TTS routes with the provided TTS service"""
    router = APIRouter(prefix="/api/tts", tags=["tts"])

    @router.get("/stats")
    async def get_tts_stats(request: Request):
        """Get TTS service statistics"""
        try:
            owner, settings_owner = _speech_owner(request)
            settings = tts_service._load_settings(settings_owner)
            enabled = settings.get("tts_enabled") is not False
            browser = enabled and settings.get("tts_provider") == "browser"
            route = managed_route_summary(
                owner=owner,
                purpose="tts",
                operation="audio.synthesize",
            )
            cache_files = list(tts_service.cache_dir.glob("*.wav")) + list(
                tts_service.cache_dir.glob("*.mp3")
            )
            available = bool(browser or (enabled and route))
            return {
                "available": available,
                "ready": available,
                "provider": "browser" if browser else ("managed" if enabled and route else "disabled"),
                "model": (
                    "Browser (Web Speech API)"
                    if browser
                    else ((route or {}).get("model_name") or (route or {}).get("model_id") or "")
                ),
                "voice": settings.get("tts_voice", "alloy"),
                "speed": _safe_speed(settings.get("tts_speed", "1")),
                "cache_entries": len(cache_files),
                "cache_size_mb": round(
                    sum(path.stat().st_size for path in cache_files) / (1024 * 1024),
                    2,
                ),
                **({"model_route_id": route["model_route_id"]} if route else {}),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get TTS stats", exc_info=True)
            raise HTTPException(status_code=503, detail="TTS service unavailable") from e

    @router.post("/synthesize")
    async def synthesize_speech(body: TTSRequest, request: Request):
        """Synthesize speech from text"""
        try:
            owner, settings_owner = _speech_owner(request)
            settings = tts_service._load_settings(settings_owner)
            if settings.get("tts_enabled") is False or settings.get("tts_provider") == "browser":
                raise HTTPException(
                    status_code=503,
                    detail={"message": "TTS service not available"}
                )
            route = managed_route_summary(
                owner=owner,
                purpose="tts",
                operation="audio.synthesize",
            )
            if route is None:
                raise HTTPException(503, detail={"message": "TTS service not available"})
            text = body.text[:5000]
            voice = str(settings.get("tts_voice") or "alloy")
            speed = _safe_speed(settings.get("tts_speed", "1"))
            audio_data, mime, _ = await synthesize_audio(
                owner=owner,
                text=text,
                voice=voice,
                speed=speed,
                response_format="mp3",
                model_route_id=route["model_route_id"],
            )
            if body.format == "base64":
                return {"audio": base64.b64encode(audio_data).decode("ascii")}
            extension = "mp3" if mime in {"audio/mpeg", "audio/mp3"} else "wav"
            return Response(
                content=audio_data,
                media_type=mime,
                headers={"Content-Disposition": f"inline; filename=speech.{extension}"},
            )
        
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Managed TTS synthesis failed", exc_info=True)
            raise HTTPException(
                status_code=502,
                detail={"message": "Synthesis failed"},
            ) from e

    @router.post("/clear-cache")
    async def clear_tts_cache(request: Request):
        """Clear TTS cache"""
        try:
            _speech_owner(request)
            tts_service.clear_cache()
            return {"success": True, "message": "Cache cleared"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to clear TTS cache", exc_info=True)
            raise HTTPException(status_code=500, detail="Cache clear failed") from e

    return router
