"""Versioned Markdown memory export and strict reader (pure, standard library only).

The document is human-readable Markdown with one machine-checked manifest.
Each item body is length-delimited (byte count plus SHA-256 in the manifest), so
texts that contain code fences or look like the format's own markers round-trip
exactly.  ``parse_markdown`` accepts a document only if it equals the canonical
rendering of its own manifest and bodies; nothing partial is ever returned.

Never written: approval, trust, activation or confidence state, internal record
ids, status, workspace or owner fields.  Items that look like secrets are
omitted with a reason, never altered.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

FORMAT = "openclank.memory-markdown"
VERSION = 1
MAX_FILE_BYTES = 10 * 1024 * 1024  # mirrors MEMORY_IMPORT_MAX_BYTES
MAX_ITEMS = 100  # the import review admits at most 100 suggestions per file (import_batch._normalize_suggestions)
MAX_ITEM_BYTES = 15000  # the import pipeline truncates documents at 15000 characters

OMISSION_REASONS = frozenset({
    "secret_detected", "item_too_large", "invalid_record",
    "export_item_bound", "export_size_bound", "source_read_bound",
})
ERROR_CODES = frozenset({
    "missing_manifest", "duplicate_manifest", "bad_manifest", "unsupported_version",
    "item_hash_mismatch", "duplicate_item_id", "file_too_large", "item_too_large",
    "too_many_items", "malformed_body",
})

_TOKEN = "<!-- openclank-memory-manifest"
_ID_RE = re.compile(r"\Aitem-[0-9a-f]{16}(?:-[0-9]{1,4})?\Z")
_KIND_RE = re.compile(r"\A[a-z0-9_]{1,40}\Z")
_LABEL_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,64}\Z")
_TIME_RE = re.compile(r"\A[0-9T:+.Z-]{10,40}\Z")
_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_ITEM_KEYS = {"id", "kind", "tags", "bytes", "sha256", "source"}
_SOURCE_KEYS = {"uri", "content_sha256", "message_ids", "revision", "state"}
_TOP_KEYS = {"format", "version", "origin", "generated_at", "items", "omissions"}


class FormatError(ValueError):
    """Refusal with a closed error code."""

    def __init__(self, code: str) -> None:
        assert code in ERROR_CODES
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    text: str
    tags: tuple = ()
    source_uri: Optional[str] = None
    source_content_sha256: Optional[str] = None
    source_message_ids: tuple = ()
    source_revision: Optional[int] = None
    provenance: str = "unresolved"  # "resolved" only when the record carried a source


@dataclass(frozen=True)
class ParsedBundle:
    format: str
    version: int
    origin: str
    generated_at: str
    items: tuple
    omissions: tuple  # ({"reason", "count"[, "at_least"]}, ...)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _default_is_secret(text: str) -> bool:
    from src.shell_policy import redact_text
    return redact_text(text) != text


def _str_list(value: Any, limit: int, width: int) -> tuple:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(v for v in value if isinstance(v, str) and 0 < len(v) <= width)[:limit]


def _record_item(record: Mapping[str, Any]) -> Optional[Item]:
    """Project a record onto exported fields only; None when it cannot be exported."""
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    meta = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    kind = str(record.get("kind") or record.get("category") or "unknown").lower()
    uri = record.get("source_uri") or meta.get("source_uri")
    uri = uri if isinstance(uri, str) and 0 < len(uri) <= 2048 else None
    digest = record.get("source_content_sha256") or meta.get("source_content_sha256")
    digest = digest if isinstance(digest, str) and _HASH_RE.match(digest) else None
    # "memory:<id>" is the record's own id, never provenance.
    ids = tuple(i for i in _str_list(record.get("source_message_ids"), 20, 200) if not i.startswith("memory:"))
    revision = record.get("source_revision")
    revision = revision if type(revision) is int and revision >= 0 else None
    resolved = bool(uri or ids or revision is not None)
    return Item("", kind if _KIND_RE.match(kind) else "unknown", text, _str_list(record.get("tags"), 20, 64),
                uri, digest, ids, revision, "resolved" if resolved else "unresolved")


def normalize_items(records: Iterable[Mapping[str, Any]], *, is_secret: Optional[Callable[[str], bool]] = None,
                    max_items: int = MAX_ITEMS) -> tuple:
    """Return (items, omissions) after the secret, size and count bounds; ids are content-derived."""
    check = is_secret or _default_is_secret
    items, counts, seen = [], {}, {}
    for record in records:
        item = _record_item(record) if isinstance(record, Mapping) else None
        reason = None
        if item is None:
            reason = "invalid_record"
        else:
            try:
                size = len(item.text.encode("utf-8"))
            except UnicodeEncodeError:
                size, reason = 0, "invalid_record"
            if reason is None and size > MAX_ITEM_BYTES:
                reason = "item_too_large"
            elif reason is None and check(item.text + "\n" + (item.source_uri or "")):
                reason = "secret_detected"
        if reason is None and len(items) >= max_items:
            reason = "export_item_bound"
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
            continue
        base = "item-" + _sha(item.kind.encode() + b"\0" + item.text.encode("utf-8"))[:16]
        n = seen.get(base, 0)
        seen[base] = n + 1
        items.append(replace(item, id=base if n == 0 else f"{base}-{n}"))
    return tuple(items), counts


def _fence(body: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


def _dumps(manifest: Mapping[str, Any]) -> str:
    # "<" and ">" are escaped so the manifest can never contain the closing "-->".
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")


def _prefix(manifest: Mapping[str, Any]) -> bytes:
    lines = [f"{_TOKEN} {FORMAT}/{VERSION}", _dumps(manifest), "-->", "", "# Memory export", "",
             f"Format {FORMAT} version {VERSION}. Origin: {manifest['origin']}. Generated: {manifest['generated_at']}.",
             f"Items: {len(manifest['items'])}. This file is not the whole memory."]
    for o in manifest["omissions"]:
        lines.append(f"- Not included: {o['count']}{'+' if o.get('at_least') else ''} ({o['reason']})")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _pre(item_id: str, kind: str, n: int) -> bytes:
    return f"### {kind} `{item_id}`\n<!-- openclank:item {item_id} {n} -->\n".encode("utf-8")


def _head(item_id: str, kind: str, n: int, fence: str) -> bytes:
    return _pre(item_id, kind, n) + f"{fence}text\n".encode("utf-8")


def _tail(fence: str) -> bytes:
    return f"\n{fence}\n<!-- /openclank:item -->\n\n".encode("utf-8")


def _manifest_entry(item: Item, body: bytes) -> dict:
    return {"id": item.id, "kind": item.kind, "tags": list(item.tags), "bytes": len(body), "sha256": _sha(body),
            "source": {"uri": item.source_uri, "content_sha256": item.source_content_sha256,
                       "message_ids": list(item.source_message_ids), "revision": item.source_revision,
                       "state": item.provenance}}


def _render(items: Sequence[Item], omissions: Sequence[Mapping[str, Any]], owner_label: str, generated_at: str) -> str:
    bodies = [i.text.encode("utf-8") for i in items]
    manifest = {"format": FORMAT, "version": VERSION, "origin": owner_label, "generated_at": generated_at,
                "items": [_manifest_entry(i, b) for i, b in zip(items, bodies)], "omissions": [dict(o) for o in omissions]}
    out = [_prefix(manifest)]
    for item, body in zip(items, bodies):
        fence = _fence(item.text)
        out += [_head(item.id, item.kind, len(body), fence), body, _tail(fence)]
    return b"".join(out).decode("utf-8")


def export_markdown(records: Iterable[Mapping[str, Any]], *, owner_label: str, generated_at: str,
                    omissions: Sequence[Mapping[str, Any]] = (), is_secret: Optional[Callable[[str], bool]] = None,
                    max_items: int = MAX_ITEMS, max_bytes: int = MAX_FILE_BYTES) -> str:
    """Render records as one document; deterministic for the same input.

    ``omissions`` are caller-known exclusions (``{"reason", "count"[, "at_least"]}``); bound and secret
    omissions found here are merged in.  The result never exceeds ``max_bytes``."""
    if not _LABEL_RE.match(owner_label or "") or not _TIME_RE.match(generated_at or ""):
        raise ValueError("owner_label or generated_at is not a safe token")
    items, counts = normalize_items(records, is_secret=is_secret, max_items=max_items)
    extra = {}
    for o in omissions:
        if o.get("reason") not in OMISSION_REASONS or type(o.get("count")) is not int or o["count"] < 0:
            raise ValueError("unknown omission")
        extra[o["reason"]] = dict(o)

    def build(kept):
        merged = dict(extra)
        for reason, n in counts.items():
            merged[reason] = {"reason": reason, "count": n + merged.get(reason, {}).get("count", 0)}
        dropped = len(items) - len(kept)
        if dropped:
            merged["export_size_bound"] = {"reason": "export_size_bound", "count": dropped}
        return _render(kept, [merged[k] for k in sorted(merged)], owner_label, generated_at)

    kept = list(items)
    document = build(kept)
    while kept and len(document.encode("utf-8")) > max_bytes:
        del kept[len(kept) - max(1, len(kept) // 10):]
        document = build(kept)
    return document


def export_markdown_files(records: Iterable[Mapping[str, Any]], *, owner_label: str, generated_at: str,
                          omissions: Sequence[Mapping[str, Any]] = (), is_secret: Optional[Callable[[str], bool]] = None,
                          max_items: int = 5000) -> tuple:
    """One self-contained, re-importable document per memory: (files, omissions) with files = [(safe_name, document)].

    Same normalisation, redaction and item ids as export_markdown. The file name is an ASCII slug of the first line
    plus the content-derived id, so it is stable, unique and never carries path characters."""
    if not _LABEL_RE.match(owner_label or "") or not _TIME_RE.match(generated_at or ""):
        raise ValueError("owner_label or generated_at is not a safe token")
    items, counts = normalize_items(records, is_secret=is_secret, max_items=max_items)
    files = []
    for item in items:
        first = item.text.strip().splitlines()[0] if item.text.strip() else ""
        slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-")[:40].strip("-")
        files.append(((slug + "-" if slug else "") + item.id + ".md", _render([item], [], owner_label, generated_at)))
    merged = {o["reason"]: dict(o) for o in omissions if o.get("reason") in OMISSION_REASONS and type(o.get("count")) is int}
    for reason, n in counts.items():
        merged[reason] = {"reason": reason, "count": n + merged.get(reason, {}).get("count", 0)}
    return files, [merged[k] for k in sorted(merged)]


def _check_manifest(manifest: Any) -> None:
    ok = (isinstance(manifest, dict) and set(manifest) == _TOP_KEYS and manifest["format"] == FORMAT
          and isinstance(manifest["origin"], str) and _LABEL_RE.match(manifest["origin"])
          and isinstance(manifest["generated_at"], str) and _TIME_RE.match(manifest["generated_at"])
          and isinstance(manifest["items"], list) and isinstance(manifest["omissions"], list))
    if not ok:
        raise FormatError("bad_manifest")
    if len(manifest["items"]) > MAX_ITEMS:
        raise FormatError("too_many_items")
    seen = set()
    for e in manifest["items"]:
        src = e.get("source") if isinstance(e, dict) else None
        if (not isinstance(e, dict) or set(e) != _ITEM_KEYS or not isinstance(src, dict) or set(src) != _SOURCE_KEYS
                or not isinstance(e["id"], str) or not _ID_RE.match(e["id"])
                or not isinstance(e["kind"], str) or not _KIND_RE.match(e["kind"])
                or type(e["bytes"]) is not int or e["bytes"] < 1
                or not isinstance(e["sha256"], str) or not _HASH_RE.match(e["sha256"])
                or not isinstance(e["tags"], list) or not all(isinstance(t, str) for t in e["tags"])
                or not (src["uri"] is None or isinstance(src["uri"], str))
                or not (src["content_sha256"] is None or (isinstance(src["content_sha256"], str) and _HASH_RE.match(src["content_sha256"])))
                or not isinstance(src["message_ids"], list) or not all(isinstance(m, str) for m in src["message_ids"])
                or not (src["revision"] is None or type(src["revision"]) is int)
                or src["state"] not in ("resolved", "unresolved")):
            raise FormatError("bad_manifest")
        if e["bytes"] > MAX_ITEM_BYTES:
            raise FormatError("item_too_large")
        if e["id"] in seen:
            raise FormatError("duplicate_item_id")
        seen.add(e["id"])
    for o in manifest["omissions"]:
        if (not isinstance(o, dict) or not {"reason", "count"} <= set(o) <= {"reason", "count", "at_least"}
                or o["reason"] not in OMISSION_REASONS or type(o["count"]) is not int or o["count"] < 0
                or ("at_least" in o and o["at_least"] is not True)):
            raise FormatError("bad_manifest")


def parse_markdown(source: "str | bytes", *, max_bytes: int = MAX_FILE_BYTES) -> ParsedBundle:
    """Strictly parse one document; raises FormatError (closed code) and returns nothing partial."""
    try:
        data = source if isinstance(source, bytes) else source.encode("utf-8", "strict")
        if isinstance(source, bytes):
            data.decode("utf-8", "strict")
    except UnicodeError:
        raise FormatError("malformed_body") from None
    if len(data) > max_bytes:
        raise FormatError("file_too_large")
    if not data.startswith(_TOKEN.encode() + b" "):
        raise FormatError("missing_manifest")
    first_end = data.index(b"\n") if b"\n" in data else len(data)
    line = data[:first_end].decode("utf-8", "replace")
    if line != f"{_TOKEN} {FORMAT}/{VERSION}":
        raise FormatError("unsupported_version" if re.fullmatch(re.escape(_TOKEN) + " [A-Za-z0-9._-]+/[0-9]+", line) else "bad_manifest")
    end = data.find(b"\n-->\n", first_end)
    if end < 0:
        raise FormatError("bad_manifest")

    def unique(pairs):
        value = {}
        for k, v in pairs:
            if k in value:
                raise ValueError("duplicate key")
            value[k] = v
        return value
    try:
        manifest = json.loads(data[first_end + 1:end].decode("utf-8"), object_pairs_hook=unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except ValueError:
        raise FormatError("bad_manifest") from None
    if isinstance(manifest, dict) and manifest.get("version") != VERSION:
        raise FormatError("unsupported_version")
    _check_manifest(manifest)
    expected = _prefix(manifest)
    if not data.startswith(expected):
        raise FormatError("duplicate_manifest" if _TOKEN.encode() in data[len(_TOKEN):end] else "bad_manifest")
    pos, items = len(expected), []
    for e in manifest["items"]:
        n = e["bytes"]
        pre = _pre(e["id"], e["kind"], n)
        ticks = re.match(rb"`+", data[pos + len(pre):pos + len(pre) + n + 100])
        fence = ticks.group().decode() if ticks and data.startswith(pre, pos) else None
        head = _head(e["id"], e["kind"], n, fence or "")
        if fence is None or not data.startswith(head, pos):
            raise FormatError("malformed_body")
        start = pos + len(head)
        body = data[start:start + n]
        if len(body) != n or _sha(body) != e["sha256"]:
            raise FormatError("item_hash_mismatch" if len(body) == n else "malformed_body")
        try:
            text = body.decode("utf-8", "strict")
        except UnicodeError:
            raise FormatError("malformed_body") from None
        if _fence(text) != fence or not data.startswith(_tail(fence), start + n):
            raise FormatError("malformed_body")
        pos = start + n + len(_tail(fence))
        src = e["source"]
        items.append(Item(e["id"], e["kind"], text, tuple(e["tags"]), src["uri"], src["content_sha256"],
                          tuple(src["message_ids"]), src["revision"], src["state"]))
    if pos != len(data):
        raise FormatError("duplicate_manifest" if _TOKEN.encode() in data[pos:] else "malformed_body")
    return ParsedBundle(FORMAT, VERSION, manifest["origin"], manifest["generated_at"], tuple(items),
                        tuple(dict(o) for o in manifest["omissions"]))
