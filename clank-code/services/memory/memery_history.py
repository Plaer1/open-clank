"""Publish references to committed Memery revisions on the host maintenance loop."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import quote

from src.memery_v2 import _provider_dossier_evidence_blocked
from src.memory_versioned import history_block_visible

log = logging.getLogger(__name__)
BATCH = 32
# Each table has an independent durable cursor. Older records/asset streams keep their wire IDs.
COLLECTIONS = (("memery_records", {}), ("memery_artifacts", {"assets": True}),
               ("general_hex", {"guidance": True}))

_HEX_COLUMNS = """rowid,owner_id,hex_id AS block_id,revision,
    json_extract(record_json,'$.hash') AS content_hash,
    'general-hex:' || json_extract(record_json,'$.hash') AS change_set_id,
    json_extract(record_json,'$.created_at') AS created_at,0 AS evidence"""


def _live_guidance(db, owner, block, revision):
    return db.execute(f"""SELECT {_HEX_COLUMNS} FROM fm_general_hex_revisions r
        WHERE r.owner_id=? AND r.hex_id=? AND r.revision=?
        AND json_type(r.record_json, '$.accepted')='true'
        AND json_type(r.record_json, '$.deleted')='false'
        AND EXISTS(SELECT 1 FROM fm_general_hex_heads h WHERE h.owner_id=r.owner_id
                   AND h.hex_id=r.hex_id AND h.deleted=0)""", (owner, block, revision)).fetchone()



def change_key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def account_current(state, owner, account):
    auth = state.auth_manager
    return (bool(account) and auth.account_id(owner) == account
            and auth.username_for_account_id(account) == owner
            and not auth.is_account_lifecycle_fenced(owner)
            and not state.account_owner_lifecycle.owner_has_active_operation(owner)
            and not state.memory_reset_coordinator().owner_is_fenced(owner))


def client_for(supervisor, account):
    return supervisor.client_for("service:memery-history:" + account, account)


def persisted_time(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp() * 1000)


def reference(row):
    return {"block": row["block_id"], "revision": row["revision"],
            "change": change_key(row["change_set_id"]), "digest": row["content_hash"],
            "at": persisted_time(row["created_at"]), "evidence": bool(row["evidence"])}


def read_guidance_revision(path, owner, hex_id, revision):
    """Rehydrate a visible, accepted owner revision. Lore stores its reference only."""
    with closing(sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro",
                                 uri=True, timeout=0.5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        row = _live_guidance(db, owner, hex_id, revision)
        if row is None:
            return None
        raw, = db.execute("""SELECT record_json FROM fm_general_hex_revisions
            WHERE owner_id=? AND hex_id=? AND revision=?""", (owner, hex_id, revision)).fetchone()
        record = json.loads(raw)
        digest = record.pop("hash")
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if (digest != hashlib.sha256(encoded.encode()).hexdigest()
                or record["hex_id"] != hex_id or record["revision"] != revision):
            return None
        return reference(row), {k: record[k] for k in ("title", "body", "enabled", "revision")}


def _query_batch(path, owner, workspace, manifest, *, assets=False, guidance=False, enumerate_only=False, recheck_offset=None):
    with closing(sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro",
                                 uri=True, timeout=0.5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        position, witness = manifest["position"], manifest["witness"]
        if guidance:
            table = "fm_general_hex_revisions"
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return 0, "", [], [entry["change"] for entry in manifest["entries"]]
            columns = _HEX_COLUMNS

            def live(block, revision):
                return _live_guidance(db, owner, block, revision)
        elif assets:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fm_v2_media_assets'").fetchone():
                return 0, "", [], [entry["change"] for entry in manifest["entries"]]
            columns = """rowid,owner_id,asset_id AS block_id,1 AS revision,canonical_sha256 AS content_hash,
                asset_id || ':' || canonical_sha256 || ':' || created_at AS change_set_id,created_at,0 AS evidence"""
            table = "fm_v2_media_assets"

            def live(block, revision):
                if revision != 1:
                    return None
                return db.execute(f"SELECT {columns} FROM {table} WHERE owner_id=? AND asset_id=? AND state='active'",
                                  (owner, block)).fetchone()
        else:
            columns = """rowid,owner_id,block_id,revision,change_set_id,content_hash,created_at,
                (reason='approved analysis snapshot') AS evidence"""
            table = "fm_v2_knowledge_revisions"

            def live(block, revision):
                row = db.execute("""SELECT r.change_set_id,r.content_hash FROM fm_v2_knowledge_revisions r
                    JOIN fm_v2_knowledge_blocks b ON b.owner_id=r.owner_id AND b.block_id=r.block_id
                    WHERE b.owner_id=? AND b.workspace_key IN (?, '') AND b.project_key=''
                      AND r.block_id=? AND r.revision=?""", (owner, workspace, block, revision)).fetchone()
                if row is None or not history_block_visible(db, owner, block) or _provider_dossier_evidence_blocked(db, owner, block, revision):
                    return None
                return row

        if position and not enumerate_only:
            last = db.execute(f"SELECT {columns} FROM {table} WHERE owner_id=? AND rowid=?", (owner, position)).fetchone()
            if last is None or change_key(last["change_set_id"]) != witness:
                position, witness = 0, ""
        rows = () if enumerate_only else db.execute(
            f"SELECT {columns} FROM {table} WHERE owner_id=? AND rowid>? ORDER BY rowid LIMIT ?", (owner, position, BATCH))
        add = []
        for row in rows:
            position, witness = row["rowid"], change_key(row["change_set_id"])
            if row["owner_id"] != owner or live(row["block_id"], row["revision"]) is None:
                continue
            try:
                item = reference(row)
            except (TypeError, ValueError, OverflowError, AttributeError):
                continue
            digest = item["digest"]
            if (not isinstance(digest, str) or len(digest) != 64 or
                    any(c not in "0123456789abcdef" for c in digest) or
                    not item["block"] or not item["block"].isprintable() or
                    len(item["block"].encode()) > 256 or item["at"] < 0):
                continue
            add.append(item)
        remove = []
        retained = manifest["entries"]
        if retained and recheck_offset is not None:
            start = recheck_offset % len(retained)
            retained = (retained[start:] + retained[:start])[:16]
        for item in retained:
            source = live(item["block"], item["revision"])
            if (source is None or source["content_hash"] != item["digest"]
                    or change_key(source["change_set_id"]) != item["change"]):
                remove.append(item["change"])
        return position, witness, add, remove


async def publish_loop(state, users):
    """Process one bounded page per account per pass, retrying the durable cursor."""
    from src.openclank.chat_source_bridge import canonical_source_provider

    last = {}
    while True:
        await asyncio.sleep(15)
        owners = tuple(users())
        last = {owner: value for owner, value in last.items() if owner in owners}
        for owner in owners:
            try:
                account = state.auth_manager.account_id(owner)
                if not account_current(state, owner, account):
                    continue
                provider = canonical_source_provider(state) or getattr(state, "memory_provider", None)
                path = provider._local_db_path() if hasattr(provider, "_local_db_path") else None
                if not path:
                    continue
                def stamp():
                    result = []
                    for name in (str(path), str(path) + "-wal"):
                        try:
                            st = os.stat(name)
                            result.append((st.st_ino, st.st_mtime_ns, st.st_size))
                        except FileNotFoundError:
                            result.append(None)
                    return (account, str(path), tuple(result))
                signature = await asyncio.to_thread(stamp)
                previous, due, offset = last.get(owner, (None, 0, 0))
                periodic = time.monotonic() >= due
                if previous == signature and not periodic:
                    continue
                more = False
                purged = False
                scope = provider._scope(owner)
                supervisor = state.history_supervisor
                client = client_for(supervisor, account)
                for _, collection in COLLECTIONS:
                    manifest = await asyncio.to_thread(client.get_memery_manifest, **collection)
                    position, witness, add, remove = await asyncio.to_thread(
                        _query_batch, path, scope.owner, scope.workspace_id, manifest, recheck_offset=offset, **collection)
                    if (not account_current(state, owner, account) or state.history_supervisor is not supervisor or
                            (canonical_source_provider(state) or getattr(state, "memory_provider", None)) is not provider):
                        break
                    advanced = (position, witness) != (manifest["position"], manifest["witness"])
                    more |= advanced
                    purged |= bool(remove)
                    if not (advanced or add or remove or (periodic and manifest["entries"])):
                        continue
                    if remove and add:
                        # Erasure must make progress even when new records hit the storage ceiling.
                        manifest = await asyncio.to_thread(client.settle_memery_manifest, manifest["generation"],
                            manifest["position"], manifest["witness"], [], remove, **collection)
                        remove = []
                    await asyncio.to_thread(client.settle_memery_manifest, manifest["generation"],
                                            position, witness, add, remove, **collection)
                else:
                    if (periodic or purged) and account_current(state, owner, account):
                        await asyncio.to_thread(client.sweep_memery_components)
                    last[owner] = (None if more else signature,
                                   time.monotonic() + 60 if periodic else due, offset + 16)
            except Exception as exc:
                log.debug("Memery history maintenance deferred (%s)", type(exc).__name__)
