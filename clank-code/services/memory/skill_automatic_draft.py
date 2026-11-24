"""Private Agent authority for a qualified automatic skill draft.

No issuer is installed by this module. The actual M5 owner must hold its
current source/session admission before constructing this authority.
"""
import asyncio
import hashlib
import json


class _SkillDraftAgentAuthority:
    def __init__(self, seal, *, state, session, publisher, loop, mode='create'):
        from src.openclank.history_client import HistoryServiceSupervisor
        from src.openclank.skill_draft_reservation import _SEAL, _SkillDraftSession, _assert_skill_draft_save_current, _assert_skill_draft_status_current
        from src.openclank.skill_history_scope import SkillHistoryScopes
        if (seal is not _SEAL or type(session) is not _SkillDraftSession or type(publisher) is not SkillHistoryScopes
                or getattr(state, 'skill_history_scopes', None) is not publisher
                or type(publisher.supervisor) is not HistoryServiceSupervisor or not loop.is_running()
                or mode not in ('create', 'read')):
            raise ValueError('automatic skill authority unavailable')
        (_assert_skill_draft_save_current if mode == 'create' else _assert_skill_draft_status_current)(session)
        self._seal, self.state, self.session, self.publisher, self.loop = seal, state, session, publisher, loop
        self.mode = mode
        self.supervisor = publisher.supervisor
        self.process = self.supervisor.process
        self.owner, self.account = session.lifecycle.owner, session.lifecycle.account_id
        self.actor = 'agent:' + self.owner
        original = session.attempt['reservation_original']['original']
        result = session.result
        self.result_text_sha256 = hashlib.sha256(result.output['text'].encode('utf-8')).hexdigest()
        self.lineage = {'version': 1, 'jobID': original['operation_id'],
            'selectionSHA256': original['selection_sha256'], 'promptSHA256': original['prompt_sha256'],
            'assignmentSHA256': original['assignment_sha256'], 'modelRouteID': result.model_route_id,
            'connectionID': result.connection_id, 'resultOperationID': result.operation_id}
        if (self.process is None or self.process.poll() is not None or not all(type(value) is str and value
                for key, value in self.lineage.items() if key != 'version')):
            raise ValueError('automatic skill authority unavailable')
        self.plan = None
        self.lineage_sha256 = hashlib.sha256(json.dumps(self.lineage, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    def assert_current(self):
        from src.openclank.skill_draft_reservation import _SEAL, _assert_skill_draft_save_current, _assert_skill_draft_status_current
        if (self._seal is not _SEAL or getattr(self.state, 'skill_history_scopes', None) is not self.publisher
                or self.publisher.supervisor is not self.supervisor or self.supervisor.process is not self.process
                or self.process.poll() is not None
                or hashlib.sha256(self.session.result.output['text'].encode('utf-8')).hexdigest() != self.result_text_sha256
                or hashlib.sha256(json.dumps(self.lineage, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest() != self.lineage_sha256):
            raise ValueError('automatic skill authority changed')
        (_assert_skill_draft_save_current if self.mode == 'create' else _assert_skill_draft_status_current)(self.session)

    def bind_once_original(self, *, action_id, skill_id, binding_sha, resource_key, directory, child):
        """Retain the exact original before the History once-prepare can issue."""
        self.assert_current()
        if self.mode != 'create' or self.plan is None or getattr(self.session, 'save_original', None) is not None:
            raise ValueError('automatic skill original already exists')
        expected = self.plan
        if (action_id != expected['action_id'] or skill_id != expected['skill_id']
                or binding_sha != expected['binding_sha256'] or directory != expected['directory']
                or child != expected['child'] or type(resource_key) is not dict):
            raise ValueError('automatic skill original changed')
        original = {**expected, 'resource_key': resource_key, 'lineage': self.lineage}
        encoded = json.dumps(original, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(encoded.encode('utf-8')) > 65536:
            raise ValueError('automatic skill original unavailable')
        self.session.save_original = json.loads(encoded)
        self.session.save_original_sha256 = hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    def _history(self, action, directory, identity, parent_fd, child, scope):
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            raise ValueError('automatic skill History must run off the event loop')
        if action in ('open_create', 'open_read') and action == ('open_create' if self.mode == 'create' else 'open_read'):
            self.assert_current()
            opener = self.publisher.open_agent_create if self.mode == 'create' else self.publisher.open_agent_read
            future = asyncio.run_coroutine_threadsafe(opener(session=self.session,
                directory=directory, identity=identity, parent_fd=parent_fd, child=child), self.loop)
            try:
                selected = future.result()
            except BaseException:
                self.supervisor.fence_now(self.process)
                raise
            try:
                self.assert_current()
                selected.assert_current()
                from src.openclank.history_client import HistoryClient
                client = HistoryClient(str(self.supervisor.socket_path), actor_id=self.actor, account_id=self.account,
                                       token=selected.active_credential.token)
                def is_current():
                    try:
                        self.assert_current(); selected.assert_current(); return True
                    except Exception:
                        return False
                return {'client': client, 'root_id': selected.root_id,
                    'workspace_id': selected.descriptor['workspace_ids'][0], 'is_current': is_current, '_scope': selected}
            except BaseException:
                close = asyncio.run_coroutine_threadsafe(self.publisher.close_agent_create(selected), self.loop)
                try: close.result()
                except BaseException: self.supervisor.fence_now(self.process)
                raise
        if action == 'close' and type(scope) is dict:
            selected = scope.get('_scope')
            future = asyncio.run_coroutine_threadsafe(self.publisher.close_agent_create(selected), self.loop)
            try: return future.result()
            except BaseException:
                self.supervisor.fence_now(self.process)
                raise
        raise ValueError('automatic skill History operation unavailable')


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _automatic_fields(response):
    from src.teacher_escalation import _extract_skill_json
    value = _extract_skill_json(response)
    if value is None:
        return None
    fields = {key: value.get(key, default) for key, default in (
        ('name', ''), ('description', ''), ('category', 'general'), ('when_to_use', ''),
        ('procedure', []), ('pitfalls', []), ('verification', []), ('tags', []))}
    if (any(type(fields[key]) is not str or len(fields[key].encode('utf-8')) > 4096
            for key in ('name', 'description', 'category', 'when_to_use'))
            or any(type(fields[key]) is not list or len(fields[key]) > 64
                   or any(type(item) is not str or len(item.encode('utf-8')) > 4096 for item in fields[key])
                   for key in ('procedure', 'pitfalls', 'verification', 'tags'))
            or not fields['name'].strip() or not fields['description'].strip()
            or not fields['procedure'] or not any(item.strip() for item in fields['procedure'])
            or len(_canonical(fields)) > 65536):
        return None
    return fields


def _automatic_action_id(account, job_id):
    return 'skill-auto-' + hashlib.sha256(_canonical(['openclank.skill-draft.action.v1', account, job_id])).hexdigest()


def add_automatic_skill_draft(manager, *, authority):
    """Save one observed model draft through pinned History once-create, or no draft."""
    import base64
    import os
    import re
    import stat
    import uuid
    from core.atomic_io import atomic_write_text
    from src.openclank.history_capture import StrictTreeLimits
    from src.openclank.attachment_admission import admit_references, settle_references
    from .skill_draft_once import create_once
    from .skill_format import slugify
    from .skill_lifecycle import ensure_revision, locked
    from .skills import _build_creation_skill, _find_creation_duplicate
    if type(authority) is not _SkillDraftAgentAuthority or authority.mode != 'create':
        raise ValueError('automatic skill authority unavailable')
    authority.assert_current()
    session = authority.session
    if getattr(session, 'save_original', None) is not None:
        raise ValueError('automatic skill original requires status')
    fields = _automatic_fields(session.result.output['text'])
    if fields is None:
        return {'state': 'no_draft'}
    original = session.attempt['reservation_original']['original']
    selector = {'version': 1, 'sources': [{'chatID': item['source']['chat_id'],
        'rootMessageID': item['source']['message_id'], 'rootHash': item['source']['content_hash'],
        'parts': item['parts']} for item in original['ordered_sources']]}
    selector_bytes = _canonical(selector)
    if len(selector_bytes) > 24576:
        raise ValueError('automatic skill source unavailable')
    source_uri = 'data:application/vnd.openclank.conversation-selection+json;base64,' + base64.b64encode(selector_bytes).decode('ascii')
    name, category = slugify(fields['name'], fallback='skill'), slugify(fields['category'] or 'general', fallback='general')
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', category) or len(name) > 80 or len(category) > 80:
        raise ValueError('automatic skill destination unavailable')
    action_id = _automatic_action_id(authority.account, original['operation_id'])
    skill_id = str(uuid.uuid5(uuid.NAMESPACE_URL, action_id))
    root = os.path.abspath(manager.skills_root)
    if os.path.realpath(root) != root or not os.path.isdir(root):
        raise ValueError('automatic skill root unavailable')
    with locked(os.path.join(root, 'PROMOTIONS')):
        with locked(os.path.join(root, '.import')):
            authority.assert_current()
            manager._assert_memory_sources_available(authority.owner, (), source_uri)
            existing = manager.load_all()
            duplicate = _find_creation_duplicate(existing, owner=authority.owner, source='repeated-workflow-inference',
                name=name, description=fields['description'], title='', when_to_use=fields['when_to_use'],
                problem='', procedure=fields['procedure'], steps=[])
            if duplicate is not None:
                return {'state': 'no_draft'}
            if name in ({item['name'] for item in existing} | manager._reserved_skill_names()):
                raise ValueError('automatic skill destination unavailable')
            category_path = os.path.join(root, category)
            if os.path.lexists(category_path):
                info = os.lstat(category_path)
                if not stat.S_ISDIR(info.st_mode) or os.path.realpath(category_path) != category_path:
                    raise ValueError('automatic skill category unavailable')
                parent, child, prefix = category_path, name, ''
            else:
                parent, child, prefix = root, category, name + '/'
            skill = _build_creation_skill(name=name, skill_id=skill_id, owner=authority.owner,
                description=fields['description'], title='', version='1.0.0', category=category,
                tags=fields['tags'], platforms=[], requires_toolsets=[], fallback_for_toolsets=[], confidence=0.8,
                source='repeated-workflow-inference', source_uri=source_uri, source_revision=original['selection_sha256'],
                source_memory_ids=[], teacher_model=original['model_route_id'], when_to_use=fields['when_to_use'],
                problem='', procedure=fields['procedure'], steps=[], pitfalls=fields['pitfalls'],
                verification=fields['verification'], solution='')
            binding = {'version': 1, 'jobID': original['operation_id'], 'account': authority.account,
                'owner': authority.owner, 'skillID': skill_id, 'name': name, 'category': category,
                'fieldsSHA256': hashlib.sha256(_canonical(fields)).hexdigest(), 'lineage': authority.lineage,
                'sourceURIHash': hashlib.sha256(source_uri.encode()).hexdigest(), 'prefix': prefix, 'child': child}
            binding_sha = hashlib.sha256(_canonical(binding)).hexdigest()
            authority.plan = {'action_id': action_id, 'skill_id': skill_id, 'binding_sha256': binding_sha,
                'directory': parent, 'child': child, 'prefix': prefix, 'name': name,
                'source_revision': original['selection_sha256']}
            limits = StrictTreeLimits(32, 4, 1024 * 1024, 2 * 1024 * 1024, 4 * 1024 * 1024, 3.0)
            admitted = []
            def build(stage):
                authority.assert_current()
                admitted.append(admit_references(authority.owner, 'skills', skill.to_markdown()))
                dest = os.path.join(stage, name) if prefix else stage
                if prefix:
                    os.mkdir(dest)
                path = os.path.join(dest, 'SKILL.md')
                atomic_write_text(path, skill.to_markdown())
                state = {'published': None, 'automatic_draft': {'version': 1, 'action_id': action_id,
                    'binding_sha256': binding_sha, 'account_id': authority.account, 'skill_id': skill_id,
                    'initial_revision': 1, 'initial_hash': skill.content_hash, 'lineage': authority.lineage}}
                ensure_revision(skill, path, state=state)
                return skill
            try:
                result = create_once(manager, authority=authority, action_id=action_id, skill_id=skill_id,
                    category_dir=parent, name=child, binding_sha=binding_sha, tool_id='skill-draft',
                    stage_prefix='.automatic-skill-', limits=limits, build=build,
                    validate=lambda client, action, action_id, owner, account, **kw: _validate_automatic_completed(
                        client, action, authority.session.save_original, owner, account, **kw),
                    status_error='automatic skill original status required')
            except BaseException:
                if getattr(session, 'save_original', None) is not None:
                    session.state = 'save_unknown'
                raise
            if admitted:
                settle_references(admitted[0])
            session.state = 'save_complete'
            fact = {'actionID': action_id, 'skillID': skill_id, 'revision': 1, 'contentHash': result['initial_hash']}
            return {'state': 'draft', 'draftSHA256': hashlib.sha256(_canonical(fact)).hexdigest(), 'draft': fact}


def _validate_automatic_completed(client, action, original, owner, account, *, expected_manifest=None,
                                  expected_binding=None, expected_resource=None):
    import base64
    import re
    from .skill_format import Skill
    if type(original) is not dict:
        raise ValueError('automatic skill original unavailable')
    action_id, skill_id = original['action_id'], original['skill_id']
    before, after = action['before_resources'], action['after_resources']
    if (action['action_id'] != action_id or action['actor_id'] != 'agent:' + owner
            or action['actor_account_id'] != account or action['actor_kind'] != 'agent'
            or action['tool_id'] != 'skill-draft' or action['operation'] != 'create' or action['state'] != 'Complete'
            or type(before) is not list or len(before) != 1 or type(after) is not list or len(after) != 1):
        raise ValueError('automatic skill action unavailable')
    b, a = before[0], after[0]
    marker = b['metadata']['opaque']
    if (marker != {'version': 1, 'binding_sha256': original['binding_sha256']}
            or action['coverage']['metadata']['batch_once'] != marker
            or action['coverage']['metadata']['skill_draft'] != original['lineage']
            or expected_binding is not None and marker['binding_sha256'] != expected_binding
            or b['existence'] != 'Absent' or b['resource_type'] != 'Directory'
            or a['existence'] != 'Present' or a['resource_type'] != 'Directory'
            or a['resource_key'] != b['resource_key'] or b['resource_key'] != original['resource_key']
            or expected_resource is not None and b['resource_key'] != expected_resource):
        raise ValueError('automatic skill action changed')
    manifest = client.read_version(action_id, a['after'])
    if type(manifest) is not bytes or len(manifest) > 4 * 1024 * 1024 or expected_manifest is not None and manifest != expected_manifest:
        raise ValueError('automatic skill after-state unavailable')
    tree = json.loads(manifest)
    rows = tree.get('entries') if type(tree) is dict and tree.get('version') == 1 and tree.get('root_type') == 'directory' else None
    if type(rows) is not list or len(rows) > 32:
        raise ValueError('automatic skill tree unavailable')
    prefix = original['prefix']
    def read(path):
        found = [row for row in rows if type(row) is dict and row.get('path') == prefix + path and row.get('type') == 'file']
        if len(found) != 1:
            raise ValueError('automatic skill file unavailable')
        row = found[0]
        raw = base64.b64decode(row['content'], validate=True)
        if len(raw) > 1024 * 1024 or row.get('size') != len(raw) or hashlib.sha256(raw).hexdigest() != row.get('sha256'):
            raise ValueError('automatic skill file changed')
        return raw
    state = json.loads(read('_lifecycle.json'))
    witness = state['automatic_draft']
    head = read('SKILL.md')
    skill = Skill.from_markdown(head.decode('utf-8'))
    head_hash = witness['initial_hash']
    history = state['history']
    if (witness != {'version': 1, 'action_id': action_id, 'binding_sha256': original['binding_sha256'],
            'account_id': account, 'skill_id': skill_id, 'initial_revision': 1, 'initial_hash': head_hash,
            'lineage': original['lineage']} or not re.fullmatch(r'[0-9a-f]{64}', head_hash)
            or state['skill_id'] != skill_id or state['owner'] != owner or state['published'] is not None
            or state['head_revision'] != 1 or state['head_hash'] != head_hash or skill.skill_id != skill_id
            or skill.revision != 1 or skill.owner != owner or skill.name != original['name']
            or skill.status != 'draft' or skill.source != 'repeated-workflow-inference'
            or skill.source_revision != original['source_revision'] or skill.content_hash != head_hash
            or skill.compute_content_hash() != head_hash or type(history) is not list or len(history) != 1):
        raise ValueError('automatic skill lifecycle unavailable')
    row = history[0]
    if (row['skill_id'] != skill_id or row['owner'] != owner or row['revision'] != 1 or row['content_hash'] != head_hash
            or row['snapshot_sha256'] != hashlib.sha256(head).hexdigest() or read(row['snapshot']) != head):
        raise ValueError('automatic skill snapshot unavailable')
    return {'actionID': action_id, 'skillID': skill_id, 'revision': 1, 'contentHash': head_hash}


def automatic_skill_draft_status(*, authority):
    """Read the one retained original action only; never create or retry it."""
    import os
    if type(authority) is not _SkillDraftAgentAuthority or authority.mode != 'read':
        raise ValueError('automatic skill status unavailable')
    authority.assert_current()
    original = authority.session.save_original
    fd = os.open(original['directory'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    scope = None
    try:
        info = os.fstat(fd)
        scope = authority._history('open_read', original['directory'], (info.st_dev, info.st_ino), fd, original['child'], None)
        action = scope['client'].get_capture_action(original['action_id'])['Action']
        fact = _validate_automatic_completed(scope['client'], action, original, authority.owner, authority.account)
        authority.assert_current()
        return {'state': 'complete', 'draftSHA256': hashlib.sha256(_canonical(fact)).hexdigest(), 'draft': fact}
    except Exception:
        return {'state': 'unknown'}
    finally:
        try:
            if scope is not None:
                authority._history('close', None, None, None, None, scope)
        finally:
            os.close(fd)
