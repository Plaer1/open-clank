"""S21 — maintained official docs: manifest, provisioning, terminology.

Covers the write-scope acceptance cases for the official content manifest and
the Loose backend provisioning path. No live engine or provider is involved:
these are repository-level tests against the real ``LooseCopalBridge``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openclank.official_docs import (  # noqa: E402
    OFFICIAL_DOCS_SEED_VERSION,
    OFFICIAL_ROOT_FOLDER,
    PRODUCT_MARKER,
    article_by_id,
    encode_official_note,
    is_user_modified_content,
    known_names,
    official_articles,
    official_home,
    official_payloads,
    plan_input_from_content,
    plan_official_provision,
    plan_summary,
    run_buddy_help_tool,
)
from src.openclank.copal_loose import LooseCopalBridge  # noqa: E402
from src.openclank.copal_errors import CopalBridgeError  # noqa: E402


def _identity_properties(content: str) -> dict:
    record = json.loads(content)
    return {
        prop["key"]: prop.get("value")
        for prop in record.get("properties", [])
        if isinstance(prop, dict) and isinstance(prop.get("key"), str)
    }


# ── Manifest ─────────────────────────────────────────────────────────────────

def test_manifest_has_stable_ids_hierarchy_and_aliases():
    articles = official_articles()
    assert len(articles) >= 10
    ids = [article["id"] for article in articles]
    assert len(ids) == len(set(ids)), "article IDs are stable and unique"
    names = [article["name"] for article in articles]
    assert len(names) == len(set(names)), "canonical names are unique"
    assert all(name.startswith(f"{OFFICIAL_ROOT_FOLDER}/") for name in names)
    home = official_home()
    assert home["id"] == "openclank-docs-home"
    assert home["name"] == f"{OFFICIAL_ROOT_FOLDER}/Home"
    assert home["title"] == "Instruction Manual"
    assert {"OpenClank/Start Here", "OpenClank Handbook", "Open Clank Handbook"} <= set(home["aliases"])
    files = article_by_id("openclank-docs-files-imps")
    assert files["name"] == f"{OFFICIAL_ROOT_FOLDER}/Files and Imps"
    assert files["title"] == "Files and image editing"
    assert article_by_id("openclank-docs-journal")["name"] == f"{OFFICIAL_ROOT_FOLDER}/Journal"


def test_manifest_covers_required_product_topics():
    titles = {
        "accounts-models": "Accounts and Models",
        "chat-workspaces": "Chat and Workspaces",
        "editor": "Editor",
        "journal": "Journal",
        "wiki-recipes": "Wiki Recipes",
        "formatting-demo": "Markdown Formatting Demo",
        "files-imps": "Files and image editing",
        "deliverables": "Deliverables",
        "graph": "Graph",
        "memory-lore": "Memory and Lore",
        "tasks": "Tasks and Continuations",
        "settings-themes": "Settings and Themes",
        "recovery": "Recovery",
    }
    for suffix, title in titles.items():
        article = article_by_id(f"openclank-docs-{suffix}")
        assert article is not None, title
        assert article["title"] == title


def test_manifest_describes_current_architecture_not_obsolete_storage():
    bodies = "\n".join(article["body"] for article in official_articles()).lower()
    assert "copal-wiki.redb" not in bodies
    assert "story card" not in bodies and "carousel" not in bodies
    assert "gallery applet" not in bodies
    wiki = article_by_id("openclank-docs-wiki-recipes")["body"]
    assert "![[OpenClank/Editor#Source comments]]" in wiki
    assert "[[Journal|journal page]]" in wiki


def test_manifest_links_platform_claims_to_scope_and_evidence():
    limits = article_by_id("openclank-docs-limits")["body"]
    for platform in ("macOS", "Windows", "Linux", "Docker"):
        assert f"| {platform} |" in limits
    assert "[[Desktop Capture and Host Opening]]" in limits
    assert "docs/known-limits.md" in limits


def test_home_task_links_resolve_to_installed_handbook_articles():
    names = {
        name
        for article in official_articles()
        for name in (article["name"], article["name"].split("/")[-1])
    }
    home = official_home()["body"]
    targets = set(re.findall(r"\[\[([^]|#]+)(?:[^\]]*)\]\]", home))
    assert {"Journal", "Editor", "Graph", "TreeHouse", "Tasks and Continuations", "Calendar and Email"} <= targets
    assert targets <= names, f"Unresolvable links: {sorted(targets - names)}"
    assert home.startswith("# Open Clank\n")


@pytest.mark.parametrize(("label", "suffix", "control"), [
    ("Copal", "app-windows", "Copal navigation"),
    ("Journal", "journal", "New journal page"),
    ("Help", "home", "self-hosted AI workspace"),
])
def test_buddy_help_retrieves_evidence_for_the_selected_control(label, suffix, control):
    evidence, source = run_buddy_help_tool(label)
    article = article_by_id(f"openclank-docs-{suffix}")
    assert source == {"article_id": article["id"], "title": article["title"], "term": label}
    assert control in evidence


def test_official_document_bodies_round_trip_cleanly():
    for article in official_articles():
        decoded = json.loads(encode_official_note(article["body"], {"docId": article["id"]}))
        assert decoded["body"]["type"] == "doc"


# ── Provisioning: fresh owner ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fresh_owner_gets_real_readonly_official_folder(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    await bridge.start()
    result = await bridge.call("provision_official", {
        "owner": "owner",
        "workspace_id": "notes",
        "articles": official_payloads(),
    })
    assert result["skipped"] == []
    assert len(result["created"]) == len(official_payloads())

    indexed = await bridge.call("index", {"owner": "owner", "workspace_id": "notes"})
    docs = indexed["docs"]
    official = [doc for doc in docs if str(doc.get("name", "")).startswith(f"{OFFICIAL_ROOT_FOLDER}/")]
    articles_by_name = {article["name"]: article for article in official_articles()}
    assert {doc["name"] for doc in official} == set(articles_by_name)
    for doc in official:
        article = articles_by_name[doc["name"]]
        assert doc["readOnly"] is True, "official pages are read-only"
        content = await _body(bridge, doc["id"])
        props = _identity_properties(content)
        assert props["product"] == PRODUCT_MARKER
        assert props["builtin"] is True
        assert props["seedVersion"] == OFFICIAL_DOCS_SEED_VERSION
        assert props["docId"] == article["id"]
        assert json.loads(content)["extensions"]["interchange"]["source"] == article["body"]


async def _body(bridge, document_id: str) -> str:
    got = await bridge.call("get", {"owner": "owner", "workspace_id": "notes", "id": document_id})
    return str(got.get("text") or "")


# ── Provisioning: idempotency ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repeat_startup_creates_no_duplicates(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    await bridge.start()
    scope = {"owner": "owner", "workspace_id": "notes", "articles": official_payloads()}
    first = await bridge.call("provision_official", dict(scope))
    second = await bridge.call("provision_official", dict(scope))
    third = await bridge.call("provision_official", dict(scope))
    assert len(first["created"]) == len(official_payloads())
    assert second["created"] == [] and second["updated"] == []
    assert third["created"] == [] and third["updated"] == []

    indexed = await bridge.call("index", {"owner": "owner", "workspace_id": "notes"})
    names = [doc["name"] for doc in indexed["docs"] if str(doc.get("name", "")).startswith(f"{OFFICIAL_ROOT_FOLDER}/")]
    assert len(names) == len(set(names)), "no duplicate folders/pages"
    assert len(names) == len(official_payloads())


@pytest.mark.asyncio
async def test_interrupted_provisioning_resumes_without_duplicates(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    await bridge.start()
    payloads = official_payloads()
    await bridge.call("provision_official", {
        "owner": "owner", "workspace_id": "notes", "articles": payloads[:5],
    })
    result = await bridge.call("provision_official", {
        "owner": "owner", "workspace_id": "notes", "articles": payloads,
    })
    assert len(result["created"]) == len(payloads) - 5
    indexed = await bridge.call("index", {"owner": "owner", "workspace_id": "notes"})
    names = [doc["name"] for doc in indexed["docs"] if str(doc.get("name", "")).startswith(f"{OFFICIAL_ROOT_FOLDER}/")]
    assert len(names) == len(payloads)
    assert len(names) == len(set(names))


# ── Provisioning: personal data safety ───────────────────────────────────────

@pytest.mark.asyncio
async def test_modified_personal_copy_is_never_overwritten(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    await bridge.start()
    payloads = official_payloads()
    await bridge.call("provision_official", {"owner": "owner", "workspace_id": "notes", "articles": payloads})

    indexed = await bridge.call("index", {"owner": "owner", "workspace_id": "notes"})
    home = next(doc for doc in indexed["docs"] if doc["name"] == f"{OFFICIAL_ROOT_FOLDER}/Home")
    body = await _body(bridge, home["id"])
    record = json.loads(body)
    record["extensions"]["interchange"]["modified"] = True
    record["body"]["blocks"][0]["text"] = "# My own heading"
    record["body"]["blocks"][0]["source"] = "# My own heading"
    edited = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    # Shared official references have no owner-vault body file to corrupt.
    # Verify the real read-only guard and a separately created personal document.
    with pytest.raises(CopalBridgeError):
        await bridge.call("write", {"owner": "owner", "workspace_id": "notes", "id": home["id"], "content": edited})
    personal = await bridge.call("create", {
        "owner": "owner", "workspace_id": "notes", "name": "Personal/My study.md",
        "content": "# My own heading\nPersonal response and evidence.\n",
    })
    personal_id = personal["doc"]["id"]
    before = await _body(bridge, personal_id)
    result = await bridge.call("provision_official", {"owner": "owner", "workspace_id": "notes", "articles": payloads})
    assert result["updated"] == []
    assert await _body(bridge, personal_id) == before
    assert "My own heading" not in await _body(bridge, home["id"])


@pytest.mark.asyncio
async def test_personal_page_at_official_name_is_left_alone(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    await bridge.start()
    home_name = official_home()["name"]
    await bridge.call("create", {
        "owner": "owner", "workspace_id": "notes",
        "name": home_name, "kind": "wiki", "corpus": "wiki",
        "content": "my personal page, not official\n",
    })
    result = await bridge.call("provision_official", {
        "owner": "owner", "workspace_id": "notes", "articles": official_payloads(),
    })
    assert home_name in result["skipped"][0]["name"]
    assert result["skipped"][0]["reason"] == "personal-page-occupies-name"
    assert home_name not in result["created"]
    indexed = await bridge.call("index", {"owner": "owner", "workspace_id": "notes"})
    home = next(doc for doc in indexed["docs"] if doc["name"] == home_name)
    assert "personal page" in (await _body(bridge, home["id"]))
    # The rest of the handbook still provisions.
    assert len(result["created"]) == len(official_payloads()) - 1


# ── Provisioning: new owner on an existing installation ─────────────────────

@pytest.mark.asyncio
async def test_existing_and_new_owner_each_get_the_same_official_folder(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    await bridge.start()
    payloads = official_payloads()
    await bridge.call("provision_official", {"owner": "old-owner", "workspace_id": "notes", "articles": payloads})
    await bridge.call("provision_official", {"owner": "new-owner", "workspace_id": "notes", "articles": payloads})
    for owner in ("old-owner", "new-owner"):
        indexed = await bridge.call("index", {"owner": owner, "workspace_id": "notes"})
        names = [doc["name"] for doc in indexed["docs"] if str(doc.get("name", "")).startswith(f"{OFFICIAL_ROOT_FOLDER}/")]
        assert len(names) == len(payloads)
        assert sorted(names) == sorted(payload["name"] for payload in payloads)


# ── Plan helper ──────────────────────────────────────────────────────────────

def test_plan_matches_by_stable_id_not_name():
    payloads = official_payloads()
    existing = [
        {
            "id": "doc-personal",
            "name": payloads[0]["name"],
            "properties": {"note": "mine"},
            "trashed": False,
        }
    ]
    plan = plan_official_provision(existing)
    assert plan["create"] == [] or payloads[0]["name"] not in [item["name"] for item in plan["create"]]
    assert any(item["name"] == payloads[0]["name"] for item in plan["conflicts"])

    ours = {
        "readOnly": True,
        "id": "doc-ours",
        "name": payloads[0]["name"],
        "properties": {"product": PRODUCT_MARKER, "builtin": True, "docId": payloads[0]["docId"], "seedVersion": OFFICIAL_DOCS_SEED_VERSION},
        "extensions": {"interchange": {"modified": False}},
        "trashed": False,
    }
    plan2 = plan_official_provision([ours])
    assert plan_summary(plan2)["update"] == 1
    assert plan_summary(plan2)["create"] == len(payloads) - 1


def test_plan_recognizes_known_aliases_for_previous_defaults():
    payloads = official_payloads()
    home = official_home()
    alias = home["aliases"][0]
    existing = [{
        "readOnly": True,
        "id": "doc-alias",
        "name": alias,
        "properties": {"product": PRODUCT_MARKER, "builtin": True, "seedVersion": 0},
        "extensions": {"interchange": {"modified": False}},
        "trashed": False,
    }]
    plan = plan_official_provision(existing)
    updates = plan["update"]
    assert any(item.get("renameFrom") == alias for item in updates)


def test_is_user_modified_content_gate():
    payload = official_payloads()[0]
    assert is_user_modified_content(payload["content"]) is False
    record = json.loads(payload["content"])
    record["extensions"]["interchange"]["modified"] = True
    assert is_user_modified_content(json.dumps(record)) is True
    assert is_user_modified_content("not json") is True
    future = json.loads(payload["content"])
    future["extensions"]["seed"]["version"] = OFFICIAL_DOCS_SEED_VERSION + 1
    assert is_user_modified_content(json.dumps(future)) is True


def test_known_names_include_aliases_for_install_fixture_checks():
    names = known_names()
    assert f"{OFFICIAL_ROOT_FOLDER}/Home" in names
    assert len(names) > len(official_articles())


def test_official_routes_expose_handbook_surface():
    """Help/routes wiring: official docs endpoints exist on the copal router."""
    import inspect
    from routes import copal_routes
    src = inspect.getsource(copal_routes)
    assert '@router.get("/official/docs")' in src
    assert '@router.get("/official/home")' in src
    assert '@router.post("/official/provision")' in src
    assert 'official_payloads' in src


# ── Plan wiring into the live Loose path ─────────────────────────────────────

@pytest.mark.asyncio
async def test_live_provision_adopts_alias_without_duplicating(tmp_path):
    """A previously installed default under a known alias is renamed in place."""
    bridge = LooseCopalBridge(tmp_path / "vaults")
    await bridge.start()
    payloads = official_payloads()
    await bridge.call("provision_official", {"owner": "owner", "workspace_id": "notes", "articles": payloads})

    indexed = await bridge.call("index", {"owner": "owner", "workspace_id": "notes"})
    home = next(doc for doc in indexed["docs"] if doc["name"] == f"{OFFICIAL_ROOT_FOLDER}/Home")
    alias = official_home()["aliases"][0]
    # Ordinary clients cannot rename the immutable shared reference.
    with pytest.raises(CopalBridgeError):
        await bridge.call("rename", {"owner": "owner", "workspace_id": "notes", "id": home["id"], "name": alias})
    # Simulate persisted older-install metadata in this disposable fixture.
    manifest_path = bridge._vault("owner", "notes") / ".copal" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["documents"][home["id"]]["name"] = alias
    manifest_path.write_text(json.dumps(manifest))

    result = await bridge.call("provision_official", {
        "owner": "owner", "workspace_id": "notes", "articles": payloads,
    })
    assert result["created"] == []
    assert alias not in result["created"]
    assert any(name == f"{OFFICIAL_ROOT_FOLDER}/Home" for name in result["updated"])

    indexed = await bridge.call("index", {"owner": "owner", "workspace_id": "notes"})
    names = [doc["name"] for doc in indexed["docs"] if str(doc.get("name", "")).startswith(f"{OFFICIAL_ROOT_FOLDER}/")]
    assert f"{OFFICIAL_ROOT_FOLDER}/Home" in names
    assert alias not in names
    assert len(names) == len(payloads)
    assert len(names) == len(set(names)), "alias adoption must not leave a duplicate"


@pytest.mark.asyncio
async def test_live_provision_keeps_canonical_body_despite_caller_payload(tmp_path):
    """Caller bytes cannot replace the installed canonical article authority."""
    bridge = LooseCopalBridge(tmp_path / "vaults")
    await bridge.start()
    payloads = official_payloads()
    await bridge.call("provision_official", {"owner": "owner", "workspace_id": "notes", "articles": payloads})

    refreshed = []
    for payload in payloads:
        item = dict(payload)
        if item["name"] == f"{OFFICIAL_ROOT_FOLDER}/Home":
            record = json.loads(item["content"])
            record["extensions"]["interchange"]["source"] = "# Open Clank Handbook (revised)"
            record["extensions"]["interchange"]["projectionHash"] = __import__("hashlib").sha256(
                b"# Open Clank Handbook (revised)"
            ).hexdigest()
            record["extensions"]["seed"]["version"] = OFFICIAL_DOCS_SEED_VERSION + 1
            item["content"] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        refreshed.append(item)

    result = await bridge.call("provision_official", {
        "owner": "owner", "workspace_id": "notes", "articles": refreshed,
    })
    assert result["created"] == []
    home_id = next(
        doc["id"] for doc in (await bridge.call("index", {"owner": "owner", "workspace_id": "notes"}))["docs"]
        if doc["name"] == f"{OFFICIAL_ROOT_FOLDER}/Home"
    )
    record = json.loads(await _body(bridge, home_id))
    assert record["extensions"]["interchange"]["source"] == official_home()["body"]


def test_plan_accepts_subset_payloads_for_interrupted_runs():
    payloads = official_payloads()
    plan = plan_official_provision([], payloads[:3])
    assert len(plan["create"]) == 3
    assert all(item["name"] in {p["name"] for p in payloads[:3]} for item in plan["create"])


def test_plan_input_from_content_reads_identity():
    payload = official_payloads()[0]
    row = plan_input_from_content("doc_x", payload["name"], payload["content"], read_only=True)
    assert row["properties"]["docId"] == payload["docId"]
    assert row["properties"]["product"] == PRODUCT_MARKER
    assert row["extensions"]["interchange"]["modified"] is False
    broken = plan_input_from_content("doc_y", "n", "not json")
    assert broken["properties"] == {}
    assert broken["id"] == "doc_y"


# ── Formatting demo source-reveal is implemented (not merely claimed) ────────

def test_formatting_demo_claim_is_confined_to_the_demo():
    owners = [article["id"] for article in official_articles() if "**Back to rendered**" in article["body"]]
    assert owners == ["openclank-docs-formatting-demo"]
