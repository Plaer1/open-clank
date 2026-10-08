"""Maintained official Open Clank documentation — source, manifest, provisioning.

This module owns the first-party official content that is exposed through
small top-level read-only reference records in every account's Copal. Canonical
bodies live in official_docs_content; owner records retain identity and revision metadata
without duplicating the handbook bytes. Content reflects the approved
host/product behavior: Wiki has a distinct page-authoring applet over the
shared Editor document core, image work uses Files/Imps, Theme lives in Settings, Graph hides
provisioned docs by identity. Repository docs under ``docs/`` remain
learning/reference material and are not relocated by this module.

The manifest is data, not a home-directory side effect. Stable article IDs,
hierarchy and known aliases are exposed here so provisioning can be
idempotent, Help can address the same content IDs as folder browsing, and the
Graph can recognize provisioned records by real identity metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from types import MappingProxyType
from typing import Any

# Stable content version. Bump when maintained bodies change so idempotent
# provisioning can replace prior official revisions in place.
# v2 — S30 reconciliation against the completed S22–S26 theme/effect UI:
# shipped effect names, typography/accessibility controls, and repaired
# clank://chat / clank://tasks app-link targets.
# v5 — expanded canonical handbook and verified demonstration media bindings.
# v6 — reconciled current Settings, LCARS and Hexes labels and workflows.
# v7 — publish the final verified Appearance artwork pool guidance and media.
# v8 — Help offers the complete shared handbook through Wiki and Copal.
# v12 — Beta 1 English handbook: 26 articles and reproducible standalone receipt.
# v13 — final Beta 1 body reflow, qualified media and current capture bindings.
# v14 — complete Copal/Wiki-only handbook; separate reader retired.
# v15 — built-in handbook guidance stays read-only without editable-copy promotion.
# v16 — manual Save/shared Undo, click-in syntax comments and current navigation.
# v17 — final Windows support prose and current manual-Save/navigation media.
# v18 — final verified read-only Help, Editor/Wiki and current media bindings.
# v19 — current hosted Files-only authority; older-store recovery preserved.
# v20 — Docker unsupported; retained legacy references carry no release gate.
# v21 — complete fresh source setup: offline assets and native workspace workers.
# v22 — packaged Mac menu-bar lifecycle and current offline release installation.
# v23 — Tutorial/Quest learning paths, current achievements and honest adapter limits.
OFFICIAL_DOCS_SEED_VERSION = 23

# Top-level folder that holds every provisioned official page. Derived identity
# (product/builtin markers) is the real recognition mechanism; this name is the
# presentation root that Graph binds when it holds only provisioned documents.
OFFICIAL_ROOT_FOLDER = "OpenClank"

# Identity markers written into every provisioned note. ``isOfficialDocument``
# and the Graph route recognize these — never a name, dot-prefix or read-only
# status alone.
PRODUCT_MARKER = "open-clank"

_ARTICLE: dict[str, dict[str, Any]] = {}


def _article(article_id: str, name: str, title: str, body: str, *, aliases: tuple[str, ...] = (), order: int = 0) -> None:
    media = article_media(ARTICLE_ASSETS.get(article_id, ()))
    if media:
        body = body.rstrip() + "\n\n## Demonstration media\n\n" + media + "\n"
    _ARTICLE[article_id] = {
        "id": article_id,
        "name": name,
        "title": title,
        "aliases": list(aliases),
        "order": order,
        "body": body,
    }


# ── Canonical content bundle ────────────────────────────────────────────────
# Keep provisioning and native-note encoding here; author bodies in one place.
from .official_docs_assets import ASSETS, article_media
from .official_docs_content import (
    ADDITIONAL_ARTICLES, ARTICLE_ASSETS, ARTICLE_CONTENT, ARTICLE_EXAMPLES,
    EXISTING_ARTICLES,
)

for _id, _name, _title, _aliases, _order in EXISTING_ARTICLES:
    _article(_id, _name, _title, ARTICLE_CONTENT[_id], aliases=_aliases, order=_order)
for _order, (_id, _title, _body) in enumerate(ADDITIONAL_ARTICLES, start=len(EXISTING_ARTICLES)):
    _article(_id, f"{OFFICIAL_ROOT_FOLDER}/{_title}", _title, _body, order=_order)


# ── Manifest access ──────────────────────────────────────────────────────────

def official_articles() -> list[dict[str, Any]]:
    """Every maintained article, in display order."""
    return sorted(_ARTICLE.values(), key=lambda item: (item["order"], item["name"]))


def official_home() -> dict[str, Any]:
    """The article Help opens first."""
    return _ARTICLE["openclank-docs-home"]


def article_by_id(article_id: str) -> dict[str, Any] | None:
    return _ARTICLE.get(str(article_id or ""))


def known_names() -> list[str]:
    """Canonical names plus historical aliases, for install-fixture checks."""
    names: list[str] = []
    for article in official_articles():
        names.append(article["name"])
        names.extend(article["aliases"])
    return names


# ── Native note encoding ─────────────────────────────────────────────────────
#
# Mirrors the record shape produced by routes.copal_routes._encode_note so a
# provisioned official page is a real native note: typed blocks, identity
# properties and a Markdown interchange source that records whether the
# maintained body is still unmodified. Provisioning never needs the route
# layer to build content.

_NOTE_SCHEMA_VERSION = 1

_NOTE_LINK = re.compile(r"(!?)\[\[([^\]\n]+)\]\]")
_NOTE_TAG = re.compile(r"(?<![\w/])#([\w][\w/-]*)", re.UNICODE)


def _stable_id(prefix: str, material: str) -> str:
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _block_from_line(line: str, namespace: str, index: int) -> dict[str, Any]:
    block: dict[str, Any]
    if not line:
        block = {"type": "blank", "text": ""}
    elif match := re.fullmatch(r"(#{1,6})\s+(.*)", line):
        block = {"type": "heading", "level": len(match.group(1)), "text": match.group(2)}
    elif match := re.fullmatch(r"(\s*)([-*+])\s+\[([ xX])\]\s*(.*)", line):
        block = {"type": "task", "indent": len(match.group(1)), "marker": match.group(2), "checked": match.group(3).lower() == "x", "text": match.group(4)}
    elif match := re.fullmatch(r"(\s*)[-*+]\s+(.*)", line):
        block = {"type": "bullet", "indent": len(match.group(1)), "text": match.group(2)}
    elif match := re.fullmatch(r"(\s*)(\d+)\.\s+(.*)", line):
        block = {"type": "ordered", "indent": len(match.group(1)), "number": int(match.group(2)), "text": match.group(3)}
    elif match := re.fullmatch(r"\s*>\s?(.*)", line):
        block = {"type": "quote", "text": match.group(1)}
    elif re.fullmatch(r"\s*```.*", line):
        block = {"type": "code-fence", "text": line.strip()[3:]}
    elif re.fullmatch(r"\s*(?:---+|___+|\*\*\*+)\s*", line):
        block = {"type": "divider", "text": ""}
    elif line.count("|") >= 2:
        block = {"type": "table-row", "text": line}
    else:
        block = {"type": "paragraph", "text": line}
    block["source"] = line
    block["id"] = _stable_id("blk", f"{namespace}\0block\0{index}\0{line}")
    return block


def _property_records(properties: dict[str, Any], namespace: str) -> list[dict[str, Any]]:
    records = []
    for key, value in properties.items():
        if isinstance(value, bool):
            kind = "checkbox"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            kind = "number"
        elif isinstance(value, (list, dict)):
            kind = "tags" if isinstance(value, list) else "object"
        elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            kind = "date"
        else:
            kind = "text"
        records.append({
            "id": _stable_id("prop", f"{namespace}\0property\0{key}"),
            "key": key,
            "type": kind,
            "value": value,
        })
    return records


def encode_official_note(body: str, properties: dict[str, Any]) -> str:
    """Encode one official article body as a native Copal note record.

    ``properties`` must already carry the identity markers (product, builtin,
    docId). The returned JSON is the document content stored by the bridge.
    """
    namespace = str(properties.get("docId") or properties.get("title") or "official")
    lines = str(body or "").split("\n")
    blocks = [_block_from_line(line, namespace, index) for index, line in enumerate(lines)]

    relations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for match in _NOTE_LINK.finditer(body or ""):
        raw = match.group(2).split("|", 1)[0].strip()
        target, _, fragment = raw.partition("#")
        target = target.strip()
        if not target:
            continue
        key = ("embed" if match.group(1) else "link", target, fragment.strip())
        if key in seen:
            continue
        seen.add(key)
        relations.append({
            "id": _stable_id("rel", f"{namespace}\0relation\0{key[0]}\0{key[1]}\0{key[2]}"),
            "kind": key[0],
            "sourceBlockId": None,
            "target": key[1],
            "targetDocumentId": None,
            "targetBlockId": None,
            "fragment": key[2] or None,
        })
    tags = list(dict.fromkeys(match.group(1) for match in _NOTE_TAG.finditer(body or "")))
    for tag in tags:
        relations.append({
            "id": _stable_id("rel", f"{namespace}\0tag\0{tag}"),
            "kind": "tag",
            "sourceBlockId": None,
            "target": tag,
            "targetDocumentId": None,
            "targetBlockId": None,
        })

    record = {
        "schemaVersion": _NOTE_SCHEMA_VERSION,
        "body": {"type": "doc", "blocks": blocks},
        "properties": _property_records(properties, namespace),
        "relations": relations,
        "tags": tags,
        "extensions": {
            "interchange": {
                "format": "markdown",
                "source": str(body or ""),
                "projectionHash": hashlib.sha256(str(body or "").encode("utf-8")).hexdigest(),
                "modified": False,
            },
            "seed": {"version": OFFICIAL_DOCS_SEED_VERSION, "name": properties.get("docId")},
        },
    }
    return json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def article_payload(article: dict[str, Any]) -> dict[str, Any]:
    """Name, encoded content and identity markers for one manifest article."""
    properties = {
        "product": PRODUCT_MARKER,
        "builtin": True,
        "docId": article["id"],
        "seedVersion": OFFICIAL_DOCS_SEED_VERSION,
        "title": article["title"],
    }
    return {
        "name": article["name"],
        "kind": "wiki",
        "corpus": "wiki",
        "read_only": True,
        "content": encode_official_note(article["body"], properties),
        "aliases": list(article["aliases"]),
        "body": article["body"],
        "title": article["title"],
        "docId": article["id"],
    }


@lru_cache(maxsize=4)
def _cached_official_payloads(version: int) -> tuple[dict[str, Any], ...]:
    """Encode the installed handbook once per process/content version."""
    del version  # The version is deliberately part of the cache key.
    return tuple(article_payload(article) for article in official_articles())


@lru_cache(maxsize=4)
def _cached_official_references(version: int) -> MappingProxyType:
    """Build immutable canonical references alongside the payload cache."""
    references: dict[str, MappingProxyType] = {}
    for payload in _cached_official_payloads(version):
        content = str(payload["content"])
        raw = content.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        doc_id = str(payload["docId"])
        references[doc_id] = MappingProxyType({
            "docId": doc_id,
            "content": content,
            "digest": f"sha256:{digest}",
            "head": f"sha256:{digest}:{len(raw)}",
            "size": len(raw),
            "version": version,
        })
    return MappingProxyType(references)


@lru_cache(maxsize=4)
def _cached_official_reference_manifest(version: int) -> MappingProxyType:
    """Return the cached, content-addressed revision of all references."""
    references = _cached_official_references(version)
    receipt = [
        (doc_id, reference["head"], reference["digest"], reference["size"])
        for doc_id, reference in sorted(references.items())
    ]
    encoded = json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return MappingProxyType({
        "version": version,
        "fingerprint": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        "count": len(receipt),
    })


def official_reference_manifest() -> dict[str, Any]:
    """A caller-safe receipt for the complete canonical reference set."""
    return dict(_cached_official_reference_manifest(OFFICIAL_DOCS_SEED_VERSION))


def official_payloads() -> list[dict[str, Any]]:
    """Every article ready for provisioning, in display order.

    Return shallow copies so a caller can prepare a hypothetical provision
    without mutating the process-wide canonical payload cache.
    """
    return [
        {**payload, "aliases": list(payload.get("aliases") or ())}
        for payload in _cached_official_payloads(OFFICIAL_DOCS_SEED_VERSION)
    ]


def official_reference(article_id: str) -> dict[str, Any] | None:
    """Resolve one installed handbook payload without creating an owner copy."""
    reference = _cached_official_references(OFFICIAL_DOCS_SEED_VERSION).get(str(article_id or ""))
    return dict(reference) if reference is not None else None


# ── Provisioning plan ────────────────────────────────────────────────────────

def _existing_properties(document: dict[str, Any]) -> dict[str, Any]:
    properties = document.get("properties")
    return dict(properties) if isinstance(properties, dict) else {}


def _existing_doc_id(document: dict[str, Any]) -> str:
    properties = _existing_properties(document)
    return str(properties.get("docId") or "")


def _is_official_record(document: dict[str, Any]) -> bool:
    """Identity-only recognition, matching Graph's ``isOfficialDocument``."""
    if document.get("builtin") is True:
        return True
    properties = _existing_properties(document)
    product = str(properties.get("product", document.get("product", ""))).strip().lower()
    return product == PRODUCT_MARKER or properties.get("builtin") is True


def _is_user_modified(document: dict[str, Any]) -> bool:
    """True when the maintained body is no longer the shipped one."""
    extensions = document.get("extensions")
    if not isinstance(extensions, dict):
        extensions = {}
    interchange = extensions.get("interchange")
    if isinstance(interchange, dict) and interchange.get("modified") is True:
        return True
    properties = _existing_properties(document)
    stored = properties.get("seedVersion")
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        if int(stored) > OFFICIAL_DOCS_SEED_VERSION:
            return True
    return False


def plan_input_from_content(
    document_id: str,
    name: str,
    content: str,
    *,
    read_only: bool = False,
    trashed: bool = False,
) -> dict[str, Any]:
    """Build one ``plan_official_provision`` input row from stored note bytes.

    Loose records keep identity inside the encoded note (product, builtin,
    docId), not on the document record. Decode that identity so the live
    provisioning path can use the same docId/alias matching as the plan helper.
    """
    properties: dict[str, Any] = {}
    extensions: dict[str, Any] = {}
    try:
        record = json.loads(str(content or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        record = None
    if isinstance(record, dict):
        raw_properties = record.get("properties")
        if isinstance(raw_properties, list):
            for prop in raw_properties:
                if isinstance(prop, dict) and isinstance(prop.get("key"), str):
                    properties[prop["key"]] = prop.get("value")
        elif isinstance(raw_properties, dict):
            properties = dict(raw_properties)
        raw_extensions = record.get("extensions")
        if isinstance(raw_extensions, dict):
            extensions = raw_extensions
    return {
        "id": document_id,
        "name": name,
        "trashed": trashed,
        "readOnly": read_only,
        "properties": properties,
        "extensions": extensions,
    }


def _is_provisioned_record(document: dict[str, Any]) -> bool:
    """Official identity on an editable personal copy is not install ownership."""
    return document.get("readOnly") is True and _is_official_record(document)


def plan_official_provision(
    existing: list[dict[str, Any]],
    payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Decide create/update/skip for every manifest article.

    Matching is by stable identity (the ``docId`` property) first. A same-named
    page that is not ours is personal data and is never overwritten or
    claimed. Known aliases of previously installed defaults are adopted and
    renamed to the canonical name in place.

    ``payloads`` defaults to the full maintained manifest; callers that
    provision a subset pass that subset so interrupted runs resume cleanly.

    Returns ``{"create": [...], "update": [...], "skip": [...], "conflicts": [...]}``
    where each entry carries the payload and, for updates, the matched
    document id.
    """
    if payloads is None:
        payloads = official_payloads()
    by_doc_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for document in existing or []:
        if not isinstance(document, dict) or document.get("trashed"):
            continue
        doc_id = _existing_doc_id(document)
        if doc_id and _is_provisioned_record(document):
            by_doc_id[doc_id] = document
        name = str(document.get("name") or "")
        if name:
            by_name[name] = document

    plan: dict[str, Any] = {"create": [], "update": [], "skip": [], "conflicts": []}
    for payload in payloads:
        doc_id = payload["docId"]
        name = payload["name"]
        matched = by_doc_id.get(doc_id)
        rename_from: str | None = None
        if matched is None:
            # A previously installed default sitting under a known historical
            # alias is adopted and renamed to the canonical name in place.
            for alias in payload.get("aliases") or ():
                alias_hit = by_name.get(str(alias))
                if alias_hit is not None and _is_provisioned_record(alias_hit) and not _is_user_modified(alias_hit):
                    matched = alias_hit
                    rename_from = str(alias_hit.get("name") or "")
                    break
        if matched is None:
            named = by_name.get(name)
            if named is not None and _is_provisioned_record(named) and not _existing_doc_id(named):
                # Previously installed default without a stable id: adopt it.
                matched = named
        if matched is not None and rename_from is None:
            # Identity matched but the record still sits under a known alias.
            current_name = str(matched.get("name") or "")
            aliases = {str(alias) for alias in payload.get("aliases") or ()}
            if current_name and current_name != name and current_name in aliases:
                rename_from = current_name
        if matched is None:
            collision = by_name.get(name)
            if collision is not None and not _is_provisioned_record(collision):
                plan["conflicts"].append({
                    "docId": doc_id,
                    "name": name,
                    "reason": "personal-page-occupies-name",
                    "existingId": collision.get("id"),
                })
                continue
            plan["create"].append(payload)
            continue
        if _is_user_modified(matched):
            plan["skip"].append({
                "docId": doc_id,
                "name": name,
                "existingId": matched.get("id"),
                "reason": "user-modified",
            })
            continue
        plan["update"].append({
            **payload,
            "existingId": matched.get("id"),
            "renameFrom": rename_from,
        })
    return plan


def plan_summary(plan: dict[str, Any]) -> dict[str, int]:
    return {
        "create": len(plan.get("create") or []),
        "update": len(plan.get("update") or []),
        "skip": len(plan.get("skip") or []),
        "conflicts": len(plan.get("conflicts") or []),
    }


def is_user_modified_content(content: str) -> bool:
    """True when stored note content is no longer an unmodified official revision.

    Used by the loose provisioning path so a personal edit of a provisioned
    page is never replaced by a newer maintained body.
    """
    try:
        record = json.loads(str(content or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        # Undecodable content is treated as user-owned; never clobber it.
        return True
    if not isinstance(record, dict):
        return True
    extensions = record.get("extensions")
    if not isinstance(extensions, dict):
        return False
    interchange = extensions.get("interchange")
    if isinstance(interchange, dict) and interchange.get("modified") is True:
        return True
    seed = extensions.get("seed")
    if isinstance(seed, dict):
        version = seed.get("version")
        if isinstance(version, (int, float)) and not isinstance(version, bool):
            if int(version) > OFFICIAL_DOCS_SEED_VERSION:
                return True
    return False
