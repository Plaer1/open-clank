"""Golden tests (B4-P2a): pin TODAY's media DISPATCH decisions (which vendor
service / adapter / profile lister is chosen, or which refusal results) for the
human path (routes/files_facade_routes.py) and the agent path
(src/openclank/agent_media.py), so the B4-P2 registry refactor can be proven
behaviour-identical.

Expected values are literals recorded from a run against UNMODIFIED code, then
frozen.  Rows marked `# was FINDING n` changed in B4-P2b: the old (violating) selection is named in the comment; they now pin strict dispatch ("the profile's own
adapter id decides, nothing else"; they are pinned as-is, not fixed (see
.clanker/robonotes/claude-last-lap-2026-11-09/B4-P2a-media-dispatch-golden.md).
To change a row: change product code first, then update the literal deliberately.

Seam: the REAL route handlers run under a FastAPI TestClient with fake
auth/session/store state; every vendor *HumanService class is replaced by a
recording fake (the narrowest boundary: the factories still run, only the class
they end in is faked).  The agent path calls the real AgentMediaService with fake
services; per-`_submit_*` rows replace the vendor submit method with a recorder.
Plain asserts, no fixtures.  No network, no vendor modules do real work.
"""
import asyncio
import hashlib
import importlib
import json
import os
import tempfile
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import routes.files_facade_routes as R
from src.openclank.agent_media import AgentMediaService
from src.openclank.file_policy import FilePolicyRepository

TRACE = []
OWNER, ACCT = 'alice', 'acct-alice'


# ==========================================================================
# FAKES
# ==========================================================================
class Auth:
    users = {OWNER: {'account_id': ACCT, 'is_admin': False}}

    def account_id(self, u): return ACCT if u == OWNER else None
    def username_for_account_id(self, a): return OWNER if a == ACCT else None
    def is_admin(self, u): return False


def _row(prefix):
    return {'connection_id': prefix + 'c', 'account_id': prefix + 'a', 'label': prefix.upper() + ' label',
            'connection_revision': 'cr', 'account_revision': 'ar', 'credential_version': 1}


TRIPO, FAL, VOID = [_row('t')], [_row('f')], [_row('v')]          # account ids ta / fa / va
COMFY = [{'connection_id': 'cc', 'label': 'COMFY label', 'revision': 'cr'}]
LOCAL = [{'connection_id': 'lc', 'label': 'LOCAL label', 'revision': 'cr'}]   # trellis / hunyuan local rows


def _binding(connection_id, account_id=None, interface='i'):
    return {'connectionID': connection_id, 'connectionRevision': 'cr', 'providerAccountID': account_id, 'providerAccountRevision': 'ar',
            'credentialVersion': 1, 'interface': interface, 'ownerAccountID': ACCT}


def _rev(binding):   # same formula as routes _mesh_profile_revision (fixture helper, not logic under test)
    keys = ('connectionID', 'connectionRevision', 'providerAccountID', 'providerAccountRevision', 'credentialVersion', 'interface')
    return hashlib.sha256(json.dumps([binding[k] for k in keys], separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


class Store:
    """Fake provider_store.  `lists` maps tripo/fal/voidless/comfy/meshy/trellis/hunyuan -> rows; `caps` = job_credential methods present."""
    def __init__(self, lists=None, caps=('tripo', 'fal', 'voidless', 'meshy'), selectors=None):
        self.lists, self.selectors = lists or {}, selectors or {}
        for c in caps: setattr(self, c + '_job_credential', lambda *a, **k: None)

    def _l(self, key, name):
        TRACE.append('list:' + name)
        return {'profiles': list(self.lists.get(key, [])), 'partial': False}

    def list_human_tripo_profiles(self, owner): return self._l('tripo', 'tripo')
    def list_human_fal_profiles(self, owner): return self._l('fal', 'fal')
    def list_human_voidless_profiles(self, owner): return self._l('voidless', 'voidless')
    def list_agent_comfy_profiles(self, owner): return self._l('comfy', 'comfy')
    def list_human_meshy_profiles(self, owner): return self._l('meshy', 'meshy')
    def list_agent_trellis_profiles(self, owner): return self._l('trellis', 'trellis')
    def list_agent_hunyuan_profiles(self, owner): return self._l('hunyuan', 'hunyuan')

    def resolve_trellis_media_selector(self, owner, selector):
        TRACE.append('resolve:' + selector); return self.selectors.get(selector, (False, None))
    resolve_hunyuan_media_selector = resolve_trellis_media_selector

    def get_account(self, owner, account_id):
        for rows in self.lists.values():
            for r in rows:
                if r.get('account_id') == account_id: return SimpleNamespace(connection_id=r['connection_id'])
        raise KeyError(account_id)

    def get_connection(self, owner, connection_id): return SimpleNamespace(revision='cr')


# vendor human-service classes replaced by recorders (module, class) -> label
SERVICES = {('src.openclank.tripo_human', 'TripoHumanService'): 'tripo', ('src.openclank.fal_human', 'FalHumanService'): 'fal',
            ('src.openclank.fal_hunyuan_human', 'FalImageToMeshHumanService'): 'fal-mesh',
            ('src.openclank.sprixen_human', 'SprixenHumanService'): 'sprixen',
            ('src.openclank.sprixen_animation_human', 'SprixenAnimationHumanService'): 'sprixen-animation',
            ('src.openclank.meshy_human', 'MeshyHumanService'): 'meshy', ('src.openclank.meshy_rig_human', 'MeshyRigHumanService'): 'meshy-rig',
            ('src.openclank.material_pack_human', 'MaterialPackHumanService'): 'material-pack',
            ('src.openclank.trellis_human', 'TrellisHumanService'): 'trellis', ('src.openclank.hunyuan_human', 'HunyuanHumanService'): 'hunyuan',
            ('src.openclank.media_human', 'HumanMediaService'): None}        # None: comfy or voidless, told apart by coordinator.adapters


class HumanFake:
    def __init__(self, label, kw):
        self.coordinator, self.scope = kw.get('coordinator'), kw.get('scope')
        self.profile_selection_real, self.barrier = kw.get('profile_selection'), kw.get('barrier')
        self.recovery_context_for, self.artifact_store = kw.get('recovery_context_for'), kw.get('artifact_store')
        if label is None:   # HumanMediaService is shared by comfy and voidless; the adapters map says which factory ran
            label = 'comfy' if 'comfyui' in self.coordinator.adapters else 'voidless' if 'voidless-image-v1' in self.coordinator.adapters else '?'
        self.label = label
        TRACE.append('build:' + label)

    async def _bounded_read(self, aw): return await aw

    def profile_selection(self, owner, account, connection_id, provider_account_id=None):
        async def go():
            TRACE.append('select:' + self.label)
            return _binding(connection_id, provider_account_id)
        return go()

    def _prepared(self, method, ident, **kw):
        TRACE.append('prepare:%s.%s(%s)' % (self.label, method, ident))
        return {'profile': _binding(kw.get('connection_id'), kw.get('provider_account_id')), 'jobID': 'job-' + self.label,
                'revision': 1, 'source': 'src-ref'}

    async def prepare(self, **kw): return self._prepared('prepare', kw['connection_id'], **kw)
    async def prepare_text3d(self, **kw): return self._prepared('prepare_text3d', kw['provider_account_id'], **kw)
    async def prepare_text_image(self, **kw): return self._prepared('prepare_text_image', kw['provider_account_id'], **kw)
    async def prepare_voidless(self, **kw): return self._prepared('prepare_voidless', kw['provider_account_id'], **kw)
    async def prepare_image_mesh(self, **kw): return self._prepared('prepare_image_mesh', kw['provider_account_id'], **kw)
    async def prepare_sprite(self, **kw): return self._prepared('prepare_sprite', kw['provider_account_id'], **kw)
    async def prepare_preview(self, **kw): return self._prepared('prepare_preview', kw['provider_account_id'], **kw)
    async def prepare_refine(self, **kw): return self._prepared('prepare_refine', kw['source_job_id'], **kw)

    def _job(self, account, owner, job_id):
        binding = _binding('fc', 'fa')
        return {'status': 'prepared', 'revision': 1, 'identity': {
            'account_id': ACCT, 'owner': OWNER, 'session_id': 's1', 'workspace_id': 'ws1', 'policy_generation': REPO.generation(),
            'operation': 'mesh.generate', 'adapter_id': 'fal-hunyuan-image3d-v1', 'adapter_version': '1', 'model_revision': 'fal-ai/hunyuan3d/v2',
            'resource_binding': {**binding, 'image_source': 'src-ref'}}}

    async def run(self, **kw):
        TRACE.append('run:' + self.label); return {'ok': True}


REPO = FilePolicyRepository(os.path.join(tempfile.mkdtemp(), 'policy.db'))
REPO.sync_subjects(Auth.users)
ROWS_CHECKED = []


def _check(rid, actual, expected):
    ROWS_CHECKED.append(rid)
    if os.environ.get('GOLDEN_DUMP'):    # recording mode: print the actual value for every row instead of asserting
        print('@@%s@@ %s %r' % (rid, 'SAME' if actual == expected else 'DIFF', actual)); return
    assert actual == expected, (rid, actual)


def human(path, body, *, store=None, job=None):
    """Call the REAL route; return (status, detail-or-view, trace)."""
    del TRACE[:]
    saved = {}
    for (mod, name), label in SERVICES.items():       # import every module first: some subclass HumanMediaService
        importlib.import_module(mod)
    for (mod, name), label in SERVICES.items():
        m = importlib.import_module(mod); saved[(mod, name)] = getattr(m, name)
        setattr(m, name, (lambda label: lambda **kw: HumanFake(label, kw))(label))
    from src import bg_jobs
    saved_get = bg_jobs.get_media
    bg_jobs.get_media = lambda job_id, account_id=None: job
    try:
        app = FastAPI(); st = app.state
        st.auth_manager, st.account_request_barrier, st.provider_store = Auth(), SimpleNamespace(), store or Store()
        st.session_manager = SimpleNamespace(get_session=lambda sid: SimpleNamespace(owner=OWNER, workspace_id='ws1', incognito=False))
        st.media_artifact_store, st.treehouse_repository = object(), object()
        st.capture_original_credential = lambda request, owner, account: (lambda: True)

        @app.middleware('http')
        async def authenticate(request: Request, call_next):
            request.state.current_user, request.state.authenticated = OWNER, True
            return await call_next(request)
        app.include_router(R.setup_files_facade_routes(application_state=st, policy_repository=REPO, session_factory=lambda: None))
        with TestClient(app) as client:
            resp = client.post('/api/files-v1' + path, json=body)
        data = resp.json()
        return resp.status_code, data.get('detail') if resp.status_code != 200 else data, list(TRACE)
    finally:
        for (mod, name), orig in saved.items(): setattr(importlib.import_module(mod), name, orig)
        bg_jobs.get_media = saved_get


def _profile_view(data):
    return [(p['profile_id'], tuple(p['input_kinds']), p['label']) for p in data['profiles']], data['partial']


# ==========================================================================
# HUMAN PATH 1: POST /media/profiles  (inventory row 8)
#   (id, operation, store lists, store caps, unused, expected (status, view, trace))
# ==========================================================================
ALL = ('tripo', 'fal', 'voidless', 'meshy')
MANY_TRIPO = [{**_row('t'), 'account_id': 'ta%02d' % i, 'connection_id': 'tc%02d' % i} for i in range(20)]
PROFILE_ROWS = [
    ('h_pr_mesh_tripo_then_fal', 'mesh.generate', {'tripo': TRIPO, 'fal': FAL}, ALL, True, (200, ([('ta', ('text_prompt',), 'T label'), ('fa', ('image',), 'F label')], False), ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal', 'build:fal-mesh', 'select:fal-mesh'])),
    # tripo lister is skipped when the store lacks tripo_job_credential (factory not installed); fal is still listed
    ('h_pr_mesh_no_tripo_cap', 'mesh.generate', {'tripo': TRIPO, 'fal': FAL}, ('fal', 'voidless', 'meshy'), True, (200, ([('fa', ('image',), 'F label')], False), ['list:fal', 'build:fal-mesh', 'select:fal-mesh'])),
    # (B4-P2b) no fal_job_credential -> fal-mesh service cannot be built -> fal skipped, partial=true, tripo rows kept
    ('h_pr_mesh_no_fal_cap', 'mesh.generate', {'tripo': TRIPO, 'fal': FAL}, ('tripo', 'voidless', 'meshy'), True, (200, ([('ta', ('text_prompt',), 'T label')], True), ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal'])),    # was FINDING 9: 409 for every vendor
    # image.generate: comfy, fal, voidless in that order; voidless label gets the hard-coded suffix; fal/voidless say text_prompt
    ('h_pr_image_comfy_fal_voidless', 'image.generate', {'comfy': COMFY, 'fal': FAL, 'voidless': VOID}, ALL, True, (200, ([('cc', ('workflow_recipe',), 'COMFY label'), ('fa', ('text_prompt',), 'F label'), ('va', ('text_prompt',), 'V label · 64px PNG')], False), ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless'])),
    ('h_pr_image_nothing_configured', 'image.generate', {}, ALL, True, (200, ([], False), ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy'])),
    # (B4-P2b) image.generate listing works again: `auth` was an undefined name in _human_media_profiles
    ('h_pr_image_NAMEERROR_auth', 'image.generate', {'comfy': COMFY, 'fal': FAL, 'voidless': VOID}, ALL, False, (200, ([('cc', ('workflow_recipe',), 'COMFY label'), ('fa', ('text_prompt',), 'F label'), ('va', ('text_prompt',), 'V label · 64px PNG')], False), ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless'])),    # was FINDING 1: 409 Media profiles unavailable or changed (NameError auth)
    # (B4-P2b) voidless job credential missing -> voidless skipped, partial=true, comfy and fal rows kept
    ('h_pr_image_no_voidless_cap', 'image.generate', {'comfy': COMFY, 'fal': FAL}, ('tripo', 'fal', 'meshy'), True, (200, ([('cc', ('workflow_recipe',), 'COMFY label'), ('fa', ('text_prompt',), 'F label')],False), ['list:fal', 'build:fal', 'list:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal'])),    # was FINDING 9: 409 for every vendor    # was partial=True: no voidless rows for this owner -> skipped silently (B4-P2b follow-up)
    # operations the agent path lists but the human neutral route does not: empty, no lister called, nothing built
    ('h_pr_audio_generate', 'audio.generate', {'comfy': COMFY}, ALL, True, (200, ([], False), [])),
    ('h_pr_material_generate', 'material.generate', {'comfy': COMFY}, ALL, True, (200, ([], False), [])),
    ('h_pr_mesh_rig', 'mesh.rig', {'meshy': [_row('m')]}, ALL, True, (200, ([], False), [])),
    ('h_pr_unknown_operation', 'bogus.op', {'fal': FAL}, ALL, True, (200, ([], False), [])),
    ('h_pr_image_no_fal_cap', 'image.generate', {'comfy': COMFY, 'fal': FAL, 'voidless': VOID}, ('tripo', 'voidless', 'meshy'), True, (200, ([('cc', ('workflow_recipe',), 'COMFY label'), ('va', ('text_prompt',), 'V label · 64px PNG')], True), ['list:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:voidless'])),
    ('h_pr_image_no_voidless_cap_with_rows', 'image.generate', {'comfy': COMFY, 'fal': FAL, 'voidless': VOID}, ('tripo', 'fal', 'meshy'), True, (200, ([('cc', ('workflow_recipe',), 'COMFY label'), ('fa', ('text_prompt',), 'F label')], True), ['list:fal', 'build:fal', 'list:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal'])),
    ('h_pr_mesh_no_fal_cap_no_rows', 'mesh.generate', {'tripo': TRIPO}, ('tripo', 'voidless', 'meshy'), True, (200, ([('ta', ('text_prompt',), 'T label')], False), ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal'])),
]


def test_human_profile_listing_rows():
    for rid, op, lists, caps, _unused, expected in PROFILE_ROWS:    # 5th column: formerly auth_global, unused since B4-P2b fixed FINDING 1
        status, data, trace = human('/media/profiles', {'sessionID': 's1', 'operation': op}, store=Store(lists, caps))
        actual = (status, _profile_view(data) if status == 200 else data, trace)
        _check(rid, actual, expected)


def test_human_profile_cap_drops_later_vendor():
    # FINDING 10: 20 tripo rows then 1 fal row -> the cap of 20 drops the later vendor (list order = vendor order), partial=True
    status, data, trace = human('/media/profiles', {'sessionID': 's1', 'operation': 'mesh.generate'}, store=Store({'tripo': MANY_TRIPO, 'fal': FAL}))
    ids = [p['profile_id'] for p in data['profiles']]
    assert (status, len(ids), ids[0], ids[-1], 'fa' in ids, data['partial']) == (200, 20, 'ta00', 'ta19', False, True)
    ROWS_CHECKED.append('h_pr_mesh_cap20_drops_fal')


# ==========================================================================
# HUMAN PATH 2: POST /media/operation/prepare  (inventory row 9)
#   (id, operation, inputs kind-or-list, profile_id, lists, caps, unused, expected (status, detail-or-jobID, trace))
#   profile_revision is taken from the real /media/profiles answer (fixture), it is not a dispatch input.
# ==========================================================================
IMG = [{'kind': 'image', 'resource_ref': 'ref-img'}]
TXT = [{'kind': 'text_prompt', 'text': 'a cat'}]
RCP = [{'kind': 'workflow_recipe', 'resource_ref': 'ref-recipe'}]
DEST = {'resource_ref': 'dest-ref', 'name': 'out'}
ALL_LISTS = {'tripo': TRIPO, 'fal': FAL, 'voidless': VOID, 'comfy': COMFY}
TRIPO_FA = [{**_row('t'), 'account_id': 'fa'}]               # a tripo row whose account id equals the fal account id
COMFY_FA = [{'connection_id': 'fa', 'label': 'COMFY label', 'revision': 'cr'}]   # a comfy connection whose id equals the fal account id
SAME_ID = [{**_row('f'), 'connection_id': 'vc'}]          # a voidless row whose account id 'fa' equals the fal account id


def _revision_for(op, lists, caps, profile_id):
    """Ask the real /media/profiles for the revision of `profile_id` (fixture only)."""
    status, data, _ = human('/media/profiles', {'sessionID': 's1', 'operation': op}, store=Store(lists, caps))
    if status != 200: return '0' * 64
    return next((p['revision'] for p in data['profiles'] if p['profile_id'] == profile_id), '0' * 64)


PREPARE_ROWS = [
    # ---- mesh.generate + image input ------------------------------------------------------------------------
    ('h_pp_mesh_image_fal', 'mesh.generate', IMG, 'fa', ALL_LISTS, ALL, True, (200, 'job-fal-mesh', ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal', 'build:fal-mesh', 'select:fal-mesh', 'list:fal', 'build:fal-mesh', 'prepare:fal-mesh.prepare_image_mesh(fa)'])),
    # profile belongs to tripo (text-only profile) but input is an image: refused by the input_kinds filter, nothing prepared
    ('h_pp_mesh_image_tripo_profile', 'mesh.generate', IMG, 'ta', ALL_LISTS, ALL, True, (409, 'Media profile changed', ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal', 'build:fal-mesh', 'select:fal-mesh'])),
    ('h_pp_mesh_image_unknown_profile', 'mesh.generate', IMG, 'nobody', ALL_LISTS, ALL, True, (409, 'Media profile changed', ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal', 'build:fal-mesh', 'select:fal-mesh'])),
    ('h_pp_mesh_image_no_profiles', 'mesh.generate', IMG, 'fa', {}, ALL, True, (409, 'Media profile changed', ['list:tripo', 'list:fal', 'build:fal-mesh'])),
    # ---- mesh.generate + text input -------------------------------------------------------------------------
    ('h_pp_mesh_text_tripo', 'mesh.generate', TXT, 'ta', ALL_LISTS, ALL, True, (200, 'job-tripo', ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal', 'build:fal-mesh', 'select:fal-mesh', 'build:tripo', 'prepare:tripo.prepare_text3d(ta)'])),
    # (B4-P2b) a FAL account id with a TEXT prompt: fal-mesh family has no text operation -> refused before any vendor call
    ('h_pp_mesh_text_fal_profile', 'mesh.generate', TXT, 'fa', ALL_LISTS, ALL, True, (409, 'Media profile unavailable or changed', ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal', 'build:fal-mesh', 'select:fal-mesh'])),    # was FINDING 3: 200, prepare:tripo.prepare_text3d(fa)
    ('h_pp_mesh_text_unknown_profile', 'mesh.generate', TXT, 'nobody', ALL_LISTS, ALL, True, (409, 'Media profile unavailable or changed', ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal', 'build:fal-mesh', 'select:fal-mesh'])),
    # tripo not installed: same refusal, decided by the profile's own family
    ('h_pp_mesh_text_fal_profile_no_tripo', 'mesh.generate', TXT, 'fa', ALL_LISTS, ('fal', 'voidless', 'meshy'), True, (409, 'Media profile unavailable or changed', ['list:fal', 'build:fal-mesh', 'select:fal-mesh'])),    # was FINDING 3: 503 Tripo jobs unavailable
    # shape refusals: kind/shape decide the branch before any profile is looked at
    ('h_pp_mesh_no_inputs', 'mesh.generate', [], 'ta', ALL_LISTS, ALL, True, (422, 'Unsupported media operation or inputs', [])),
    ('h_pp_mesh_recipe_input', 'mesh.generate', RCP, 'ta', ALL_LISTS, ALL, True, (422, 'Unsupported media operation or inputs', [])),
    ('h_pp_mesh_two_inputs', 'mesh.generate', IMG + IMG, 'fa', ALL_LISTS, ALL, True, (422, 'Unsupported media operation or inputs', [])),
    # ---- image.generate + text input (voidless first, then fal; membership decides) ---------------------------------
    ('h_pp_image_text_voidless', 'image.generate', TXT, 'va', ALL_LISTS, ALL, True, (200, 'job-voidless', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless', 'list:voidless', 'build:voidless', 'prepare:voidless.prepare_voidless(va)'])),
    ('h_pp_image_text_fal', 'image.generate', TXT, 'fa', ALL_LISTS, ALL, True, (200, 'job-fal', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless', 'list:voidless', 'list:fal', 'build:fal', 'prepare:fal.prepare_text_image(fa)'])),
    # (B4-P2b) same account id in the voidless AND the fal list -> refused, never picked by order
    ('h_pp_image_text_id_in_both', 'image.generate', TXT, 'fa', {**ALL_LISTS, 'voidless': SAME_ID}, ALL, True, (409, 'Media profile changed', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless'])),    # was FINDING 5: 409 Media profile changed after prepare:voidless.prepare_voidless(fa)
    ('h_pp_image_text_voidless_duplicate', 'image.generate', TXT, 'va', {**ALL_LISTS, 'voidless': VOID + VOID}, ALL, True, (409, 'Media profile unavailable', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless', 'select:voidless', 'list:voidless'])),
    ('h_pp_image_text_comfy_profile', 'image.generate', TXT, 'cc', ALL_LISTS, ALL, True, (409, 'Media profile changed', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless'])),
    ('h_pp_image_text_unknown_profile', 'image.generate', TXT, 'nobody', ALL_LISTS, ALL, True, (409, 'Media profile changed', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless'])),
    # (B4-P2b) the neutral image.generate flow works again
    ('h_pp_image_text_NAMEERROR_auth', 'image.generate', TXT, 'va', ALL_LISTS, ALL, False, (200, 'job-voidless', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless', 'list:voidless', 'build:voidless', 'prepare:voidless.prepare_voidless(va)'])),    # was FINDING 1: 409 Media profiles unavailable or changed (NameError auth)
    # ---- image.generate + workflow_recipe input (comfy) -------------------------------------------------------
    ('h_pp_image_recipe_comfy', 'image.generate', RCP, 'cc', ALL_LISTS, ALL, True, (200, 'job-comfy', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless', 'build:comfy', 'prepare:comfy.prepare(cc)'])),
    # (B4-P2b) a recipe goes only to a comfy profile; a FAL / VOIDLESS id is refused
    ('h_pp_image_recipe_fal_profile', 'image.generate', RCP, 'fa', ALL_LISTS, ALL, True, (409, 'Media profile unavailable or changed', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless'])),    # was FINDING 4: 200, prepare:comfy.prepare(fa)
    ('h_pp_image_recipe_voidless_profile', 'image.generate', RCP, 'va', ALL_LISTS, ALL, True, (409, 'Media profile unavailable or changed', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless'])),    # was FINDING 4: 200, prepare:comfy.prepare(va)
    ('h_pp_image_recipe_unknown_profile', 'image.generate', RCP, 'nobody', ALL_LISTS, ALL, True, (409, 'Media profile unavailable or changed', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless'])),
    ('h_pp_image_no_inputs', 'image.generate', [], 'cc', ALL_LISTS, ALL, True, (422, 'Unsupported media operation or inputs', [])),
    ('h_pp_image_mesh_input', 'image.generate', [{'kind': 'mesh', 'resource_ref': 'r'}], 'cc', ALL_LISTS, ALL, True, (422, 'Unsupported media operation or inputs', [])),
    # ---- operations with no neutral human implementation: fall to the final recipe-shape check ------------------
    ('h_pp_audio_generate_recipe', 'audio.generate', RCP, 'cc', ALL_LISTS, ALL, True, (422, 'Unsupported media operation or inputs', [])),
    ('h_pp_mesh_rig', 'mesh.rig', [{'kind': 'mesh', 'resource_ref': 'r'}], 'ma', ALL_LISTS, ALL, True, (422, 'Unsupported media operation or inputs', [])),
    # video.trim is decided by the literal profile id 'trim-webm-v1'
    ('h_pp_trim_wrong_profile', 'video.trim', [{'kind': 'host_video', 'resource_ref': 'r', 'expected_revision': {}}], 'fa', ALL_LISTS, ALL, True, (422, 'Unsupported trim inputs', [])),
    ('h_pp_mesh_image_id_in_both', 'mesh.generate', IMG, 'fa', {**ALL_LISTS, 'tripo': TRIPO_FA}, ALL, True, (409, 'Media profile changed', ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal', 'build:fal-mesh', 'select:fal-mesh'])),
    ('h_pp_mesh_text_id_in_both', 'mesh.generate', TXT, 'fa', {**ALL_LISTS, 'tripo': TRIPO_FA}, ALL, True, (409, 'Media profile unavailable or changed', ['list:tripo', 'build:tripo', 'select:tripo', 'list:fal', 'build:fal-mesh', 'select:fal-mesh'])),
    ('h_pp_image_recipe_id_in_comfy_and_fal', 'image.generate', RCP, 'fa', {**ALL_LISTS, 'comfy': COMFY_FA}, ALL, True, (409, 'Media profile unavailable or changed', ['list:fal', 'build:fal', 'list:voidless', 'build:voidless', 'list:comfy', 'build:comfy', 'select:comfy', 'select:fal', 'select:voidless'])),
]


def test_human_operation_prepare_rows():
    for rid, op, inputs, pid, lists, caps, _unused, expected in PREPARE_ROWS:    # 7th column: formerly auth_global, unused since B4-P2b
        revision = _revision_for(op, lists, caps, pid)
        params = {'start_ms': 0, 'end_ms': 1} if op == 'video.trim' else {}
        body = {'sessionID': 's1', 'operation': op, 'profile_id': pid, 'inputs': inputs, 'parameters': params,
                'destination': DEST, 'profile_revision': revision if op != 'video.trim' else '0' * 64}
        status, data, trace = human('/media/operation/prepare', body, store=Store(lists, caps))
        actual = (status, data['jobID'] if status == 200 else data, trace)
        _check(rid, actual, expected)


# ==========================================================================
# HUMAN PATH 3: per-vendor prepare routes and the shared comfy /media/prepare (inventory rows 4-5, 10): the URL picks the vendor,
# no profile family is checked here.  (id, path, body, caps, expected (status, detail-or-jobID, trace))
# ==========================================================================
VBODY = {'sessionID': 's1', 'connectionID': 'xc', 'providerAccountID': 'xa', 'destinationRef': 'dest-ref-1', 'destinationName': 'out', 'parameters': {'prompt': 'p'}}
ROUTE_ROWS = [
    ('h_rt_tripo', '/media/tripo/prepare', VBODY, ALL, (200, 'job-tripo', ['build:tripo', 'prepare:tripo.prepare_text3d(xa)'])),
    ('h_rt_tripo_no_cap', '/media/tripo/prepare', VBODY, ('fal', 'voidless', 'meshy'), (503, 'Tripo jobs unavailable', [])),
    ('h_rt_sprixen', '/media/sprixen/prepare', VBODY, ALL, (200, 'job-sprixen', ['build:sprixen', 'prepare:sprixen.prepare_sprite(xa)'])),
    ('h_rt_meshy_preview', '/media/meshy/prepare', VBODY, ALL, (200, 'job-meshy', ['build:meshy', 'prepare:meshy.prepare_preview(xa)'])),
    ('h_rt_meshy_preview_no_cap', '/media/meshy/prepare', VBODY, ('tripo', 'fal', 'voidless'), (503, 'Meshy jobs unavailable', [])),
    ('h_rt_meshy_refine', '/media/meshy/refine/prepare',
     {'sessionID': 's1', 'sourceJobID': 'job-1', 'destinationRef': 'dest-ref-1', 'destinationName': 'out', 'parameters': {}}, ALL, (200, 'job-meshy', ['build:meshy', 'prepare:meshy.prepare_refine(job-1)'])),
    ('h_rt_comfy_prepare', '/media/prepare',
     {'sessionID': 's1', 'connectionID': 'cc', 'recipeRef': 'recipe-ref-1', 'destinationRef': 'dest-ref-1', 'destinationName': 'out', 'parameters': {}}, ALL, (200, 'job-comfy', ['build:comfy', 'prepare:comfy.prepare(cc)'])),
]


def test_human_vendor_route_rows():
    for rid, path, body, caps, expected in ROUTE_ROWS:
        status, data, trace = human(path, body, store=Store({}, caps))
        actual = (status, data['jobID'] if status == 200 else data, trace)
        _check(rid, actual, expected)


# ==========================================================================
# HUMAN PATH 4: job -> service dispatch chain `_service_for_job`, reached via POST /media/run  (inventory rows 4-6)
#   (id, adapter_id in the stored job identity, expected (status, detail-or-ok, trace))
# ==========================================================================
def _job(adapter_id):
    ident = {'account_id': ACCT, 'owner': OWNER, 'session_id': 's1', 'workspace_id': 'ws1', 'policy_generation': REPO.generation()}
    if adapter_id is not None: ident['adapter_id'] = adapter_id
    return {'id': 'j1', 'owner': OWNER, 'account_id': ACCT, 'workspace_id': 'ws1', 'identity': ident}


JOB_ROWS = [
    ('h_job_voidless', 'voidless-image-v1', (200, 'ok', ['build:voidless', 'run:voidless'])),
    ('h_job_fal_flux', 'fal-flux-schnell-v1', (200, 'ok', ['build:fal', 'run:fal'])),
    ('h_job_fal_hunyuan', 'fal-hunyuan-image3d-v1', (200, 'ok', ['build:fal-mesh', 'run:fal-mesh'])),
    ('h_job_meshy_rig', 'meshy-rig-v1', (200, 'ok', ['build:meshy-rig', 'run:meshy-rig'])),    # was FINDING 11: 409, nothing built (ImportError MeshyRigAdapter)
    ('h_job_comfy_material', 'comfyui-material-v1', (200, 'ok', ['build:comfy', 'run:comfy'])),
    ('h_job_hunyuan_local', 'hunyuan3d-v2-shape-v1', (200, 'ok', ['build:hunyuan', 'run:hunyuan'])),
    ('h_job_trellis', 'trellis-image3d-v1', (200, 'ok', ['build:trellis', 'run:trellis'])),
    ('h_job_material_pack', 'material-pack-v1', (200, 'ok', ['build:material-pack', 'run:material-pack'])),
    ('h_job_tripo', 'tripo-text3d-v1', (200, 'ok', ['build:tripo', 'run:tripo'])),
    ('h_job_sprixen_animation', 'sprixen-animation-v1', (200, 'ok', ['build:sprixen', 'build:sprixen-animation', 'run:sprixen-animation'])),
    ('h_job_meshy_preview', 'meshy-preview-v1', (200, 'ok', ['build:meshy', 'run:meshy'])),
    ('h_job_meshy_refine', 'meshy-refine-v1', (200, 'ok', ['build:meshy', 'run:meshy'])),
    ('h_job_sprixen_sprite', 'sprixen-sprite-v1', (200, 'ok', ['build:sprixen', 'run:sprixen'])),
    ('h_job_comfyui', 'comfyui', (200, 'ok', ['build:comfy', 'run:comfy'])),
    # (B4-P2b) flac is a real comfy adapter (stays comfy); unknown, empty and missing ids are refused, no fallback service
    ('h_job_FALLBACK_flac', 'comfyui-flac-v1', (200, 'ok', ['build:comfy', 'run:comfy'])),
    # BQ-20 (MOC:25 "keep old v1 records readable"): older records carry no/unknown adapter id; the snapshot fallback service is restored
    ('h_job_FALLBACK_unknown', 'someone-elses-v9', (200, 'ok', ['build:comfy', 'run:comfy'])),
    ('h_job_FALLBACK_empty', '', (200, 'ok', ['build:comfy', 'run:comfy'])),
    ('h_job_FALLBACK_missing', None, (200, 'ok', ['build:comfy', 'run:comfy'])),
]


def test_human_service_for_job_rows():
    for rid, adapter_id, expected in JOB_ROWS:
        status, data, trace = human('/media/run', {'jobID': 'j1', 'revision': 1, 'parameters': {}}, job=_job(adapter_id))
        actual = (status, data if status != 200 else 'ok', trace)
        _check(rid, actual, expected)


# ==========================================================================
# AGENT PATH
# ==========================================================================
AUTH = {'account_id': ACCT, 'owner': OWNER, 'session_id': 's1', 'workspace_id': 'ws1', 'actor_id': 'a1', 'root_operation_id': 'r1',
        'turn_id': 't1', 'message_id': 'm1', 'tool_call_id': 'c1', 'policy_generation': 1, 'execution_revision': 1}


class AgentCoord:
    def __init__(self, name): self.name = name
    async def status(self, identity, job_id): TRACE.append('coord.status:' + self.name); return {'stop': True}
    async def collect(self, identity, job_id): TRACE.append('coord.collect:' + self.name); return {'stop': True}


class AgentSvc:
    def __init__(self, name, interface='i'):
        self.name, self.interface, self.coordinator = name, interface, AgentCoord(name)
    async def _bounded_read(self, aw): return await aw

    @property
    def scope(self):   # the inline comfy submit path calls scope.capture next; stop there, it is past the dispatch decision
        class Scope:
            def capture(_, *a, **k): TRACE.append('capture:' + self.name); raise RuntimeError('stop at comfy capture')
        return Scope()

    def profile_selection(self, owner, account, connection_id, provider_account_id=None):
        async def go():
            TRACE.append('select:' + self.name)
            return _binding(connection_id, provider_account_id, self.interface)
        return go()

    def _job(self, account, owner, job_id): return {'identity': {'adapter_id': self.adapter, 'account_id': ACCT}, 'workspace_id': 'ws1'}


SVC_KW = {'media': 'media_service', 'tripo': 'tripo_service', 'fal': 'fal_service', 'fal_mesh': 'fal_mesh_service', 'material': 'material_service',
          'sprite': 'sprite_service', 'trellis': 'trellis_service', 'hunyuan': 'hunyuan_service', 'meshy_rig': 'meshy_rig_service',
          'voidless': 'voidless_service'}
EVERY = tuple(SVC_KW)


def agent_service(have=EVERY, store=None):
    kw = {SVC_KW[n]: AgentSvc(n, {'trellis': 'trellis-image3d-v1', 'hunyuan': 'hunyuan3d-v2-shape-v1'}.get(n, 'i')) for n in have if n in SVC_KW}
    kw.setdefault('media_service', AgentSvc('media'))
    async def current(): return True
    svc = AgentMediaService(provider_store=store or Store(), current=current, **kw)
    svc.blender = {k: AgentSvc('blender:' + k) for k in ('image.render', 'material.bake')} if 'blender' in have else {}
    return svc


def _stub_submits(svc):
    def rec(name, *keys):
        async def f(**kw):
            TRACE.append('submit:%s(%s)' % (name, ','.join('%s=%s' % (k, kw.get(k) if k != 'row' else (kw['row']['account_id'] if kw.get('row') else None))
                                                           for k in keys)))
            return 'sentinel'
        return f
    svc._submit_voidless = rec('voidless', 'row')
    svc._submit_fal = rec('fal', 'profile_id')
    svc._submit_mesh = rec('tripo', 'profile_id')
    svc._submit_trellis = rec('trellis', 'profile_id')
    svc._submit_local_mesh = rec('local_mesh', 'validator', 'profile_id')
    svc._submit_fal_mesh = lambda **kw: rec('fal_mesh', 'profile_id', 'selected_id')(**{**kw, 'selected_id': (kw.get('selected') or {}).get('account_id')})
    svc._submit_material = rec('material', 'profile_id')
    svc._submit_sprite = rec('sprite', 'operation', 'profile_id')
    svc._submit_blender = rec('blender', 'operation', 'profile_id')
    return svc


def agent_submit(op, profile_id, inputs, *, have=EVERY, lists=None, selectors=None, stub=True, parameters=None):
    del TRACE[:]
    svc = agent_service(have, Store(lists or {}, selectors=selectors))
    if stub: _stub_submits(svc)
    try:
        out = asyncio.run(svc.submit(authority=AUTH, operation=op, profile_id=profile_id, inputs=inputs,
                                     parameters={} if parameters is None else parameters, destination=DEST))
        res = 'ok' if out == 'sentinel' else 'ok:' + repr(out)
    except Exception as exc:
        res = '%s: %s' % (type(exc).__name__, exc)
    return res, list(TRACE)


A_TXT = [{'kind': 'text', 'text': 'a cat'}]
A_IMG = [{'kind': 'image', 'resource_ref': 'ref-img'}]
A_RCP = [{'kind': 'workflow_recipe', 'resource_ref': 'ref-recipe'}]
A_MESH = [{'kind': 'mesh', 'resource_ref': 'ref-mesh'}]
LISTS = {'tripo': TRIPO, 'fal': FAL, 'voidless': VOID, 'comfy': COMFY}
SEL = {'hunyuan:lc': (True, None), 'trellis:lc': (True, None), 'hunyuan:both': (True, FAL[0]), 'trellis:both': (True, FAL[0]),
       'hunyuan:remote': (False, FAL[0]), 'trellis:remote': (False, FAL[0]), 'hunyuan:remote-wrong': (False, TRIPO[0])}
NO = lambda *names: tuple(n for n in EVERY if n not in names)

# (id, operation, profile_id, inputs, services present, store lists, selectors, stub submits, expected (result, trace))
SUBMIT_ROWS = [
    # ---- image.generate + text: voidless membership first, then fal for EVERYTHING else ---------------------------
    ('a_img_text_voidless', 'image.generate', 'va', A_TXT, EVERY, LISTS, SEL, True, ('ok', ['list:voidless', 'list:fal', 'submit:voidless(row=va)'])),    # was FINDING 5/6: same selection, but only voidless was listed (now every family is listed to prove exactly one owns the id)
    ('a_img_text_fal', 'image.generate', 'fa', A_TXT, EVERY, LISTS, SEL, True, ('ok', ['list:voidless', 'list:fal', 'submit:fal(profile_id=fa)'])),    # was FINDING 5/6: same selection; trace lacked list:fal (now every family is listed)
    # (B4-P2b) an id that no family lists is refused at dispatch
    ('a_img_text_unknown_FALLS_TO_FAL', 'image.generate', 'nobody', A_TXT, EVERY, LISTS, SEL, True, ('ValueError: media profile unavailable', ['list:voidless', 'list:fal'])),    # was FINDING 6: dispatched to submit:fal(profile_id=nobody)
    ('a_img_text_comfy_connection_FALLS_TO_FAL', 'image.generate', 'cc', A_TXT, EVERY, LISTS, SEL, True, ('ValueError: media profile unavailable', ['list:voidless', 'list:fal'])),    # was FINDING 6: dispatched to submit:fal(profile_id=cc)
    # voidless service absent: its lister is never consulted; the id is then unlisted -> refused
    ('a_img_text_voidless_id_no_voidless_service', 'image.generate', 'va', A_TXT, NO('voidless'), LISTS, SEL, True, ('ValueError: media profile unavailable', ['list:fal'])),    # was FINDING 6: dispatched to submit:fal(profile_id=va) without any listing
    # (B4-P2b) same account id listed by voidless and fal -> ambiguous, refused
    ('a_img_text_id_in_both', 'image.generate', 'fa', A_TXT, EVERY, {**LISTS, 'voidless': SAME_ID}, SEL, True, ('ValueError: ambiguous media profile', ['list:voidless', 'list:fal'])),    # was FINDING 5: voidless won by order, submit:voidless(row=fa)
    ('a_img_text_voidless_duplicate', 'image.generate', 'va', A_TXT, EVERY, {**LISTS, 'voidless': VOID + VOID}, SEL, True, ('ValueError: ambiguous media profile', ['list:voidless'])),
    # real _submit_fal (not stubbed): fal absent -> operation refusal; present but id unlisted -> profile refusal
    ('a_img_text_unknown_real_fal', 'image.generate', 'nobody', A_TXT, EVERY, LISTS, SEL, False, ('ValueError: media profile unavailable', ['list:voidless', 'list:fal'])),
    ('a_img_text_real_no_fal_service', 'image.generate', 'fa', A_TXT, NO('fal', 'voidless'), LISTS, SEL, False, ('ValueError: media operation unavailable', [])),
    # image.generate + non-text input never reaches fal/voidless: inline comfy path (first act: comfy profile_selection) or operation refusal
    ('a_img_recipe_comfy_path', 'image.generate', 'cc', A_RCP, EVERY, LISTS, SEL, False, ('RuntimeError: stop at comfy capture', ['select:media', 'capture:media'])),
    ('a_img_image_input_refused', 'image.generate', 'cc', A_IMG, EVERY, LISTS, SEL, False, ('ValueError: media operation unavailable', [])),
    ('a_audio_generate_recipe_comfy_path', 'audio.generate', 'cc', A_RCP, EVERY, LISTS, SEL, False, ('RuntimeError: stop at comfy capture', ['select:media', 'capture:media'])),
    # ---- mesh.generate + image: selector prefix, store resolve, then fal-mesh fallback --------------------------------
    ('a_mesh_img_hunyuan_local', 'mesh.generate', 'hunyuan:lc', A_IMG, EVERY, LISTS, SEL, True, ('ok', ['resolve:hunyuan:lc', 'submit:local_mesh(validator=hunyuan,profile_id=lc)'])),
    ('a_mesh_img_trellis_local', 'mesh.generate', 'trellis:lc', A_IMG, EVERY, LISTS, SEL, True, ('ok', ['resolve:trellis:lc', 'submit:trellis(profile_id=lc)'])),
    ('a_mesh_img_hunyuan_ambiguous', 'mesh.generate', 'hunyuan:both', A_IMG, EVERY, LISTS, SEL, True, ('ValueError: ambiguous media profile', ['resolve:hunyuan:both'])),
    ('a_mesh_img_trellis_ambiguous', 'mesh.generate', 'trellis:both', A_IMG, EVERY, LISTS, SEL, True, ('ValueError: ambiguous media profile', ['resolve:trellis:both'])),
    # (B4-P2b) a 'hunyuan:'/'trellis:' selector that the store resolves to a REMOTE row is refused adapter id
    ('a_mesh_img_hunyuan_prefix_remote_TO_FAL_MESH', 'mesh.generate', 'hunyuan:remote', A_IMG, EVERY, LISTS, SEL, True, ('ValueError: media profile unavailable', ['resolve:hunyuan:remote'])),    # was FINDING 7: submit:fal_mesh(selected_id=fa)
    ('a_mesh_img_trellis_prefix_remote_TO_FAL_MESH', 'mesh.generate', 'trellis:remote', A_IMG, EVERY, LISTS, SEL, True, ('ValueError: media profile unavailable', ['resolve:trellis:remote'])),    # was FINDING 7: submit:fal_mesh(selected_id=fa)
    ('a_mesh_img_hunyuan_unresolved', 'mesh.generate', 'hunyuan:zzz', A_IMG, EVERY, LISTS, SEL, True, ('ValueError: media profile unavailable', ['resolve:hunyuan:zzz'])),
    ('a_mesh_img_trellis_unresolved', 'mesh.generate', 'trellis:zzz', A_IMG, EVERY, LISTS, SEL, True, ('ValueError: media profile unavailable', ['resolve:trellis:zzz'])),
    ('a_mesh_img_hunyuan_local_no_service', 'mesh.generate', 'hunyuan:lc', A_IMG, NO('hunyuan'), LISTS, SEL, True, ('ValueError: media profile unavailable', ['resolve:hunyuan:lc'])),
    ('a_mesh_img_trellis_local_no_service', 'mesh.generate', 'trellis:lc', A_IMG, NO('trellis'), LISTS, SEL, True, ('ValueError: media profile unavailable', ['resolve:trellis:lc'])),
    # (B4-P2b) no prefix: the id must belong to exactly one family (tripo or fal-mesh); no fal-mesh fallback
    ('a_mesh_img_fal_id', 'mesh.generate', 'fa', A_IMG, EVERY, LISTS, SEL, True, ('ok', ['list:tripo', 'list:fal', 'submit:fal_mesh(profile_id=fa,selected_id=None)'])),    # was FINDING 7: same selection; the id is now proven to belong to exactly one family first (list:tripo, list:fal)
    ('a_mesh_img_tripo_id_FALLBACK_FAL_MESH', 'mesh.generate', 'ta', A_IMG, EVERY, LISTS, SEL, True, ('ValueError: media operation unavailable', ['list:tripo', 'list:fal'])),    # was FINDING 7: submit:fal_mesh(profile_id=ta)
    ('a_mesh_img_unknown_id_FALLBACK_FAL_MESH', 'mesh.generate', 'nobody', A_IMG, EVERY, LISTS, SEL, True, ('ValueError: media profile unavailable', ['list:tripo', 'list:fal'])),    # was FINDING 7: submit:fal_mesh(profile_id=nobody)
    ('a_mesh_img_unknown_real_fal_mesh', 'mesh.generate', 'nobody', A_IMG, EVERY, LISTS, SEL, False, ('ValueError: media profile unavailable', ['list:tripo', 'list:fal'])),    # was FINDING 7: same refusal text, produced inside fal-mesh after list:fal only
    ('a_mesh_img_real_no_fal_mesh_service', 'mesh.generate', 'fa', A_IMG, NO('fal_mesh'), LISTS, SEL, False, ('ValueError: media profile unavailable', ['list:tripo'])),    # was FINDING 7: refusal was media operation unavailable from the fal-mesh fallback
    # ---- mesh.generate + text/other: input KIND only picks an operation inside the profile's own family (B4-P2b) ----
    ('a_mesh_text_tripo', 'mesh.generate', 'ta', A_TXT, EVERY, LISTS, SEL, True, ('ok', ['list:tripo', 'list:fal', 'submit:tripo(profile_id=ta)'])),    # was FINDING 8: same selection; fal is now listed too to prove tripo owns the id
    ('a_mesh_text_fal_id_TO_TRIPO', 'mesh.generate', 'fa', A_TXT, EVERY, LISTS, SEL, True, ('ValueError: media operation unavailable', ['list:tripo', 'list:fal'])),    # was FINDING 8: submit:tripo(profile_id=fa)
    ('a_mesh_mesh_input_TO_TRIPO', 'mesh.generate', 'ta', A_MESH, EVERY, LISTS, SEL, True, ('ValueError: media operation unavailable', [])),    # was FINDING 8: submit:tripo(profile_id=ta) chosen by input kind
    ('a_mesh_text_real_unknown', 'mesh.generate', 'nobody', A_TXT, EVERY, LISTS, SEL, False, ('ValueError: media profile unavailable', ['list:tripo', 'list:fal'])),    # was FINDING 8: same refusal text, produced inside tripo after list:tripo only
    ('a_mesh_text_real_fal_id', 'mesh.generate', 'fa', A_TXT, EVERY, LISTS, SEL, False, ('ValueError: media operation unavailable', ['list:tripo', 'list:fal'])),    # was FINDING 8: media profile unavailable from tripo lookup
    ('a_mesh_text_real_no_tripo_service', 'mesh.generate', 'ta', A_TXT, NO('tripo'), LISTS, SEL, False, ('ValueError: media profile unavailable', ['list:fal'])),    # was FINDING 8: media operation unavailable from the tripo fallback
    # ---- literal-id / operation-keyed dispatch --------------------------------------------------------------
    ('a_material_pack', 'material.pack', 'material-pack-v1', A_MESH, EVERY, LISTS, SEL, True, ('ok', ['submit:material(profile_id=material-pack-v1)'])),
    ('a_image_pack', 'image.pack', 'sprite-sheet-v1', A_IMG, EVERY, LISTS, SEL, True, ('ok', ['submit:sprite(operation=image.pack,profile_id=sprite-sheet-v1)'])),
    ('a_image_pack_crops', 'image.pack-crops', 'sprite-sheet-crops-v1', A_IMG, EVERY, LISTS, SEL, True, ('ok', ['submit:sprite(operation=image.pack-crops,profile_id=sprite-sheet-crops-v1)'])),
    ('a_image_render', 'image.render', 'bc', A_MESH, EVERY + ('blender',), LISTS, SEL, True, ('ok', ['submit:blender(operation=image.render,profile_id=bc)'])),
    ('a_material_bake', 'material.bake', 'bc', A_MESH, EVERY + ('blender',), LISTS, SEL, True, ('ok', ['submit:blender(operation=material.bake,profile_id=bc)'])),
    ('a_mesh_rig_no_service', 'mesh.rig', 'ma', A_MESH, NO('meshy_rig'), LISTS, SEL, False, ('ValueError: media operation unavailable', [])),
    ('a_mesh_rig_unknown_profile', 'mesh.rig', 'nobody', A_MESH, EVERY, {'meshy': [_row('m')]}, SEL, False, ('ValueError: media profile unavailable', ['list:meshy'])),
    ('a_material_generate_bad_input', 'material.generate', 'cc', A_IMG, EVERY, LISTS, SEL, False, ('ValueError: media operation unavailable', [])),
    ('a_unknown_operation', 'bogus.op', 'cc', A_RCP, EVERY, LISTS, SEL, False, ('ValueError: media operation unavailable', [])),
    ('a_mesh_id_in_both_tripo_fal', 'mesh.generate', 'fa', A_IMG, EVERY, {**LISTS, 'tripo': TRIPO_FA}, SEL, True, ('ValueError: ambiguous media profile', ['list:tripo', 'list:fal'])),
    ('a_mesh_text_hunyuan_prefix', 'mesh.generate', 'hunyuan:lc', A_TXT, EVERY, LISTS, SEL, True, ('ValueError: media operation unavailable', ['resolve:hunyuan:lc'])),
]


def test_agent_submit_dispatch_rows():
    for rid, op, pid, inputs, have, lists, sel, stub, expected in SUBMIT_ROWS:
        actual = agent_submit(op, pid, inputs, have=have, lists=lists, selectors=sel, stub=stub)
        _check(rid, actual, expected)


# ---- agent job -> service lookup (inventory row 32) and allowed adapter-id sets (row 33) -------------------------------
JOBSVC_ROWS = [
    ('a_job_trellis', 'trellis-image3d-v1', EVERY, 'trellis'),
    ('a_job_hunyuan_local', 'hunyuan3d-v2-shape-v1', EVERY, 'hunyuan'),
    ('a_job_tripo', 'tripo-text3d-v1', EVERY, 'tripo'),
    ('a_job_fal_flux', 'fal-flux-schnell-v1', EVERY, 'fal'),
    ('a_job_voidless', 'voidless-image-v1', EVERY, 'voidless'),
    ('a_job_fal_hunyuan', 'fal-hunyuan-image3d-v1', EVERY, 'fal_mesh'),
    ('a_job_meshy_rig', 'meshy-rig-v1', EVERY, 'meshy_rig'),
    ('a_job_material_pack', 'material-pack-v1', EVERY, 'material'),
    ('a_job_sprite', 'sprite-sheet-v1', EVERY, 'sprite'),
    ('a_job_sprite_crops', 'sprite-sheet-crops-v1', EVERY, 'sprite'),
    ('a_job_comfyui', 'comfyui', EVERY, 'media'),
    ('a_job_comfy_flac', 'comfyui-flac-v1', EVERY, 'media'),
    ('a_job_comfy_material', 'comfyui-material-v1', EVERY, 'media'),
    ('a_job_blender_png', 'blender-glb-png-v1', EVERY + ('blender',), 'blender:image.render'),
    ('a_job_blender_bake', 'blender-material-bake-v1', EVERY + ('blender',), 'blender:material.bake'),
    # refusals: unknown id, id whose service is absent, adapters the agent has no service for (meshy preview/refine, sprixen, tripo w/o service)
    ('a_job_unknown', 'someone-elses-v9', EVERY, 'ValueError: media job unavailable'),
    ('a_job_tripo_no_service', 'tripo-text3d-v1', NO('tripo'), 'ValueError: media job unavailable'),
    ('a_job_meshy_preview', 'meshy-preview-v1', EVERY, 'ValueError: media job unavailable'),
    ('a_job_sprixen_sprite', 'sprixen-sprite-v1', EVERY, 'ValueError: media job unavailable'),
    ('a_job_none', None, EVERY, 'ValueError: media job unavailable'),
]


def test_agent_job_service_rows():
    for rid, adapter_id, have, expected in JOBSVC_ROWS:
        svc = agent_service(have)
        try: actual = svc._job_service({'identity': {'adapter_id': adapter_id}}).name
        except Exception as exc: actual = '%s: %s' % (type(exc).__name__, exc)
        _check(rid, actual, expected)


def test_agent_action_allowed_adapter_ids():
    # `action` first refuses ids outside its fixed set ('media job unavailable'), then looks the service up by id; an allowed id reaches that
    # service's coordinator.  (id, adapter_id, expected (result, trace))
    rows = [('a_act_trellis', 'trellis-image3d-v1', ('reached', ['coord.status:trellis'])), ('a_act_voidless', 'voidless-image-v1', ('reached', ['coord.status:voidless'])),
            ('a_act_comfy_flac', 'comfyui-flac-v1', ('reached', ['coord.status:media'])), ('a_act_unknown', 'someone-elses-v9', ('ValueError: media job unavailable', [])),
            ('a_act_meshy_preview', 'meshy-preview-v1', ('ValueError: media job unavailable', []))]
    for rid, adapter_id, expected in rows:
        del TRACE[:]
        svc = agent_service()
        svc.media._job = lambda account, owner, job_id, a=adapter_id: {'identity': {'adapter_id': a, 'account_id': ACCT}, 'workspace_id': 'ws1', 'session_id': 's1'}
        try: asyncio.run(svc.action(authority=AUTH, job_id='j1', action='status')); res = 'ok'
        except Exception as exc: res = '%s: %s' % (type(exc).__name__, exc)
        # the coordinator fake returns a stub that `project` cannot render; reaching the coordinator is the decision pinned here
        actual = (res if res.startswith('ValueError: media job unavailable') else 'reached', list(TRACE))
        _check(rid, actual, expected)


# ---- agent profile listing (inventory row 30) -----------------------------------------------------------------------
def agent_profiles(operation, *, have=EVERY, lists=None, selectors=None):
    del TRACE[:]
    svc = agent_service(have, Store(lists or {}, selectors=selectors))
    try:
        out = asyncio.run(svc.profiles(authority=AUTH, operation=operation))
        res = ([(p['profile_id'], tuple(sorted((o['id'], tuple(o['input_kinds'])) for o in p['operations']))) for p in out['profiles']], out['partial'])
    except Exception as exc:
        res = '%s: %s' % (type(exc).__name__, exc)
    return res, list(TRACE)


LIST_LISTS = {'tripo': TRIPO, 'fal': FAL, 'voidless': VOID, 'comfy': COMFY, 'trellis': LOCAL, 'hunyuan': LOCAL, 'meshy': [_row('m')]}
LIST_SEL = {'trellis:lc': (True, None), 'hunyuan:lc': (True, None)}
LISTING_ROWS = [
    # mesh.generate: tripo(text) then fal_mesh(image) then trellis then hunyuan; each vendor's own lister, skipped when its service is absent
    ('a_ls_mesh_all', 'mesh.generate', EVERY, LIST_LISTS, LIST_SEL, (([('ta', (('mesh.generate', ('text',)),)), ('fa', (('mesh.generate', ('image',)),)), ('trellis:lc', (('mesh.generate', ('image',)),)), ('hunyuan:lc', (('mesh.generate', ('image',)),))], False), ['list:tripo', 'select:tripo', 'list:fal', 'select:fal_mesh', 'list:trellis', 'resolve:trellis:lc', 'select:trellis', 'list:hunyuan', 'resolve:hunyuan:lc', 'select:hunyuan', 'resolve:trellis:lc', 'resolve:hunyuan:lc'])),
    ('a_ls_mesh_only_fal_mesh', 'mesh.generate', ('fal_mesh', 'media'), LIST_LISTS, LIST_SEL, (([('fa', (('mesh.generate', ('image',)),))], False), ['list:fal', 'select:fal_mesh'])),
    # trellis selector that resolves to a remote row too: the profile is dropped and the listing is partial
    ('a_ls_mesh_trellis_ambiguous', 'mesh.generate', ('trellis', 'media'), {'trellis': LOCAL}, {'trellis:lc': (True, FAL[0])}, (([], True), ['list:trellis', 'resolve:trellis:lc'])),
    # image.generate: comfy rows (also advertise audio/material), then voidless (kind 'text'), then fal ('text')
    ('a_ls_image_all', 'image.generate', EVERY, LIST_LISTS, LIST_SEL, (([('cc', (('image.generate', ('workflow_recipe',)),)), ('va', (('image.generate', ('text',)),)), ('fa', (('image.generate', ('text',)),))], False), ['list:comfy', 'select:media', 'list:voidless', 'select:voidless', 'list:fal', 'select:fal'])),
    ('a_ls_image_no_voidless_no_fal', 'image.generate', NO('voidless', 'fal'), LIST_LISTS, LIST_SEL, (([('cc', (('image.generate', ('workflow_recipe',)),))], False), ['list:comfy', 'select:media'])),
    ('a_ls_mesh_rig', 'mesh.rig', EVERY, LIST_LISTS, LIST_SEL, (([('ma', (('mesh.rig', ('mesh',)),))], False), ['list:meshy', 'select:meshy_rig'])),
    ('a_ls_mesh_rig_no_service', 'mesh.rig', NO('meshy_rig'), LIST_LISTS, LIST_SEL, (([], False), [])),
    ('a_ls_audio_generate', 'audio.generate', EVERY, LIST_LISTS, LIST_SEL, (([('cc', (('audio.generate', ('workflow_recipe',)),))], False), ['list:comfy', 'select:media'])),
    ('a_ls_material_pack', 'material.pack', EVERY, LIST_LISTS, LIST_SEL, (([('material-pack-v1', (('material.pack', ('texture_map',)),))], False), [])),
    ('a_ls_image_pack', 'image.pack', EVERY, LIST_LISTS, LIST_SEL, (([('sprite-sheet-v1', (('image.pack', ('image',)),))], False), [])),
    ('a_ls_unknown_operation', 'bogus.op', EVERY, LIST_LISTS, LIST_SEL, (([], False), [])),
]


def test_agent_profile_listing_rows():
    for rid, op, have, lists, sel, expected in LISTING_ROWS:
        actual = agent_profiles(op, have=have, lists=lists, selectors=sel)
        _check(rid, actual, expected)
