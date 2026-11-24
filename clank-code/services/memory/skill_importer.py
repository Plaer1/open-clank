"""Import SKILL.md bundles from public GitHub (or skills.sh → GitHub) URLs."""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import ssl
from dataclasses import dataclass, replace
from typing import Collection, Dict, Iterable, Iterator, List, Optional, Tuple, cast
from urllib.parse import quote, urljoin, urlparse

import httpcore
import httpx

from src.url_safety import check_outbound_url

logger = logging.getLogger(__name__)

MAX_FILES = 64
MAX_TOTAL_BYTES = 2_000_000
MAX_FILE_BYTES = 400_000
MAX_LANDING_BYTES = 400_000
MAX_METADATA_BYTES = 100_000
MAX_DEPTH = 4
ALLOWED_SUFFIXES = (
    ".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".toml",
    ".js", ".ts", ".css", ".html", ".xml", ".csv",
)
TEXT_NAMES = {"skill.md", "license", "license.md", "readme.md"}
_GITHUB_HOSTS = frozenset({
    "github.com", "www.github.com", "api.github.com", "raw.githubusercontent.com",
})
_SKILLS_SH_HOSTS = frozenset({"skills.sh", "www.skills.sh"})
_GITHUB_SOURCE_HOSTS = frozenset({
    "github.com", "www.github.com", "raw.githubusercontent.com",
})
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_GITHUB_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _github_host(url: str) -> str:
    return (urlparse(str(url)).hostname or "").lower()


def _assert_github_url(url: str, *, context: str = "URL") -> None:
    _assert_fetch_origin(
        url,
        allowed_hosts=_GITHUB_HOSTS,
        require_https=True,
        context=context,
    )


@dataclass
class ResolvedSource:
    owner: str
    repo: str
    ref: str
    path: str  # directory or file path inside repo (no leading slash)
    kind: str = "directory"

    @property
    def canonical_uri(self) -> str:
        sha = _require_pinned_ref(self)
        view = "blob" if self.kind == "file" else "tree"
        base = (
            f"https://github.com/{quote(self.owner, safe='')}/"
            f"{quote(self.repo, safe='')}/{view}/{sha}"
        )
        return f"{base}/{quote(self.path, safe='/')}" if self.path else base


class SkillImportError(ValueError):
    pass


class SkillPathNotFound(SkillImportError):
    pass


def _require_pinned_ref(src: ResolvedSource) -> str:
    ref = str(src.ref or "").strip()
    if not _COMMIT_SHA.fullmatch(ref):
        raise SkillImportError("GitHub source is not pinned to an immutable commit")
    return ref.lower()


def _safe_relpath(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip()
    if (
        "\x00" in rel
        or re.match(r"^[A-Za-z]:", rel)
        or rel.startswith(("/", "//"))
    ):
        raise SkillImportError(f"unsafe path: {rel!r}")
    if not rel or rel.startswith("..") or "/../" in f"/{rel}/":
        raise SkillImportError(f"unsafe path: {rel!r}")
    parts = [p for p in rel.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise SkillImportError(f"unsafe path: {rel!r}")
    return "/".join(parts)


def _is_text_file(name: str) -> bool:
    low = name.lower()
    if low in TEXT_NAMES:
        return True
    return any(low.endswith(s) for s in ALLOWED_SUFFIXES)


# Max redirect hops to follow manually while re-validating each one.
_MAX_FETCH_REDIRECTS = 5


class _PinnedBackend(httpcore.NetworkBackend):
    """Connect to one validated IP while httpcore keeps the URL host for TLS."""

    def __init__(self, ip: str):
        self._ip = ip
        self._real = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        return self._real.connect_tcp(
            self._ip, port, timeout, local_address, socket_options
        )

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return self._real.connect_unix_socket(path, timeout, socket_options)

    def sleep(self, seconds: float) -> None:
        return self._real.sleep(seconds)


_HTTPCORE_TRANSPORT_ERRORS = (
    httpcore.NetworkError,
    httpcore.ProtocolError,
    httpcore.ProxyError,
    httpcore.TimeoutException,
    httpcore.UnsupportedProtocol,
)


class _PinnedResponseStream(httpx.SyncByteStream):
    def __init__(self, stream: Iterable[bytes]):
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._stream
        except _HTTPCORE_TRANSPORT_ERRORS as exc:
            raise httpx.TransportError(str(exc)) from exc

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()


class _PinnedTransport(httpx.BaseTransport):
    """Pin TCP to a validated address without changing Host or TLS SNI."""

    def __init__(self, ip: str):
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=_PinnedBackend(ip),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            response = self._pool.handle_request(core_request)
        except _HTTPCORE_TRANSPORT_ERRORS as exc:
            raise httpx.TransportError(str(exc), request=request) from exc
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_PinnedResponseStream(cast(Iterable[bytes], response.stream)),
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._pool.close()


def _resolve_host_ips(host: str) -> List[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SkillImportError(f"host does not resolve: {exc}") from exc
    return [str(info[4][0]).split("%", 1)[0] for info in infos]


def _assert_fetch_origin(
    url: str,
    *,
    allowed_hosts: Collection[str],
    require_https: bool,
    context: str,
) -> None:
    parsed = urlparse(str(url))
    host = (parsed.hostname or "").lower()
    if parsed.username is not None or parsed.password is not None:
        raise SkillImportError(f"{context} must not contain credentials")
    if require_https and parsed.scheme.lower() != "https":
        raise SkillImportError(f"{context} must use HTTPS")
    if host not in allowed_hosts:
        raise SkillImportError(
            f"{context} must stay on an allowed origin (got {host or 'unknown host'})"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise SkillImportError(f"{context} has an invalid port") from exc
    if port not in (None, 443):
        raise SkillImportError(f"{context} must use the standard HTTPS origin")


def _validate_user_github_url(url: str) -> None:
    parsed = urlparse(url)
    _assert_fetch_origin(
        url,
        allowed_hosts=_GITHUB_SOURCE_HOSTS,
        require_https=True,
        context="GitHub URL",
    )
    if parsed.query:
        raise SkillImportError("GitHub URL must not contain a query string")
    if parsed.fragment:
        raise SkillImportError("GitHub URL must not contain a fragment")


def _validate_user_skills_url(url: str) -> None:
    parsed = urlparse(url)
    _assert_fetch_origin(
        url,
        allowed_hosts=_SKILLS_SH_HOSTS,
        require_https=True,
        context="skills.sh URL",
    )
    if parsed.query:
        raise SkillImportError("skills.sh URL must not contain a query string")
    if parsed.fragment:
        raise SkillImportError("skills.sh URL must not contain a fragment")


def _check_fetch_url(url: str) -> Tuple[str, ...]:
    """Validate one fetch hop and return the exact public addresses it resolved.

    Skill bundles only ever come from public GitHub, never an internal
    address, so block private/loopback/link-local targets on every hop —
    matching the hardened web-fetch path in
    ``services/search/content.py:_get_public_url`` rather than the lenient
    default used for admin-configured model endpoints.
    """
    host = urlparse(url).hostname or ""
    raw_ips = _resolve_host_ips(host)
    ok, reason = check_outbound_url(
        url,
        block_private=True,
        resolver=lambda _host: raw_ips,
    )
    if not ok:
        raise SkillImportError(reason)
    parsed_ips = []
    for raw in raw_ips:
        try:
            parsed_ips.append(str(ipaddress.ip_address(raw)))
        except ValueError:
            continue
    validated = tuple(dict.fromkeys(parsed_ips))
    if not validated:
        raise SkillImportError("host does not resolve to an IP")
    return validated


def _get_checked(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: float = 30.0,
    max_bytes: Optional[int] = None,
    allowed_hosts: Optional[Collection[str]] = None,
    require_https: bool = False,
) -> httpx.Response:
    """GET with a validated, DNS-pinned connection for every redirect hop.

    ``httpx``'s ``follow_redirects=True`` validates only the initial URL, so a
    ``3xx`` to an internal address (``169.254.169.254``, ``127.0.0.1``, …) would
    still be connected to before any post-hoc host check. Each hop resolves
    once, validates that answer, and pins TCP to it while retaining the original
    hostname for the Host header, certificate verification, and TLS SNI.
    """
    current = url
    request_headers = dict(headers or {})
    request_headers["Accept-Encoding"] = "identity"
    for _ in range(_MAX_FETCH_REDIRECTS + 1):
        if allowed_hosts is not None:
            _assert_fetch_origin(
                current,
                allowed_hosts=allowed_hosts,
                require_https=require_https,
                context="fetch URL",
            )
        validated_ips = _check_fetch_url(current)
        with httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            transport=_PinnedTransport(validated_ips[0]),
        ) as client:
            if max_bytes is not None and hasattr(client, "stream"):
                with client.stream("GET", current, headers=request_headers) as streamed:
                    response_headers = streamed.headers
                    declared = response_headers.get("content-length")
                    try:
                        declared_size = int(declared) if declared is not None else None
                    except (TypeError, ValueError):
                        declared_size = None
                    if declared_size is not None and declared_size > max_bytes:
                        raise SkillImportError(f"response too large: {current}")

                    body = bytearray()
                    if streamed.status_code not in (301, 302, 303, 307, 308):
                        encoding = (
                            response_headers.get("content-encoding") or ""
                        ).strip().lower()
                        if encoding and encoding != "identity":
                            raise SkillImportError(
                                f"compressed response cannot be safely bounded: {current}"
                            )
                        for chunk in streamed.iter_bytes():
                            body.extend(chunk)
                            if len(body) > max_bytes:
                                raise SkillImportError(f"response too large: {current}")
                    r = httpx.Response(
                        streamed.status_code,
                        headers=response_headers,
                        content=bytes(body),
                        request=httpx.Request("GET", str(streamed.url)),
                    )
            else:
                # Test doubles and old httpx-compatible clients may only expose
                # get(); production httpx streams above so oversized bodies are
                # rejected before they are buffered in full.
                r = client.get(current, headers=request_headers)
            encoding = (
                (getattr(r, "headers", {}) or {}).get("content-encoding") or ""
            ).strip().lower()
            if (
                r.status_code not in (301, 302, 303, 307, 308)
                and encoding
                and encoding != "identity"
            ):
                raise SkillImportError(
                    f"compressed response cannot be safely bounded: {current}"
                )
            if max_bytes is not None:
                declared = (getattr(r, "headers", {}) or {}).get("content-length")
                try:
                    declared_size = int(declared) if declared is not None else None
                except (TypeError, ValueError):
                    declared_size = None
                if declared_size is not None and declared_size > max_bytes:
                    raise SkillImportError(f"response too large: {current}")
                response_body = getattr(r, "content", None)
                if response_body is not None and len(response_body) > max_bytes:
                    raise SkillImportError(f"response too large: {current}")
            if r.status_code in (301, 302, 303, 307, 308):
                location = r.headers.get("location")
                if not location:
                    return r
                current = urljoin(str(r.url), location)
                continue
            return r
    raise SkillImportError("too many redirects while fetching skill bundle")


def parse_skill_source(url: str) -> ResolvedSource:
    """Normalize skills.sh / GitHub web URLs into owner/repo/ref/path."""
    raw = (url or "").strip()
    if not raw:
        raise SkillImportError("URL is required")

    # skills.sh often links to GitHub; unwrap only the exact trusted host.
    source_url = urlparse(raw)
    source_host = (source_url.hostname or "").lower()
    if source_host in _SKILLS_SH_HOSTS:
        _validate_user_skills_url(raw)
        r = _get_checked(
            raw,
            timeout=20.0,
            max_bytes=MAX_LANDING_BYTES,
            allowed_hosts=_SKILLS_SH_HOSTS | _GITHUB_SOURCE_HOSTS,
            require_https=True,
        )
        if r.status_code >= 400:
            raise _github_response_error(r)
        final = str(r.url)
        final_host = _github_host(final)
        # Page may embed a GitHub link; prefer the final URL if redirected.
        if final_host in _GITHUB_HOSTS:
            raw = final
        elif final_host in _SKILLS_SH_HOSTS:
            m = re.search(r"https?://(?:www\.)?github\.com/[^\s\"')]+", r.text or "")
            if m:
                raw = m.group(0).rstrip(".,)")
            else:
                raise SkillImportError("skills.sh page did not link to a GitHub skill")
        else:
            raise SkillImportError(
                f"skills.sh redirect must stay on skills.sh or GitHub (got {final_host or 'unknown host'})"
            )

    _validate_user_github_url(raw)
    parsed = urlparse(raw)
    host = _github_host(raw)

    if host == "raw.githubusercontent.com":
        # /owner/repo/ref/path/to/file
        bits = [p for p in parsed.path.split("/") if p]
        if len(bits) < 4:
            raise SkillImportError("Invalid raw GitHub URL")
        owner, repo, ref = bits[0], bits[1], bits[2]
        path = "/".join(bits[3:])
        kind = "file" if path.lower().endswith("skill.md") else "directory"
        source = ResolvedSource(
            owner=owner,
            repo=repo,
            ref=ref,
            path=path,
            kind=kind,
        )
        _validate_source(source)
        return source

    bits = [p for p in parsed.path.split("/") if p]
    if len(bits) < 2:
        raise SkillImportError("Invalid GitHub URL")
    owner, repo = bits[0], bits[1]
    ref = "main"
    path = ""

    if len(bits) >= 4 and bits[2] in ("tree", "blob"):
        ref = bits[3]
        path = "/".join(bits[4:])
        kind = "file" if bits[2] == "blob" else "directory"
    elif len(bits) == 2:
        path = ""
        kind = "directory"
    else:
        raise SkillImportError("GitHub URL must include /tree/<branch>/... or /blob/<branch>/...")

    source = ResolvedSource(
        owner=owner,
        repo=repo,
        ref=ref,
        path=path,
        kind=kind,
    )
    _validate_source(source)
    return source


def _validate_source(src: ResolvedSource) -> None:
    if not _GITHUB_SLUG.fullmatch(src.owner or ""):
        raise SkillImportError("invalid GitHub owner")
    if not _GITHUB_SLUG.fullmatch(src.repo or ""):
        raise SkillImportError("invalid GitHub repository")
    ref = str(src.ref or "")
    if (
        not ref
        or len(ref) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in ref)
    ):
        raise SkillImportError("invalid GitHub revision")
    if src.path:
        _safe_relpath(src.path)


def _raw_url(src: ResolvedSource, rel_path: str) -> str:
    rel = _safe_relpath(rel_path)
    sha = _require_pinned_ref(src)
    return f"https://raw.githubusercontent.com/{src.owner}/{src.repo}/{sha}/{quote(rel, safe='/')}"


def _api_contents_url(src: ResolvedSource, rel_path: str = "") -> str:
    rel = _safe_relpath(rel_path) if rel_path else ""
    sha = _require_pinned_ref(src)
    base = f"https://api.github.com/repos/{src.owner}/{src.repo}/contents"
    if rel:
        base += f"/{quote(rel, safe='/')}"
    return f"{base}?ref={sha}"


def _api_commit_url(src: ResolvedSource) -> str:
    return (
        f"https://api.github.com/repos/{quote(src.owner, safe='')}/"
        f"{quote(src.repo, safe='')}/commits/{quote(src.ref, safe='')}"
    )


def _github_response_error(response: httpx.Response) -> SkillImportError:
    """Turn a failed GitHub HTTP response into a user-visible import error."""
    status = response.status_code
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("message") or "").strip()
    except Exception:
        detail = (response.text or "").strip()[:200]

    low = detail.lower()
    if status == 403 and "rate limit" in low:
        return SkillImportError(
            "GitHub API rate limit exceeded — try again in a bit"
            + (f" ({detail})" if detail else "")
        )
    if status == 404:
        return SkillImportError("path not found on GitHub")
    if detail:
        return SkillImportError(f"GitHub request failed ({status}): {detail}")
    return SkillImportError(f"GitHub request failed ({status})")


def _fetch_bytes(
    url: str,
    *,
    max_bytes: int = MAX_FILE_BYTES,
    headers: Optional[dict] = None,
) -> bytes:
    r = _get_checked(
        url,
        headers=headers or {"Accept": "application/vnd.github+json"},
        timeout=30.0,
        max_bytes=max_bytes,
        allowed_hosts=_GITHUB_HOSTS,
        require_https=True,
    )
    if r.status_code >= 400:
        raise _github_response_error(r)
    _assert_github_url(str(r.url), context="redirect target")
    if len(r.content) > max_bytes:
        raise SkillImportError(f"response too large: {url}")
    return r.content


def _fetch_text(url: str) -> str:
    data = _fetch_bytes(url)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SkillImportError(f"non-text file: {url}") from e


def _resolve_pinned_source(src: ResolvedSource) -> ResolvedSource:
    _validate_source(src)
    data = _fetch_bytes(
        _api_commit_url(src),
        max_bytes=MAX_METADATA_BYTES,
        headers={"Accept": "application/vnd.github.sha"},
    )
    text = data.decode("utf-8", errors="strict").strip()
    sha = text if _COMMIT_SHA.fullmatch(text) else ""
    if not sha:
        try:
            payload = httpx.Response(
                200,
                content=data,
                headers={"content-type": "application/json"},
            ).json()
        except Exception as exc:
            raise SkillImportError("GitHub returned an invalid commit revision") from exc
        if isinstance(payload, dict):
            sha = str(payload.get("sha") or "").strip()
    if not _COMMIT_SHA.fullmatch(sha):
        raise SkillImportError("GitHub returned an invalid commit revision")
    return replace(src, ref=sha.lower())


def _list_github_dir(
    src: ResolvedSource,
    rel_dir: str,
    out: Dict[str, str],
    *,
    depth: int = 0,
    base_dir: Optional[str] = None,
    total_bytes: int = 0,
) -> int:
    if depth > MAX_DEPTH:
        raise SkillImportError("skill bundle exceeds directory depth limit")
    base_dir = rel_dir if base_dir is None else base_dir
    url = _api_contents_url(src, rel_dir)
    r = _get_checked(
        url,
        headers={"Accept": "application/vnd.github+json"},
        timeout=30.0,
        max_bytes=MAX_METADATA_BYTES,
        allowed_hosts=_GITHUB_HOSTS,
        require_https=True,
    )
    if r.status_code >= 400:
        raise _github_response_error(r)
    _assert_github_url(str(r.url), context="redirect target")
    entries = r.json()
    if not isinstance(entries, list):
        raise SkillImportError("expected a directory on GitHub")
    if any(not isinstance(entry, dict) for entry in entries):
        raise SkillImportError("GitHub returned an invalid directory listing")
    entries = sorted(entries, key=lambda entry: str(entry.get("name") or ""))
    for ent in entries:
        if not isinstance(ent, dict):
            raise SkillImportError("GitHub returned an invalid directory listing")
        name = str(ent.get("name") or "")
        if (
            not name
            or name in (".", "..")
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            raise SkillImportError("GitHub returned an unsafe directory entry")
        ent_type = ent.get("type")
        rel = _safe_relpath(f"{rel_dir}/{name}" if rel_dir else name)
        if ent_type == "dir":
            if depth >= MAX_DEPTH:
                raise SkillImportError("skill bundle exceeds directory depth limit")
            total_bytes = _list_github_dir(
                src,
                rel,
                out,
                depth=depth + 1,
                base_dir=base_dir,
                total_bytes=total_bytes,
            )
            continue
        if ent_type != "file":
            raise SkillImportError("GitHub returned an unsupported directory entry")
        if not _is_text_file(name):
            continue
        if len(out) >= MAX_FILES:
            raise SkillImportError("skill bundle exceeds file count limit")
        key = rel
        if base_dir:
            prefix = f"{base_dir.rstrip('/')}/"
            if not rel.startswith(prefix):
                raise SkillImportError("GitHub listing escaped the skill directory")
            key = rel[len(prefix):]
        if key in out:
            raise SkillImportError("GitHub returned a duplicate skill file")
        text = _fetch_text(_raw_url(src, rel))
        total_bytes += len(text.encode("utf-8"))
        if total_bytes > MAX_TOTAL_BYTES:
            raise SkillImportError("skill bundle exceeds size limit")
        out[key] = text
    return total_bytes


def fetch_skill_bundle(url: str) -> Tuple[Dict[str, str], ResolvedSource]:
    """Download SKILL.md and sibling text assets. Returns relative_path → content."""
    src = _resolve_pinned_source(parse_skill_source(url))
    files: Dict[str, str] = {}

    path = _safe_relpath(src.path) if src.path else ""
    if src.kind == "file":
        if not path.lower().endswith("skill.md"):
            raise SkillImportError("GitHub blob URL must point to SKILL.md")
        parent = "/".join(path.split("/")[:-1])
        _list_github_dir(src, parent, files, base_dir=parent)
    else:
        _list_github_dir(src, path, files, base_dir=path)

    if "SKILL.md" not in files:
        raise SkillImportError(
            "No SKILL.md found — link to a skill folder or SKILL.md on GitHub"
        )
    return files, src


def pick_skill_md(files: Dict[str, str]) -> Tuple[str, str]:
    if "SKILL.md" in files:
        return "SKILL.md", files["SKILL.md"]
    for rel, content in files.items():
        if rel.lower().endswith("skill.md"):
            return rel, content
    raise SkillImportError("bundle has no SKILL.md")


def default_category_from_source(src: ResolvedSource) -> str:
    return "imported"
