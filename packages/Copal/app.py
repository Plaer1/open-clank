#!/usr/bin/env python3
"""
Copal — tiny local server (zero dependencies, stdlib only).

It does exactly two jobs:
  1. Serves the built static site in ./out   (the galaxy/timeline/calendar UI)
  2. Reads & writes your data in ./move-data.json via a tiny JSON API:
        GET  /api/data   -> the current move-data.json
        POST /api/data   -> overwrite move-data.json with the body

Run:
    python3 app.py
Then open http://localhost:8765

Your edits in the app are saved to move-data.json on disk, so they survive
restarts and stay readable for you or an AI calendar manager. You can also
edit move-data.json by hand while the server runs — the app reloads it on
next page load.
"""

from __future__ import annotations

import json
import os
import re
import threading
import zipfile
from datetime import datetime, timezone
from html import escape
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
SITE_DIR = ROOT / "out"                       # built static site (next build -> out/)
DATA_FILE = ROOT / "move-data.json"           # your data — the source of truth
SEED_FILE = SITE_DIR / "data" / "move-data.json"  # fallback seed (bundled by the build)
VAULT_DIR = Path(os.environ.get("COPAL_VAULT_DIR", ROOT / "sample-vault")).resolve()
UI_STATE_FILE = VAULT_DIR / ".copal" / "ui-state.json"
PORT = int(os.environ.get("PORT", "8765"))

_write_lock = threading.Lock()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


def read_data() -> bytes:
    """Current JSON bytes: the data file, else the seed, else '{}'."""
    if DATA_FILE.exists():
        return DATA_FILE.read_bytes()
    if SEED_FILE.exists():
        return SEED_FILE.read_bytes()
    return b"{}"


def ensure_data_file() -> None:
    """Seed move-data.json on first run so there's a real file to edit."""
    if not DATA_FILE.exists():
        DATA_FILE.write_bytes(read_data())


NOTE_SUFFIXES = {".md", ".markdown", ".base", ".canvas", ".dclg"}
TEXT_EXPORT_SUFFIXES = {".md", ".markdown", ".base", ".canvas", ".dclg"}
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
WIKILINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
TAG_RE = re.compile(r"(^|[\s(])#([A-Za-z0-9_/-]+)")
TASK_RE = re.compile(r"^(\s*)[-*]\s+\[([ xX/-])\]\s+(.*)$")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
DONE_DATE_RE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
DUE_DATE_RE = re.compile(r"(?:📅|due::|\[due::)\s*(\d{4}-\d{2}-\d{2})", re.I)
SCHEDULED_DATE_RE = re.compile(r"(?:⏳|scheduled::|\[scheduled::)\s*(\d{4}-\d{2}-\d{2})", re.I)
RECURRENCE_RE = re.compile(r"(?:🔁|repeat::|\[repeat::)\s*([^\]\n]+)", re.I)


def _json(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _error(message: str, code: int = 400) -> tuple[int, bytes]:
    return code, _json({"error": message})


def _vault_path(rel_path: str) -> Path | None:
    clean = unquote(rel_path).replace("\\", "/").lstrip("/")
    candidate = (VAULT_DIR / clean).resolve()
    try:
        candidate.relative_to(VAULT_DIR)
    except ValueError:
        return None
    return candidate


def _is_exportable_vault_file(fp: Path) -> bool:
    try:
        rel = fp.relative_to(VAULT_DIR)
    except ValueError:
        return False
    parts = rel.parts
    if any(part.startswith(".") for part in parts):
        return False
    if any(part in {"node_modules", "__pycache__", "target"} for part in parts):
        return False
    return fp.is_file() and fp.suffix.lower() in NOTE_SUFFIXES


def _backup_path(fp: Path) -> Path:
    rel = fp.relative_to(VAULT_DIR)
    return VAULT_DIR / ".copal" / "backups" / _now_stamp() / rel


def backup_file(fp: Path) -> Path | None:
    if not fp.exists() or not fp.is_file():
        return None
    dest = _backup_path(fp)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(fp.read_bytes())
    return dest


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, object]:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected json object")
    return data


def list_notes() -> list[dict[str, object]]:
    if not VAULT_DIR.exists():
        return []
    out: list[dict[str, object]] = []
    for fp in sorted(VAULT_DIR.rglob("*")):
        if not _is_exportable_vault_file(fp):
            continue
        rel = fp.relative_to(VAULT_DIR).as_posix()
        stat = fp.stat()
        out.append(
            {
                "path": rel,
                "name": fp.name,
                "suffix": fp.suffix.lower(),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    return out


def read_note_entry(entry: dict[str, object]) -> dict[str, object] | None:
    fp = _vault_path(str(entry["path"]))
    if fp is None or not fp.is_file():
        return None
    return {
        **entry,
        "content": fp.read_text("utf-8", errors="replace"),
    }


def note_title(path: str, content: str) -> str:
    title = re.search(r"(?m)^title:\s*(.+?)\s*$", content)
    if title:
        return title.group(1).strip().strip('"\'')
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", content)
    if heading:
        return heading.group(1).strip()
    return Path(path).stem


def task_status(symbol: str) -> str:
    if symbol in {"x", "X"}:
        return "done"
    if symbol == "/":
        return "in-progress"
    if symbol == "-":
        return "cancelled"
    return "pending"


def strip_task_markers(text: str) -> str:
    out = re.sub(r"(✅|📅|⏳)\s*\d{4}-\d{2}-\d{2}", "", text)
    out = re.sub(r"(🔁)\s*[^\]\n]+", "", out)
    out = re.sub(r"\[(due|scheduled|priority|repeat)::\s*[^\]]+\]", "", out, flags=re.I)
    out = re.sub(r"[🔺⏫🔼🔽⏬]", "", out)
    return re.sub(r"\s+", " ", out).strip()


def task_priority(text: str) -> str:
    if re.search(r"🔺|⏫|\[priority::\s*high\]", text, flags=re.I):
        return "high"
    if re.search(r"🔽|⏬|\[priority::\s*low\]", text, flags=re.I):
        return "low"
    return "medium"


def first_group(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def build_vault_index() -> dict[str, object]:
    entries = list_notes()
    notes: list[dict[str, object]] = []
    by_title: dict[str, str] = {}
    for entry in entries:
        note = read_note_entry(entry)
        if not note:
            continue
        content = str(note["content"])
        title = note_title(str(note["path"]), content)
        note["title"] = title
        note["wikilinks"] = sorted({m.group(1).strip() for m in WIKILINK_RE.finditer(content)})
        note["tags"] = sorted({m.group(2) for m in TAG_RE.finditer(content)})
        notes.append(note)
        by_title[title.lower()] = str(note["path"])
        by_title[Path(str(note["path"])).stem.lower()] = str(note["path"])

    tasks: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    nodes: dict[str, dict[str, object]] = {}
    backlinks: dict[str, list[dict[str, object]]] = {}
    for note in notes:
        path = str(note["path"])
        title = str(note["title"])
        content = str(note["content"])
        nodes[path] = {"id": path, "label": title, "path": path, "type": "note"}
        link_lines: dict[str, int] = {}
        for idx, line in enumerate(content.splitlines(), start=1):
            for match in WIKILINK_RE.finditer(line):
                link_lines.setdefault(match.group(1).strip(), idx)
        for link in note["wikilinks"]:  # type: ignore[index]
            target = by_title.get(str(link).lower())
            target_id = target or f"missing:{str(link).lower()}"
            nodes[target_id] = nodes.get(
                target_id,
                {"id": target_id, "label": str(link), "path": target, "type": "note" if target else "missing"},
            )
            edge = {"id": f"{path}->{target_id}:wikilink", "from": path, "to": target_id, "type": "wikilink"}
            edges.append(edge)
            if target:
                backlinks.setdefault(target, []).append({"sourcePath": path, "sourceTitle": title, "type": "wikilink", "line": link_lines.get(str(link), 1)})
        for tag in note["tags"]:  # type: ignore[index]
            tag_id = f"tag:{tag}"
            nodes[tag_id] = {"id": tag_id, "label": f"#{tag}", "type": "tag"}
            edges.append({"id": f"{path}->{tag_id}:tag", "from": path, "to": tag_id, "type": "tag"})
        for idx, line in enumerate(content.splitlines(), start=1):
            match = TASK_RE.match(line)
            if not match:
                continue
            text = match.group(3).strip()
            dates = DATE_RE.findall(text)
            tasks.append(
                {
                    "id": f"note:{path}:{idx}",
                    "sourcePath": path,
                    "noteTitle": title,
                    "line": idx,
                    "status": task_status(match.group(2)),
                    "title": strip_task_markers(text),
                    "text": text,
                    "dates": dates,
                    "doneDate": first_group(DONE_DATE_RE, text),
                    "dueDate": first_group(DUE_DATE_RE, text),
                    "scheduledDate": first_group(SCHEDULED_DATE_RE, text),
                    "recurrence": first_group(RECURRENCE_RE, text),
                    "priority": task_priority(text),
                    "tags": sorted({m.group(2) for m in TAG_RE.finditer(text)}),
                }
            )

    return {
        "vaultPath": str(VAULT_DIR),
        "notes": [{k: v for k, v in note.items() if k != "content"} for note in notes],
        "tasks": tasks,
        "graph": {"nodes": list(nodes.values()), "edges": edges},
        "backlinks": backlinks,
    }


def export_vault_for_ai() -> dict[str, object]:
    notes = []
    for entry in list_notes():
        fp = _vault_path(str(entry["path"]))
        if fp is None or not fp.is_file():
            continue
        notes.append(
            {
                **entry,
                "content": fp.read_text("utf-8", errors="replace"),
            }
        )
    return {
        "schema": "copal.ai-export.v0",
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "vaultPath": str(VAULT_DIR),
        "notes": notes,
    }


def export_okf() -> dict[str, object]:
    index = build_vault_index()
    return {
        "schema": "copal.okf-inspired.v0",
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "catalog": {
            "name": VAULT_DIR.name,
            "path": str(VAULT_DIR),
        },
        "resources": index["notes"],
        "relationships": index["graph"]["edges"],  # type: ignore[index]
        "tasks": index["tasks"],
    }


def export_doclang_draft() -> bytes:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<doclang>",
        "  <head>",
        "    <label>Copal Vault Draft Export</label>",
        f"    <description>Draft DocLang-style export from {escape(str(VAULT_DIR))}</description>",
        "  </head>",
    ]
    for entry in list_notes():
        note = read_note_entry(entry)
        if not note:
            continue
        path = str(note["path"])
        content = str(note["content"])
        title = note_title(path, content)
        cdata = content.replace("]]>", "]]]]><![CDATA[>")
        parts.extend(
            [
                "  <section>",
                f"    <label>{escape(title)}</label>",
                f"    <custom><path>{escape(path)}</path><suffix>{escape(str(note['suffix']))}</suffix></custom>",
                f"    <content><![CDATA[{cdata}]]></content>",
                "  </section>",
            ]
        )
    parts.append("</doclang>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def export_markdown_bundle() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in list_notes():
            if str(entry["suffix"]) not in TEXT_EXPORT_SUFFIXES:
                continue
            fp = _vault_path(str(entry["path"]))
            if fp is None or not fp.is_file():
                continue
            zf.write(fp, str(entry["path"]))
    return buf.getvalue()


def read_ui_state() -> dict[str, object]:
    if not UI_STATE_FILE.exists():
        return {"pinnedInfantecimemes": []}
    try:
        data = json.loads(UI_STATE_FILE.read_text("utf-8"))
        return data if isinstance(data, dict) else {"pinnedInfantecimemes": []}
    except Exception:
        return {"pinnedInfantecimemes": []}


def write_ui_state(data: dict[str, object]) -> None:
    UI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    current = read_ui_state()
    current.update(data)
    tmp = UI_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(UI_STATE_FILE)


class Handler(BaseHTTPRequestHandler):
    server_version = "MoveTimeline/1.0"

    # ---- helpers --------------------------------------------------------
    def _quiet(self, *args):  # silence default request logging
        pass

    log_message = _quiet

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _safe_path(self, url_path: str) -> Path | None:
        """Resolve a URL path under SITE_DIR, blocking traversal escapes."""
        clean = url_path.split("?", 1)[0].split("#", 1)[0]
        if clean == "/" or clean == "":
            clean = "/index.html"
        candidate = (SITE_DIR / clean.lstrip("/")).resolve()
        try:
            candidate.relative_to(SITE_DIR.resolve())
        except ValueError:
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate if candidate.is_file() else None

    # ---- routes ---------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/data":
            return self._send(200, read_data(), "application/json; charset=utf-8")
        if path == "/api/vault":
            return self._send(
                200,
                _json({"path": str(VAULT_DIR), "exists": VAULT_DIR.exists()}),
                "application/json; charset=utf-8",
            )
        if path == "/api/ui-state":
            return self._send(200, _json(read_ui_state()), "application/json; charset=utf-8")
        if path == "/api/notes":
            return self._send(
                200,
                _json({"vaultPath": str(VAULT_DIR), "notes": list_notes()}),
                "application/json; charset=utf-8",
            )
        if path == "/api/index":
            return self._send(200, _json(build_vault_index()), "application/json; charset=utf-8")
        if path == "/api/search":
            q = parse_qs(parsed.query).get("q", [""])[0].strip().lower()
            results = []
            if q:
                for entry in list_notes():
                    note = read_note_entry(entry)
                    if not note:
                        continue
                    hay = f"{note['path']}\n{note['content']}".lower()
                    if q in hay:
                        content = str(note["content"])
                        at = content.lower().find(q)
                        start = max(0, at - 120)
                        end = min(len(content), at + len(q) + 180)
                        results.append({**entry, "excerpt": content[start:end]})
            return self._send(
                200,
                _json({"query": q, "results": results}),
                "application/json; charset=utf-8",
            )
        if path == "/api/backlinks":
            note_path = parse_qs(parsed.query).get("path", [""])[0]
            fp = _vault_path(note_path)
            if fp is None:
                return self._send(400, _json({"error": "invalid path"}), "application/json; charset=utf-8")
            rel = fp.relative_to(VAULT_DIR).as_posix()
            index = build_vault_index()
            return self._send(
                200,
                _json({"path": rel, "backlinks": index["backlinks"].get(rel, [])}),  # type: ignore[index]
                "application/json; charset=utf-8",
            )
        if path == "/api/graph":
            return self._send(
                200,
                _json(build_vault_index()["graph"]),
                "application/json; charset=utf-8",
            )
        if path == "/api/tasks":
            return self._send(
                200,
                _json({"tasks": build_vault_index()["tasks"]}),
                "application/json; charset=utf-8",
            )
        if path == "/api/export/ai":
            return self._send(
                200,
                _json(export_vault_for_ai()),
                "application/json; charset=utf-8",
            )
        if path == "/api/export/okf":
            return self._send(200, _json(export_okf()), "application/json; charset=utf-8")
        if path == "/api/export/doclang":
            return self._send(200, export_doclang_draft(), "application/xml; charset=utf-8")
        if path == "/api/export/markdown-bundle":
            body = export_markdown_bundle()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="copal-vault-markdown-bundle.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if path == "/api/note":
            note_path = parse_qs(parsed.query).get("path", [""])[0]
            fp = _vault_path(note_path)
            if fp is None or not fp.is_file() or fp.suffix.lower() not in NOTE_SUFFIXES:
                return self._send(404, b'{"error":"note not found"}', "application/json")
            rel = fp.relative_to(VAULT_DIR).as_posix()
            body = fp.read_text("utf-8", errors="replace")
            return self._send(
                200,
                _json(
                    {
                        "path": rel,
                        "name": fp.name,
                        "suffix": fp.suffix.lower(),
                        "content": body,
                        "mtime": fp.stat().st_mtime,
                    }
                ),
                "application/json; charset=utf-8",
            )
        if path == "/api/vault-asset":
            asset_path = parse_qs(parsed.query).get("path", [""])[0]
            fp = _vault_path(asset_path)
            if fp is None or not fp.is_file() or fp.suffix.lower() not in ASSET_SUFFIXES:
                return self._send(404, b'{"error":"asset not found"}', "application/json")
            return self._send(200, fp.read_bytes(), MIME.get(fp.suffix.lower(), "application/octet-stream"))
        self._serve()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/note":
            try:
                data = read_json_body(self)
                rel_path = str(data.get("path") or "")
                content = str(data.get("content") if data.get("content") is not None else "")
                fp = _vault_path(rel_path)
                if fp is None or fp.suffix.lower() not in NOTE_SUFFIXES:
                    code, body = _error("invalid note path")
                    return self._send(code, body, "application/json; charset=utf-8")
                backup = backup_file(fp)
                fp.parent.mkdir(parents=True, exist_ok=True)
                tmp = fp.with_suffix(fp.suffix + ".tmp")
                tmp.write_text(content, "utf-8")
                tmp.replace(fp)
                return self._send(
                    200,
                    _json({"ok": True, "path": fp.relative_to(VAULT_DIR).as_posix(), "backup": str(backup.relative_to(VAULT_DIR)) if backup else None}),
                    "application/json; charset=utf-8",
                )
            except Exception as exc:
                code, body = _error(str(exc))
                return self._send(code, body, "application/json; charset=utf-8")
        if path == "/api/note/rename":
            try:
                data = read_json_body(self)
                src = _vault_path(str(data.get("path") or ""))
                dst = _vault_path(str(data.get("newPath") or ""))
                if src is None or dst is None or src.suffix.lower() not in NOTE_SUFFIXES or dst.suffix.lower() not in NOTE_SUFFIXES:
                    code, body = _error("invalid note path")
                    return self._send(code, body, "application/json; charset=utf-8")
                if not src.is_file():
                    code, body = _error("source note not found", 404)
                    return self._send(code, body, "application/json; charset=utf-8")
                if dst.exists():
                    code, body = _error("destination exists", 409)
                    return self._send(code, body, "application/json; charset=utf-8")
                backup = backup_file(src)
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                return self._send(
                    200,
                    _json({"ok": True, "path": dst.relative_to(VAULT_DIR).as_posix(), "backup": str(backup.relative_to(VAULT_DIR)) if backup else None}),
                    "application/json; charset=utf-8",
                )
            except Exception as exc:
                code, body = _error(str(exc))
                return self._send(code, body, "application/json; charset=utf-8")
        if path == "/api/note/delete":
            try:
                data = read_json_body(self)
                fp = _vault_path(str(data.get("path") or ""))
                if fp is None or fp.suffix.lower() not in NOTE_SUFFIXES:
                    code, body = _error("invalid note path")
                    return self._send(code, body, "application/json; charset=utf-8")
                if not fp.is_file():
                    code, body = _error("note not found", 404)
                    return self._send(code, body, "application/json; charset=utf-8")
                backup = backup_file(fp)
                fp.unlink()
                return self._send(
                    200,
                    _json({"ok": True, "backup": str(backup.relative_to(VAULT_DIR)) if backup else None}),
                    "application/json; charset=utf-8",
                )
            except Exception as exc:
                code, body = _error(str(exc))
                return self._send(code, body, "application/json; charset=utf-8")
        if path == "/api/mkdir":
            try:
                data = read_json_body(self)
                fp = _vault_path(str(data.get("path") or ""))
                if fp is None:
                    code, body = _error("invalid directory path")
                    return self._send(code, body, "application/json; charset=utf-8")
                fp.mkdir(parents=True, exist_ok=True)
                return self._send(200, _json({"ok": True, "path": fp.relative_to(VAULT_DIR).as_posix()}), "application/json; charset=utf-8")
            except Exception as exc:
                code, body = _error(str(exc))
                return self._send(code, body, "application/json; charset=utf-8")
        if path == "/api/ui-state":
            try:
                data = read_json_body(self)
                write_ui_state(data)
                return self._send(200, _json({"ok": True, "state": read_ui_state()}), "application/json; charset=utf-8")
            except Exception as exc:
                code, body = _error(str(exc))
                return self._send(code, body, "application/json; charset=utf-8")
        if path != "/api/data":
            return self._send(404, b"not found")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            json.loads(raw.decode("utf-8"))  # validate JSON before writing
        except Exception:
            return self._send(400, b'{"error":"invalid json"}', "application/json")
        with _write_lock:
            tmp = DATA_FILE.with_suffix(".json.tmp")
            tmp.write_bytes(raw)
            tmp.replace(DATA_FILE)
        self._send(200, b'{"ok":true}', "application/json")

    def _serve(self):
        fp = self._safe_path(self.path)
        if fp is None:
            # SPA fallback: unknown paths get index.html (it's a single-page app)
            fp = SITE_DIR / "index.html"
            if not fp.is_file():
                return self._send(
                    404,
                    b"Build not found. Run: bun install && bun run build",
                )
        ctype = MIME.get(fp.suffix, "application/octet-stream")
        self._send(200, fp.read_bytes(), ctype)


def main() -> None:
    if not (SITE_DIR / "index.html").is_file():
        print(
            "!! 'out/' not found. Build the site first:\n"
            "      bun install && bun run build\n"
            "   Then run: python3 app.py"
        )
        raise SystemExit(1)
    ensure_data_file()
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print("─" * 56)
    print(f"  Copal          →  {url}")
    print(f"  Data file      →  {DATA_FILE}")
    print(f"  Vault          →  {VAULT_DIR}")
    print("  Stop           →  Ctrl+C")
    print("─" * 56)
    print("  (edit move-data.json by hand any time; reload the page to see it)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
