"""BQ-12: /media/profiles reports operation facts per profile and an operation-keyed catalog ('*'), and lists local Blender
profiles by operation. Reuses the golden harness; custody, dispatch and adapters are untouched."""
from types import SimpleNamespace

from tests.test_media_dispatch_golden import ALL, COMFY, FAL, Store, TRIPO, human


class BlenderStore(Store):
    def __init__(self, connections=(), **kw):
        super().__init__(**kw); self.connections = list(connections)

    def list_connections(self, owner, include_deleted=False): return self.connections


def conn(cid, label, family='blender', kind='local', enabled=True, revision=3):
    return SimpleNamespace(id=cid, label=label, family_id=family, kind=kind, enabled=enabled, revision=revision)


def test_rows_carry_operation_label_and_form_but_never_the_family():
    status, data, _ = human('/media/profiles', {'sessionID': 's1', 'operation': 'mesh.generate'}, store=Store({'tripo': TRIPO, 'fal': FAL}, ALL))
    assert status == 200 and data['profiles']
    for row in data['profiles']:
        assert row['operation_label'] == 'Generate 3D model' and 'family' not in row


def test_blender_operations_list_only_enabled_local_blender_profiles_with_form_metadata():
    store = BlenderStore([conn('b1', 'Blender A'), conn('b2', 'Off', enabled=False), conn('x', 'Other', family='comfyui'), conn('r', 'Remote', kind='remote')])
    for op, form in (('blender.render', {'output': {'noun': 'PNG', 'default_name': 'render.png'}}),
                     ('material.bake', {'resolution_choices': [256, 512, 1024, 2048], 'output': {'noun': 'ZIP', 'default_name': 'material.zip'}}),
                     ('blender.rig_pose', {'select_joint': True, 'output': {'noun': 'PNG', 'default_name': 'render.png'}})):
        status, data, _ = human('/media/profiles', {'sessionID': 's1', 'operation': op}, store=store)
        assert status == 200 and [(r['profile_id'], r['label'], r['operation']) for r in data['profiles']] == [('b1', 'Blender A', op)], data
        assert data['profiles'][0]['form'] == form and 'family' not in data['profiles'][0]
    status, data, _ = human('/media/profiles', {'sessionID': 's1', 'operation': 'blender.render'}, store=BlenderStore([]))
    assert data == {'profiles': [], 'partial': False}


def test_catalog_lists_every_operation_with_profiles_or_a_reason():
    store = BlenderStore([conn('b1', 'Blender A'), conn('b2', 'Blender B')], lists={'comfy': COMFY, 'tripo': TRIPO}, caps=ALL)
    status, data, _ = human('/media/profiles', {'sessionID': 's1', 'operation': '*'}, store=store)
    assert status == 200
    by = {o['operation']: o for o in data['operations']}
    assert set(by) == {'image.generate', 'mesh.generate', 'animation.generate', 'material.bake', 'blender.render', 'blender.four_headings', 'blender.rig_pose'}
    assert len(by['blender.render']['profiles']) == 2 and 'reason' not in by['blender.render']
    assert by['animation.generate']['profiles'] == [] and by['animation.generate']['reason'] and by['animation.generate']['label'] == 'Animate sprite'
    assert all('family' not in p for o in data['operations'] for p in o['profiles'])
