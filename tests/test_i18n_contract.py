import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "static" / "i18n"
REGISTRY = json.loads((I18N / "registry.json").read_text(encoding="utf-8"))
LOCALES = tuple(REGISTRY["locales"])
PLACEHOLDERS = re.compile(r"\{(?:[A-Za-z_][A-Za-z0-9_]*|\d+)\}")
BIDI_CONTROLS = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def load(name):
    return json.loads((I18N / name).read_text(encoding="utf-8"))


def test_registry_has_exact_supported_locale_set_and_rtl_metadata():
    registry = load("registry.json")
    assert registry["default_locale"] == "en"
    # 68 canonical selections + the approved French-backed
    # “Canadian English” display alias (en-CA). Coverage is additive only.
    assert len(LOCALES) == 69
    assert {key for key, value in registry["locales"].items() if value["dir"] == "rtl"} == {"ar", "ur", "fa", "pa-Arab", "dv"}
    assert registry["aliases"]["zh-TW"] == "zh-Hant"
    assert registry["aliases"]["pa-PK"] == "pa-Arab"
    assert registry["aliases"]["br"] == "pt-BR"
    assert registry["aliases"]["ms-MY"] == "ms"
    assert registry["aliases"]["ms-BN"] == "ms"
    assert registry["aliases"]["fr-CA"] == "fr"
    assert registry["do_not_auto_map"] == []


def test_canadian_english_is_french_backed_display_alias_without_duplicate_catalog():
    registry = load("registry.json")
    alias = registry["locales"]["en-CA"]
    assert alias["name"] == "Canadian English"
    assert alias["catalog"] == "fr"
    assert alias["html_lang"] == "fr"
    assert alias.get("alias_of") == "fr"
    # Real French selection stays first-class with its own catalog file.
    assert registry["locales"]["fr"]["name"] == "Français"
    assert registry["locales"]["fr"]["catalog"] == "fr"
    assert (I18N / "fr.json").exists()
    assert not (I18N / "en-CA.json").exists()
    # Malay is reused, never duplicated.
    assert registry["locales"]["ms"]["name"] == "Bahasa Melayu"
    assert registry["locales"]["ms"]["catalog"] == "ms"
    assert (I18N / "ms.json").exists()
    assert not (I18N / "ms-MY.json").exists()


def test_freeze_manifests_inventory_every_advertised_locale_with_explicit_fallbacks():
    freeze = json.loads((I18N / "freeze" / "manifest.json").read_text(encoding="utf-8"))
    locales = json.loads((I18N / "freeze" / "locales.json").read_text(encoding="utf-8"))
    assert freeze["translations_authored"] is False
    assert freeze["advertised_locales"] == 69
    assert freeze["english_fallback_locales"] == 31
    assert freeze["display_alias_locales"] == 1
    assert freeze["malay"]["locale"] == "ms"
    assert freeze["canadian_english"]["catalog"] == "fr"
    by_id = {item["id"]: item for item in locales["locales"]}
    assert set(by_id) == set(LOCALES)
    fallbacks = [item for item in locales["locales"] if item["kind"] == "english-fallback"]
    assert len(fallbacks) == 31
    for item in fallbacks:
        assert item["catalog"] == "en"
        assert item["english_fallback"] is True
        assert item["complete_translation"] is False
    ms = by_id["ms"]
    assert ms["counts"]["keys"] == freeze["english_keys"]
    assert ms["counts"]["translated"] > 0
    assert ms["counts"]["english_fallback"] > 0
    batches = json.loads((I18N / "freeze" / "batches.json").read_text(encoding="utf-8"))
    assert batches["entry_count"] == freeze["english_keys"]


def test_every_catalog_has_parity_safe_placeholders_and_locked_brands():
    english = load("en.json")
    brands = load("brands.json")["brands"]
    for locale in LOCALES:
        descriptor = REGISTRY["locales"][locale]
        catalog_name = descriptor.get("catalog", locale)
        # Catalog expansion is deliberately staged: the registry exposes every
        # audited language while untranslated entries safely render English.
        if locale != "en" and catalog_name == "en":
            continue
        catalog = load(f"{catalog_name}.json")
        assert catalog.keys() == english.keys(), locale
        for key, source in english.items():
            target = catalog[key]
            assert isinstance(target, str) and target.strip(), (locale, key)
            assert sorted(PLACEHOLDERS.findall(target)) == sorted(PLACEHOLDERS.findall(source)), (locale, key)
            assert not BIDI_CONTROLS.search(target), (locale, key)
            assert not re.search(r"</?[a-z][^>]*>", target, re.I), (locale, key)
            for brand in brands:
                if brand in source:
                    assert brand in target, (locale, key, brand)


def test_all_served_html_surfaces_load_shared_runtime_and_settings_has_selector():
    pages = (
        "static/index.html", "static/login.html", "static/treehouse-architecture-map.html",
        "static/treehouse-course-overview.html", "static/treehouse-troubleshooting.html",
        "packages/Copal/ui/index.html",
    )
    for page in pages:
        assert '/static/js/i18n.js' in (ROOT / page).read_text(encoding="utf-8"), page
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")
    assert 'id="set-interface-language"' in index
    assert 'data-language-select' in index
    # The initial HTML keeps the shipped catalogs small; i18n.js hydrates every
    # canonical registry entry, including staged English-fallback locales.
    assert "Object.entries(registry.locales)" in (ROOT / "static/js/i18n.js").read_text(encoding="utf-8")


def test_runtime_preserves_user_content_and_requires_consent_before_browser_switch():
    runtime = (ROOT / "static/js/i18n.js").read_text(encoding="utf-8")
    assert ".msg .body" in runtime
    assert "[contenteditable=\"true\"]" in runtime
    assert "if (!saved) offerLocale(browserLocale())" in runtime
    assert "localStorage.setItem(STORAGE_KEY, next)" in runtime
    assert "document.documentElement.dir" in runtime


def test_service_worker_precaches_runtime_and_all_catalogs():
    worker = (ROOT / "static/sw.js").read_text(encoding="utf-8")
    assert "/static/js/i18n.js" in worker
    assert "/static/js/custom-context-menu.js" in worker
    for locale, descriptor in REGISTRY["locales"].items():
        catalog_name = descriptor.get("catalog", locale)
        if locale != "en" and catalog_name == "en":
            continue
        assert f"/static/i18n/{catalog_name}.json" in worker
