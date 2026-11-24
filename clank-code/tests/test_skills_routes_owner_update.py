import json
import textwrap
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.datastructures import State

import routes.skills_routes as skills_routes
from routes.skills_routes import (
    SkillAddRequest,
    SkillImportUrlRequest,
    SkillUpdateRequest,
    setup_skills_routes,
)
from services.memory.skill_format import slugify
from services.memory.skill_importer import ResolvedSource
from services.memory.skills import SkillsManager


def _write_skill_md(skills_root: Path, category: str, name: str,
                    owner: str, description: str = "test") -> Path:
    skill_dir = skills_root / slugify(category or "general", fallback="general") / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = textwrap.dedent(f"""\
        ---
        name: {name}
        description: {description}
        version: 1.0.0
        category: {category}
        tags: []
        status: draft
        confidence: 0.8
        source: learned
        owner: {owner}
        created: 2026-01-01T00:00:00Z
        ---

        # When to use
        test

        # Procedure
        - step 1
        """)
    path = skill_dir / "SKILL.md"
    path.write_text(md, encoding="utf-8")
    return path


def _request(user: str, body=None) -> Request:
    class DummyApp:
        state = State()

    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope={
        "type": "http",
        "method": "POST" if body is not None else "PUT",
        "headers": [(b"content-type", b"application/json")] if body is not None else [],
        "app": DummyApp(),
        "state": {"current_user": user},
    }, receive=receive)


def _route_handler(router, path: str, method: str):
    return next(
        route.endpoint for route in router.routes
        if route.path == path and method in route.methods
    )


@pytest.mark.asyncio
async def test_update_skill_route_passes_owner_to_manager(tmp_path):
    skills_root = tmp_path / "skills"
    alice_path = _write_skill_md(skills_root, "alice-cat", "caveman-mode", "alice", "alice original")
    bob_path = _write_skill_md(skills_root, "bob-cat", "caveman-mode", "bob", "bob original")

    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    update_route = _route_handler(router, "/api/skills/{skill_id}", "PUT")

    result = await update_route(
        _request("alice"),
        "caveman-mode",
        SkillUpdateRequest(description="alice updated"),
    )

    assert result == {"ok": True}
    alice_after = alice_path.read_text(encoding="utf-8")
    bob_after = bob_path.read_text(encoding="utf-8")
    assert "status: draft" in alice_after
    assert "alice updated" in alice_after
    assert "status: draft" in bob_after
    assert "bob original" in bob_after


@pytest.mark.asyncio
async def test_save_skill_markdown_route_passes_owner_to_manager(tmp_path):
    skills_root = tmp_path / "skills"
    skill_path = _write_skill_md(skills_root, "general", "caveman-mode", "alice", "before")

    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    save_route = _route_handler(router, "/api/skills/{skill_id}/markdown", "POST")
    markdown = textwrap.dedent("""\
        ---
        name: caveman-mode
        description: after
        version: 1.0.0
        category: general
        tags: []
        status: published
        confidence: 0.9
        source: user
        owner: alice
        created: 2026-01-01T00:00:00Z
        ---

        # When to use
        after

        # Procedure
        - updated step
        """)

    result = await save_route(
        _request("alice", {"markdown": markdown}),
        "caveman-mode",
    )

    assert result == {"ok": True, "name": "caveman-mode"}
    saved = skill_path.read_text(encoding="utf-8")
    assert "description: after" in saved
    assert "status: draft" in saved
    assert "- updated step" in saved


@pytest.mark.asyncio
async def test_authenticated_add_can_explicitly_publish(tmp_path):
    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    add_route = _route_handler(router, "/api/skills/add", "POST")

    result = await add_route(
        _request("alice"),
        SkillAddRequest(
            name="human-published",
            description="explicit user publish",
            when_to_use="test",
            procedure=["step"],
            status="draft",
        ),
    )

    skill = result["skill"]
    sm.set_necessity(skill["skill_id"], True, owner="alice")
    sm.set_audit(skill["skill_id"], "pass", worker_model="judge", owner="alice")
    update_route = _route_handler(router, "/api/skills/{skill_id}", "PUT")
    await update_route(
        _request("alice"),
        skill["name"],
        SkillUpdateRequest(
            status="published",
            expected_revision=skill["revision"],
            expected_hash=skill["content_hash"],
        ),
    )
    assert sm.index_for(owner="alice")[0]["name"] == "human-published"


@pytest.mark.asyncio
async def test_add_requested_as_published_is_returned_as_staged(tmp_path):
    sm = SkillsManager(str(tmp_path))
    add_route = _route_handler(
        setup_skills_routes(sm),
        "/api/skills/add",
        "POST",
    )

    result = await add_route(
        _request("alice"),
        SkillAddRequest(
            name="needs-attestation",
            description="must be audited",
            procedure=["verify it"],
            status="published",
        ),
    )

    assert result["ok"] is True
    assert result["staged"] is True
    assert result["skill"]["status"] == "draft"
    assert result["publish_readiness"]["ready"] is False
    assert sm.load(owner="alice")[0]["active"] is False


@pytest.mark.asyncio
async def test_url_import_persists_only_canonical_pinned_provenance(
    tmp_path,
    monkeypatch,
):
    from services.memory import skill_importer

    sha = "c" * 40
    source = ResolvedSource(
        owner="Example",
        repo="skills",
        ref=sha,
        path="bundles/pinned",
        kind="directory",
    )
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(
        skill_importer,
        "fetch_skill_bundle",
        lambda url: ({
            "SKILL.md": textwrap.dedent("""\
                ---
                name: pinned-route
                description: route provenance
                status: published
                ---

                # Procedure
                - verify
                """),
        }, source),
    )
    sm = SkillsManager(str(tmp_path))
    route = _route_handler(
        setup_skills_routes(sm),
        "/api/skills/import-from-url",
        "POST",
    )

    result = await route(
        _request("alice"),
        SkillImportUrlRequest(
            url="https://github.com/Example/skills/tree/main/bundles/pinned"
        ),
    )

    assert result["skill"]["status"] == "draft"
    assert result["skill"]["source_uri"] == source.canonical_uri
    assert result["skill"]["source_revision"] == sha
    assert "main" not in result["skill"]["source_uri"]
    assert "?" not in result["skill"]["source_uri"]
    assert "@" not in result["skill"]["source_uri"]


def test_passed_audit_uses_explicit_publish_authority(tmp_path, monkeypatch):
    sm = SkillsManager(str(tmp_path))
    sm.add_skill(
        name="audited-skill",
        description="audited",
        when_to_use="test",
        procedure=["step"],
        owner="alice",
    )
    monkeypatch.setattr(
        skills_routes,
        "_audit_auto_publish_policy",
        lambda owner: (True, 0.8),
    )

    status = skills_routes._audit_finalize_status(
        sm,
        "audited-skill",
        "alice",
        "pass",
        0.95,
    )
    assert status == "draft"
    assert sm.load(owner="alice")[0]["status"] == "draft"
    sm.set_necessity("audited-skill", True, owner="alice")
    sm.set_audit("audited-skill", "pass", worker_model="judge", owner="alice")
    ready = sm.publish_readiness("audited-skill", "alice")
    assert sm.publish_skill(
        "audited-skill",
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )

    status = skills_routes._audit_finalize_status(
        sm,
        "audited-skill",
        "alice",
        "fail",
        0.95,
    )
    assert status == "draft"
    assert sm.load(owner="alice")[0]["status"] == "draft"
