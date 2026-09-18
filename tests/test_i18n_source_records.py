"""Current source census and translation recovery invariants."""

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "static" / "i18n"


def load(name):
    return json.loads((I18N / name).read_text(encoding="utf-8"))


def test_census_retains_compatibility_keys_and_excludes_generated_editor_chunks():
    ledger = load("ledger.json")
    assert ledger["version"] == 2
    assert ledger["source_count"] == len(ledger["entries"])
    assert ledger["compatibility_count"] > 0
    assert any(entry["status"] == "compatibility" for entry in ledger["entries"])
    locations = [location for entry in ledger["entries"] for location in entry["locations"]]
    assert not any("codemirror-" in location or "/chunk" in location or "/vendor" in location for location in locations)
    assert all(entry["locations"] for entry in ledger["entries"] if entry["status"] == "live")


def test_live_census_excludes_css_selectors_protocols_and_templates():
    ledger = load("ledger.json")
    css_property = re.compile(
        r"^(?:--|align|animation|appearance|backdrop|background|border|bottom|box|color|column|content|cursor|display|fill|filter|flex|float|font|gap|grid|height|inset|justify|left|letter|line|margin|max|min|object|opacity|order|outline|overflow|padding|perspective|pointer|position|right|table|text|top|transform|transition|user|vertical|visibility|white|width|word|z-index|zoom)(?:-[a-z]+)*$",
        re.I,
    )
    selector = re.compile(
        r"^(?:[.#][\w-]+|\[[^\]]+\]|(?:select|div|span|button|input|textarea|form|option|table|tr|td|th|h[1-6])(?:[.#:[\s]|$))",
    )
    protocol = re.compile(
        r"^(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD|SELECT|INSERT|UPDATE|DROP)\b|"
        r"^(?:python3|python|node|bash|sh)\s+-c\b|^\[\s*[\"']-[a-z]",
    )
    code_id = re.compile(r"^(?:[a-z][\w-]*:){1,2}[\w.-]+$")
    bad = []
    for entry in ledger["entries"]:
        if entry["status"] != "live":
            continue
        source = entry["source"]
        declarations = [part.strip() for part in source.split(";") if part.strip()]
        css = any(
            (match := re.match(r"^([\w-]+)\s*:\s*(.+)$", part))
            and css_property.fullmatch(match.group(1))
            and (
                len(declarations) > 1
                or re.search(r"(?:[-\d.]|var\(|#|\{|\b(?:auto|none|inherit|initial|transparent|flex|block|inline|absolute|relative|fixed|hidden|pointer)\b)", match.group(2), re.I)
            )
            for part in declarations
        )
        if css or selector.search(source) or protocol.search(source) or code_id.fullmatch(source) or re.fullmatch(r"\{\{[\s\S]*\}\}", source):
            bad.append((entry["key"], source))
    assert not bad, bad[:10]


def test_active_catalogs_cover_the_current_english_source_set():
    english = load("en.json")
    registry = load("registry.json")
    active = [
        (locale, metadata.get("catalog", locale))
        for locale, metadata in registry["locales"].items()
        if locale != "en" and metadata.get("catalog", locale) != "en"
    ]
    assert len(active) >= 30
    for locale, catalog_name in active:
        assert set(load(f"{catalog_name}.json")) == set(english), locale


def test_donor_recovery_records_schema_mapping_digests_and_review_boundaries():
    provenance = load("provenance.json")
    assert provenance["schema"] == 1
    assert provenance["donor"]["revision"].startswith("08eeb26")
    assert provenance["donor"]["license"] == "AGPL-3.0"
    assert provenance["donor"]["files"]
    assert all(row["committed_sha256"] or row["working_sha256"] for row in provenance["donor"]["files"])
    assert provenance["mapping"] == {"zh-CN": "zh-Hans", "zh-TW": "zh-Hant"}
    assert provenance["counts"]["baseline_changed_sources"] == 20
    assert provenance["counts"]["conflicts"] > 0
    census = provenance["current_source_census"]
    assert census["baseline_keys"] == 7208
    assert census["added_count"] == len(census["added_keys"])
    assert census["removed_count"] == len(census["removed_keys"])


def test_registry_preserves_regional_and_script_distinctions():
    registry = load("registry.json")
    assert registry["aliases"]["zh-TW"] == "zh-Hant"
    assert registry["aliases"]["pa"] == "pa-Arab"
    assert registry["locales"]["es"]["catalog"] == "es"
    assert registry["locales"]["es-419"]["catalog"] == "es-419"
    assert registry["locales"]["pt"]["catalog"] == "pt"
    assert registry["locales"]["pt-BR"]["catalog"] == "pt-BR"
    assert registry["locales"]["pa-Guru"]["dir"] == "ltr"
    assert registry["locales"]["pa-Arab"]["dir"] == "rtl"


def test_runtime_batches_mutations_and_skips_user_content_subtrees():
    runtime = (ROOT / "static/js/i18n.js").read_text(encoding="utf-8")
    assert "function queueTranslation(root)" in runtime
    assert "pendingTranslationRoots" in runtime
    assert "root !== document.documentElement && shouldSkip(root)" in runtime
