#!/usr/bin/env python3
"""Extract first-party structured UI copy from Python content modules.

S27 mechanical inventory. TreeHouse lessons/awards and official handbook
articles are data, not free-form runtime strings, so the line-oriented
extractor in ``i18n-catalog.mjs`` cannot see implicit concatenation or
positional ``_lesson`` / ``_article`` arguments. This helper is the
extraction marker contract for those modules:

* ``_lesson(key, title, result, completion, destination, explanation, why, body, practice=…)``
* class/section dicts with ``title`` / ``summary`` / ``description``
* ``_article(article_id, name, title, body)``
* achievement ``_n`` / ``_s`` / ``_u`` (id, key, title, kind, families, summary)

Stable keys are semantic (``treehouse.*`` / ``docs.*`` / ``award.*``) so S28
batches stay addressable when English wording is polished.

Usage: python3 scripts/i18n_structured_extract.py
Prints one JSON object on stdout. Never writes catalogs.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Dict fields that hold UI copy. Keys, destinations, completion enums and
# achievement ids are NOT copy.
TRANSLATABLE_FIELDS = frozenset(
    {
        "title",
        "summary",
        "description",
        "explanation",
        "whyThisHelps",
        "body",
        "result",
        "seed",
        "cleanup",
        "expectedEvidence",
        "audience",
        "name",
        "label",
        "text",
        "hint",
        "toast",
    }
)

# Positional UI-copy slots for _lesson(...)
LESSON_COPY_SLOTS = {
    1: "title",
    2: "result",
    5: "explanation",
    6: "whyThisHelps",
    7: "body",
}

# Positional UI-copy slots for _article(...)
ARTICLE_COPY_SLOTS = {
    2: "title",
    3: "body",
}
ARTICLE_NAME_SLOT = 1

# Positional UI-copy slots for achievement helpers _n/_s/_u
ACHIEVEMENT_COPY_SLOTS = {
    2: "title",
    5: "summary",
}


def _const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _walk_dicts(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            yield node


def _dict_pairs(node: ast.Dict) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for key_node, value_node in zip(node.keys, node.values):
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            out[key_node.value] = value_node
    return out


def _stable(module: str, owner: str, field: str, source: str) -> str:
    """Deterministic catalog key for structured content."""
    owner_slug = owner.replace(" ", "-").replace("_", "-").lower()
    owner_slug = "".join(ch if ch.isalnum() or ch in "-." else "-" for ch in owner_slug)
    owner_slug = "-".join(part for part in owner_slug.split("-") if part)
    field_slug = field.replace("_", "-")
    return f"{module}.{owner_slug}.{field_slug}"


def _add(
    records: list[dict[str, Any]],
    seen: set[str],
    module: str,
    owner: str,
    field: str,
    source: str,
    location: str,
    note: str,
) -> None:
    text = source.replace("\r\n", "\n").strip()
    if not text or len(text) < 2:
        return
    key = _stable(module, owner, field, text)
    if key in seen:
        # Same owner+field can repeat only if source matches; otherwise suffix.
        existing = next(r for r in records if r["key"] == key)
        if existing["source"] == text:
            if location not in existing["locations"]:
                existing["locations"].append(location)
            return
        digest = format(abs(hash(text)) % (16**8), "08x")
        key = f"{key}.{digest}"
    if key in seen:
        return
    seen.add(key)
    records.append(
        {
            "key": key,
            "source": text,
            "field": field,
            "owner": owner,
            "module": module,
            "kind": "structured-prose" if len(text) > 120 else "structured-label",
            "locations": [location],
            "note": note,
        }
    )


def extract_field_guide(path: Path) -> list[dict[str, Any]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(ROOT).as_posix()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "_lesson":
                continue
            lesson_key = _const(node.args[0]) if node.args else None
            if not lesson_key:
                continue
            loc = f"{rel}:{node.lineno}"
            for index, field in LESSON_COPY_SLOTS.items():
                if index >= len(node.args):
                    continue
                value = _const(node.args[index])
                if value:
                    _add(records, seen, "treehouse.lesson", lesson_key, field, value, loc, field)
            for kw in node.keywords:
                if kw.arg == "practice" and isinstance(kw.value, ast.Dict):
                    pairs = _dict_pairs(kw.value)
                    for field, value_node in pairs.items():
                        value = _const(value_node)
                        if value and field in TRANSLATABLE_FIELDS:
                            _add(
                                records,
                                seen,
                                "treehouse.lesson",
                                f"{lesson_key}.practice",
                                field,
                                value,
                                f"{rel}:{value_node.lineno}",
                                "practice",
                            )
                elif kw.arg == "practice" and kw.value is not None and not isinstance(kw.value, ast.Constant):
                    # practice=None is not copy
                    pass

    for node in _walk_dicts(tree):
        pairs = _dict_pairs(node)
        if "lessons" not in pairs or "key" not in pairs:
            continue
        owner = _const(pairs["key"]) or "class"
        loc = f"{rel}:{node.lineno}"
        for field in ("title", "summary", "description"):
            if field in pairs:
                value = _const(pairs[field])
                if value:
                    _add(records, seen, "treehouse.class", owner, field, value, loc, field)

    # Section specs (title/description even without lessons)
    for node in _walk_dicts(tree):
        pairs = _dict_pairs(node)
        if "classKeys" in pairs and "title" in pairs:
            owner = _const(pairs.get("id")) or _const(pairs.get("key")) or "section"
            loc = f"{rel}:{node.lineno}"
            for field in ("title", "description"):
                if field in pairs:
                    value = _const(pairs[field])
                    if value:
                        _add(records, seen, "treehouse.section", owner, field, value, loc, field)

    # Manifest-level audience / titles
    for node in _walk_dicts(tree):
        pairs = _dict_pairs(node)
        if "templateKey" in pairs and "title" in pairs:
            loc = f"{rel}:{node.lineno}"
            for field in ("title", "audience"):
                if field in pairs:
                    value = _const(pairs[field])
                    if value:
                        _add(records, seen, "treehouse.manifest", "field-guide", field, value, loc, field)

    return records


def extract_official_docs(path: Path) -> list[dict[str, Any]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(ROOT).as_posix()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "_article":
            continue
        article_id = _const(node.args[0]) if node.args else "article"
        loc = f"{rel}:{node.lineno}"
        name_value = _const(node.args[ARTICLE_NAME_SLOT]) if len(node.args) > ARTICLE_NAME_SLOT else None
        if name_value and "{OFFICIAL_ROOT_FOLDER}" not in name_value:
            _add(records, seen, "docs", article_id, "name", name_value, loc, "article name")
        for index, field in ARTICLE_COPY_SLOTS.items():
            if index >= len(node.args):
                continue
            value = _const(node.args[index])
            if value:
                _add(records, seen, "docs", article_id, field, value, loc, field)
    return records


def extract_achievements(path: Path) -> list[dict[str, Any]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(ROOT).as_posix()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name not in {"_n", "_s", "_u"}:
            continue
        # _n(num, key, title, kind, families, summary)
        award_key = _const(node.args[1]) if len(node.args) > 1 else None
        if not award_key:
            continue
        loc = f"{rel}:{node.lineno}"
        for index, field in ACHIEVEMENT_COPY_SLOTS.items():
            if index >= len(node.args):
                continue
            value = _const(node.args[index])
            if value:
                _add(records, seen, "award", award_key, field, value, loc, field)
    return records


def main() -> int:
    records: list[dict[str, Any]] = []
    records.extend(extract_field_guide(ROOT / "src" / "openclank" / "treehouse_field_guide.py"))
    records.extend(extract_official_docs(ROOT / "src" / "openclank" / "official_docs.py"))
    records.extend(extract_achievements(ROOT / "src" / "openclank" / "treehouse_achievements.py"))
    records.sort(key=lambda item: item["key"])
    payload = {
        "schema": 1,
        "count": len(records),
        "records": records,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
