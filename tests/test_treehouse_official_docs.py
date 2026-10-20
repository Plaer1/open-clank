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


def test_current_treehouse_media_uses_qualified_unchanged_masters_and_same_readme_hub():
    from pathlib import Path
    import hashlib
    from src.openclank.official_docs_assets import ASSETS
    from src.openclank.official_docs_content import ARTICLE_ASSETS
    active=ARTICLE_ASSETS['openclank-docs-treehouse']
    assert active and all(asset_id.endswith('-20261014') for asset_id in active)
    assert active == ('treehouse-four-main-quests-20261014', 'treehouse-connected-topics-20261014', 'treehouse-openclank-work-20261014', 'treehouse-current-work-20261014')
    for asset_id in active:
        asset=ASSETS[asset_id]
        assert asset['qualification']=='source-candidate-normal-auth'
        assert asset['provenance']['source_digest'].startswith('sha256:')
        for item in asset['masters']+asset['variants']:
            file=Path(item.get('path') or item['url'].lstrip('/'))
            assert file.stat().st_size==item['bytes']
            assert hashlib.sha256(file.read_bytes()).hexdigest()==item['sha256']
        assert asset['masters'][0]['mime']=='image/png'
    hub=ASSETS['treehouse-four-main-quests-20261014']
    variant=next(v for v in hub['variants'] if v['role']=='image-960')
    assert variant['url'].lstrip('/') in Path('README.md').read_text()
    body=article_by_id('openclank-docs-treehouse')['body']
    assert all(next(v['url'] for v in ASSETS[key]['variants'] if v['role']=='image') in body for key in active)
    assert 'treehouse-player-20261007' not in body


def test_four_root_guidance_preserves_honest_updates_drafts_and_practical_scope():
    body = article_by_id("openclank-docs-treehouse")["body"]
    for name in ("Programming", "Games", "AI", "Open Clank"):
        assert f"**{name}**" in body
    assert "all four roots" in body
    assert "all-global reset includes the three" not in body
    assert "Historical completions keep the requirements" in body
    assert "Opening a Quest records a visit" in body
    assert "Previous answer" in body and "not a submission or practical success" in body
    assert "closing or reloading can lose it" in body
    assert "only confirmed practical success waits" in body
    assert "Required conceptual work has supplied inputs" in body
    assert "synthetic practice in an isolated source candidate" in body
    assert "earlier three-Main-Quest source candidate" not in body


def test_storage_activation_guidance_is_separate_from_retiring_edited_defaults():
    from pathlib import Path
    import subprocess
    import sys
    body = article_by_id("openclank-docs-treehouse")["body"]
    guide = Path("docs/upgrading.md").read_text()
    assert "Recheck shared quests" in body and "only reads availability" in body
    assert "schema-only activation" in body and "safe private learning stays available" in body
    assert "both pristine and edited recognized defaults" in body and "revokes their grants" in body
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
    for label in ("Explore Quests", "Open Quest", "Resume Quest", "Quest lessons", "Connected lessons",
                  "Skills", "Library", "Search learning", "All Main Quests", "Records",
                  "Request completion record", "Back", "Forward", "Reset this Quest"):
        assert f"**{label}**" in body
    for obsolete in ("Explore topics", "Explore subject", "Resume subject", "Reset this subject", "Discuss & board"):
        assert obsolete not in body
    assert "not a proficiency score" in body
    assert "not a new issuer for shared Main Quests" in body
    assert "Navigation labels may differ" in body
