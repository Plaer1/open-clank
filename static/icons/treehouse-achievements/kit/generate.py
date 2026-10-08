#!/usr/bin/env python3
"""Rebuild original Open Clank medallions. Standard-library Python only."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
ALLOWED = {'svg', 'g', 'path', 'circle', 'ellipse', 'rect', 'polygon', 'polyline', 'line'}

def mascot_source(path):
    root = ET.fromstring(path.read_text())
    if root.tag.split('}')[-1] != 'svg' or root.get('viewBox') != '0 0 100 100':
        raise ValueError('Mascot must be a self-contained SVG with viewBox="0 0 100 100"')
    for node in root.iter():
        if node.tag.split('}')[-1] not in ALLOWED:
            raise ValueError('Mascot supports vector geometry only; outline text and embed no images')
        for key, value in node.attrib.items():
            if key.lower().startswith('on') or 'href' in key.lower() or 'url(' in value.lower() or key == 'style':
                raise ValueError('No scripts, styles, external references or URL paints in mascot')
        node.tag = node.tag.split('}')[-1]
    return ''.join(ET.tostring(node, encoding='unicode') for node in root)

STYLE = '''.plate{fill:var(--oc-art-panel,#26313f);stroke:var(--oc-art-accent,#f6be48);stroke-width:5}.inset{fill:var(--oc-art-ground,#1c2531);stroke:var(--oc-art-outline,#c89542);stroke-width:2}.line{fill:none;stroke:var(--oc-art-accent,#f6be48);stroke-width:4;stroke-linecap:round;stroke-linejoin:round}.mascot{color:var(--oc-art-mascot,#f6be48)}.oc-icon-body{fill:var(--oc-art-accent,#f6be48);stroke:var(--oc-art-ground,#1c2531);stroke-width:1}.oc-icon-detail{stroke:var(--oc-art-ground,#1c2531);stroke-width:1.5}.oc-icon-ink{stroke:var(--oc-art-accent,#f6be48);stroke-width:2}.oc-icon-dot{fill:var(--oc-art-ground,#1c2531)}.oc-icon-ink-dot{fill:var(--oc-art-accent,#f6be48)}.oc-icon-highlight{stroke:#fff;stroke-width:1;opacity:.55}.oc-icon-shade{fill:#000;opacity:.16}'''

STYLE = ''.join('.oc-achievement-art ' + rule + '}' for rule in STYLE.split('}') if rule)

def artwork(item, mascot):
    shape = item['shape']
    frames = ['<circle class="plate" cx="128" cy="119" r="101"/>', '<rect class="plate" x="27" y="18" width="202" height="202" rx="48"/>', '<path class="plate" d="m128 15 98 51v111l-98 51-98-51V66Z"/>']
    inner = '<circle class="inset" cx="128" cy="119" r="82"/>'
    ticks = ''.join(f'<path class="line" opacity=".65" transform="rotate({i*360/item["notches"]} 128 119)" d="M128 26v9"/>' for i in range(item['notches']))
    return f'''<svg class="oc-achievement-art" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256"><style>{STYLE}</style><g id="background"><path d="m66 184-13 57 31-12 21 18 14-44m28 0 14 44 21-18 31 12-13-57" fill="var(--oc-art-accent,#f6be48)" stroke="var(--oc-art-ground,#1c2531)" stroke-width="4"/>{frames[shape]}{inner}{ticks}</g><g id="mascot" class="mascot" transform="translate(61 43) scale(1.22)">{mascot}</g><g id="foreground"><path class="line" d="M81 184h59"/><circle cx="184" cy="174" r="34" class="inset"/><g transform="translate(160 150) scale(2)" fill="none" stroke="var(--oc-art-accent,#f6be48)" stroke-linecap="round" stroke-linejoin="round">{item['motif']}</g></g></svg>\n'''

def generic(concealed=False):
    mark = '<path class="line" d="M111 98c0-23 34-23 34 0 0 16-17 13-17 30m0 16v2"/>' if concealed else '<path class="line" d="m128 70 13 28 32 4-23 22 6 32-28-15-28 15 6-32-23-22 32-4Z"/>'
    return f'<svg class="oc-achievement-art" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256"><style>{STYLE}</style><g id="background"><circle class="plate" cx="128" cy="128" r="101"/><circle class="inset" cx="128" cy="128" r="82"/></g><g id="foreground">{mark}</g></svg>\n'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mascot', type=Path, default=HERE/'mascot.svg')
    parser.add_argument('--output', type=Path, required=True, help='New output folder; existing files are never replaced')
    args = parser.parse_args()
    config = json.loads((HERE/'composition.json').read_text())
    mascot = mascot_source(args.mascot)
    outputs = {f'{item["key"]}.svg': artwork(item, mascot) for item in config['icons']}
    outputs.update({'fallback.svg':generic(), 'concealed.svg':generic(True)})
    if any((args.output/name).exists() for name in outputs):
        raise ValueError('Output contains existing artwork; use a new directory')
    args.output.mkdir(parents=True, exist_ok=True)
    for name, content in outputs.items():
        (args.output/name).write_text(content)
    print(f'Exported {len(config["icons"])} artwork files plus two generic slots to {args.output}')

if __name__ == '__main__':
    main()
