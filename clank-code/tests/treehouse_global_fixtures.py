"""Disposable four-root packages, independent of publication's in-flight files."""
import copy
import json
import shutil
from pathlib import Path

from src.openclank.treehouse_global_catalogue import canonical_digest


def four_root_package(path):
    source = Path(__file__).resolve().parents[1] / 'src/openclank/treehouse_curriculum'
    shutil.copytree(source, path)
    manifest = json.loads((path / 'manifest.json').read_text())
    if len(manifest['mainQuests']) == 3:
        ai = manifest['mainQuests']['treehouse:ai']
        oc = copy.deepcopy(ai)
        oc.update(id='treehouse:openclank', title='Open Clank', members=[])
        keep = []
        for member in ai['members']:
            cid = member['courseId']
            if cid.startswith('ai:openclank-'):
                manifest['subjects'][cid]['mainQuestId'] = oc['id']
                oc['members'].append(member)
            else:
                keep.append(member)
        ai['members'] = keep
        manifest['mainQuests'][oc['id']] = oc
        # The fixture tests transport/progress, not authored bridge pedagogy.
        anchors = [uid for member in oc['members'] for uid in manifest['subjects'][member['courseId']]['unitIds']]
        existing = {(e['fromId'], e['toId'], e['type']) for e in manifest['topicLinks']}
        manifest['topicLinks'] += [{'fromId': a, 'toId': b, 'type': 'related', 'reason': 'Disposable fixture bridge'} for a, b in zip(anchors, anchors[1:]) if (a, b, 'related') not in existing]
    manifest['packageRevision'] = 'fixture:four-root'
    manifest['packageDigest'] = canonical_digest({k: v for k, v in manifest.items() if k != 'packageDigest'})
    (path / 'manifest.json').write_text(json.dumps(manifest))
    return path
