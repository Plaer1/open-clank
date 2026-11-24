#!/usr/bin/env python3
"""Small Open Clank scoped API helper for Codex terminal sessions."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _usage() -> int:
    print("usage:", file=sys.stderr)
    print("  openclank_api.py capabilities", file=sys.stderr)
    print("  openclank_api.py sessions list [offset] [limit]", file=sys.stderr)
    print("  openclank_api.py sessions read|progress SESSION_ID", file=sys.stderr)
    print("  openclank_api.py sessions create JSON_PAYLOAD", file=sys.stderr)
    print("  openclank_api.py sessions send SESSION_ID JSON_PAYLOAD", file=sys.stderr)
    print("  openclank_api.py sessions steer SESSION_ID ACTOR_ID MAP_REV EXEC_REV JSON_PAYLOAD", file=sys.stderr)
    print("  openclank_api.py todos list", file=sys.stderr)
    print("  openclank_api.py todos add TITLE", file=sys.stderr)
    print("  openclank_api.py emails list [limit]", file=sys.stderr)
    print("  openclank_api.py emails read UID", file=sys.stderr)
    print("  openclank_api.py emails draft-doc JSON_PAYLOAD", file=sys.stderr)
    print("  openclank_api.py documents list [limit]", file=sys.stderr)
    print("  openclank_api.py documents read DOC_ID", file=sys.stderr)
    print("  openclank_api.py documents create JSON_PAYLOAD", file=sys.stderr)
    print("  openclank_api.py documents delete DOC_ID", file=sys.stderr)
    print("  openclank_api.py cookbook tasks", file=sys.stderr)
    print("  openclank_api.py cookbook servers", file=sys.stderr)
    print("  openclank_api.py cookbook cached [HOST]", file=sys.stderr)
    print("  openclank_api.py cookbook presets", file=sys.stderr)
    print("  openclank_api.py cookbook output SESSION_ID [tail]", file=sys.stderr)
    print("  openclank_api.py cookbook serve REPO_ID 'CMD' [REMOTE_HOST]", file=sys.stderr)
    print("  openclank_api.py cookbook preset NAME", file=sys.stderr)
    print("  openclank_api.py cookbook adopt SESSION_ID MODEL [HOST] [PORT]", file=sys.stderr)
    print("  openclank_api.py cookbook stop SESSION_ID", file=sys.stderr)
    print("  openclank_api.py METHOD /api/codex/path [json-body]", file=sys.stderr)
    return 2


def _config() -> tuple[str, str] | None:
    base_url = os.environ.get("OPEN_CLANK_URL", "").strip().rstrip("/")
    token = os.environ.get("OPEN_CLANK_API_TOKEN", "").strip()
    missing = []
    if not base_url:
        missing.append("OPEN_CLANK_URL")
    if not token:
        missing.append("OPEN_CLANK_API_TOKEN")
    if missing:
        print(f"missing {', '.join(missing)}; create a Codex Agent token in Open Clank Settings", file=sys.stderr)
        return None
    return base_url, token


def main() -> int:
    if len(sys.argv) < 2:
        return _usage()

    command = sys.argv[1].lower()
    if command == "capabilities":
        method = "GET"
        path = "/api/codex/capabilities"
        body = None
    elif command == "sessions":
        from urllib.parse import quote, urlencode
        if len(sys.argv) < 3:
            return _usage()
        action = sys.argv[2].lower()
        if action == "list" and len(sys.argv) <= 5:
            method, body = "GET", None
            path = "/api/codex/sessions?" + urlencode({"offset": sys.argv[3] if len(sys.argv) > 3 else 0, "limit": sys.argv[4] if len(sys.argv) > 4 else 50})
        elif action in {"read", "progress"} and len(sys.argv) == 4:
            method, body = "GET", None
            path = "/api/codex/sessions/" + quote(sys.argv[3], safe="") + ("/progress" if action == "progress" else "")
        elif action == "create" and len(sys.argv) == 4:
            method, path, body = "POST", "/api/codex/sessions", sys.argv[3]
        elif action == "send" and len(sys.argv) == 5:
            method, body = "POST", sys.argv[4]
            path = "/api/codex/sessions/" + quote(sys.argv[3], safe="") + "/send"
        elif action == "steer" and len(sys.argv) == 8:
            method, body = "POST", sys.argv[7]
            path = "/api/codex/sessions/" + quote(sys.argv[3], safe="") + "/actors/" + quote(sys.argv[4], safe="") + "/steer?" + urlencode({"map_revision": sys.argv[5], "expected_execution_revision": sys.argv[6]})
        else:
            return _usage()
    elif command == "todos":
        if len(sys.argv) < 3:
            return _usage()
        action = sys.argv[2].lower()
        path = "/api/codex/todos"
        if action == "list":
            method = "GET"
            body = None
        elif action == "add" and len(sys.argv) >= 4:
            method = "POST"
            body = json.dumps({"action": "add", "title": " ".join(sys.argv[3:])})
        else:
            return _usage()
    elif command == "emails":
        if len(sys.argv) < 3:
            return _usage()
        action = sys.argv[2].lower()
        if action == "list":
            method = "GET"
            limit = sys.argv[3] if len(sys.argv) >= 4 else "10"
            path = f"/api/codex/emails?folder=INBOX&limit={limit}&offset=0&filter=all"
            body = None
        elif action == "read" and len(sys.argv) >= 4:
            method = "GET"
            path = f"/api/codex/emails/{sys.argv[3]}"
            body = None
        elif action in ("draft-doc", "draft_document") and len(sys.argv) >= 4:
            method = "POST"
            path = "/api/codex/emails/draft-document"
            body = " ".join(sys.argv[3:])
        else:
            return _usage()
    elif command in ("documents", "docs"):
        if len(sys.argv) < 3:
            return _usage()
        action = sys.argv[2].lower()
        if action == "list":
            method = "GET"
            limit = sys.argv[3] if len(sys.argv) >= 4 else "50"
            path = f"/api/codex/documents?limit={limit}"
            body = None
        elif action == "read" and len(sys.argv) >= 4:
            method = "GET"
            path = f"/api/codex/documents/{sys.argv[3]}"
            body = None
        elif action == "create" and len(sys.argv) >= 4:
            method = "POST"
            path = "/api/codex/documents"
            body = " ".join(sys.argv[3:])
        elif action == "delete" and len(sys.argv) >= 4:
            method = "DELETE"
            path = f"/api/codex/documents/{sys.argv[3]}"
            body = None
        else:
            return _usage()
    elif command == "cookbook":
        if len(sys.argv) < 3:
            return _usage()
        action = sys.argv[2].lower()
        if action == "tasks":
            method = "GET"
            path = "/api/codex/cookbook/tasks"
            body = None
        elif action == "servers":
            method = "GET"
            path = "/api/codex/cookbook/servers"
            body = None
        elif action == "output" and len(sys.argv) >= 4:
            method = "GET"
            sid = sys.argv[3]
            tail = sys.argv[4] if len(sys.argv) >= 5 else "400"
            path = f"/api/codex/cookbook/output/{sid}?tail={tail}"
            body = None
        elif action == "cached":
            method = "GET"
            if len(sys.argv) >= 4:
                from urllib.parse import quote
                path = f"/api/codex/cookbook/cached?host={quote(sys.argv[3])}"
            else:
                path = "/api/codex/cookbook/cached"
            body = None
        elif action == "presets":
            method = "GET"
            path = "/api/codex/cookbook/presets"
            body = None
        elif action == "preset" and len(sys.argv) >= 4:
            from urllib.parse import quote
            method = "POST"
            path = f"/api/codex/cookbook/preset/{quote(sys.argv[3])}"
            body = None
        elif action == "adopt" and len(sys.argv) >= 5:
            method = "POST"
            path = "/api/codex/cookbook/adopt"
            payload = {"tmux_session": sys.argv[3], "model": sys.argv[4]}
            if len(sys.argv) >= 6: payload["host"] = sys.argv[5]
            if len(sys.argv) >= 7: payload["port"] = int(sys.argv[6])
            body = json.dumps(payload)
        elif action == "serve" and len(sys.argv) >= 5:
            method = "POST"
            path = "/api/codex/cookbook/serve"
            payload = {"repo_id": sys.argv[3], "cmd": sys.argv[4]}
            if len(sys.argv) >= 6:
                payload["remote_host"] = sys.argv[5]
            body = json.dumps(payload)
        elif action == "stop" and len(sys.argv) >= 4:
            method = "POST"
            path = f"/api/codex/cookbook/stop/{sys.argv[3]}"
            body = None
        else:
            return _usage()
    else:
        if len(sys.argv) < 3:
            return _usage()
        method = sys.argv[1].upper()
        path = sys.argv[2]
        body = sys.argv[3] if len(sys.argv) > 3 else None

    if not path.startswith("/"):
        path = "/" + path
    if not path.startswith("/api/codex/"):
        print("refusing non-/api/codex path; use scoped Open Clank integration endpoints only", file=sys.stderr)
        return 2

    config = _config()
    if config is None:
        return 2
    base_url, token = config

    data = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if body is not None:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            print(f"invalid json body: {exc}", file=sys.stderr)
            return 2
        data = json.dumps(parsed).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(resp.read().decode("utf-8"))
            return 0
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        print(text or f"HTTP {exc.code}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
