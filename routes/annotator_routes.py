"""Allow a signed-in browser to submit a private Annotator observation."""

import json
import re
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from src.auth_helpers import require_user
from src.openclank.annotator_feedback import (
    FeedbackConflict,
    FeedbackUnavailable,
    record_feedback,
    resolve_source_checkout,
)


_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.:\-]*")
_MAX_REQUEST_BYTES = 64 * 1024


def _browser_account(request: Request) -> str:
    if getattr(request.state, "internal_tool_authenticated", False):
        raise HTTPException(403, "Browser authentication required")
    user = require_user(request)
    account = request.app.state.auth_manager.account_id(user)
    if not account:
        raise HTTPException(401, "Account unavailable")
    return account


async def _read_submission(request: Request) -> tuple[str, str, str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON submission required")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_REQUEST_BYTES:
            raise HTTPException(413, "Feedback is too large")
        body.extend(chunk)
    try:
        fields = json.loads(body)
        if not isinstance(fields, dict) or set(fields) != {"submission_id", "page", "element", "comment"}:
            raise ValueError
        receipt, page, element, comment = (fields[key] for key in ("submission_id", "page", "element", "comment"))
        if not all(isinstance(value, str) for value in (receipt, page, element, comment)):
            raise ValueError
        if len(receipt) != 36 or str(UUID(receipt)) != receipt:
            raise ValueError
        if len(page) > 96 or len(element) > 120 or not (_IDENTIFIER.fullmatch(page) and _IDENTIFIER.fullmatch(element)):
            raise ValueError
        if not comment.strip() or len(comment) > 4000:
            raise ValueError
        # Reject lone escaped surrogates before reaching the private writer.
        comment.encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(422, "Invalid feedback submission") from exc
    return receipt, page, element, comment


def setup_annotator_routes(*, source_root: Path, configured_root: str | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/annotator", tags=["annotator"])
    checkout = resolve_source_checkout(source_root, configured_root)

    @router.get("/availability")
    def availability(request: Request, response: Response):
        _browser_account(request)
        response.headers["Cache-Control"] = "no-store"
        return {"available": checkout is not None}

    @router.post("/feedback")
    async def feedback(request: Request, response: Response):
        account = _browser_account(request)
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.headers.get("origin") != expected_origin or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Same-origin feedback required")
        if checkout is None:
            raise HTTPException(503, "Annotator is unavailable")
        receipt, page, element, comment = await _read_submission(request)
        try:
            status = await run_in_threadpool(
                record_feedback, checkout, account=account, submission_id=receipt,
                page=page, element=element, comment=comment,
            )
        except FeedbackConflict as exc:
            raise HTTPException(409, "Submission ID already used") from exc
        except FeedbackUnavailable as exc:
            raise HTTPException(503, "Feedback storage is unavailable") from exc
        response.headers["Cache-Control"] = "no-store"
        return {"status": status, "submission_id": receipt}

    return router
