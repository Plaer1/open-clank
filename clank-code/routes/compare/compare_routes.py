# routes/compare_routes.py
"""Model A/B comparison routes."""
import json
import os
import uuid
import random
from datetime import datetime
from fastapi import APIRouter, Form, HTTPException, Request
from typing import List
from pydantic import BaseModel
import logging

from core.database import Comparison, SessionLocal
from core.session_manager import SessionManager
from src.auth_helpers import get_current_user
from src.openclank.chat_routing import (
    MANAGED_ENGINE_PUBLIC_URL,
    ChatRouteUnavailable,
    resolve_chat_route,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/compare", tags=["compare"])


class RecordVoteRequest(BaseModel):
    prompt: str
    models: List[str]
    winner: str           # model name or "tie"
    is_blind: bool = True


def setup_compare_routes(session_manager: SessionManager):
    """Setup comparison routes."""

    @router.post("/start")
    async def start_comparison(
        request: Request,
        prompt: str = Form(...),
        model_a: str = Form(...),
        model_b: str = Form(...),
        endpoint_a: str = Form(""),
        endpoint_b: str = Form(""),
        endpoint_a_id: str = Form(""),
        endpoint_b_id: str = Form(""),
        is_blind: str = Form("true"),
    ):
        """Create two ephemeral sessions and a comparison record.

        Returns the comparison ID and the two session IDs so the client
        can fire two independent SSE streams to /api/chat_stream.
        """
        user = getattr(request.state, 'current_user', None)
        comp_id = str(uuid.uuid4())
        sid_a = str(uuid.uuid4())
        sid_b = str(uuid.uuid4())

        # Blind mapping: randomly assign left/right
        blind = str(is_blind).lower() == "true"
        if blind:
            mapping = {"left": "a", "right": "b"}
            if random.random() > 0.5:
                mapping = {"left": "b", "right": "a"}
        else:
            mapping = {"left": "a", "right": "b"}

        # Map session IDs to left/right based on blind mapping
        session_left = sid_a if mapping["left"] == "a" else sid_b
        session_right = sid_a if mapping["right"] == "a" else sid_b

        # In blind mode, name the helper sessions by their neutral slot
        # ("Model A" / "Model B") instead of the real model. Otherwise the
        # session name leaks the model in the sidebar and GET /api/sessions,
        # de-anonymizing the comparison before the user votes (issue #1285).
        slot_name = {session_left: "Model A", session_right: "Model B"}

        # Resolve and authorize BOTH normalized routes before creating either
        # helper session. Raw endpoint URLs are legacy display fields only and
        # never become network authority; provider credentials stay inside the
        # managed engine lease boundary.
        resolved = []
        for sid, model, endpoint_id in [
            (sid_a, model_a, endpoint_a_id),
            (sid_b, model_b, endpoint_b_id),
        ]:
            eid = endpoint_id.strip() if isinstance(endpoint_id, str) else ""
            if not eid:
                raise HTTPException(
                    422,
                    "endpoint_a_id and endpoint_b_id must identify normalized provider connections",
                )
            try:
                route = resolve_chat_route(
                    owner=user,
                    endpoint_id=eid,
                    model_id=model,
                )
            except ChatRouteUnavailable as exc:
                raise HTTPException(400, str(exc)) from exc

            auth_manager = getattr(request.app.state, "auth_manager", None)
            get_privileges = getattr(auth_manager, "get_privileges", None)
            privileges = get_privileges(user) if get_privileges and user else {}
            allowed = set((privileges or {}).get("allowed_models") or [])
            restricted = bool((privileges or {}).get("allowed_models_restricted")) or bool(allowed)
            if (privileges or {}).get("block_all_models") or (
                restricted
                and route.provider_model_id not in allowed
                and route.model_route_id not in allowed
            ):
                raise HTTPException(
                    403,
                    f"Your account is not allowed to use model {route.provider_model_id!r}",
                )
            resolved.append((sid, route))

        # Both routes validated — only now create the secret-free helper sessions.
        for sid, route in resolved:
            name = (
                f"[CMP] {slot_name[sid]}"
                if blind
                else f"[CMP] {route.provider_model_id.split('/')[-1]}"
            )
            session_manager.create_session(
                session_id=sid,
                name=name,
                endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
                model=route.provider_model_id,
                rag=False,
                owner=user,
                endpoint_id=route.public_endpoint_id,
                provider_model_route_id=route.model_route_id,
            )

        # Store comparison record
        db = SessionLocal()
        try:
            comp = Comparison(
                id=comp_id,
                prompt=prompt,
                model_a=model_a,
                model_b=model_b,
                # Historical non-null endpoint fields retain only the public
                # managed sentinel. Stable route IDs carry execution identity.
                endpoint_a=MANAGED_ENGINE_PUBLIC_URL,
                endpoint_b=MANAGED_ENGINE_PUBLIC_URL,
                provider_model_route_a_id=resolved[0][1].model_route_id,
                provider_model_route_b_id=resolved[1][1].model_route_id,
                is_blind=blind,
                blind_mapping=json.dumps(mapping),
                owner=user,
            )
            db.add(comp)
            db.commit()
        finally:
            db.close()

        # In blind mode, withhold the model identities AND the left/right
        # mapping from the response. The client already knows model_a/model_b
        # (it sent them), so returning either would defeat blind mode. They are
        # revealed by POST /api/compare/{id}/vote once the user has voted (#1285).
        return {
            "id": comp_id,
            "session_left": session_left,
            "session_right": session_right,
            "model_left": None if blind else (model_a if mapping["left"] == "a" else model_b),
            "model_right": None if blind else (model_a if mapping["right"] == "a" else model_b),
            "is_blind": blind,
            "mapping": None if blind else mapping,
        }

    @router.post("/{comp_id}/vote")
    def vote_comparison(
        request: Request,
        comp_id: str,
        winner: str = Form(...),  # "left", "right", or "tie"
    ):
        """Record the user's vote and reveal model names if blind."""
        user = get_current_user(request)
        db = SessionLocal()
        try:
            comp = db.query(Comparison).filter(Comparison.id == comp_id).first()
            if not comp:
                raise HTTPException(404, "Comparison not found")
            # SECURITY: strict ownership — null-owner Comparisons were
            # accessible to every user.
            if user and comp.owner != user:
                raise HTTPException(404, "Comparison not found")
            if comp.winner:
                raise HTTPException(400, "Already voted")

            mapping = json.loads(comp.blind_mapping) if comp.blind_mapping else {"left": "a", "right": "b"}

            if winner == "tie":
                comp.winner = "tie"
            elif winner == "left":
                comp.winner = mapping["left"]
            elif winner == "right":
                comp.winner = mapping["right"]
            else:
                raise HTTPException(400, "winner must be 'left', 'right', or 'tie'")

            comp.voted_at = datetime.utcnow()
            db.commit()

            return {
                "winner": comp.winner,
                "model_a": comp.model_a,
                "model_b": comp.model_b,
                "revealed": {
                    "left": comp.model_a if mapping["left"] == "a" else comp.model_b,
                    "right": comp.model_a if mapping["right"] == "a" else comp.model_b,
                },
            }
        finally:
            db.close()

    @router.post("/record")
    def record_comparison(request: Request, body: RecordVoteRequest):
        """Lightweight endpoint to record a comparison vote from the frontend."""
        user = get_current_user(request)
        comp_id = str(uuid.uuid4())

        model_a = body.models[0] if len(body.models) > 0 else ""
        model_b = body.models[1] if len(body.models) > 1 else ""

        # For N>2 models, store the full list as JSON in blind_mapping
        if len(body.models) > 2:
            blind_mapping = json.dumps({"models": body.models})
        else:
            blind_mapping = None

        db = SessionLocal()
        try:
            comp = Comparison(
                id=comp_id,
                prompt=body.prompt[:500],
                model_a=model_a,
                model_b=model_b,
                endpoint_a="",
                endpoint_b="",
                winner=body.winner,
                is_blind=body.is_blind,
                blind_mapping=blind_mapping,
                voted_at=datetime.utcnow(),
                owner=user,
            )
            db.add(comp)
            db.commit()
        finally:
            db.close()

        return {"status": "ok", "id": comp_id}

    @router.get("/history")
    def list_comparisons(request: Request):
        """List past comparisons."""
        user = get_current_user(request)
        db = SessionLocal()
        try:
            q = db.query(Comparison)
            if user:
                q = q.filter(Comparison.owner == user)
            comps = q.order_by(Comparison.created_at.desc()).limit(50).all()
            return [
                {
                    "id": c.id,
                    "prompt": c.prompt[:100],
                    "model_a": c.model_a,
                    "model_b": c.model_b,
                    "winner": c.winner,
                    "is_blind": c.is_blind,
                    "voted_at": c.voted_at.isoformat() if c.voted_at else None,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in comps
            ]
        finally:
            db.close()

    @router.delete("/{comp_id}")
    def delete_comparison(request: Request, comp_id: str):
        """Delete a comparison and its ephemeral sessions."""
        user = get_current_user(request)
        db = SessionLocal()
        try:
            comp = db.query(Comparison).filter(Comparison.id == comp_id).first()
            if not comp:
                raise HTTPException(404, "Comparison not found")
            # SECURITY: strict ownership — null-owner Comparisons were
            # accessible to every user.
            if user and comp.owner != user:
                raise HTTPException(404, "Comparison not found")
            db.delete(comp)
            db.commit()
            return {"status": "deleted"}
        finally:
            db.close()

    return router
