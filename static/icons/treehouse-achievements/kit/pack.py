#!/usr/bin/env python3
"""Bundle exported PNGs into an account-local achievement artwork pack."""
import argparse
import base64
import json
from pathlib import Path
import struct

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('folder', type=Path, help='Contains opaque-key.png exports; partial packs are supported')
parser.add_argument('--name', required=True)
parser.add_argument('--output', required=True, type=Path)
args = parser.parse_args()
if not args.name.strip() or len(args.name) > 80:
    parser.error('Name must contain 1–80 characters')
if args.output.exists():
    parser.error('Choose a new output file; existing packs are never overwritten')
config = json.loads((Path(__file__).parent/'composition.json').read_text())
allowed = {item['key'] for item in config['icons']}
icons = {}
for path in sorted(args.folder.glob('*.png')):
    if path.stem not in allowed:
        parser.error(f'Unknown slot: {path.name}')
    data = path.read_bytes()
    if len(data) > 192*1024 or len(data) < 33 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR' or struct.unpack('>II', data[16:24]) != (256,256):
        parser.error(f'Expected a 256×256 PNG under 192 KiB: {path.name}')
    icons[path.stem] = 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')
if not icons or len(icons) > 37:
    parser.error('Include between 1 and 37 PNG artwork slots')
result = json.dumps({'schemaVersion':1,'kitVersion':'1','name':args.name.strip(),'icons':icons},separators=(',',':'))
if len(result) > 4*1024*1024:
    parser.error('Pack exceeds 4 MiB; export smaller PNGs')
args.output.write_text(result+'\n')
print(f'Bundled {len(icons)} slots; unspecified slots keep the default art')
