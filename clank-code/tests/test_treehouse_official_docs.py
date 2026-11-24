"""TreeHouse handbook identity/alias/privacy contracts; no prose-size checks."""
import json
from src.openclank.official_docs import article_by_id, known_names, official_reference, official_payloads, OFFICIAL_DOCS_SEED_VERSION


def test_treehouse_title_change_keeps_stable_identity_and_previous_aliases():
    article=article_by_id("openclank-docs-treehouse")
    assert article["name"]=="OpenClank/TreeHouse"
    assert "TreeHouse and Field Guide" in article["aliases"]
    assert "OpenClank/TreeHouse and Field Guide" in known_names()
    reference=official_reference(article["id"])
    assert reference["version"]==OFFICIAL_DOCS_SEED_VERSION
    assert reference["docId"]==article["id"]


def test_learning_body_contains_no_private_author_payload_or_executable_source():
    article=article_by_id("openclank-docs-treehouse")
    assert ".clanker/" not in article["body"]
    assert '"completionCriteria"' not in article["body"]
    payload=next(p for p in official_payloads() if p.get("docId")==article["id"])
    assert payload["read_only"] is True
    record=json.loads(payload["content"])
    properties={p["key"]:p.get("value") for p in record["properties"]}
    assert properties["docId"]==article["id"]
    assert properties["builtin"] is True


def test_treehouse_media_resolves_with_provenance_and_file_integrity():
    from pathlib import Path
    import hashlib
    from src.openclank.official_docs_assets import ASSETS
    from src.openclank.official_docs_content import ARTICLE_ASSETS
    active = ARTICLE_ASSETS["openclank-docs-treehouse"]
    assert active
    body = article_by_id("openclank-docs-treehouse")["body"]
    for asset_id in active:
        asset = ASSETS[asset_id]
        assert asset["state"] == "ready"
        assert asset["alt"] and asset["caption"] and asset["qualification"]
        provenance = asset["provenance"]
        assert provenance["kind"] and provenance["license"]
        if asset["qualification"] == "source-candidate-normal-auth":
            assert provenance["source_digest"].startswith("sha256:")
            assert asset["masters"][0]["mime"] == "image/png"
        else:
            assert provenance.get("source_digest") or provenance.get("source")
        assert next(item["url"] for item in asset["variants"] if item["role"] == "image") in body

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    assert any(
        item["url"].lstrip("/") in readme
        for asset_id in active
        for item in ASSETS[asset_id]["variants"]
        if item["role"] in {"image", "image-960"}
    ), "the README illustration must resolve to a current handbook asset"
    for asset_id in active:
        asset = ASSETS[asset_id]
        for item in asset["masters"] + asset["variants"]:
            file = Path(item.get("path") or item["url"].lstrip("/"))
            assert file.stat().st_size == item["bytes"]
            assert hashlib.sha256(file.read_bytes()).hexdigest() == item["sha256"]
    assert 'treehouse-player-20261007' not in body


def test_four_root_guidance_distinguishes_draft_submission_and_practical_evidence():
    body = article_by_id("openclank-docs-treehouse")["body"]
    intro = body.split("## Explore a Quest", 1)[0]
    for name in ("Programming", "Games", "AI", "Open Clank"):
        assert name in intro
    assert "all-global reset includes the three" not in body
    for control in ("Save draft", "Submit response", "Save attempt", "I achieved the stated outcome", "Previous answer"):
        assert f"**{control}**" in body
    assert "blocked or failed attempt stays pending" in body
    assert "Self-reported work keeps that status until reviewed or verified" in body
    assert "earlier three-Main-Quest source candidate" not in body


def test_storage_activation_guidance_is_separate_from_retiring_edited_defaults():
    from pathlib import Path
    import subprocess
    import sys
    body = article_by_id("openclank-docs-treehouse")["body"]
    guide = Path("docs/upgrading.md").read_text()
    assert "Recheck shared quests" in body and "private learning stays available" in body
    assert "docs/upgrading.md" in body
    assert "schema-only activation" in guide
    assert "both pristine and edited\nrecognized defaults" in guide
    assert "revokes/deactivates their grants" in guide
    assert "only recognized pristine defaults" not in guide
    assert "not preserved as active courses" in guide
    for mode in ("preview", "apply", "verify", "restore"):
        assert f"scripts/activate_treehouse_storage.py {mode} --db" in guide
    assert "--writers-stopped" in guide and "does not stop processes" in guide
    assert "availability.status: ready" in guide and "outputDigest" in guide
    help_text = subprocess.run([sys.executable, "scripts/activate_treehouse_storage.py", "--help"], capture_output=True, text=True, check=True).stdout
    for flag in ("--db", "--operation-id", "--preview", "--backup", "--receipt", "--output", "--writers-stopped"):
        assert flag in help_text and flag in guide


def test_current_learner_discovery_and_solo_navigation_guidance():
    body = article_by_id("openclank-docs-treehouse")["body"]
    for label in ("Explore Quests", "Connected lessons", "Skills", "Library → Search learning", "Records",
                  "Copal → Achievements", "Completion evidence", "How to earn it", "Author → Open source"):
        assert f"**{label}**" in body
    for obsolete in ("Explore topics", "Explore subject", "Resume subject", "Reset this subject", "Discuss & board"):
        assert obsolete not in body
    assert "Eligible courses offer completion records" in body
    assert "Course sharing uses separate grants" in body
