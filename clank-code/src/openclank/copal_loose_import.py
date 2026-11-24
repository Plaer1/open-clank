"""The existing scoped vault-import operation for the Files Copal adapter.

Called only under copal_commit_lock. A private changed-file journal makes the
manifest the commit point: interrupted precommit publication rolls back before
any adapter operation can read it. This is deliberately not a general writer.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from src.openclank.copal_errors import CopalBridgeError

MAX_FILES = 10_000
MAX_BYTES = 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_NOTE_BYTES = 8_388_608
MAX_TRANSACTION_BYTES = 4 * MAX_BYTES
MIN_FREE_BYTES = 64 * 1024 * 1024
JOURNAL = ".copal-import-journal.json"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_HEAD = re.compile(r"^sha256:[a-f0-9]{64}:\d+$")
_UNSPECIFIED = object()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parts(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise CopalBridgeError("invalid import relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise CopalBridgeError("invalid import relative path")
    return path.parts


@contextmanager
def _parent(root, relative, *, create=False):
    """Pin each parent without following source/destination symlink swaps."""
    parts = _parts(relative)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd, parts[-1]
    finally:
        os.close(fd)


@contextmanager
def _regular(root, relative):
    with _parent(root, relative) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise CopalBridgeError("import paths must be regular files")
        yield stream


def _fingerprint(root, relative):
    try:
        with _regular(root, relative) as stream:
            digest, size = hashlib.sha256(), 0
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            return f"sha256:{digest.hexdigest()}:{size}"
    except FileNotFoundError:
        return None


def _copy(root, relative, target, *, limit=MAX_MEMBER_BYTES):
    with _regular(root, relative) as source, target.open("xb") as destination:
        before = os.fstat(source.fileno())
        digest, size = hashlib.sha256(), 0
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise CopalBridgeError("import member exceeds its byte limit")
            digest.update(chunk)
            destination.write(chunk)
        after = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or size != before.st_size:
            raise CopalBridgeError("import source changed while being read")
        destination.flush()
        os.fsync(destination.fileno())
    return f"sha256:{digest.hexdigest()}:{size}", size


def _install(repo, source, relative, expected):
    """Publish one staged file through an anchored, fsynced atomic rename."""
    with _parent(repo.data_dir, relative, create=True) as (parent, name):
        # Keep an interrupted publication copy inside our reclaimable staging
        # tree, not as an unrecorded private payload beside a user's document.
        temporary = source.parent.parent / f"publish-{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as destination, source.open("rb") as origin:
                digest, size = hashlib.sha256(), 0
                while chunk := origin.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    destination.write(chunk)
                if f"sha256:{digest.hexdigest()}:{size}" != expected:
                    raise CopalBridgeError("import staging fingerprint changed")
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, name, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _cleanup(repo, directory):
    if directory.parent != repo.data_dir or not re.fullmatch(r"\.copal-import-[A-Za-z0-9_-]+", directory.name) or directory.is_symlink():
        raise CopalBridgeError("invalid import transaction directory")
    # This is solely the exact private staging directory created by this import.
    # Leave the durable marker until every payload is gone. A process crash
    # during reclamation must not strand an unrecognizable private partial tree.
    for child in directory.iterdir():
        if child.name == "owner.json":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    (directory / "owner.json").unlink(missing_ok=True)
    directory.rmdir()


def recover_import(repo):
    """Finish/roll back one interrupted import before any adapter operation."""
    path = repo.data_dir / JOURNAL
    if not os.path.lexists(path):
        _reclaim_orphans(repo)
        return None
    journal = repo._read_json_file(path, label="Copal import journal")
    owner = repo._validate_mutable_owner(journal.get("owner"))
    workspace = journal.get("workspace")
    vault = repo._vault(owner, workspace)
    prefix = vault.relative_to(repo.data_dir).as_posix() + "/"
    transaction = str(journal.get("transaction") or "")
    if not re.fullmatch(r"\.copal-import-[A-Za-z0-9_-]+", transaction) or journal.get("version") != 1:
        raise CopalBridgeError("invalid Copal import journal")
    directory = repo.data_dir / transaction
    if directory.is_symlink() or not directory.is_dir():
        raise CopalBridgeError("Copal import recovery staging is unavailable")
    writes = journal.get("writes")
    if not isinstance(writes, list) or not writes or len(writes) > MAX_FILES * 2 + 1:
        raise CopalBridgeError("invalid Copal import recovery writes")
    seen = set()
    for item in writes:
        relative = item.get("path") if isinstance(item, dict) else None
        _parts(relative)
        if not relative.startswith(prefix) or relative in seen or not _HEAD.fullmatch(str(item.get("after") or "")) or (item.get("before") is not None and not _HEAD.fullmatch(str(item["before"]))):
            raise CopalBridgeError("invalid Copal import recovery target")
        seen.add(relative)
    if writes[-1]["path"] != prefix + ".copal/manifest.json":
        raise CopalBridgeError("Copal import manifest commit point is missing")
    manifest = writes[-1]
    actual_manifest = _fingerprint(repo.data_dir, manifest["path"])
    committed = actual_manifest == manifest["after"]
    if not committed:
        if actual_manifest != manifest["before"]:
            raise CopalBridgeError("Copal import recovery pending: manifest changed externally")
        # Check the entire recovery plan first. Never overwrite an unexplained
        # external change while rolling back some other member of the batch.
        for index, item in enumerate(writes):
            current = _fingerprint(repo.data_dir, item["path"])
            if current not in {item["before"], item["after"]}:
                raise CopalBridgeError("Copal import recovery pending: target changed externally")
            if item["before"] is not None and _fingerprint(directory, f"before/{index}") != item["before"]:
                raise CopalBridgeError("Copal import recovery preimage is unavailable")
        for index in reversed(range(len(writes))):
            item = writes[index]
            if _fingerprint(repo.data_dir, item["path"]) == item["before"]:
                continue
            if item["before"] is not None:
                _install(repo, directory / "before" / str(index), item["path"], item["before"])
            else:
                with _parent(repo.data_dir, item["path"]) as (parent, name):
                    os.unlink(name, dir_fd=parent)
                    os.fsync(parent)
    # A committed manifest is the authority. Later external document edits must
    # not be mistaken for a failed import and restored over by startup recovery.
    path.unlink()
    repo._fsync_directory(repo.data_dir)
    _cleanup(repo, directory)
    return "committed" if committed else "rolled_back"


def _reclaim_orphans(repo):
    """Only our marked, private staging trees; unknown lookalikes stay intact.

    The cross-process repository lock excludes active imports. A pre-marker
    crash can leave only an empty directory, never source bytes.
    """
    changed = False
    for directory in repo.data_dir.glob(".copal-import-*"):
        if not re.fullmatch(r"\.copal-import-[A-Za-z0-9_-]+", directory.name):
            continue
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            continue
        if not any(directory.iterdir()):
            directory.rmdir()
            changed = True
            continue
        try:
            with _regular(directory, "owner.json") as stream:
                marker = json.load(stream)
        except (OSError, ValueError, CopalBridgeError):
            continue
        if marker == {"version":1,"transaction":directory.name}:
            _cleanup(repo, directory)
            changed = True
    if changed:
        repo._fsync_directory(repo.data_dir)


def _collect(root):
    root_meta = root.lstat()
    if not stat.S_ISDIR(root_meta.st_mode) or stat.S_ISLNK(root_meta.st_mode):
        raise CopalBridgeError("import root must be a real directory")
    files, total, portable, directories = [], 0, set(), 0
    def visit(directory):
        nonlocal total, directories
        directories += 1
        if directories > MAX_FILES * 2 or len(directory.relative_to(root).parts) > 128:
            raise CopalBridgeError("import directory enumeration exceeds limits")
        for path in sorted(directory.iterdir()):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise CopalBridgeError("symbolic links and special files are not permitted in imports")
            if stat.S_ISDIR(info.st_mode):
                visit(path)
                continue
            relative = path.relative_to(root).as_posix()
            _parts(relative)
            key = unicodedata.normalize("NFC", relative).casefold()
            if key in portable:
                raise CopalBridgeError("import paths collide on portable filesystems")
            portable.add(key)
            total += info.st_size
            if len(files) >= MAX_FILES or info.st_size > MAX_MEMBER_BYTES or total > MAX_BYTES:
                raise CopalBridgeError("import exceeds file count or byte limits")
            files.append((relative, info.st_size))
    visit(root)
    return files


def _native_note(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return False
    body = value.get("body") if isinstance(value, dict) else None
    return isinstance(value, dict) and type(value.get("schemaVersion")) is int and value["schemaVersion"] == 1 and isinstance(body, dict) and body.get("type") == "doc" and isinstance(body.get("blocks"), list)


def _classify(relative, text, note_kind, identity, planning):
    """Keep the existing importer classifications; unknown formats stay inert."""
    parts = PurePosixPath(relative).parts
    wiki = parts[:2] == (".copal", "wiki") and len(parts) > 2
    event = parts[0] == ".events" and len(parts) > 1
    name = PurePosixPath(*parts[2:]).as_posix() if wiki else relative
    kind, corpus, reason = note_kind, "wiki" if note_kind == "wiki" or wiki else "notes", None
    if relative == planning or relative == ".copal/treehouse-state.json":
        try:
            json.loads(text)
        except (TypeError, ValueError) as exc:
            raise CopalBridgeError("planning or TreeHouse JSON is invalid") from exc
        return ("planning", "events", "move-data.json", None) if relative == planning else ("treehouse-state", "treehouse", relative, None)
    if relative in {".copal/tracks.json", ".copal/planning-migration.json"}:
        kind = "copal-tracks" if relative.endswith("tracks.json") else "copal-migration"
        try:
            record = json.loads(text)
        except (TypeError, ValueError):
            record = None
        versions = {1, 2} if kind == "copal-tracks" else {1}
        if not isinstance(record, dict) or type(record.get("schemaVersion")) is not int or record["schemaVersion"] not in versions:
            kind, reason = "compatibility", "invalid or future reserved JSON preserved inertly"
        return kind, "events", name, reason
    hidden = any(p.startswith(".") for p in PurePosixPath(name).parts)
    suffix = PurePosixPath(name).suffix.lower()
    native_wiki = wiki and identity is not None and identity.get("kind") == "wiki"
    native_asset = wiki and identity is not None and identity.get("kind") == "asset"
    note = (not hidden or native_wiki or event) and (suffix in {".md", ".markdown", ".base", ".canvas", ".dclg"} or native_wiki)
    if event:
        corpus = "events"
    if note:
        if text is None:
            return "compatibility", corpus, name, "non-UTF-8 or oversized note preserved as inert bytes"
        if event:
            from src.openclank.copal_planning import split_frontmatter
            try:
                properties, _ = split_frontmatter(text)
                valid = properties.get("copal_type") == "event" and properties.get("copal_schema") in {1, 2}
            except ValueError:
                valid = False
            if not valid:
                return "compatibility", corpus, name, "invalid or future event record preserved as inert bytes"
            kind = "copal-event"
        elif suffix in {".md", ".markdown"} and note_kind in {"note", "wiki"} and not native_wiki and not _native_note(text):
            return "compatibility", corpus, name, "unprepared Markdown preserved as inert bytes"
        else:
            kind = {".base":"base", ".canvas":"canvas", ".dclg":"markdown"}.get(suffix, "wiki" if wiki else note_kind)
        return kind, corpus, name, None
    return ("compatibility", corpus, name, "dot-namespace data preserved inertly") if hidden and not native_asset else ("asset", corpus, name, None)


def _import_vault(repo, args):
    """Preflight the entire existing operation, then publish one scoped batch."""
    owner = repo._validate_mutable_owner(args.get("owner"))
    workspace = str(args.get("workspace_id") or "")
    if not workspace or workspace.strip() != workspace:
        raise CopalBridgeError("workspace is required")
    root = Path(str(args.get("path") or ""))
    if not args.get("path"):
        raise CopalBridgeError("import path is required")
    files = _collect(root)
    note_kind = str(args.get("note_kind") or "markdown")
    if note_kind not in {"markdown", "note", "wiki"}:
        raise CopalBridgeError("note kind must be markdown, note, or wiki")
    planning = args.get("planning_path")
    if planning:
        try:
            planning = Path(planning).relative_to(root).as_posix()
        except ValueError as exc:
            raise CopalBridgeError("planning file must be inside the import root") from exc
        if planning not in {name for name, _ in files}:
            raise CopalBridgeError("planning file is not a real imported file")
    identities = args.get("restore_ids", {})
    heads = args.get("expected_heads", {})
    if not isinstance(identities, dict) or not isinstance(heads, dict):
        raise CopalBridgeError("restore identities and expected heads must be objects")
    if set(identities) - {name for name, _ in files}:
        raise CopalBridgeError("restore identity map does not reconcile imported paths")
    ids = set()
    for name, identity in identities.items():
        _parts(name)
        if not isinstance(identity, dict) or not isinstance(identity.get("id"), str) or not _ID.fullmatch(identity["id"]) or not isinstance(identity.get("kind"), str) or not identity["kind"] or not isinstance(identity.get("corpus"), str) or not identity["corpus"] or identity["id"] in ids:
            raise CopalBridgeError("restore identity map contains invalid or duplicate fields")
        ids.add(identity["id"])
    vault = repo._vault(owner, workspace)
    prefix = vault.relative_to(repo.data_dir).as_posix() + "/"
    # Reading the manifest through anchored descriptors also rejects a symlink
    # owner/workspace root before any private source bytes can be published.
    manifest_head = _fingerprint(repo.data_dir, prefix + ".copal/manifest.json")
    manifest = copy.deepcopy(repo._load(vault))
    documents = manifest["documents"]
    for identifier, item in documents.items():
        if not isinstance(item, dict) or item.get("owner") != owner or item.get("workspace_id") != workspace or item.get("id") != identifier or not _ID.fullmatch(identifier):
            raise CopalBridgeError("import scope manifest ownership is invalid")
    if ids:
        for path in repo.data_dir.glob("*/*/.copal/manifest.json"):
            relative = path.relative_to(repo.data_dir).as_posix()
            _fingerprint(repo.data_dir, relative)
            other = repo._read_json_file(path, label="Copal identity manifest")
            for identifier in ids.intersection(other.get("documents", {})):
                item = other["documents"][identifier]
                if path != repo._manifest_path(vault) or item.get("trashed") or item.get("builtin") or (item.get("readOnly") and item.get("kind") not in {"asset", "compatibility"}) or repo._is_official_reference(item):
                    raise CopalBridgeError("restore identity is owned outside the mutable requested scope")
    for identifier, head in heads.items():
        record = documents.get(identifier)
        if not record or record.get("trashed") or not isinstance(head, str) or not record.get("path") or _fingerprint(repo.data_dir, prefix + record["path"]) != head:
            raise CopalBridgeError("stale or missing restore head")
    directory = Path(tempfile.mkdtemp(prefix=".copal-import-", dir=repo.data_dir))
    repo._atomic_write(directory / "owner.json", _json({"version":1,"transaction":directory.name}))
    repo._fsync_directory(repo.data_dir)
    (directory / "source").mkdir(mode=0o700)
    (directory / "after").mkdir(mode=0o700)
    (directory / "before").mkdir(mode=0o700)
    writes, targets = [], set()
    stats = {"notes":0, "assets":0, "compatibility":0, "unchanged":0, "planning":False, "treehouse":False, "restored_identities":len(identities), "entries":[], "op":""}
    journal_written = False
    admitted, committed = [], False
    staged_bytes, publication_bytes = 0, 0
    def reserve(size, *, publication=0):
        nonlocal staged_bytes, publication_bytes
        if size < 0 or staged_bytes + size > MAX_TRANSACTION_BYTES:
            raise CopalBridgeError("import transaction staging exceeds byte limit")
        if shutil.disk_usage(repo.data_dir).free < size + publication_bytes + publication + MIN_FREE_BYTES:
            raise CopalBridgeError("insufficient disk headroom for atomic import")
        staged_bytes += size
        publication_bytes += publication
    def add_write(relative, source, after, *, expected_before=_UNSPECIFIED):
        relative = prefix + relative
        if relative in targets:
            raise CopalBridgeError("import destination paths collide")
        targets.add(relative)
        before = _fingerprint(repo.data_dir, relative)
        if expected_before is not _UNSPECIFIED and before != expected_before:
            raise CopalBridgeError("stale import target changed after its head guard")
        index = len(writes)
        if before is not None:
            reserve(int(before.rsplit(":",1)[1]))
            copied, _ = _copy(repo.data_dir, relative, directory / "before" / str(index), limit=MAX_BYTES)
            if copied != before:
                raise CopalBridgeError("import target changed while preserving preimage")
        reserve(source.stat().st_size, publication=source.stat().st_size)
        shutil.copyfile(source, directory / "after" / str(index))
        with (directory / "after" / str(index)).open("rb") as stream:
            os.fsync(stream.fileno())
        writes.append({"path":relative, "before":before, "after":after})
    try:
        portable = {}
        for record in documents.values():
            if not record.get("trashed") and record.get("corpus") == "wiki":
                key = unicodedata.normalize("NFC", record["name"]).casefold()
                if key in portable:
                    raise CopalBridgeError("existing Wiki resources collide on portable path")
                portable[key] = record
        from src.openclank.attachment_inventory import resource_references
        changed, references = [], set()
        for index, (relative, planned_size) in enumerate(files):
            staged = directory / "source" / str(index)
            reserve(planned_size)
            digest, size = _copy(root, relative, staged)
            if size != planned_size:
                raise CopalBridgeError("import source size changed after enumeration")
            text = None
            if size <= MAX_NOTE_BYTES:
                try:
                    text = staged.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    pass
            identity = identities.get(relative)
            kind, corpus, name, reason = _classify(relative, text, note_kind, identity, planning)
            if text is not None and kind not in {"asset", "compatibility"}:
                references.update(resource_references(text))
            if corpus == "wiki":
                key = unicodedata.normalize("NFC", name).casefold()
                prior = portable.get(key)
                if prior and (prior["name"] != name or prior["kind"] != kind or (identity is not None and prior["id"] != identity["id"])):
                    raise CopalBridgeError("Wiki resources collide on portable path")
            record = next((d for d in documents.values() if not d.get("trashed") and (d["corpus"],d["kind"],d["name"]) == (corpus,kind,name)), None)
            if identity and (identity["kind"] != kind or identity["corpus"] != corpus):
                raise CopalBridgeError("restore identity does not match imported corpus and kind")
            if identity and identity["id"] in documents and (record is None or record["id"] != identity["id"]):
                raise CopalBridgeError("restore identity does not match imported path")
            if record:
                repo._assert_not_official_reference(record)
                if (record.get("readOnly") and kind not in {"asset", "compatibility"}) or (identity and record["id"] != identity["id"]):
                    raise CopalBridgeError("import target is read-only or has conflicting identity")
                old_head = _fingerprint(repo.data_dir, prefix + record["path"])
                if old_head is None:
                    raise CopalBridgeError("import target file is missing")
                if record["id"] in heads and old_head != heads[record["id"]]:
                    raise CopalBridgeError("stale import target changed after its expected head guard")
                status = "unchanged" if old_head == digest else "updated"
            else:
                identifier = identity["id"] if identity else "doc_" + uuid.uuid4().hex
                physical = name
                if kind == "compatibility":
                    physical = f".copal/imported-compat/{identifier}.bin"
                elif corpus == "wiki":
                    physical = ".copal/wiki/" + name
                else:
                    repo._safe_name(name, kind)
                if any(d.get("path") == physical for d in documents.values()) or _fingerprint(repo.data_dir, prefix + physical) is not None:
                    raise CopalBridgeError("import destination already exists outside its document identity")
                record = {"id":identifier,"owner":owner,"workspace_id":workspace,"kind":kind,"corpus":corpus,"name":name,"path":physical,"createdAt":time.time(),"updatedAt":time.time(),"readOnly":kind in {"asset","compatibility"},"trashed":False}
                documents[identifier] = record
                if corpus == "wiki":
                    portable[key] = record
                status, old_head = "created", None
            if status != "unchanged":
                if old_head is not None:
                    if kind not in {"asset","compatibility"} and int(old_head.rsplit(":",1)[1]) > MAX_NOTE_BYTES:
                        raise CopalBridgeError("prior note exceeds import history byte limit")
                    prior = directory / f"preimage-{index}"
                    reserve(int(old_head.rsplit(":",1)[1]))
                    preserved, _ = _copy(repo.data_dir, prefix + record["path"], prior)
                    if preserved != old_head:
                        raise CopalBridgeError("stale import target changed while capturing history")
                if old_head is not None and kind in {"asset","compatibility"}:
                    add_write(f".copal/history/{record['id']}/{old_head}.bin", prior, old_head)
                elif old_head is not None:
                    history = directory / f"history-{index}"
                    previous = prior.read_text(encoding="utf-8")
                    history_text = _json({"head":old_head,"commit":old_head,"content":previous,"name":name,"ts":time.time(),"message":"import"})
                    reserve(len(history_text.encode("utf-8")))
                    history.write_text(history_text, encoding="utf-8")
                    add_write(f".copal/history/{record['id']}/{old_head}.json", history, repo._fingerprint(history.read_text(encoding="utf-8")))
                add_write(record["path"], staged, digest, expected_before=old_head)
                record["head"], record["updatedAt"] = digest, time.time()
                record.pop("mtime_ns", None)
                changed.append(record["id"])
            if status == "unchanged": stats["unchanged"] += 1
            elif kind == "planning": stats["planning"] = True
            elif kind == "treehouse-state": stats["treehouse"] = True
            elif kind in {"asset","compatibility"}: stats[kind if kind == "compatibility" else "assets"] += 1
            else: stats["notes"] += 1
            stats["entries"].append({"path":relative,"status":status,"corpus":corpus,"kind":kind,**({"reason":reason} if reason else {})})
        repo._operation(manifest, "import", f"scoped vault import: {stats['notes']} notes, {stats['assets']} assets, {stats['compatibility']} compatibility, {stats['unchanged']} unchanged", action_id=args.get("action_id"))
        manifest["operations"][-1]["changedIds"] = changed
        stats["op"] = manifest["operations"][-1]["id"]
        staged_manifest = directory / "manifest"
        manifest_text = _json(manifest)
        reserve(len(manifest_text.encode("utf-8")))
        staged_manifest.write_text(manifest_text, encoding="utf-8")
        add_write(".copal/manifest.json", staged_manifest, repo._fingerprint(staged_manifest.read_text(encoding="utf-8")))
        if writes[-1]["before"] != manifest_head:
            raise CopalBridgeError("import manifest changed during preflight")
        # Full preflight has completed. Only now may canonical files change.
        repo._fsync_directory(directory / "before")
        repo._fsync_directory(directory / "after")
        repo._fsync_directory(directory)
        from src.openclank.attachment_admission import admit_references
        admitted = admit_references(owner, "copal", [provider + ":" + identifier for provider, identifier in sorted(references)])
        journal_text = _json({"version":1,"owner":owner,"workspace":workspace,"transaction":directory.name,"writes":writes})
        reserve(len(journal_text.encode("utf-8")))
        repo._atomic_write(repo.data_dir / JOURNAL, journal_text)
        journal_written = True
        for index, item in enumerate(writes):
            if _fingerprint(repo.data_dir, item["path"]) != item["before"]:
                raise CopalBridgeError("import target changed during publication")
            _install(repo, directory / "after" / str(index), item["path"], item["after"])
        committed = True
        recover_import(repo)
        return stats
    except Exception as exc:
        if journal_written:
            try:
                state = recover_import(repo)
            except Exception as recovery_error:
                raise CopalBridgeError("Copal import recovery pending; original preimages retained") from recovery_error
            if state == "committed" or _fingerprint(repo.data_dir, writes[-1]["path"]) == writes[-1]["after"]:
                committed = True
                return stats
        elif directory.exists():
            _cleanup(repo, directory)
        if isinstance(exc, CopalBridgeError):
            raise
        raise CopalBridgeError("Copal import failed and was rolled back") from exc
    finally:
        # A crashed process retains pending byte-owner claims for the existing
        # explicit inventory recovery; never release uncertain committed bytes.
        from src.openclank.attachment_admission import settle_references
        uncertain = journal_written and (repo.data_dir / JOURNAL).exists()
        if not uncertain:
            settle_references(admitted, committed=committed)


def import_vault(repo, args):
    try:
        return _import_vault(repo, args)
    except OSError as exc:
        raise CopalBridgeError("import paths are unavailable or unsafe") from exc
