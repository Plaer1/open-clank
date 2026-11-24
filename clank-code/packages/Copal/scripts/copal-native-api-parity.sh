#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f out/index.html ]; then
  bun run build
fi

TMP="$(mktemp -d)"
PY_LOG="$TMP/python.log"
NATIVE_LOG="$TMP/native.log"
PY_PID=""
NATIVE_PID=""
MOVE_BACKUP="$TMP/move-data.json.bak"
MOVE_EXISTED=0

cleanup() {
  if [ -n "$PY_PID" ]; then kill "$PY_PID" 2>/dev/null || true; wait "$PY_PID" 2>/dev/null || true; fi
  if [ -n "$NATIVE_PID" ]; then kill "$NATIVE_PID" 2>/dev/null || true; wait "$NATIVE_PID" 2>/dev/null || true; fi
  if [ "$MOVE_EXISTED" -eq 1 ]; then
    cp "$MOVE_BACKUP" move-data.json
  else
    rm -f move-data.json
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

mkdir -p "$TMP/vault"
cp -a sample-vault/. "$TMP/vault/"

if [ -f move-data.json ]; then
  MOVE_EXISTED=1
  cp move-data.json "$MOVE_BACKUP"
fi

PORT="${PORT:-18765}"
while curl -sf --max-time 1 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; do
  PORT=$((PORT + 1))
done

env PORT="$PORT" COPAL_VAULT_DIR="$TMP/vault" python3 app.py >"$PY_LOG" 2>&1 &
PY_PID=$!
PY_URL="http://127.0.0.1:$PORT"

for _ in $(seq 1 80); do
  curl -sf --max-time 1 "$PY_URL/api/notes" >/dev/null 2>&1 && break
  sleep 0.25
done
curl -sf --max-time 2 "$PY_URL/api/notes" >/dev/null

CARGO_TARGET_DIR=/tmp/copal-servo-target cargo build --manifest-path servo-shell/Cargo.toml --no-default-features --features native-api >/dev/null
# COPAL_SOURCE=files: this script checks parity of the two FILE-serving
# implementations; the native binary otherwise defaults to the copal-db store.
env COPAL_NATIVE_API_ONLY=1 COPAL_SOURCE=files COPAL_VAULT_DIR="$TMP/vault" /tmp/copal-servo-target/debug/copal-servo-shell >"$NATIVE_LOG" 2>&1 &
NATIVE_PID=$!

NATIVE_URL=""
for _ in $(seq 1 80); do
  NATIVE_URL="$(sed -n 's/^native_api_url=//p' "$NATIVE_LOG" | tail -1)"
  if [ -n "$NATIVE_URL" ] && curl -sf --max-time 1 "$NATIVE_URL/api/notes" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
if [ -z "$NATIVE_URL" ]; then
  echo "ERROR: native API did not print native_api_url" >&2
  cat "$NATIVE_LOG" >&2 || true
  exit 1
fi

python3 - "$PY_URL" "$NATIVE_URL" <<'PY'
import io, json, sys, urllib.parse, urllib.request, zipfile
from copy import deepcopy

py_url, native_url = sys.argv[1:3]

def get(base, path, binary=False):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        data = response.read()
    return data if binary else json.loads(data.decode("utf-8"))

def post(base, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))

def normalize(value):
    value = deepcopy(value)
    def walk(obj):
        if isinstance(obj, dict):
            obj.pop("exportedAt", None)
            obj.pop("mtime", None)
            if "vaultPath" in obj:
                obj["vaultPath"] = "<vault>"
            if "path" in obj and isinstance(obj["path"], str) and "/copal-native-api" in obj["path"]:
                obj["path"] = "<path>"
            for k, v in list(obj.items()):
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
            obj.sort(key=lambda x: json.dumps(x, sort_keys=True) if not isinstance(x, str) else x)
    walk(value)
    return value

def assert_same(path):
    a = normalize(get(py_url, path))
    b = normalize(get(native_url, path))
    if a != b:
        raise AssertionError(f"mismatch {path}: {first_diff(a, b)}\npython={json.dumps(a, indent=2, sort_keys=True)[:1200]}\nnative={json.dumps(b, indent=2, sort_keys=True)[:1200]}")

def first_diff(a, b, at="$"):
    if type(a) is not type(b):
        return f"{at}: type {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        ak, bk = set(a), set(b)
        if ak != bk:
            return f"{at}: keys only-python={sorted(ak-bk)} only-native={sorted(bk-ak)}"
        for key in sorted(ak):
            diff = first_diff(a[key], b[key], f"{at}.{key}")
            if diff:
                return diff
        return ""
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{at}: len {len(a)} != {len(b)}"
        for i, (left, right) in enumerate(zip(a, b)):
            diff = first_diff(left, right, f"{at}[{i}]")
            if diff:
                return diff
        return ""
    if a != b:
        return f"{at}: {a!r} != {b!r}"
    return ""

for path in [
    "/api/data",
    "/api/vault",
    "/api/ui-state",
    "/api/notes",
    "/api/index",
    "/api/search?q=project",
    "/api/graph",
    "/api/tasks",
    "/api/export/ai",
    "/api/export/okf",
]:
    assert_same(path)

note_path = urllib.parse.quote("Welcome to Copal.md")
assert_same(f"/api/note?path={note_path}")
assert_same(f"/api/backlinks?path={note_path}")

py_xml = get(py_url, "/api/export/doclang", binary=True).decode("utf-8")
native_xml = get(native_url, "/api/export/doclang", binary=True).decode("utf-8")
for needle in ["<doclang>", "Copal Vault Draft Export", "Welcome"]:
    if needle not in py_xml or needle not in native_xml:
        raise AssertionError(f"doclang missing {needle}")

def zip_names(base):
    data = get(base, "/api/export/markdown-bundle", binary=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return sorted(zf.namelist())

if zip_names(py_url) != zip_names(native_url):
    raise AssertionError("markdown bundle zip names differ")

for base in [py_url, native_url]:
    post(base, "/api/ui-state", {"active": "wiki"})
    post(base, "/api/mkdir", {"path": "Parity/Nested"})
    post(base, "/api/note", {"path": "Parity/Nested/Note.md", "content": "# Parity\n"})
    post(base, "/api/note/rename", {"path": "Parity/Nested/Note.md", "newPath": "Parity/Nested/Renamed.md"})
    post(base, "/api/note/delete", {"path": "Parity/Nested/Renamed.md"})

print("native_api_parity=ok")
PY

echo "python_url=$PY_URL"
echo "native_url=$NATIVE_URL"
