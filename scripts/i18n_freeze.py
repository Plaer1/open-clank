#!/usr/bin/env python3
"""S27 freeze: final string/context/locale manifests and Sol batches.

Mechanical inventory only. This tool never authors translations. It:

1. merges structured first-party copy (TreeHouse lessons/awards/docs) into
   the English catalog with stable semantic keys;
2. pads every other catalog with explicit English fallbacks for new keys
   (never a silent missing key, never a fake translation);
3. freezes locale manifests for every advertised selection, including the
   approved French-backed ``en-CA`` “Canadian English” alias and Malay reuse;
4. writes non-overlapping semantic batches (150–300 strings, smaller for
   complex prose) for the S28 Sol authoring lane.

Usage:
  python3 scripts/i18n_freeze.py            # apply + write freeze artifacts
  python3 scripts/i18n_freeze.py --check    # verify freeze matches catalogs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "static" / "i18n"
FREEZE = I18N / "freeze"
STRUCTURED_EXTRACT = ROOT / "scripts" / "i18n_structured_extract.py"

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*|\d+)\}")

# Product names that must survive translation intact (S27 glossary freeze).
PRODUCT_NAMES = (
    "Open Clank",
    "OpenClank",
    "Copal",
    "Clanker",
    "Menmery",
    "Lore",
    "TreeHouse",
    "Imps",
    "MiMo",
    "Field Guide",
    "Meatbag Tasks",
)

# Semantic batch targets from the original S27 slice.
BATCH_MIN = 150
BATCH_MAX = 300
COMPLEX_PROSE_MAX = 40  # long bodies / lesson prose


def read_json(path: Path, fallback: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def placeholders(value: str) -> list[str]:
    return sorted(PLACEHOLDER.findall(value))


def load_structured() -> list[dict[str, Any]]:
    result = subprocess.run(
        [sys.executable, str(STRUCTURED_EXTRACT)],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    payload = json.loads(result.stdout)
    return list(payload["records"])


def normalize_ws(value: str) -> str:
    return " ".join(value.split())


def merge_structured(en: dict[str, str]) -> dict[str, Any]:
    """Merge structured copy into English. Returns change report."""
    records = load_structured()
    by_source = {v: k for k, v in en.items()}
    by_norm = {normalize_ws(v): k for k, v in en.items()}
    added: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    for record in records:
        source = record["source"]
        if source in en and record["key"] in en and en[record["key"]] == source:
            reused.append({"key": record["key"], "via": "same-key"})
            continue
        if source in by_source:
            reused.append({"key": record["key"], "via": by_source[source]})
            continue
        if normalize_ws(source) in by_norm:
            reused.append({"key": record["key"], "via": by_norm[normalize_ws(source)]})
            continue
        key = record["key"]
        if key in en and en[key] != source:
            digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
            key = f"{key}.{digest}"
        en[key] = source
        added.append({**record, "key": key})
    return {"added": added, "reused": reused, "records": records}


def pad_catalogs(en: dict[str, str], locales: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Ensure every dedicated catalog has full key parity via English fallback."""
    stats: dict[str, dict[str, Any]] = {}
    for locale, meta in locales.items():
        catalog_name = meta.get("catalog", locale)
        if catalog_name == "en":
            stats[locale] = {"catalog": catalog_name, "padded": 0, "keys": len(en)}
            continue
        path = I18N / f"{catalog_name}.json"
        catalog = read_json(path, {})
        padded = 0
        for key, source in en.items():
            if not isinstance(catalog.get(key), str) or not catalog[key].strip():
                catalog[key] = source
                padded += 1
        # Drop extra keys that are not in English (honest parity).
        extras = [key for key in list(catalog) if key not in en]
        for key in extras:
            del catalog[key]
        ordered = {key: catalog[key] for key in en}
        write_json(path, ordered)
        stats[locale] = {
            "catalog": catalog_name,
            "padded": padded,
            "dropped_extra": len(extras),
            "keys": len(ordered),
        }
    return stats


def rebuild_ledger(en: dict[str, str], structured_added: list[dict[str, Any]]) -> dict[str, Any]:
    ledger = read_json(I18N / "ledger.json", {}) or {}
    entries_by_key = {entry["key"]: entry for entry in ledger.get("entries", [])}
    records = []
    for key, source in en.items():
        prior = entries_by_key.get(key)
        if prior:
            records.append(
                {
                    "key": key,
                    "source": source,
                    "kind": prior.get("kind", "compatibility"),
                    "status": prior.get("status", "compatibility"),
                    "locations": prior.get("locations", []),
                    **({"reason": prior["reason"]} if prior.get("reason") else {}),
                }
            )
            continue
        match = next((item for item in structured_added if item["key"] == key), None)
        if match:
            records.append(
                {
                    "key": key,
                    "source": source,
                    "kind": match.get("kind", "structured-prose"),
                    "status": "live",
                    "locations": list(match.get("locations", [])),
                    "reason": f"structured extraction: {match.get('module', '')} {match.get('owner', '')} {match.get('field', '')}".strip(),
                }
            )
        else:
            records.append(
                {
                    "key": key,
                    "source": source,
                    "kind": "compatibility",
                    "status": "compatibility",
                    "locations": [],
                    "reason": "retained catalog key absent from current first-party census",
                }
            )
    records.sort(key=lambda item: item["key"])
    live = sum(1 for item in records if item["status"] == "live")
    compatibility = sum(1 for item in records if item["status"] == "compatibility")
    ledger_out = {
        **ledger,
        "version": 2,
        "source_count": len(records),
        "current_source_count": live,
        "compatibility_count": compatibility,
        "source_hash": sha256_text(json.dumps(en, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        "entries": records,
    }
    return ledger_out


def update_registry(locales: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    registry = read_json(I18N / "registry.json", {})
    if not registry:
        raise SystemExit("registry.json missing")
    # Insert en-CA immediately after fr so the joke label sits beside real French.
    new_locales: dict[str, Any] = {}
    for locale_id, meta in locales.items():
        new_locales[locale_id] = meta
        if locale_id == "fr":
            new_locales["en-CA"] = {
                "name": "Canadian English",
                "dir": "ltr",
                "catalog": "fr",
                "html_lang": "fr",
                "alias_of": "fr",
                "display_only": True,
            }
    if "en-CA" not in new_locales:
        new_locales["en-CA"] = {
            "name": "Canadian English",
            "dir": "ltr",
            "catalog": "fr",
            "html_lang": "fr",
            "alias_of": "fr",
            "display_only": True,
        }
    merged_aliases = dict(registry.get("aliases") or {})
    merged_aliases.update(aliases)
    # Keep alias map free of a self-referential en-CA entry; en-CA is a
    # selectable display alias whose catalog is French.
    registry["locales"] = new_locales
    registry["aliases"] = dict(sorted(merged_aliases.items()))
    registry["default_locale"] = registry.get("default_locale", "en")
    registry["version"] = 2
    write_json(I18N / "registry.json", registry)
    return registry


def surface_for(locations: list[str], module: str, key: str) -> str:
    if module:
        return module.split(".", 1)[0]
    if not locations:
        return "compatibility"
    path = locations[0].split(":")[0].lower()
    if "treehouse" in path:
        return "treehouse"
    if "theme" in path:
        return "themes"
    if "editor" in path or "codemirror" in path:
        return "editor"
    if "imp" in path or "gallery" in path:
        return "imps"
    if "graph" in path or "galaxy" in path or "vault" in path:
        return "graph"
    if "file" in path:
        return "files"
    if "settings" in path or "admin" in path:
        return "settings"
    if "calendar" in path or "todo" in path or "task" in path or "goal" in path:
        return "tasks"
    if "email" in path or "mail" in path:
        return "email"
    if "memory" in path:
        return "memory"
    if "note" in path or "wiki" in path or "document" in path:
        return "docs"
    if "chat" in path or "message" in path:
        return "chat"
    if "login" in path or "auth" in path:
        return "auth"
    if "provider" in path or "model" in path:
        return "providers"
    if "cookbook" in path:
        return "cookbook"
    if path.endswith(".html"):
        return "html-shell"
    if path.endswith(".py") or path.startswith("routes") or path.startswith("src/") or path.startswith("services"):
        return "server"
    return "ui-misc"


def build_batches(en: dict[str, str], ledger: dict[str, Any], structured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    structured_by_key = {item["key"]: item for item in structured}
    entries = {item["key"]: item for item in ledger.get("entries", [])}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, source in en.items():
        entry = entries.get(key, {})
        record = structured_by_key.get(key, {})
        module = record.get("module") or ""
        owner = record.get("owner") or ""
        locations = entry.get("locations") or record.get("locations") or []
        if module.startswith("treehouse.lesson"):
            group = f"treehouse.lesson.{owner.split('.')[0] or 'general'}"
        elif module.startswith("treehouse"):
            group = module
        elif module.startswith("award"):
            group = "award"
        elif module.startswith("docs"):
            group = f"docs.{owner}"
        else:
            group = surface_for(locations, module, key)
        item = {
            "key": key,
            "source": source,
            "placeholders": placeholders(source),
            "kind": entry.get("kind") or record.get("kind") or "unknown",
            "status": entry.get("status") or "live",
            "locations": locations,
            "context": record.get("note") or entry.get("reason") or "",
            "complexity": "prose" if len(source) > 120 else "label",
        }
        groups[group].append(item)

    batches: list[dict[str, Any]] = []
    for group in sorted(groups):
        items = groups[group]
        # Complex prose groups (docs bodies, lesson bodies) get small batches.
        prose_share = sum(1 for item in items if item["complexity"] == "prose") / max(len(items), 1)
        max_entries = COMPLEX_PROSE_MAX if prose_share > 0.35 else BATCH_MAX
        min_entries = 1 if prose_share > 0.35 else BATCH_MIN
        current: list[dict[str, Any]] = []
        chars = 0

        def flush() -> None:
            nonlocal current, chars
            if not current:
                return
            batch_id = f"batch-{len(batches) + 1:03d}"
            batches.append(
                {
                    "id": batch_id,
                    "group": group,
                    "surface": group.split(".", 1)[0],
                    "entry_count": len(current),
                    "character_count": chars,
                    "complexity": "prose" if prose_share > 0.35 else "mixed",
                    "instruction": (
                        "Complex prose batch: keep meaning, tone and product voice; "
                        "prefer natural software language over literal wording."
                        if prose_share > 0.35
                        else "UI label/message batch: concise, consistent terminology; "
                        "preserve placeholders and locked brand tokens."
                    ),
                    "entries": current,
                }
            )
            current = []
            chars = 0

        for item in items:
            size = len(item["key"]) + len(item["source"]) + 32
            if current and (len(current) >= max_entries or chars + size > 18000):
                # Keep batches non-overlapping; flush before adding.
                if len(current) < min_entries and len(items) - items.index(item) >= (min_entries - len(current)):
                    pass
                flush()
            current.append(item)
            chars += size
        flush()
    return batches


def coverage(en: dict[str, str], catalog: dict[str, str]) -> dict[str, int]:
    translated = 0
    english_fallback = 0
    for key, source in en.items():
        target = catalog.get(key, "")
        if isinstance(target, str) and target.strip() and target != source:
            translated += 1
        else:
            english_fallback += 1
    return {
        "keys": len(en),
        "translated": translated,
        "english_fallback": english_fallback,
    }


def write_freeze(registry: dict[str, Any], en: dict[str, str], ledger: dict[str, Any], batches: list[dict[str, Any]]) -> dict[str, Any]:
    if FREEZE.exists():
        for path in sorted(FREEZE.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    FREEZE.mkdir(parents=True, exist_ok=True)

    locales_meta = registry["locales"]
    locale_manifests = []
    for locale_id, meta in locales_meta.items():
        catalog_name = meta.get("catalog", locale_id)
        if catalog_name == "en":
            catalog = en
            kind = "english-source" if locale_id == "en" else "english-fallback"
        else:
            catalog = read_json(I18N / f"{catalog_name}.json", {}) or {}
            if locale_id == catalog_name:
                kind = "dedicated"
            elif meta.get("alias_of"):
                kind = "display-alias"
            else:
                kind = "shared-catalog"
        cov = coverage(en, catalog)
        missing = [key for key in en if key not in catalog]
        extra = [key for key in catalog if key not in en]
        manifest = {
            "id": locale_id,
            "name": meta.get("name", locale_id),
            "dir": meta.get("dir", "ltr"),
            "catalog": catalog_name,
            "html_lang": meta.get("html_lang") or (catalog_name if catalog_name != "en" else "en"),
            "kind": kind,
            "alias_of": meta.get("alias_of"),
            "advertised": True,
            "english_fallback": kind in {"english-fallback"} or (kind == "display-alias" and catalog_name == "en"),
            "counts": cov,
            "missing_keys": len(missing),
            "extra_keys": len(extra),
            "complete_translation": cov["english_fallback"] == 0,
        }
        locale_manifests.append(manifest)
        write_json(FREEZE / "locales" / f"{locale_id}.json", manifest)

    dedicated = [item for item in locale_manifests if item["kind"] == "dedicated"]
    fallbacks = [item for item in locale_manifests if item["kind"] == "english-fallback"]
    aliases = [item for item in locale_manifests if item["kind"] == "display-alias"]

    glossary = {
        "schema": 1,
        "product_names": list(PRODUCT_NAMES),
        "brands": read_json(I18N / "brands.json", {}).get("brands", []),
        "stable_tokens": read_json(I18N / "brands.json", {}).get("stable_tokens", []),
        "rules": [
            "Never translate product names (Open Clank, Copal, Clanker, Imps, Menmery, Lore, TreeHouse, MiMo, Field Guide).",
            "Keep locked brand and protocol tokens exactly as in the English source.",
            "Prefer real software terminology people meet in localized interfaces over textbook literalism.",
            "One voice: concise UI labels; complete sentences for errors and help.",
            "User documents, chat bodies, editor source and code snippets are never rewritten by UI translation.",
        ],
    }

    rules = {
        "schema": 1,
        "interpolation": {
            "placeholder_syntax": "{name} or {0}",
            "regex": PLACEHOLDER.pattern,
            "must_match_english": True,
            "reorder_allowed": True,
            "do_not_translate_placeholder_names": True,
        },
        "plurals": {
            "runtime": "Intl.PluralRules(locale).select(count)",
            "api": "window.openClankI18n.plural(count, forms)",
            "forms": ["zero", "one", "two", "few", "many", "other"],
            "required": ["other"],
            "note": "S28 supplies forms per locale; English may use one/other only.",
        },
        "formatting": {
            "forbidden": ["HTML tags in strings", "Unicode bidi controls in strings"],
            "escaping": "Runtime assigns text nodes/attributes; catalogs hold plain text.",
            "whitespace": "Leading/trailing whitespace of source is preserved around translations.",
        },
        "integrity": {
            "key_parity": True,
            "no_silent_fallback_as_complete": True,
            "english_fallback_is_explicit": True,
            "brands_locked": True,
        },
    }

    string_manifest = {
        "schema": 1,
        "source_hash": ledger["source_hash"],
        "source_count": ledger["source_count"],
        "current_source_count": ledger["current_source_count"],
        "compatibility_count": ledger["compatibility_count"],
        "keys": [
            {
                "key": key,
                "placeholders": placeholders(source),
                "length": len(source),
                "complexity": "prose" if len(source) > 120 else "label",
            }
            for key, source in en.items()
        ],
    }

    context_manifest = {
        "schema": 1,
        "surfaces": {
            "editor_wiki": "Editor, Wiki pages, rich comments, source view",
            "files_imps": "Files tree, managed Imps image projects",
            "graph": "Graph modes, filters, camera",
            "treehouse": "Classes, lessons, Field Guide, awards",
            "themes": "Theme settings and effect names",
            "tasks": "Tasks, goals, timeline, calendar",
            "errors": "HTTP/error/status messages",
            "menus": "Context menus, rail, window chrome",
            "settings": "Settings panels and language selector",
            "upstream_ui": "Adopted upstream Odysseus/MiMo UI already in census",
        },
        "exclude": [
            "user documents",
            "chat transcripts",
            "editor source text",
            "code snippets",
            "generated/vendor bundles",
        ],
        "entries": [
            {
                "key": item["key"],
                "locations": item["locations"],
                "kind": item["kind"],
                "context": item.get("reason") or item.get("context") or "",
            }
            for item in ledger["entries"]
        ],
    }

    write_json(FREEZE / "glossary.json", glossary)
    write_json(FREEZE / "rules.json", rules)
    write_json(FREEZE / "strings.json", string_manifest)
    write_json(FREEZE / "context.json", context_manifest)
    write_json(
        FREEZE / "locales.json",
        {
            "schema": 1,
            "advertised_count": len(locale_manifests),
            "dedicated_catalog_locales": len(dedicated) + 1,  # + en
            "english_fallback_locales": len(fallbacks),
            "display_alias_locales": len(aliases),
            "catalog_files": sorted({item["catalog"] for item in locale_manifests}),
            "locales": locale_manifests,
        },
    )
    for batch in batches:
        write_json(FREEZE / "batches" / f"{batch['id']}.json", batch)

    write_json(
        FREEZE / "batches.json",
        {
            "schema": 1,
            "policy": "non-overlapping catalog writes; one batch owns each key exactly once",
            "batch_count": len(batches),
            "entry_count": sum(batch["entry_count"] for batch in batches),
            "min_entries": BATCH_MIN,
            "max_entries": BATCH_MAX,
            "complex_prose_max": COMPLEX_PROSE_MAX,
            "batches": [
                {
                    "id": batch["id"],
                    "group": batch["group"],
                    "surface": batch["surface"],
                    "entry_count": batch["entry_count"],
                    "character_count": batch["character_count"],
                    "complexity": batch["complexity"],
                }
                for batch in batches
            ],
        },
    )

    manifest = {
        "schema": 1,
        "slice": "S27",
        "operation": "final-string-inventory-and-locale-infrastructure",
        "translations_authored": False,
        "translation_lane": "S28-gpt-5.6-sol",
        "source_hash": ledger["source_hash"],
        "english_keys": len(en),
        "ledger_source_count": ledger["source_count"],
        "ledger_current_source_count": ledger["current_source_count"],
        "ledger_compatibility_count": ledger["compatibility_count"],
        "advertised_locales": len(locale_manifests),
        "dedicated_catalogs_including_english": 37,
        "english_fallback_locales": len(fallbacks),
        "display_alias_locales": len(aliases),
        "malay": {
            "locale": "ms",
            "name": "Bahasa Melayu",
            "catalog_file": "ms.json",
            "reuse": "existing dedicated catalog; no duplicate",
            "aliases": ["ms-MY", "ms-BN"],
        },
        "canadian_english": {
            "id": "en-CA",
            "display_name": "Canadian English",
            "catalog": "fr",
            "html_lang": "fr",
            "note": "Humorous display alias over French data; no separate prose/dialect catalog. Real fr/Français remains selectable with its own keys.",
        },
        "batches": len(batches),
        "files": {
            "registry": "static/i18n/registry.json",
            "runtime": "static/js/i18n.js",
            "extractor": "scripts/i18n-catalog.mjs",
            "structured_extract": "scripts/i18n_structured_extract.py",
            "freeze": "static/i18n/freeze",
        },
    }
    write_json(FREEZE / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify freeze matches catalogs without writing")
    args = parser.parse_args()

    en = read_json(I18N / "en.json", {})
    if not en:
        raise SystemExit("en.json missing")
    registry = read_json(I18N / "registry.json", {})
    locales = dict(registry.get("locales") or {})

    merge = merge_structured(dict(en))
    en = {key: en[key] for key in en}
    for item in merge["added"]:
        en[item["key"]] = item["source"]
    # Keep English catalog in a stable order: existing keys first, then new.
    # (JSON object order is preserved by our writer.)

    if args.check:
        freeze_manifest = read_json(FREEZE / "manifest.json", {})
        ok = freeze_manifest.get("source_hash") and freeze_manifest.get("english_keys") == len(en)
        print(json.dumps({"check": "ok" if ok else "drift", "english_keys": len(en), "freeze_keys": freeze_manifest.get("english_keys")}, indent=2))
        return 0 if ok else 1

    pad_stats = pad_catalogs(en, locales)
    ledger = rebuild_ledger(en, merge["added"])
    write_json(I18N / "en.json", en)
    write_json(I18N / "ledger.json", ledger)

    aliases = {
        "ms-MY": "ms",
        "ms-BN": "ms",
        "fr-CA": "fr",
    }
    registry = update_registry(locales, aliases)
    batches = build_batches(en, ledger, merge["records"])
    manifest = write_freeze(registry, en, ledger, batches)

    report = {
        "structured_added": len(merge["added"]),
        "structured_reused": len(merge["reused"]),
        "english_keys": len(en),
        "source_hash": ledger["source_hash"],
        "advertised_locales": manifest["advertised_locales"],
        "english_fallback_locales": manifest["english_fallback_locales"],
        "display_alias_locales": manifest["display_alias_locales"],
        "batches": manifest["batches"],
        "pad": pad_stats,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
