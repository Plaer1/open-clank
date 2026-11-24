"""Read public, source-qualified marketplace entries for the existing Skills draft importer."""

from __future__ import annotations

import hashlib
import io
import re
import stat
import zipfile
from urllib.parse import quote, urlencode

from .marketplace_importer import _encoded, _json, draft_name, safe_path
from .marketplace_source import _drained, github_reader
from .skill_importer import _get_checked

_ZCODE = "https://cdn-zcode.z.ai/zcode/official-plugin/"
_CLAW = "https://clawhub.ai"
_HERMES = "https://hermes-agent.nousresearch.com/docs/api/skills-index.json"
_HOSTS = frozenset({"cdn-zcode.z.ai", "clawhub.ai", "hermes-agent.nousresearch.com"})
_LABELS = {"zcode": "ZCode", "clawhub": "ClawHub", "hermes": "Hermes Skills Hub"}
_ARCHIVE_LIMIT = 16 * 1024 * 1024
_ENTRY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_SHA = re.compile(r"[a-f0-9]{64}")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _label(value, limit=160):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


async def _fetch(url, current, *, max_bytes, accept="application/json"):
    if not await current():
        raise ValueError("Marketplace authority changed")
    response = await _drained(lambda: _get_checked(
        url, headers={"Accept": accept}, timeout=5, max_bytes=max_bytes,
        allowed_hosts=_HOSTS, require_https=True,
    ))
    if response.status_code != 200 or not await current():
        raise ValueError("Marketplace response unavailable")
    return bytes(response.content)


async def _object(url, current, *, max_bytes=1024 * 1024):
    from json import loads

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Marketplace returned ambiguous fields")
            result[key] = value
        return result

    value = loads(await _fetch(url, current, max_bytes=max_bytes), object_pairs_hook=unique_pairs)
    if type(value) is not dict:
        raise ValueError("Marketplace catalog unavailable")
    return value


def _claw_url(path, **params):
    query = urlencode({key: value for key, value in params.items() if value})
    return _CLAW + path + ("?" + query if query else "")


def _claw_entry(row):
    if type(row) is not dict:
        return None
    slug = row.get("slug")
    owner = _label(row.get("ownerHandle") or (row.get("publisher") or {}).get("handle"))
    install = row.get("install")
    if ((isinstance(install, dict) and install.get("kind") != "clawhub")
            or (row.get("source") and row["source"] != "clawhub")):
        return None
    if not isinstance(slug, str) or not _ENTRY.fullmatch(slug) or owner and not _ENTRY.fullmatch(owner):
        return None
    name = f"{owner}/{slug}" if owner else slug
    if len(name) > 58:
        return None
    return {"name": name, "slug": slug, "owner": owner, "publisher": owner or None,
            "title": _label(row.get("displayName")) or slug, "actual": "clawhub",
            "version": _label(row.get("version")) or None}


async def _catalog(registry, query, current):
    if registry == "zcode":
        catalog = await _object(_ZCODE + "marketplace.json", current)
        if catalog.get("name") != "zcode-plugins-official":
            raise ValueError("ZCode marketplace identity changed")
        rows = []
        for row in catalog.get("plugins", []):
            name, version = row.get("name"), row.get("version")
            source, artifact = row.get("source"), row.get("_artifact")
            if not (isinstance(name, str) and _ENTRY.fullmatch(name) and len(name) <= 58
                    and isinstance(version, str) and _ENTRY.fullmatch(version)
                    and "skill" in (row.get("componentTypes") or [])
                    and isinstance(source, dict) and isinstance(artifact, dict)
                    and source.get("type") == "zip" and source.get("source") == "url"):
                continue
            path = f"plugins/{name}/{version}/plugin.zip"
            digest = source.get("sha256")
            if (source.get("url") != _ZCODE + path or artifact.get("path") != path
                    or not isinstance(digest, str) or not _SHA.fullmatch(digest)
                    or digest != artifact.get("sha256") or source.get("path") != name):
                continue
            rows.append({"name": name, "actual": "zcode", "version": version, "sha256": digest,
                         "title": _label(row.get("displayName")) or name,
                         "publisher": _label((row.get("author") or {}).get("name")) or None,
                         "publisher_type": _label(row.get("publisherType")),
                         "components": [_label(value, 48) for value in row.get("componentTypes", [])],
                         "url": _ZCODE + path})
    elif registry == "clawhub":
        endpoint = _claw_url("/api/v1/search", q=query, limit=40) if query else _claw_url("/api/v1/trending", kind="skills", limit=40)
        catalog = await _object(endpoint, current)
        rows = [entry for row in catalog.get("results" if query else "items", [])
                if (entry := _claw_entry(row)) is not None]
    else:
        catalog = await _object(_HERMES, current, max_bytes=40 * 1024 * 1024)
        rows = []
        for row in catalog.get("skills", []):
            identifier = _label(row.get("identifier"), 512)
            title = _label(row.get("name"))
            if not identifier or query and query.casefold() not in f"{title} {identifier}".casefold():
                continue
            if row.get("source") == "clawhub" and identifier.startswith("clawhub/"):
                slug = identifier.removeprefix("clawhub/")
                owner, slash, leaf = slug.removeprefix("@").partition("/")
                entry = _claw_entry({"slug": leaf if slash else slug, "ownerHandle": owner if slash else None,
                                     "displayName": title, "source": "clawhub"})
                if entry:
                    rows.append(entry)
                continue
            repo, directory = row.get("repo"), row.get("path")
            if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                continue
            try:
                directory = safe_path(directory)
            except ValueError:
                continue
            stem = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32] or "skill"
            name = f"{stem}-{_digest(_encoded([repo, directory]))[:12]}"
            rows.append({"name": name, "actual": "github", "title": title or directory,
                         "publisher": repo.split("/")[0], "repo": repo, "directory": directory,
                         "trust": _label(row.get("trust_level"), 48), "version": None})
    if registry == "zcode" and query:
        rows = [row for row in rows if query.casefold() in f"{row['name']} {row['title']}".casefold()]
    unique = {}
    for row in rows:
        key = row["name"]
        if key in unique and unique[key] != row:
            unique[key] = None
        else:
            unique[key] = row
    return [row for row in unique.values() if row is not None][:64]


def _archive_files(data):
    archive = zipfile.ZipFile(io.BytesIO(data))
    try:
        entries = archive.infolist()
        if len(entries) > 4096:
            raise ValueError("Marketplace archive has too many entries")
        known = set()
        files = {}
        for entry in entries:
            path = safe_path(entry.filename.rstrip("/"))
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if path.casefold() in known or kind not in {0, stat.S_IFREG, stat.S_IFDIR} or entry.flag_bits & 1:
                raise ValueError("Marketplace archive contains unsupported members")
            known.add(path.casefold())
            if not entry.is_dir():
                files[path] = entry
        return archive, files
    except BaseException:
        archive.close()
        raise


def _zcode_components(archive, members, name, version):
    manifest = {}
    for path in (f"{name}/.zcode-plugin/plugin.json", f"{name}/.claude-plugin/plugin.json"):
        member = members.get(path)
        if member:
            if member.file_size > 100000:
                raise ValueError("ZCode plugin manifest exceeds its bounds")
            with archive.open(member) as stream:
                manifest = _json(stream.read(100001))
            break
    if manifest.get("name", name) != name or manifest.get("version", version) != version:
        raise ValueError("ZCode plugin manifest identity changed")
    declared = manifest.get("skills", ["skills"])
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, list) or len(declared) > 32:
        raise ValueError("ZCode skill components are unavailable")
    roots = set()
    for value in declared:
        if not isinstance(value, str):
            raise ValueError("ZCode skill component path is unavailable")
        relative = value.removeprefix("./").removesuffix("/SKILL.md")
        directory = name if relative in {"", ".", "SKILL.md"} else f"{name}/{safe_path(relative)}"
        if f"{directory}/SKILL.md" in members:
            roots.add(directory)
        for path in members:
            if not path.startswith(f"{directory}/") or not path.endswith("/SKILL.md"):
                continue
            parent = path.rsplit("/", 1)[0]
            if parent.count("/") == directory.count("/") + 1:
                roots.add(parent)
    if not roots and "skills" not in manifest and f"{name}/SKILL.md" in members:
        roots.add(name)
    return sorted(roots), _label(manifest.get("license"), 256) or None


def _bundle_file(path, raw, files):
    if len(raw) > 400000 or len(files) >= 64 or len(path.split("/")) > 5:
        raise ValueError("Marketplace skill exceeds its review bounds")
    text = raw.decode("utf-8")
    if "\x00" in text:
        raise ValueError("Marketplace skill contains a binary companion")
    files[path] = text


def _bundle_zip(archive, members, root):
    files = {}
    prefix = f"{root}/" if root else ""
    total = 0
    for filename, entry in members.items():
        if not filename.startswith(prefix):
            continue
        path = filename[len(prefix):]
        total += entry.file_size
        if total > 2_000_000 or entry.file_size > 400000:
            raise ValueError("Marketplace skill exceeds its review bounds")
        with archive.open(entry) as stream:
            _bundle_file(path, stream.read(400001), files)
    return files


async def _bundle_github(reader):
    files = {}
    total = 0

    async def visit(directory, level):
        nonlocal total
        if level > 4:
            raise ValueError("Marketplace skill exceeds its review bounds")
        for row in await reader.list(directory):
            path = safe_path("/".join(filter(None, (directory, row["name"]))))
            if row["kind"] == "directory":
                await visit(path, level + 1)
                continue
            raw = await reader.read(path)
            total += len(raw)
            if total > 2_000_000:
                raise ValueError("Marketplace skill exceeds its review bounds")
            _bundle_file(path, raw, files)

    await visit("", 0)
    await reader.finish()
    return files


def _claw_review(security):
    if not isinstance(security, dict):
        return {"status": "unavailable", "findings": [], "risk": None}
    status = _label(security.get("status"), 48).lower()
    findings = []
    for finding in (security.get("findings") or [])[:24]:
        if isinstance(finding, dict):
            text = _label(finding.get("title") or finding.get("message") or finding.get("summary"), 240)
            if text:
                findings.append({"severity": _label(finding.get("severity"), 48), "finding": text})
        elif isinstance(finding, str):
            findings.append({"severity": "", "finding": _label(finding, 240)})
    scanners = security.get("scanners")
    llm = scanners.get("llm") if isinstance(scanners, dict) else None
    summary = _label(llm.get("summary"), 400) if isinstance(llm, dict) else ""
    return {
        "status": status or "unavailable", "findings": findings,
        "risk": _label(security.get("riskLevel") or security.get("risk"), 64) or None,
        "has_warnings": security.get("hasWarnings") if type(security.get("hasWarnings")) is bool else None,
        "has_scan_result": security.get("hasScanResult") if type(security.get("hasScanResult")) is bool else None,
        "scanner_summary": summary or None,
    }


async def inspect_registry(source, action, plugin_name, skill_path, current):
    expected = {"kind", "registry", "query"}
    if set(source) not in (expected, expected | {"pin"}) or source.get("registry") not in _LABELS:
        raise ValueError("Marketplace selector unavailable")
    registry, query = source["registry"], source["query"]
    if not isinstance(query, str) or len(query) > 100 or any(ord(char) < 32 for char in query):
        raise ValueError("Marketplace query unavailable")
    selector = {"kind": "registry", "registry": registry, "query": query}
    rows = await _catalog(registry, query.strip(), current)
    if action == "catalog":
        return selector, {"version": 1, "name": _LABELS[registry], "plugins": [
            {"name": row["name"], "title": row["title"], "publisher": row["publisher"],
             "version": row["version"], "registry": _LABELS[row["actual"]] if row["actual"] != "github" else "GitHub",
             "review_status": "Review required", "supported": True}
            for row in rows]}, None
    selected = next((row for row in rows if row["name"] == plugin_name), None)
    if selected is None:
        raise ValueError("Marketplace listing changed")
    actual = selected["actual"]
    report = {"status": "unavailable", "findings": [], "risk": None}
    unsupported = []
    files = None
    license_value = None
    if actual == "zcode":
        version = selected["version"]
        artifact = await _fetch(selected["url"], current, max_bytes=_ARCHIVE_LIMIT, accept="application/zip")
        if _digest(artifact) != selected["sha256"]:
            raise ValueError("ZCode artifact digest changed")
        pin = {"registry": "zcode", "publisher": selected["publisher"], "version": version,
               "artifact_sha256": selected["sha256"]}
        unsupported = [value for value in selected["components"] if value != "skill"]
        archive, members = _archive_files(artifact)
        with archive:
            roots, license_value = _zcode_components(archive, members, selected["name"], version)
            if action in {"preview", "import"} and skill_path in roots:
                files = _bundle_zip(archive, members, skill_path)
        publisher_type = selected["publisher_type"]
        report = {**report, "publisher_type": publisher_type}
    elif actual == "clawhub":
        slug, owner = selected["slug"], selected["owner"]
        path = "/api/v1/skills/" + quote(slug, safe="")
        detail = await _object(_claw_url(path, ownerHandle=owner), current)
        actual_owner = _label((detail.get("owner") or {}).get("handle"))
        if (detail.get("skill") or {}).get("slug") != slug or not _ENTRY.fullmatch(actual_owner) or owner and owner != actual_owner:
            raise ValueError("ClawHub skill identity changed")
        earlier = source.get("pin")
        version = earlier.get("version") if isinstance(earlier, dict) else (detail.get("latestVersion") or {}).get("version")
        if not isinstance(version, str) or not _ENTRY.fullmatch(version):
            raise ValueError("ClawHub release unavailable")
        release = await _object(_claw_url(f"{path}/versions/{quote(version, safe='')}", ownerHandle=actual_owner), current)
        release_value = release.get("version") or {}
        if release_value.get("version") != version:
            raise ValueError("ClawHub release identity changed")
        report = _claw_review(release_value.get("security"))
        if report["status"] in {"malicious", "blocked", "denied"}:
            return selector, {"version": 1, "plugin_name": plugin_name, "skills": [],
                              "review": report, "blocked": True}, None
        artifact = await _fetch(_claw_url("/api/v1/download", slug=slug, ownerHandle=actual_owner, version=version),
                                current, max_bytes=_ARCHIVE_LIMIT, accept="application/zip")
        pin = {"registry": "clawhub", "publisher": actual_owner, "slug": slug, "version": version,
               "artifact_sha256": _digest(artifact)}
        archive, members = _archive_files(artifact)
        with archive:
            roots = [""] if "SKILL.md" in members else []
            if action in {"preview", "import"} and skill_path in roots:
                files = _bundle_zip(archive, members, "")
    else:
        prior = source.get("pin")
        ref = prior.get("url") if isinstance(prior, dict) else None
        base = f"https://github.com/{selected['repo']}/tree/"
        directory = quote(selected["directory"], safe="/")
        if ref is not None and (not isinstance(ref, str) or
                not re.fullmatch(re.escape(base) + r"[a-f0-9]{40}/" + re.escape(directory), ref)):
            raise ValueError("GitHub skill source changed")
        reader = await github_reader(ref or f"{base}HEAD/{directory}", current=current)
        version = reader.source.ref
        pin = {"registry": "github", "publisher": selected["publisher"], "version": version,
               "url": reader.identity["url"]}
        roots = [""] if any(row == {"name": "SKILL.md", "kind": "file"} for row in await reader.list("")) else []
        report = {**report, "hermes_trust": selected.get("trust") or "unavailable"}
        if action in {"preview", "import"} and skill_path in roots:
            files = await _bundle_github(reader)
    if source.get("pin") is not None and source["pin"] != pin:
        raise ValueError("Marketplace release changed. Review the new release first.")
    selector["pin"] = pin
    evidence = {"publisher": pin["publisher"], "registry": pin["registry"], "discovery": registry,
                "version": version, "review": report}
    if action == "components":
        return selector, {"version": 1, "plugin_name": plugin_name, "skills": roots,
                          "unsupported": unsupported, **evidence}, None
    if not files or "SKILL.md" not in files:
        raise ValueError("Marketplace skill unavailable")
    descriptors = [{"path": path, "size": len(files[path].encode()), "sha256": _digest(files[path].encode())}
                   for path in sorted(files)]
    facts = {"version": 1, "catalog": {"source": evidence, "manifest_sha256": _digest(_encoded(evidence))},
             "plugin": {"name": plugin_name, "source": pin, "manifest_sha256": pin.get("artifact_sha256")},
             "skill_path": skill_path, "name": draft_name(plugin_name, skill_path),
             "category": "imported", "license": license_value, "plugin_version": version,
             "unsupported": unsupported, "files": descriptors, "bundle_sha256": _digest(_encoded(descriptors))}
    facts["review_sha256"] = _digest(_encoded(facts))
    return selector, facts, files
