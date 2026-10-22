#!/usr/bin/env python3
"""Refresh verified native payloads after a coordinated Rust-only handoff."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
import tarfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import linux_package as package
from scripts.windows_helper_artifacts import source_inputs as helper_source_inputs


def refresh(bundle, native, output):
    package.verify_bundle(bundle)
    if output.exists(): raise RuntimeError('Refresh output exists; preserve it')
    original = json.loads((bundle / 'build-provenance.json').read_text())
    before = {item['path']:item['sha256'] for item in original['source_inputs']}
    now = package.source_inputs()
    after = {item['path']:item['sha256'] for item in now}
    # Changed compiled Python or production browser assets require a refreeze.
    for name in set(before) | set(after):
        protected = name.startswith(('src/', 'core/', 'routes/', 'services/', 'static/', 'config/', 'contracts/')) or name == 'app.py'
        protected |= name in {'scripts/openclank_bootstrap.py', 'scripts/openclank_engine.py', 'scripts/linux_entry.py'}
        if protected and before.get(name) != after.get(name):
            raise RuntimeError('Runtime source changed; refreeze required: ' + name)
    output.mkdir(parents=True)
    target = output / 'openclank'
    shutil.copytree(bundle, target, symlinks=True)
    bindir = target / '_internal/bin'
    for name in package.HELPERS[:-1]: shutil.copy2(native / name, bindir / name)
    inventory = json.loads((bindir / 'helper-artifacts.json').read_text())
    for record in inventory['artifacts']:
        name = record['name']
        record.update(sha256=package.digest(bindir / name), bytes=(bindir / name).stat().st_size)
        if name in package.HELPERS[:2]: record['source_inputs'] = helper_source_inputs(ROOT, 'packages/odysseus-files')
        elif name == 'openclank-history-service': record['source_inputs'] = helper_source_inputs(ROOT, 'packages/openclank-history')
        elif name == 'fm-mcp': record['source_inputs'] = helper_source_inputs(ROOT, 'mcp_servers/frankenmemory')
        else:
            for item in record['source_inputs']: item['sha256'] = package.digest(ROOT / item['path'])
    package.write_json(bindir / 'helper-artifacts.json', inventory)
    # Qualification tooling/docs are uncompiled resources; refresh those exact
    # public inputs without altering compiled application modules.
    for name in ('qualify_linux_portable.py', 'qualify_linux_graph.py', 'linux_package_refresh.py'):
        shutil.copy2(ROOT / 'scripts' / name, target / '_internal/scripts' / name)
    shutil.copy2(ROOT / 'docs/linux-package.md', target / 'README.md')
    original['frozen_source_inputs'] = original.get('frozen_source_inputs', original['source_inputs'])
    original['source_inputs'] = now
    original['packaging_only_source_refresh'] = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    original['native_refresh'] = {'prior_manifest_sha256': package.digest(bundle / package.MANIFEST),
                                  'artifacts': [{'name':name, 'sha256':package.digest(bindir / name)} for name in package.HELPERS]}
    original['elf'] = package.elf_inventory(target)
    package.write_json(target / 'build-provenance.json', original)
    package.seal(target)
    version = package.verify_bundle(target)['version']
    archive = output / f'Open-Clank-{version}-linux-x86_64.tar.gz'
    with tarfile.open(archive, 'w:gz', compresslevel=3, dereference=False) as tar:
        tar.add(target, arcname='openclank')
    archive.with_name(archive.name + '.sha256').write_text(package.digest(archive) + '  ' + archive.name + '\n')
    package.source_archive(output / f'Open-Clank-{version}-linux-source.tar.gz')
    print(target)


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle',type=Path,required=True);p.add_argument('--native-bin',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();refresh(a.bundle.resolve(),a.native_bin.resolve(),a.output.resolve())
