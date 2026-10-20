"""Skill URL importer — GitHub path parsing."""
import json
import textwrap

import httpx
import pytest

from services.memory import skill_importer
from services.memory.skills import SkillsManager
from services.memory.skill_importer import (
    MAX_DEPTH,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_LANDING_BYTES,
    MAX_TOTAL_BYTES,
    ResolvedSource,
    SkillImportError,
    _assert_github_url,
    _fetch_bytes,
    _list_github_dir,
    _safe_relpath,
    fetch_skill_bundle,
    parse_skill_source,
)

PINNED = "a" * 40


@pytest.fixture(autouse=True)
def _hermetic_importer_dns(monkeypatch):
    monkeypatch.setattr(
        skill_importer,
        "_resolve_host_ips",
        lambda _host: ["1.1.1.1"],
    )


def test_parse_github_blob_skill_md():
    src = parse_skill_source(
        "https://github.com/anthropics/skills/blob/main/skills/pdf/SKILL.md"
    )
    assert src.owner == "anthropics"
    assert src.repo == "skills"
    assert src.ref == "main"
    assert src.path.endswith("skills/pdf/SKILL.md")
    assert src.kind == "file"


def test_parse_github_tree_directory():
    src = parse_skill_source(
        "https://github.com/example/my-skills/tree/develop/caveman-skill"
    )
    assert src.owner == "example"
    assert src.repo == "my-skills"
    assert src.ref == "develop"
    assert src.path == "caveman-skill"
    assert src.kind == "directory"


def test_parse_raw_github():
    src = parse_skill_source(
        "https://raw.githubusercontent.com/o/r/main/path/SKILL.md"
    )
    assert src.owner == "o"
    assert src.repo == "r"
    assert src.ref == "main"
    assert src.path == "path/SKILL.md"


def test_rejects_non_github():
    with pytest.raises(SkillImportError):
        parse_skill_source("https://example.com/skill.md")


def test_rejects_untrusted_host_with_skills_sh_in_path():
    with pytest.raises(SkillImportError, match="allowed origin"):
        parse_skill_source("https://evil.example/skills.sh")


def test_skills_sh_landing_page_body_is_capped(monkeypatch):
    class _Resp:
        url = "https://skills.sh/example"
        status_code = 200
        headers = {}
        content = b"x" * (MAX_LANDING_BYTES + 1)
        text = content.decode()

    _mock_httpx_client(monkeypatch, _Resp())
    with pytest.raises(SkillImportError, match="response too large"):
        parse_skill_source("https://skills.sh/example")


def test_skills_sh_landing_page_stream_stops_at_body_cap(monkeypatch):
    class _Stream:
        url = "https://skills.sh/example"
        status_code = 200
        headers = {}

        def iter_bytes(self):
            yield b"x" * MAX_LANDING_BYTES
            yield b"x"

    class _Context:
        def __enter__(self):
            return _Stream()

        def __exit__(self, *args):
            return False

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, headers=None):
            return _Context()

        def get(self, url, headers=None):
            raise AssertionError("bounded production path must stream")

    monkeypatch.setattr("services.memory.skill_importer.httpx.Client", _Client)
    monkeypatch.setattr(
        "services.memory.skill_importer.check_outbound_url",
        lambda url, **kwargs: (True, ""),
    )
    with pytest.raises(SkillImportError, match="response too large"):
        parse_skill_source("https://skills.sh/example")


def test_streaming_fetch_rejects_compressed_body(monkeypatch):
    class _Stream:
        url = "https://skills.sh/example"
        status_code = 200
        headers = {"content-encoding": "gzip"}

        def iter_bytes(self):
            raise AssertionError("compressed response must be rejected before decoding")

    class _Context:
        def __enter__(self):
            return _Stream()

        def __exit__(self, *args):
            return False

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, headers=None):
            assert headers["Accept-Encoding"] == "identity"
            return _Context()

    monkeypatch.setattr("services.memory.skill_importer.httpx.Client", _Client)
    monkeypatch.setattr(
        "services.memory.skill_importer.check_outbound_url",
        lambda url, **kwargs: (True, ""),
    )
    with pytest.raises(SkillImportError, match="compressed response"):
        parse_skill_source("https://skills.sh/example")


def test_skills_sh_landing_page_can_link_to_github(monkeypatch):
    class _Resp:
        url = "https://skills.sh/example"
        status_code = 200
        headers = {}
        content = b'<a href="https://github.com/o/r/tree/main/example">skill</a>'
        text = content.decode()

    _mock_httpx_client(monkeypatch, _Resp())
    src = parse_skill_source("https://skills.sh/example")
    assert (src.owner, src.repo, src.ref, src.path) == ("o", "r", "main", "example")


def test_fetch_bytes_rejects_cross_host_redirect(monkeypatch):
    class _Resp:
        url = "https://evil.example/secret"
        status_code = 200
        content = b"x"

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr("services.memory.skill_importer.httpx.Client", _Client)
    monkeypatch.setattr(
        "services.memory.skill_importer.check_outbound_url",
        lambda url, **kwargs: (True, ""),
    )
    with pytest.raises(SkillImportError, match="redirect target"):
        _fetch_bytes("https://raw.githubusercontent.com/o/r/main/SKILL.md")


def test_assert_github_url_allows_api_host():
    _assert_github_url(
        f"https://api.github.com/repos/o/r/contents?ref={PINNED}",
        context="redirect target",
    )


def test_list_github_dir_accepts_api_github_response(monkeypatch):
    monkeypatch.setattr(
        "services.memory.skill_importer._fetch_text",
        lambda url: "# skill\n",
    )
    monkeypatch.setattr(
        "services.memory.skill_importer.check_outbound_url",
        lambda url, **kwargs: (True, ""),
    )

    class _Resp:
        url = f"https://api.github.com/repos/o/r/contents?ref={PINNED}"
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [{
                "name": "SKILL.md",
                "type": "file",
                "download_url": "https://raw.githubusercontent.com/o/r/main/SKILL.md",
            }]

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr("services.memory.skill_importer.httpx.Client", _Client)

    out = {}
    src = ResolvedSource(owner="o", repo="r", ref=PINNED, path="")
    _list_github_dir(src, "", out)
    assert "SKILL.md" in out


def _mock_httpx_client(monkeypatch, response):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            return response

    monkeypatch.setattr("services.memory.skill_importer.httpx.Client", _Client)
    monkeypatch.setattr(
        "services.memory.skill_importer.check_outbound_url",
        lambda url, **kwargs: (True, ""),
    )


def test_list_github_dir_surfaces_rate_limit(monkeypatch):
    class _Resp:
        url = f"https://api.github.com/repos/o/r/contents?ref={PINNED}"
        status_code = 403

        def json(self):
            return {"message": "API rate limit exceeded for 203.0.113.1"}

    _mock_httpx_client(monkeypatch, _Resp())
    src = ResolvedSource(owner="o", repo="r", ref=PINNED, path="")
    with pytest.raises(SkillImportError, match="rate limit"):
        _list_github_dir(src, "", {})


def test_fetch_bytes_surfaces_github_error_detail(monkeypatch):
    class _Resp:
        url = f"https://raw.githubusercontent.com/o/r/{PINNED}/SKILL.md"
        status_code = 403
        content = b""

        def json(self):
            return {"message": "Forbidden"}

    _mock_httpx_client(monkeypatch, _Resp())
    with pytest.raises(SkillImportError, match="GitHub request failed \\(403\\): Forbidden"):
        _fetch_bytes(f"https://raw.githubusercontent.com/o/r/{PINNED}/SKILL.md")


@pytest.mark.parametrize(
    "url, message",
    [
        ("http://github.com/o/r/tree/main/skill", "HTTPS"),
        ("https://user@github.com/o/r/tree/main/skill", "credentials"),
        ("https://github.com/o/r/tree/main/skill?token=secret", "query"),
        ("https://github.com/o/r/tree/main/skill#readme", "fragment"),
    ],
)
def test_user_github_url_rejects_credential_and_url_smuggling(url, message):
    with pytest.raises(SkillImportError, match=message):
        parse_skill_source(url)


@pytest.mark.parametrize("path", ["C:relative/SKILL.md", "C:/root/SKILL.md", r"C:\root\SKILL.md"])
def test_safe_relpath_rejects_windows_drive_paths(path):
    with pytest.raises(SkillImportError, match="unsafe path"):
        _safe_relpath(path)


def test_fetch_bundle_pins_ref_before_listing_and_uses_only_pinned_file_urls(monkeypatch):
    calls = []
    markdown = b"""---
name: pinned
description: pinned import
status: published
---

# Procedure
- verify
"""

    def fake_get(url, **kwargs):
        calls.append(url)
        if "/commits/main" in url:
            body = PINNED.encode()
        elif "/contents/pinned" in url:
            body = json.dumps([
                {"name": "SKILL.md", "type": "file"},
            ]).encode()
        elif f"/{PINNED}/pinned/SKILL.md" in url:
            body = markdown
        else:
            raise AssertionError(f"unexpected fetch: {url}")
        return httpx.Response(
            200,
            content=body,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(skill_importer, "_get_checked", fake_get)
    files, source = fetch_skill_bundle(
        "https://github.com/o/r/tree/main/pinned"
    )

    assert calls[0].endswith("/commits/main")
    assert source.ref == PINNED
    assert source.canonical_uri == f"https://github.com/o/r/tree/{PINNED}/pinned"
    assert files == {"SKILL.md": markdown.decode()}
    assert all("/main/" not in url and "ref=main" not in url for url in calls[1:])
    assert f"ref={PINNED}" in calls[1]
    assert f"/{PINNED}/" in calls[2]


def _listing_response(url, entries):
    return httpx.Response(
        200,
        content=json.dumps(entries).encode(),
        request=httpx.Request("GET", url),
    )


def test_listing_caps_accept_exact_boundary_and_reject_first_excess(monkeypatch):
    monkeypatch.setattr(skill_importer, "MAX_FILES", 2)
    monkeypatch.setattr(skill_importer, "MAX_TOTAL_BYTES", 2)
    monkeypatch.setattr(skill_importer, "_fetch_text", lambda url: "x")
    source = ResolvedSource("o", "r", PINNED, "", "directory")

    entries = [
        {"name": "SKILL.md", "type": "file"},
        {"name": "README.md", "type": "file"},
    ]
    monkeypatch.setattr(
        skill_importer,
        "_get_checked",
        lambda url, **kwargs: _listing_response(url, entries),
    )
    files = {}
    assert _list_github_dir(source, "", files) == 2
    assert set(files) == {"README.md", "SKILL.md"}

    entries.append({"name": "extra.txt", "type": "file"})
    with pytest.raises(SkillImportError, match="file count"):
        _list_github_dir(source, "", {})

    monkeypatch.setattr(skill_importer, "MAX_FILES", 3)
    with pytest.raises(SkillImportError, match="size limit"):
        _list_github_dir(source, "", {})


def test_individual_file_cap_accepts_exact_boundary_and_rejects_first_excess(monkeypatch):
    size = 7
    monkeypatch.setattr(skill_importer, "MAX_FILE_BYTES", size)

    def response_with(body):
        return lambda url, **kwargs: httpx.Response(
            200,
            content=body,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(skill_importer, "_get_checked", response_with(b"x" * size))
    assert len(_fetch_bytes(
        f"https://raw.githubusercontent.com/o/r/{PINNED}/SKILL.md",
        max_bytes=size,
    )) == size

    monkeypatch.setattr(skill_importer, "_get_checked", response_with(b"x" * (size + 1)))
    with pytest.raises(SkillImportError, match="response too large"):
        _fetch_bytes(
            f"https://raw.githubusercontent.com/o/r/{PINNED}/SKILL.md",
            max_bytes=size,
        )


def test_listing_depth_boundary_fails_closed(monkeypatch):
    monkeypatch.setattr(skill_importer, "MAX_DEPTH", 1)
    monkeypatch.setattr(skill_importer, "_fetch_text", lambda url: "ok")
    source = ResolvedSource("o", "r", PINNED, "", "directory")

    def exact(url, **kwargs):
        entries = (
            [{"name": "SKILL.md", "type": "file"}]
            if "/contents/a?" in url
            else [{"name": "a", "type": "dir"}]
        )
        return _listing_response(url, entries)

    monkeypatch.setattr(skill_importer, "_get_checked", exact)
    assert _list_github_dir(source, "", {}) == 2

    def too_deep(url, **kwargs):
        entries = (
            [{"name": "b", "type": "dir"}]
            if "/contents/a?" in url
            else [{"name": "a", "type": "dir"}]
        )
        return _listing_response(url, entries)

    monkeypatch.setattr(skill_importer, "_get_checked", too_deep)
    with pytest.raises(SkillImportError, match="depth"):
        _list_github_dir(source, "", {})


def test_directory_listing_error_is_not_swallowed_by_file_fallback(monkeypatch):
    monkeypatch.setattr(
        skill_importer,
        "_resolve_pinned_source",
        lambda source: ResolvedSource(
            source.owner,
            source.repo,
            PINNED,
            source.path,
            source.kind,
        ),
    )
    monkeypatch.setattr(
        skill_importer,
        "_list_github_dir",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SkillImportError("listing failed")
        ),
    )
    fetched = []
    monkeypatch.setattr(
        skill_importer,
        "_fetch_text",
        lambda url: fetched.append(url) or "unexpected",
    )

    with pytest.raises(SkillImportError, match="listing failed"):
        fetch_skill_bundle("https://github.com/o/r/tree/main/pinned")
    assert fetched == []


def _importable_skill(name="imported"):
    return textwrap.dedent(f"""\
        ---
        name: {name}
        description: imported
        status: published
        ---

        # Procedure
        - verify
        """)


@pytest.mark.parametrize(
    "reserved",
    [
        "_lifecycle.json",
        "_revisions/forged.md",
        ".lifecycle.lock",
    ],
)
def test_bundle_import_rejects_remote_lifecycle_control_files(tmp_path, reserved):
    manager = SkillsManager(str(tmp_path))
    with pytest.raises(SkillImportError, match="reserved path"):
        manager.import_bundle_from_files(
            {
                "SKILL.md": _importable_skill("reserved-import"),
                reserved: "{}",
            },
            owner="alice",
        )

    assert manager.load(owner="alice") == []
    assert list(tmp_path.glob(".skill-import-*")) == []


def test_bundle_import_rejects_normalized_duplicate_paths(tmp_path):
    manager = SkillsManager(str(tmp_path))
    with pytest.raises(SkillImportError, match="duplicate paths"):
        manager.import_bundle_from_files(
            {
                "SKILL.md": _importable_skill("duplicate-import"),
                "references/guide.txt": "first",
                "references/./GUIDE.txt": "second",
            },
            owner="alice",
        )

    assert manager.load(owner="alice") == []


def test_bundle_import_rolls_back_when_directory_sync_fails(tmp_path, monkeypatch):
    import core.atomic_io as atomic_io

    manager = SkillsManager(str(tmp_path))
    original = atomic_io._fsync_directory
    calls = 0

    def fail_once(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected directory sync failure")
        return original(path)

    monkeypatch.setattr(atomic_io, "_fsync_directory", fail_once)
    with pytest.raises(OSError, match="injected directory sync failure"):
        manager.import_bundle_from_files(
            {"SKILL.md": _importable_skill("sync-failure")},
            owner="alice",
        )

    assert manager.load(owner="alice") == []
    assert not (tmp_path / "skills" / "imported" / "sync-failure").exists()
    assert list(tmp_path.glob(".skill-import-*")) == []
