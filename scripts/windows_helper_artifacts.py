#!/usr/bin/env python3
"""Inventory actual target-specific Windows helpers; never build or relabel them."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

HELPERS = {
    'odysseus-files-service.exe': ('packages/odysseus-files', 'required'),
    'odysseus-shell-thumbnail-helper.exe': ('packages/odysseus-files', 'required'),
    'openclank-history-service.exe': ('packages/openclank-history', 'required'),
    'fm-mcp.exe': ('mcp_servers/frankenmemory', 'required'),
    'openclank-windows-host-apps.exe': ('src/openclank/windows_host_apps.py', 'required'),
    'openclank-windows-desktop-capture.exe': ('src/windows_desktop_capture.py', 'required'),
}

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def source_inputs(root, relative):
    base=root/relative
    if base.is_symlink() or getattr(base.lstat(),'st_file_attributes',0)&0x400:raise ValueError('Helper source root reparse refused')
    candidates=[base] if base.is_file() else sorted(base.rglob('*'))
    files=[]
    for path in candidates:
        rel=path.relative_to(root)
        if any(part in {'target','node_modules','.git','__pycache__','cache','data'} for part in rel.parts): continue
        admitted=path.suffix in {'.rs','.toml','.lock','.proto','.py'}
        if (path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&0x400) and (admitted or path.is_dir()):
            raise ValueError('Helper source symlink/reparse input refused: '+rel.as_posix())
        if not path.is_file() or not admitted: continue
        files.append({'path':rel.as_posix(),'sha256':sha(path)})
    if relative.endswith('windows_host_apps.py'):
        for relative in ['src/openclank/host_apps.py','src/openclank/macos_host_apps.py','src/constants.py']:
            files.append({'path':relative,'sha256':sha(root/relative)})
    elif relative.endswith('windows_desktop_capture.py'):
        for relative in ['src/desktop_capture.py','requirements.txt']:
            files.append({'path':relative,'sha256':sha(root/relative)})
    if not files: raise ValueError('Helper source provenance is empty')
    return sorted(files,key=lambda item:item['path'])

RUST_NAMES = tuple(list(HELPERS)[:4])

def require_hash(path, expected):
    if not expected or len(expected) != 64 or sha(path) != expected.lower():
        raise ValueError("Reviewed manifest SHA256 mismatch")

def rust_adoption(args):
    from src.openclank.engine_build import _verify_pe_target

    if args.output.exists(): raise ValueError("Adoption manifest exists; preserve it")
    require_hash(args.source_state, args.source_state_sha256)
    state=json.loads(args.source_state.read_text(encoding="utf-8-sig"))
    rust_target='aarch64-pc-windows-msvc' if args.target=='windows-arm64' else 'x86_64-pc-windows-msvc'
    receipts=[];covered=set()
    for receipt_path in args.build_receipt:
        record=json.loads(receipt_path.read_text(encoding='utf-8-sig'))
        if record.get('stage')!='Sidecar' or record.get('status')!='stage-complete' or Path(record['sourceRoot']).resolve()!=args.source_root.resolve():
            raise ValueError('Exact successful source-root Sidecar receipt required')
        for command in record['commands']:
            argv=command['arguments']
            if command['exitCode']!=0: raise ValueError('Failed command in accepted Sidecar receipt')
            if 'build' not in argv or '--manifest-path' not in argv: continue
            if '--locked' not in argv or '--release' not in argv or argv[argv.index('--target')+1]!=rust_target:
                raise ValueError('Locked release/target receipt mismatch')
            crate=argv[argv.index('--manifest-path')+1].replace('\\','/')
            for name in RUST_NAMES:
                if crate==HELPERS[name][0]+'/Cargo.toml' and any(argv[i+1]==name[:-4] for i,x in enumerate(argv[:-1]) if x=='--bin'):
                    covered.add(name)
        receipts.append({'path':str(receipt_path),'sha256':sha(receipt_path),'runId':record['runId']})
    if covered!=set(RUST_NAMES): raise ValueError('Successful target build receipts must cover all four Rust bins')
    artifacts=[]
    for name in RUST_NAMES:
        binary=args.bin_dir/name
        if binary.is_symlink() or not binary.is_file():raise ValueError('Ordinary adopted Rust artifact required: '+name)
        _verify_pe_target(binary,args.target)
        inputs=source_inputs(args.source_root,HELPERS[name][0])
        prefix=HELPERS[name][0]+'/'
        historical=[]
        for item in state['files']:
            relative=Path(item['path'])
            if not item['path'].startswith(prefix) or relative.suffix not in {'.rs','.toml','.lock','.proto','.py'}:continue
            if any(part in {'target','node_modules','.git','__pycache__','cache','data'} for part in relative.parts):continue
            if item['kind']!='file':raise ValueError('Historical helper symlink input refused')
            historical.append({'path':item['path'],'sha256':item['sha256']})
        if sorted(historical,key=lambda item:item['path'])!=inputs:
            raise ValueError('Exact historical/current helper source set differs (including additions/deletions): '+name)
        artifacts.append({'name':name,'target':args.target,'sha256':sha(binary),'size':binary.stat().st_size,'source_inputs':inputs})
    payload={'schema_version':1,'target':args.target,'bin_directory':str(args.bin_dir.resolve()),'artifacts':artifacts,
             'accepted_source_state_sha256':args.source_state_sha256,'build_receipts':receipts,
             'qualification':'adopted-current-binary-PE/hash after historical-source and successful-build-receipt checks; original build did not record binary hash'}
    args.output.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'adoption_manifest_sha256':sha(args.output),'rust_helpers':len(artifacts)}))

def rust_reuse(args):
    from src.openclank.engine_build import _verify_pe_target

    require_hash(args.reuse_rust_manifest,args.reuse_rust_sha256)
    manifest=json.loads(args.reuse_rust_manifest.read_text(encoding='utf-8-sig'))
    if manifest.get('schema_version')!=1 or manifest.get('target')!=args.target or not manifest.get('qualification','').startswith('adopted-current-binary-PE/hash'):
        raise ValueError('Reviewed target Rust adoption manifest required')
    records=manifest['artifacts']
    if len(records)!=4 or {x['name'] for x in records}!=set(RUST_NAMES):raise ValueError('Exact four Rust helpers required')
    origin=Path(manifest['bin_directory']).resolve()
    for record in records:
        name=record['name'];binary=origin/name;destination=args.bin_dir/name
        if binary.is_symlink() or destination.exists():raise ValueError('Ordinary source and absent staged helper required')
        if record['target']!=args.target or sha(binary)!=record['sha256'] or binary.stat().st_size!=record['size']:
            raise ValueError('Adopted binary changed: '+name)
        _verify_pe_target(binary,args.target)
        if source_inputs(args.source_root,HELPERS[name][0])!=record['source_inputs']:
            raise ValueError('Adopted helper source changed: '+name)
    # All inputs checked before any copy; staged bytes are rechecked individually.
    for record in records:
        destination=args.bin_dir/record['name'];shutil.copyfile(origin/record['name'],destination)
        if sha(destination)!=record['sha256']:raise ValueError('Staged helper hash mismatch')
    shutil.copyfile(args.reuse_rust_manifest,args.bin_dir/'rust-reuse.json')
    require_hash(args.bin_dir/'rust-reuse.json',args.reuse_rust_sha256)
    args.output.write_text(json.dumps({'status':'verified-rust-artifacts-staged','target':args.target,'manifest_sha256':args.reuse_rust_sha256},indent=2)+'\n')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',required=True,choices=['windows-arm64','windows-x64'])
    p.add_argument('--bin-dir',type=Path,required=True)
    p.add_argument('--source-root',type=Path,default=ROOT)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--adopt-rust',action='store_true')
    p.add_argument('--source-state',type=Path)
    p.add_argument('--source-state-sha256')
    p.add_argument('--build-receipt',type=Path,action='append',default=[])
    p.add_argument('--reuse-rust-manifest',type=Path)
    p.add_argument('--reuse-rust-sha256')
    p.add_argument('--adoption-provenance-sha256')
    args=p.parse_args();artifacts=[]
    if args.adopt_rust:
        if not args.source_state or not args.build_receipt:raise ValueError('Historical state and build receipts required')
        rust_adoption(args);return
    if args.reuse_rust_manifest:
        rust_reuse(args);return
    from src.openclank.engine_build import _verify_pe_target

    for name,(source,requirement) in HELPERS.items():
        binary=args.bin_dir/name
        if not binary.is_file():
            if requirement=='required': raise ValueError('Required helper missing: '+name)
            continue
        _verify_pe_target(binary,args.target)
        inputs=source_inputs(args.source_root,source)
        artifacts.append({'name':name,'target':args.target,'sha256':sha(binary),'size':binary.stat().st_size,
                          'requirement':requirement,'source_inputs':inputs,
                          'source_sha256':hashlib.sha256(json.dumps(inputs,sort_keys=True,separators=(',',':')).encode()).hexdigest()})
    payload={'schema_version':1,'target':args.target,'artifacts':artifacts,
             'qualification':'PE/hash inventory only; protocol and workflow smoke results are separate'}
    if args.adoption_provenance_sha256:
        require_hash(args.bin_dir/'rust-reuse.json',args.adoption_provenance_sha256)
        payload['rust_adoption']={'path':'rust-reuse.json','sha256':args.adoption_provenance_sha256,'qualification':'adopted current binary hashes; original build receipts retained in manifest'}
    args.output.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'target':args.target,'helpers':len(artifacts),'inventory_sha256':sha(args.output)}))

if __name__=='__main__':main()
