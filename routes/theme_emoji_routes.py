"""Same-origin routes resolving solely to immutable packaged emoji artwork."""
import sqlite3
import sys
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from src.openclank.theme_emoji_assets import get_google_catalog, get_kitchen_catalog_text, get_packaged_asset, artwork_install_hint

router = APIRouter(prefix="/api/theme-emoji", tags=["theme-emoji"])
_CATALOG_HEADERS = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error)


def _unavailable_detail() -> str:
    message = "Packaged emoji artwork is unavailable"
    if getattr(sys, "frozen", False):
        message += ". " + artwork_install_hint()
    return message


@router.get("/kitchen/catalog", response_class=PlainTextResponse)
def kitchen_catalog():
    try:
        return PlainTextResponse(get_kitchen_catalog_text(), headers=_CATALOG_HEADERS)
    except _ERRORS as exc:
        raise HTTPException(status_code=503, detail=_unavailable_detail()) from exc


@router.get("/google/catalog")
def google_catalog():
    try:
        return JSONResponse(get_google_catalog(), headers=_CATALOG_HEADERS)
    except _ERRORS as exc:
        raise HTTPException(status_code=503, detail=_unavailable_detail()) from exc


def _image(asset_id: str, kind: str, request: Request):
    try:
        data, media_type, digest = get_packaged_asset(asset_id, kind)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Packaged emoji image not found") from exc
    except _ERRORS as exc:
        raise HTTPException(status_code=503, detail=_unavailable_detail()) from exc
    headers = {"Cache-Control": "private, max-age=86400, immutable", "ETag": '"' + digest + '"', "X-Content-Type-Options": "nosniff"}
    if request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type=media_type, headers=headers)


@router.get("/kitchen/{asset_id}")
def kitchen_asset(asset_id: str, request: Request):
    return _image(asset_id, "kitchen", request)


@router.get("/google/{asset_id}")
def google_asset(asset_id: str, request: Request):
    return _image(asset_id, "google", request)
