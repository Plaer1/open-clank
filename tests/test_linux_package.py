"""Focused Linux archive integrity and relocation invariants."""
import json
import os
from pathlib import Path
import struct
import tarfile
import pytest
from scripts import linux_package as package


def elf(path):
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', header, 18, 62)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header)
    path.chmod(0o755)


@pytest.fixture
def bundle(tmp_path):
    from src.constants import APP_VERSION
    root = tmp_path / 'bundle with spaces'
    for name in package.HELPERS:
        elf(root / '_internal/bin' / name)
    elf(root / 'openclank')
    elf(root / '_internal/python/bin/python3.13')
    (root / '_internal/python/bin/python3').symlink_to('python3.13')
    engine = root / '_internal/libexec/openclank/engine'
    artifact = engine / '1.0.3/linux-x64/identity'
    elf(artifact / 'openclank-engine')
    (engine / 'current.json').write_text(json.dumps({'artifact': '1.0.3/linux-x64/identity'}))
    (artifact / 'provenance.json').write_text(json.dumps({'target': package.TARGET,
        'open_clank_version': APP_VERSION, 'binary': {'name': 'openclank-engine',
        'sha256': package.digest(artifact / 'openclank-engine')}}))
    package.seal(root)
    return root


def test_safe_links_survive_archive_relocation(bundle, tmp_path):
    output = tmp_path / 'app.tar.gz'
    package.archive(bundle, output)
    extracted = tmp_path / 'relocated app'
    with tarfile.open(output) as archive:
        archive.extractall(extracted, filter='data')
    moved = extracted / 'openclank'
    assert (moved / '_internal/python/bin/python3').is_symlink()
    package.verify_bundle(moved)


@pytest.mark.parametrize('mutation', ['content', 'mode', 'missing', 'extra'])
def test_payload_changes_rejected(bundle, mutation):
    path = bundle / '_internal/bin/fm-mcp'
    if mutation == 'content': path.write_bytes(b'tampered')
    elif mutation == 'mode': path.chmod(0o644)
    elif mutation == 'missing': path.unlink()
    else: (bundle / 'unlisted').write_text('extra')
    with pytest.raises(RuntimeError): package.verify_bundle(bundle)


@pytest.mark.parametrize('target', ['/etc/passwd', '../../outside', 'missing'])
def test_unsafe_or_broken_links_rejected(tmp_path, target):
    root = tmp_path / 'bundle'; root.mkdir()
    (root / 'link').symlink_to(target)
    with pytest.raises(RuntimeError): package.entries(root)


def test_foreign_elf_rejected(tmp_path):
    path = tmp_path / 'foreign'
    elf(path)
    value = bytearray(path.read_bytes()); struct.pack_into('<H', value, 18, 183); path.write_bytes(value)
    with pytest.raises(RuntimeError): package.verify_elf(path)


def test_linux_bootstrap_reads_sealed_engine_inputs(bundle, monkeypatch):
    from scripts import openclank_bootstrap as bootstrap
    from src.openclank import engine_build
    engine = bundle / '_internal/libexec/openclank/engine'
    artifact = engine / '1.0.3/linux-x64/identity'
    provenance = json.loads((artifact / 'provenance.json').read_text())
    provenance['inputs'] = {'source_sha256': '1' * 64, 'vendor_manifest_sha256': '2' * 64}
    (artifact / 'provenance.json').write_text(json.dumps(provenance))
    package.seal(bundle)
    monkeypatch.setattr(bootstrap, 'verify_portable_payload', lambda: package.verify_bundle(bundle))
    monkeypatch.setattr(bootstrap.sys, 'platform', 'linux')
    monkeypatch.setattr(engine_build, 'default_install_root', lambda: engine)
    assert bootstrap.packaged_engine_expectations() == {
        'expected_target': 'linux-x64', 'expected_source_sha256': '1' * 64,
        'expected_vendor_manifest_sha256': '2' * 64}


def test_metadata_symlink_rejected(bundle, tmp_path):
    manifest = bundle / package.MANIFEST
    external = tmp_path / 'outside-manifest.json'
    manifest.rename(external)
    manifest.symlink_to(external)
    with pytest.raises(RuntimeError): package.verify_bundle(bundle)


@pytest.mark.parametrize('executable,arguments,accepted', [
    ('/test app/openclank', b'/test app/openclank\0__managed-server\0--port\07777\0', True),
    ('/another/openclank', b'/test app/openclank\0__managed-server\0', False),
    ('/test app/openclank', b'/test app/openclank\0engine\0', False),
    ('/test app/openclank', b'/test app/openclank other\0__managed-server\0', False),
])
def test_frozen_linux_process_identity(monkeypatch, executable, arguments, accepted):
    from src.openclank import server_manager as manager
    monkeypatch.setattr(manager.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(manager.sys, 'platform', 'linux')
    monkeypatch.setattr(manager.sys, 'executable', '/test app/openclank')
    monkeypatch.setattr(manager.LocalServerManager, '_alive', staticmethod(lambda _: True))
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        if str(path) == '/proc/4321/exe': return Path(executable)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    monkeypatch.setattr(Path, 'read_bytes', lambda _: arguments)
    assert manager.LocalServerManager._looks_like_server(4321) is accepted


def test_corresponding_source_preserves_internal_links(tmp_path, monkeypatch):
    root=tmp_path / 'source';root.mkdir()
    (root / 'asset.txt').write_text('source asset')
    (root / 'asset-link').symlink_to('asset.txt')
    monkeypatch.setattr(package, 'ROOT', root)
    monkeypatch.setattr(package, 'source_inputs', lambda: [
        {'path':'asset.txt','sha256':package.digest(root / 'asset.txt')},
        {'path':'asset-link','link':'asset.txt','sha256':'unused'}])
    archive=tmp_path / 'source.tar.gz';package.source_archive(archive)
    with tarfile.open(archive) as tar: tar.extractall(tmp_path / 'extracted',filter='data')
    link=tmp_path / 'extracted/openclank-source/asset-link'
    assert link.is_symlink() and link.read_text() == 'source asset'


def test_corresponding_source_requires_link_target_included(tmp_path, monkeypatch):
    root=tmp_path / 'source';root.mkdir()
    (root / 'asset.txt').write_text('source asset')
    (root / 'asset-link').symlink_to('asset.txt')
    monkeypatch.setattr(package, 'ROOT', root)
    monkeypatch.setattr(package, 'source_inputs', lambda: [{'path':'asset-link','link':'asset.txt','sha256':'unused'}])
    with pytest.raises(RuntimeError): package.source_archive(tmp_path / 'source.tar.gz')
