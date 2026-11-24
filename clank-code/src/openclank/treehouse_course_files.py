"""Readable course text files and bounded, selected-item hydration.

Metadata is sufficient for ownership, graph and progress calculations. Text is
read only for the requested lesson/work or an explicit course export.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from src.openclank.copal_errors import CopalBridgeError

TEXT_FIELDS = ('prompt', 'description', 'successConditions', 'failureFeedback',
               'hint', 'verificationLimit', 'action')
PRIVATE_TEXT_FIELDS = TEXT_FIELDS + ('questions', 'rubric', 'objectives')
MAX_CONTENT_BYTES = 2 * 1024 * 1024


def content_head(raw: bytes) -> str:
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def canonical_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


def source_name(descriptor: dict, *, suffix: str) -> str:
    """Pure lexical admission; never touch a potentially remote body path."""
    if not isinstance(descriptor, dict) or set(descriptor) != {'path', 'originDigest'}:
        raise ValueError('Invalid course content reference')
    name = descriptor['path']
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(p in {'', '.', '..'} for p in name.split('/'))
            or PurePosixPath(name).is_absolute() or Path(name).suffix != suffix):
        raise ValueError('Course content reference escapes source')
    digest = descriptor['originDigest']
    if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith('sha256:'):
        raise ValueError('Invalid course content origin digest')
    try:
        int(digest[7:], 16)
    except ValueError as exc:
        raise ValueError('Invalid course content origin digest') from exc
    return name


def source_path(root: Path, descriptor: dict, *, suffix: str) -> Path:
    """Selected read containment, never a caller-selected host path."""
    name = source_name(descriptor, suffix=suffix)
    root = Path(root).resolve()
    target = root / name
    # A later symlink substitution cannot turn a source reference into a grant.
    if any(part.is_symlink() for part in (target, *target.parents) if part != root.parent):
        raise ValueError('Symlink course content reference')
    if not target.resolve().is_relative_to(root):
        raise ValueError('Course content reference escapes source')
    return target


def source_key(identity: str) -> str:
    """Recognizable stable ID slug with a collision suffix, independent of title."""
    slug = re.sub(r'[^a-z0-9]+', '-', identity.lower()).strip('-')[:88].rstrip('-') or 'course'
    return slug + '-' + hashlib.sha256(identity.encode()).hexdigest()[:12]


def read_content(root: Path, descriptor: dict, *, suffix: str) -> tuple[str, str]:
    path = source_path(root, descriptor, suffix=suffix)
    with path.open('rb') as stream:
        raw = stream.read(MAX_CONTENT_BYTES + 1)
    if len(raw) > MAX_CONTENT_BYTES:
        raise ValueError('Course content file exceeds limit')
    return raw.decode('utf-8'), content_head(raw)


def split_course_definition(definition: dict, *, private: bool = False) -> tuple[dict, dict[str, bytes]]:
    """Extract exact strings; metadata remains a native work/progress definition."""
    value, files = copy.deepcopy(definition), {}
    course_id = next(iter(value['courses']))
    base = 'courses/' + source_key(course_id)

    def save(relative, raw):
        files[relative] = raw
        return {'path': relative, 'originDigest': content_head(raw)}

    for identity, activity in value['activities'].items():
        key = source_key(identity)
        activity['contentFile'] = save(f'{base}/lessons/{key}.md', activity.pop('content').encode('utf-8'))
        for index, section in enumerate(activity.get('optionalSections', [])):
            section['contentFile'] = save(f'{base}/lessons/{key}-optional-{index}.md', section.pop('content').encode('utf-8'))
    for identity, assignment in value['assignments'].items():
        key = source_key(identity)
        text = {field: assignment.pop(field) for field in (PRIVATE_TEXT_FIELDS if private else TEXT_FIELDS) if field in assignment}
        assignment['workFile'] = save(f'{base}/work/{key}.json', canonical_bytes(text))
    return value, files


def validate_content_references(root: Path, definition: dict, *, prefix: str = '') -> None:
    """Validate references without opening/statting every body."""
    paths = set()
    if len(definition['courses']) != 1:
        raise ValueError('One course source authority is required')
    course_id = next(iter(definition['courses']))
    allowed = prefix + 'courses/' + source_key(course_id) + '/'
    for kind, field, suffix in (('activities', 'contentFile', '.md'), ('assignments', 'workFile', '.json')):
        for item in definition[kind].values():
            descriptors = [item[field]]
            if kind == 'activities':
                if 'content' in item:
                    raise ValueError('Course content has two authorities')
                descriptors += [section['contentFile'] for section in item.get('optionalSections', [])]
            elif any(key in item for key in (PRIVATE_TEXT_FIELDS if prefix else TEXT_FIELDS)):
                raise ValueError('Course work has two authorities')
            for descriptor in descriptors:
                source_name(descriptor, suffix=suffix)
                if not descriptor['path'].startswith(allowed):
                    raise ValueError('Course content reference escapes declared course authority')
                if descriptor['path'] in paths:
                    raise ValueError('Duplicate course text path')
                paths.add(descriptor['path'])


def hydrate_activity(root: Path, item: dict) -> tuple[dict, dict]:
    value = copy.deepcopy(item)
    descriptor = value.pop('contentFile')
    value['content'], head = read_content(root, descriptor, suffix='.md')
    for section in value.get('optionalSections', []):
        section['content'], _ = read_content(root, section.pop('contentFile'), suffix='.md')
    return value, {'kind': 'course-files', 'contentHead': head, 'originDigest': descriptor['originDigest']}


def hydrate_assignment(root: Path, item: dict, *, private: bool = False, with_source: bool = False):
    value = copy.deepcopy(item)
    descriptor = value.pop('workFile')
    text, head = read_content(root, descriptor, suffix='.json')
    fields = json.loads(text)
    if not isinstance(fields, dict) or set(fields) - set(PRIVATE_TEXT_FIELDS if private else TEXT_FIELDS):
        raise ValueError('Invalid course work text file')
    if not private and not {'prompt', 'successConditions'}.issubset(fields):
        raise ValueError('Course work text is incomplete')
    for key, text in fields.items():
        if private and key in {'questions','rubric','objectives','successConditions'}:
            continue
        if key == 'successConditions':
            if not isinstance(text, list) or not text or not all(isinstance(s, str) and s.strip() for s in text):
                raise ValueError('Invalid course success conditions')
        elif not isinstance(text, str):
            raise ValueError('Invalid course work text')
    value.update(fields)
    if private:
        from src.openclank.treehouse_assessment import assessment_fields
        assessment_fields({}, value)
    if with_source:
        return value, {'kind':'course-files','contentHead':head,'originDigest':descriptor['originDigest']}
    return value


class CourseFilesCommitError(ValueError):
    """Known source commits survive a later staging/index failure."""
    def __init__(self, action_id, receipts, cause):
        super().__init__('Course source partly saved; retain the receipt and recheck')
        self.content_write = {'outcome':'committed-index-pending','partial':True,
            'receipt':{'actionId':action_id,'outcome':'partial'},
            'sourceReceipts':receipts,'reason':type(cause).__name__}


class PrivateCourseFiles:
    """Course-scoped adapter over existing Loose files, history and head CAS.

    Callers authorize course access first. Owner/workspace are trusted server
    identity, not parameters supplied by an Editor resource request.
    """

    def __init__(self, root: Path):
        from src.openclank.copal_loose import LooseCopalRepository
        self.loose = LooseCopalRepository(root, refresh_prefix="TreeHouse/Courses/")

    def _call(self, owner: str, workspace: str, operation: str, **args):
        return self.loose._call_sync(operation, {'owner': owner, 'workspace_id': workspace, **args})

    @staticmethod
    def prefix(pointer: dict) -> str:
        return 'TreeHouse/Courses/courses/' + source_key(pointer['courseId']) + '/'

    def contained(self, pointer: dict, relative: str) -> None:
        if not isinstance(relative, str) or not relative.startswith(self.prefix(pointer)):
            raise ValueError('Course source escapes declared course authority')
        suffix = Path(relative).suffix
        if suffix not in {'.md', '.json'}:
            raise ValueError('Invalid course source type')
        source_path(self.vault(pointer), {'path':relative,'originDigest':'sha256:'+'0'*64}, suffix=suffix)

    def metadata(self, pointer: dict, *, account_id: str, workspace: str) -> dict:
        if (pointer.get('format') != 'copal-course-files-v1' or pointer.get('accountId') != account_id
                or pointer.get('workspaceId') != workspace or not isinstance(pointer.get('owner'), str)):
            raise ValueError('Course source identity differs')
        self.contained(pointer, self.prefix(pointer) + 'course.json')
        record = self._call(pointer['owner'], workspace, 'get', id=pointer['documentId'])
        if record.get('name') != self.prefix(pointer) + 'course.json':
            raise ValueError('Course metadata document mapping differs')
        value = json.loads(record['text'])
        if not isinstance(value, dict) or set(value) != {'courses','modules','activities','assignments','skills','badges','quests'}:
            raise ValueError('Invalid private course metadata')
        cid = pointer['courseId']
        if set(value['courses']) != {cid} or value['courses'][cid].get('ownerId') != account_id:
            raise ValueError('Private course source ownership differs')
        validate_content_references(self.vault(pointer), value, prefix='TreeHouse/Courses/')
        for activity in value['activities'].values():
            references = [activity['contentFile'], *[s['contentFile'] for s in activity.get('optionalSections', [])]]
            if any(not ref['path'].startswith(self.prefix(pointer)) for ref in references):
                raise ValueError('Course source escapes declared course authority')
        if any(not item['workFile']['path'].startswith(self.prefix(pointer)) for item in value['assignments'].values()):
            raise ValueError('Course work escapes declared course authority')
        for kind in ('modules','activities','assignments','skills','badges','quests'):
            if any(item.get('courseId') != cid or key != item.get('id') for key,item in value[kind].items()):
                raise ValueError('Private course source has foreign entities')
        identities = [key for records in value.values() for key in records]
        if len(identities) != len(set(identities)):
            raise ValueError('Private course source has duplicate identities')
        from src.openclank.copal_treehouse import new_treehouse_state, validate_treehouse_state, TreeHouseError
        from src.openclank.copal_treehouse_contracts import validate_course_contract
        checked = new_treehouse_state(account_id)
        checked.update(value)
        try:
            validate_treehouse_state(checked)
            validate_course_contract(checked,cid,publishing=value['courses'][cid].get('status') == 'published')
        except TreeHouseError as exc:
            raise ValueError('Invalid private course source contract') from exc
        for module_id in value['courses'][cid].get('moduleIds', []):
            if module_id not in value['modules']:
                raise ValueError('Private course source has missing module')
        for module_id,module in value['modules'].items():
            for kind,field in (('activities','activityIds'),('assignments','assignmentIds')):
                if any(key not in value[kind] or value[kind][key].get('moduleId') != module_id for key in module.get(field, [])):
                    raise ValueError('Private source module ownership differs')
        for kind,field in (('activities','activityIds'),('assignments','assignmentIds')):
            for key,item in value[kind].items():
                module = value['modules'].get(item.get('moduleId'))
                if not module or key not in module.get(field, []):
                    raise ValueError('Private source work ownership differs')
        return value

    def vault(self, pointer: dict) -> Path:
        return self.loose._vault(pointer['owner'], pointer['workspaceId'])

    def selected_activity(self, pointer: dict, item: dict) -> tuple[dict, dict]:
        self.contained(pointer, item['contentFile']['path'])
        for section in item.get('optionalSections', []):
            self.contained(pointer, section['contentFile']['path'])
        return hydrate_activity(self.vault(pointer), item)

    def selected_assignment(self, pointer: dict, item: dict) -> dict:
        self.contained(pointer, item['workFile']['path'])
        return hydrate_assignment(self.vault(pointer), item, private=True)

    def selected_assignment_with_source(self, pointer: dict, item: dict) -> tuple[dict,dict]:
        self.contained(pointer, item['workFile']['path'])
        return hydrate_assignment(self.vault(pointer), item, private=True, with_source=True)

    def assignment_source(self, pointer: dict, item: dict) -> dict:
        self.contained(pointer, item['workFile']['path'])
        _, head = read_content(self.vault(pointer), item['workFile'], suffix='.json')
        return {'kind':'course-files', 'contentHead':head, 'originDigest':item['workFile']['originDigest']}

    @staticmethod
    def learning_definition(state: dict, cid: str) -> dict:
        value = {kind:{key:copy.deepcopy(item) for key,item in state[kind].items()
                       if key == cid or item.get('courseId') == cid}
                 for kind in ('courses','modules','activities','assignments','skills','badges','quests')}
        for course in value['courses'].values():
            for key in ('shareToken','contentAuthority','sourceMetadataHead','contentError','retiredAt','replacementMainQuestId'):
                course.pop(key, None)
        for activity in value['activities'].values():
            activity.pop('sourceAttachments', None)
        return value

    def commit_definition(self, pointer: dict, definition: dict, *, metadata_base: str,
                          content_bases: dict[str,str], action_id: str, actor_id: str) -> tuple[dict,dict]:
        """File commit first, derived index second; the caller reports pending.

        Expected text and metadata heads are checked before any write, then
        each selected native write also CASes its own actual file head.
        """
        previous = self.metadata(pointer, account_id=pointer['accountId'], workspace=pointer['workspaceId'])
        meta_path = self.prefix(pointer) + 'course.json'
        meta_doc = self.document(pointer, meta_path)
        if meta_doc['head'] != metadata_base:
            raise ValueError('source_metadata_conflict')
        if meta_doc.get('readOnly'):
            raise ValueError('source_read_only')
        value = copy.deepcopy(definition)
        plans = []
        for kind in ('activities','assignments'):
            for identity, item in value[kind].items():
                old = previous[kind].get(identity)
                field = 'contentFile' if kind == 'activities' else 'workFile'
                dirty = 'content' in item if kind == 'activities' else any(k in item for k in PRIVATE_TEXT_FIELDS)
                if not dirty:
                    if old and field not in item:
                        item[field] = copy.deepcopy(old[field])
                    continue
                if kind == 'activities':
                    raw = item.pop('content').encode('utf-8')
                else:
                    text = {k:copy.deepcopy(v) for k,v in (self.selected_assignment(pointer, old).items() if old else []) if k in PRIVATE_TEXT_FIELDS}
                    text.update({k:item.pop(k) for k in PRIVATE_TEXT_FIELDS if k in item})
                    raw = canonical_bytes(text)
                if old:
                    descriptor = old[field]
                    if self.document(pointer, descriptor['path']).get('readOnly'):
                        raise ValueError('source_read_only')
                    current_text, current_head = read_content(self.vault(pointer), descriptor, suffix='.md' if kind == 'activities' else '.json')
                    if content_bases.get(identity) != current_head:
                        raise ValueError('content_conflict')
                    relative = descriptor['path']
                else:
                    folder, suffix = ('lessons','.md') if kind == 'activities' else ('work','.json')
                    relative = self.prefix(pointer) + folder + '/' + source_key(identity) + suffix
                    descriptor = {'path':relative, 'originDigest':content_head(raw)}
                    current_head = None
                item[field] = copy.deepcopy(descriptor)
                plans.append((relative, raw, current_head))
        validate_content_references(self.vault(pointer),value,prefix='TreeHouse/Courses/')
        from src.openclank.copal_treehouse import new_treehouse_state, validate_treehouse_state
        from src.openclank.copal_treehouse_contracts import validate_course_contract
        checked = new_treehouse_state(pointer['accountId']); checked.update(value)
        validate_treehouse_state(checked)
        validate_course_contract(checked,pointer['courseId'],publishing=value['courses'][pointer['courseId']].get('status') == 'published')
        receipts = []
        def pending(reason):
            return value, {'outcome':'committed-index-pending','partial':True,'reason':reason,
                           'receipt':{'actionId':action_id,'outcome':'partial'},
                           'sourceReceipts':copy.deepcopy(receipts)}
        for relative, raw, base in plans:
            token = action_id + ':' + hashlib.sha256(relative.encode()).hexdigest()[:16]
            try:
                if base is None:
                    result = self._call(pointer['owner'], pointer['workspaceId'], 'create', name=relative,
                                        kind='markdown', content=raw.decode('utf-8'), action_id=token, actor_id=actor_id)
                    receipts.append({'actionId':token,'outcome':result.get('outcome','created')})
                else:
                    result = self.save_text(pointer, relative, raw.decode('utf-8'), base=base,
                                            action_id=token, actor_id=actor_id)
                    if result.get('outcome') != 'committed':
                        if receipts: return pending('Later source Save conflicted; earlier file receipts are committed')
                        raise ValueError('content_conflict' if result.get('outcome') == 'stale' else 'content_save_unresolved')
                    receipts.append({'actionId':token,'outcome':'committed'})
            except (OSError, ValueError, CopalBridgeError) as exc:
                if receipts: return pending(type(exc).__name__)
                raise
        # Guard the metadata against an intervening external edit even after
        # the text commit. Never replace the file with an old database body.
        try:
            result = self.save_text(pointer, meta_path, canonical_bytes(value).decode('utf-8'),
                                    base=content_head(meta_doc['text'].encode('utf-8')),
                                    action_id=action_id + ':metadata', actor_id=actor_id)
        except (OSError, CopalBridgeError) as exc:
            if receipts: return pending(type(exc).__name__)
            raise
        if result.get('outcome') != 'committed':
            if receipts: return pending('Metadata Save unresolved; earlier file receipts are committed')
            raise ValueError('source_metadata_conflict')
        return value, {'outcome':'committed', 'receipt':{'actionId':action_id,'outcome':'committed'},
                       'sourceReceipts':receipts,
                       'sourceMetadataHead':result['doc']['head'],
                       **({'contentHead':content_head(plans[-1][1])} if plans else {})}

    def document(self, pointer: dict, relative: str) -> dict:
        # This is an exact index lookup; never list/read every course body.
        self.contained(pointer, relative)
        result = self._call(pointer['owner'], pointer['workspaceId'], 'find_by_name', name=relative)
        if not isinstance(result, dict):
            raise ValueError('Course source document mapping is missing')
        return result

    def save_text(self, pointer: dict, relative: str, text: str, *, base: str, action_id: str, actor_id: str) -> dict:
        doc = self.document(pointer, relative)
        request = {'content':text, 'base':base, 'actionId':action_id}
        args = {'id':doc['id'], 'content':text, 'base':doc['head'], 'action_id':action_id,
                'actor_id':actor_id, 'document_write_request':request}
        replay = self._call(pointer['owner'], pointer['workspaceId'], 'write_receipt', **args)
        if replay.get('outcome') != 'missing':
            return replay
        current = content_head(doc['text'].encode('utf-8'))
        if base != current:
            return {'outcome':'stale', 'contentHead':current}
        return self._call(pointer['owner'], pointer['workspaceId'], 'write', id=doc['id'],
                          content=text, base=doc['head'], action_id=action_id, actor_id=actor_id,
                          document_write_request=request)

    def install(self, definition: dict, *, owner: str, account_id: str, workspace: str, action_id: str, with_receipts: bool = False, actor_id: str | None = None):
        """Stage one exact source before a separate catalogue authority switch.

        Existing files are not overwritten by conversion/replay. A differing
        visible file requires explicit recovery rather than a silent reseed.
        """
        cid = next(iter(definition['courses']))
        clean = copy.deepcopy(definition)
        for course in clean['courses'].values():
            for key in ('shareToken','contentAuthority','sourceMetadataHead','contentError','retiredAt','replacementMainQuestId'):
                course.pop(key, None)
        for activity in clean['activities'].values():
            activity.pop('sourceAttachments', None)
        metadata, files = split_course_definition(clean, private=True)
        prefix = 'TreeHouse/Courses/'
        # Rebase references to their actual ordinary Loose paths.
        def rebase(value):
            if isinstance(value, dict):
                if set(value) == {'path','originDigest'}:
                    value['path'] = prefix + value['path']
                else:
                    for item in value.values(): rebase(item)
            elif isinstance(value, list):
                for item in value: rebase(item)
        rebase(metadata)
        files = {prefix + path: raw for path,raw in files.items()}
        metadata_path = prefix + 'courses/' + source_key(cid) + '/course.json'
        files[metadata_path] = canonical_bytes(metadata)
        metadata_id = None
        receipts = []
        try:
            for name, raw in files.items():
                doc = self._call(owner, workspace, 'find_by_name', name=name)
                if doc:
                    if doc.get('text', '').encode('utf-8') != raw:
                        raise ValueError('Existing course source differs; preserve and resolve it')
                else:
                    key = hashlib.sha256(name.encode()).hexdigest()
                    result = self._call(owner, workspace, 'create', name=name, kind='markdown',
                                        content=raw.decode('utf-8'), action_id=action_id + ':' + key, actor_id=actor_id or account_id)
                    doc = result['doc']
                    receipts.append(result)
                if name == metadata_path:
                    metadata_id = doc['id']
        except (ValueError, OSError, CopalBridgeError) as exc:
            if receipts:
                raise CourseFilesCommitError(action_id,receipts,exc) from exc
            raise
        pointer = {'format':'copal-course-files-v1', 'accountId':account_id, 'owner':owner,
                   'workspaceId':workspace, 'courseId':cid, 'documentId':metadata_id}
        if with_receipts:
            return pointer, metadata, {'outcome':'committed','receipt':{'actionId':action_id,'outcome':'committed'},'sourceReceipts':receipts,'sourceMetadataHead':doc['head']}
        return pointer, metadata


FILE_AUTHOR_COMMANDS = {
    'course.create','course.update','course.publish','course.archive','course.delete','course.author.add','course.reorder_modules',
    'module.create','module.update','module.delete','module.reorder_items',
    'activity.create','activity.update','activity.delete',
    'assignment.create','assignment.update','assignment.publish','assignment.delete',
    'skill.create','skill.update','skill.delete','badge.create','badge.update','badge.delete','quest.create','quest.update','quest.delete'}

def install_private_definition(repo, state, cid, *, owner, account_id, workspace, action_id, actor_id=None):
    store = repo.course_source_store()
    definition = store.learning_definition(state, cid)
    pointer, metadata, outcome = store.install(definition, owner=owner, account_id=account_id, workspace=workspace, action_id=action_id, with_receipts=True, actor_id=actor_id or account_id)
    state.setdefault('_courseFileSources', {})[cid] = pointer
    state['_courseFilesEnabled'] = True
    apply_file_index(state, cid, metadata)
    state['courses'][cid]['contentAuthority'] = 'course-files'
    state['courses'][cid]['sourceMetadataHead'] = outcome['sourceMetadataHead']
    return outcome

def apply_file_index(state, cid, definition):
    for kind in ('courses','modules','activities','assignments','skills','badges','quests'):
        previous = state[kind]
        incoming = copy.deepcopy(definition[kind])
        for key,item in incoming.items():
            for field in ('shareToken','sourceAttachments','retiredAt','replacementMainQuestId'):
                if field in previous.get(key, {}): item[field] = copy.deepcopy(previous[key][field])
        state[kind] = {key:item for key,item in previous.items() if key != cid and item.get('courseId') != cid}
        state[kind].update(incoming)

def persist_file_authoring(repo, scope, previous, next_state, command, result):
    from src.openclank.copal_treehouse import TreeHouseError
    if command.type not in FILE_AUTHOR_COMMANDS:
        return None
    cid = command.payload.get('courseId') or result.get('courseId')
    if not cid:
        for key,kind in (('activityId','activities'),('assignmentId','assignments'),('moduleId','modules'),('skillId','skills'),('badgeId','badges'),('questId','quests')):
            identity = result.get(key) or command.payload.get(key)
            if identity in next_state[kind]:
                cid = next_state[kind][identity].get('courseId')
                break
    if not cid:
        return None
    pointer = previous.get('_courseFileSources', {}).get(cid)
    if not pointer:
        if command.type == 'course.create' and repo.course_files_enabled(previous):
            return install_private_definition(repo,next_state,cid,owner=scope['owner'],account_id=scope['account_id'],workspace=scope['workspace_id'],action_id=command.command_id,actor_id=scope.get('source_actor',scope['account_id']))
        return None
    if command.payload.get('metadataBase') != previous['courses'][cid].get('sourceMetadataHead'):
        raise TreeHouseError('Course source changed; keep this form and reload', code='source_metadata_conflict', status=409)
    owner = scope.get('_treehouse_owner') or scope['account_id']
    if not repo.access(scope['account_id'],scope['workspace_id'],owner,cid,'edit'):
        raise TreeHouseError('Course edit access changed', code='course_forbidden', status=403)
    if scope.get('_grant_id'):
        grant = repo.grant(scope['_grant_id'])
        if not grant or grant['revoked_at'] or int(grant['revision']) != int(scope['_grant_revision']):
            raise TreeHouseError('Course edit grant changed', code='stale_access', status=409)
    store = repo.course_source_store()
    definition = store.learning_definition(next_state,cid)
    bases = {}
    for kind,key,fields in (('activities','activityId',('content',)),('assignments','assignmentId',PRIVATE_TEXT_FIELDS)):
        identity = command.payload.get(key)
        if identity in definition[kind] and identity in previous[kind]:
            if any(field in command.payload for field in fields):
                bases[identity] = command.payload.get('contentBase')
            elif kind == 'assignments':
                # A selected quiz must be hydrated to validate publish,
                # but unchanged prompt/questions are not another write.
                for field in fields: definition[kind][identity].pop(field, None)
    try:
        metadata, outcome = store.commit_definition(pointer,definition,
            metadata_base=command.payload['metadataBase'], content_bases=bases,
            action_id=command.command_id,actor_id=scope.get('source_actor',scope['account_id']))
    except ValueError as exc:
        code = str(exc) if str(exc) in {'source_metadata_conflict','content_conflict','source_read_only'} else 'course_content_unavailable'
        raise TreeHouseError('Course source changed or needs repair; keep this form and reload', code=code, status=409) from exc
    if outcome['outcome'] == 'committed':
        apply_file_index(next_state,cid,metadata)
        next_state['courses'][cid]['contentAuthority'] = 'course-files'
        next_state['courses'][cid]['sourceMetadataHead'] = outcome['sourceMetadataHead']
    return outcome



def hydrate_private_command(repo, state, payload, kind):
    from src.openclank.copal_treehouse import TreeHouseError
    if kind in {'activity.complete','evidence.submit','evidence.review'}:
        item = (state.get('activities',{}).get(payload.get('activityId'),{}) if kind == 'activity.complete' else
            state.get('skills',{}).get(payload.get('skillId') or state.get('evidence',{}).get(payload.get('evidenceId'),{}).get('skillId'),{}))
        cid = item.get('courseId')
        if cid and state.get('_courseFileSources',{}).get(cid):
            if state['courses'][cid].get('contentError'):
                raise TreeHouseError('Course source needs repair',code='course_content_unavailable',status=409)
            state.setdefault('_validatedCourseSources',{})[cid] = state['courses'][cid].get('sourceMetadataHead')
    if kind == 'course.publish':
        cid = payload.get('courseId')
        pointer = state.get('_courseFileSources', {}).get(cid)
        if pointer:
            store = repo.course_source_store()
            state['_populatedLessonIds'] = [key for key,item in state['activities'].items()
                if item.get('courseId') == cid and item.get('status') == 'published' and not item.get('deletedAt')
                and store.selected_activity(pointer,item)[0].get('content','').strip()]
    aid = str(payload.get('assignmentId') or '')
    if kind == 'submission.grade':
        aid = state['submissions'].get(str(payload.get('submissionId') or ''), {}).get('assignmentId','')
    assignment = state['assignments'].get(aid)
    if assignment and kind in {'submission.submit','submission.draft','submission.grade','assignment.update','assignment.publish'}:
        cid = assignment['courseId']
        if state['courses'][cid].get('contentError'):
            raise TreeHouseError('Course source metadata needs repair',code='course_content_unavailable',status=409)
        pointer = state.get('_courseFileSources', {}).get(cid)
        if pointer:
            store = repo.course_source_store()
            selected, source = store.selected_assignment_with_source(pointer,assignment)
            if kind in {'submission.submit','submission.draft','submission.grade'}:
                if payload.get('workBase') != source['contentHead']:
                    raise TreeHouseError('Work source changed; keep your response and reopen the assignment',code='stale_work_source',status=409)
                if kind == 'submission.grade':
                    attempt = state['submissions'].get(str(payload.get('submissionId') or ''),{})
                    if attempt.get('workHead') and attempt['workHead'] != source['contentHead']:
                        raise TreeHouseError('This attempt used an earlier work source; review its original source history',code='stale_attempt_source',status=409)
            state['assignments'][aid] = selected
            if kind in {'submission.submit','submission.draft','submission.grade'}:
                state.setdefault('_validatedWorkHeads',{})[aid] = source['contentHead']
    activity = state['activities'].get(str(payload.get('activityId') or ''))
    if activity and kind == 'activity.complete':
        cid = activity['courseId']
        if state['courses'][cid].get('contentError'):
            raise TreeHouseError('Course source metadata needs repair',code='course_content_unavailable',status=409)
        pointer = state.get('_courseFileSources', {}).get(cid)
        if pointer: repo.course_source_store().selected_activity(pointer,activity)


def assert_private_evidence_current(repo, state, owner, workspace):
    from src.openclank.copal_treehouse import TreeHouseError
    heads = state.get('_validatedWorkHeads', {})
    courses = state.get('_validatedCourseSources',{})
    if not heads and not courses:
        return
    current, _ = repo.get_catalogue(owner, workspace)
    for cid, displayed in courses.items():
        course = (current or {}).get('courses',{}).get(cid,{})
        if course.get('contentError') or course.get('sourceMetadataHead') != displayed:
            raise TreeHouseError('Course metadata changed before evidence was saved',code='source_metadata_conflict',status=409)
    for aid, displayed in heads.items():
        assignment = state['assignments'][aid]
        cid = assignment['courseId']
        course = (current or {}).get('courses', {}).get(cid, {})
        if course.get('contentError') or course.get('sourceMetadataHead') != state['courses'][cid].get('sourceMetadataHead'):
            raise TreeHouseError('Course metadata changed before evidence was saved', code='source_metadata_conflict', status=409)
        pointer = current.get('_courseFileSources', {}).get(cid)
        try:
            _, source = repo.course_source_store().selected_assignment_with_source(pointer, current['assignments'][aid])
        except (ValueError, OSError, KeyError, TypeError, CopalBridgeError) as exc:
            raise TreeHouseError('Work source needs repair before evidence can be saved',code='course_content_unavailable',status=409) from exc
        if source['contentHead'] != displayed:
            raise TreeHouseError('Work source changed before evidence was saved; keep your response', code='stale_work_source', status=409)

