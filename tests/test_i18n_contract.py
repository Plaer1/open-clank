import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "static" / "i18n"
LOCALES = (
    "en", "zh-Hans", "ja", "ko", "es", "hi", "ar", "ru", "pt", "id",
    "pa-Guru", "bn", "sw", "ur", "fa",
)
PLACEHOLDERS = re.compile(r"\{(?:[A-Za-z_][A-Za-z0-9_]*|\d+)\}")
BIDI_CONTROLS = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def load(name):
    return json.loads((I18N / name).read_text(encoding="utf-8"))


def test_registry_has_exact_supported_locale_set_and_rtl_metadata():
    registry = load("registry.json")
    assert registry["default_locale"] == "en"
    assert tuple(registry["locales"]) == LOCALES
    assert {key for key, value in registry["locales"].items() if value["dir"] == "rtl"} == {"ar", "ur", "fa"}
    assert {"zh-TW", "zh-HK", "zh-MO", "zh-Hant", "pa-PK", "pa-Arab"} <= set(registry["do_not_auto_map"])


def test_every_catalog_has_parity_safe_placeholders_and_locked_brands():
    english = load("en.json")
    brands = load("brands.json")["brands"]
    for locale in LOCALES:
        catalog = load(f"{locale}.json")
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
    for locale in LOCALES:
        assert f'value="{locale}"' in index


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
    for locale in LOCALES:
        assert f"/static/i18n/{locale}.json" in worker
