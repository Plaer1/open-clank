"""Bounded normal-auth Treehouse journeys against an actual installed server.

HTTP journeys use no application imports, database access or auth bypass.
The separately labelled offline packaged-child storage proof uses only its own
temporary synthetic database. No fabricated focus receipts or live user data.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import secrets
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CHECK = 'installed-treehouse-normal-auth-tutorial-quest-and-resource-closure'
DETAILED_CHECK = 'installed-treehouse-private-quest-review-correction-and-global-self-report'
RESTART_CHECK = 'installed-treehouse-learning-and-stats-persist-after-restart'
PREFIX = '/api/copal/treehouse'
ROOT_IDS = {'treehouse:programming', 'treehouse:games', 'treehouse:ai', 'treehouse:openclank'}


def qualify_storage_activation(resource_root: Path, private_python: Path, work: Path) -> dict:
    """Offline packaged-child proof; never connects to the running app's DB."""
    program = r'''
import json,sqlite3,subprocess,sys
from pathlib import Path
root,work=map(Path,sys.argv[1:]);sys.path.insert(0,str(root))
from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.openclank.copal_treehouse import new_treehouse_state
from src.openclank.treehouse_field_guide import instantiate_field_guide
from src.openclank.treehouse_storage_activation import readonly,logical_snapshot
from src.openclank.treehouse_global_catalogue import load_global_catalogue,GlobalLearning
db=work/'legacy.sqlite3';repo=TreeHouseRepository(db)
state=instantiate_field_guide(new_treehouse_state('packaged-synthetic-owner'),'packaged-synthetic-owner')
for unit in state['activities'].values():unit['content']+='\nSynthetic edited legacy fixture.'
repo.put_catalogue('packaged-synthetic-owner','fixture',state,expected_revision=0)
with sqlite3.connect(db) as connection:
 for suffix in ('progress','idempotency','reset_generations','completions'):connection.execute('DROP TABLE treehouse_global_'+suffix)
with readonly(db) as connection:before=logical_snapshot(connection)
repo=TreeHouseRepository(db)
assert repo.global_storage_status()['status']=='activation-required'
with readonly(db) as connection:assert logical_snapshot(connection)==before
def command(*args):
 subprocess.run([sys.executable,'-I','-B',str(root/'scripts/activate_treehouse_storage.py'),*args],check=True,stdout=subprocess.DEVNULL,timeout=60)
command('preview','--db',str(db),'--output',str(work/'preview.json'))
command('apply','--db',str(db),'--preview',str(work/'preview.json'),'--backup',str(work/'backup.sqlite3'),'--writers-stopped','--output',str(work/'receipt.json'))
command('verify','--db',str(db),'--output',str(work/'verify.json'))
receipt=json.loads((work/'receipt.json').read_text());verified=json.loads((work/'verify.json').read_text())
assert verified['integrity']=='ok' and verified['logicalDigest']==receipt['outputDigest'] and verified['availability']['status']=='ready'
command('apply','--db',str(db),'--preview',str(work/'preview.json'),'--backup',str(work/'never-created.sqlite3'),'--writers-stopped','--output',str(work/'replay.json'))
assert json.loads((work/'replay.json').read_text())==dict(receipt,replayed=True)
assert not (work/'never-created.sqlite3').exists()
catalogue=load_global_catalogue();g=GlobalLearning(catalogue,repo,'packaged-synthetic-learner','fixture')
assert len(g.summary()['mainQuests'])==4
cid='ai:models';uid=catalogue.subject_metadata(cid)['unitIds'][0];aid=catalogue.unit(cid,uid)['assignmentIds'][0]
g.command({'type':'submission.draft','commandId':'packaged-offline-copy-draft','expectedRevision':0,'payload':{'courseId':cid,'sourceRef':g.source_ref(cid),'packageRevision':catalogue.revision,'resetGeneration':0,'assignmentId':aid,'assignmentRequirementDigest':catalogue.requirements(aid)['requirementDigest'],'answer':'Disposable package storage rehearsal only; no earned-work claim.'}})
with readonly(db) as connection:
 after=logical_snapshot(connection)
 assert all(item in after[1:] for item in before[1:])
 assert set(map(tuple,before[0][1])).issubset(set(map(tuple,after[0][1])))
 for (raw,) in connection.execute('SELECT state_json FROM treehouse_catalogues'):
  assert all(not c.get('retiredAt') for c in json.loads(raw)['courses'].values())
 # Same fixture, explicit incompatible shape; never a user DB.
with sqlite3.connect(db) as connection:connection.execute('ALTER TABLE treehouse_global_progress RENAME COLUMN revision TO invalid_revision')
assert repo.global_storage_status()['status']=='incompatible'
command('preview','--db',str(db),'--operation-id','bad-shape','--output',str(work/'bad-preview.json'))
bad=subprocess.run([sys.executable,'-I','-B',str(root/'scripts/activate_treehouse_storage.py'),'apply','--db',str(db),'--preview',str(work/'bad-preview.json'),'--backup',str(work/'bad-backup'),'--writers-stopped'],capture_output=True,timeout=60)
assert bad.returncode!=0 and not (work/'bad-backup').exists()
print(json.dumps({'proof':'offline-packaged-child','missingAllFourBeforeActivation':True,'fiveEditedCoursesAndAllOldSchemaRowsUnchanged':len(state['courses'])==5,'replay':True,'fourRootReadAndDraft':True,'incompatibleRefused':True,'packageDigest':catalogue.manifest['packageDigest']}))
'''
    result = subprocess.run([str(private_python), '-I', '-B', '-c', program, str(resource_root.resolve()), str(work.resolve())],
                            check=True, capture_output=True, text=True, timeout=180)
    return json.loads(result.stdout)


def verify_public_resources(journey, resource_root: Path, manifest: dict):
    canonical_path = Path(__file__).resolve().parents[1] / 'src/openclank/treehouse_curriculum/manifest.json'
    canonical = json.loads(canonical_path.read_text(encoding='utf-8'))
    identity = ('sourceId', 'packageRevision', 'packageDigest')
    if any(not isinstance(canonical.get(key), str) or not canonical[key]
           or manifest.get(key) != canonical[key] for key in identity):
        raise RuntimeError('Installed curriculum identity differs from the qualification source manifest')
    expected = canonical.get('resources')
    records = manifest.get('resources')
    if (not isinstance(expected, list) or not expected or records != expected
            or any(not isinstance(row, dict) or not isinstance(row.get('id'), str)
                   or not isinstance(row.get('path'), str) for row in expected)
            or len({row['id'] for row in expected}) != len(expected)
            or len({row['path'] for row in expected}) != len(expected)):
        raise RuntimeError('Installed public resource register differs from the canonical source descriptors')
    total = 0
    for descriptor in records:
        path = resource_root / descriptor['path']
        status, headers, body = journey.raw('/' + descriptor['path'])
        if (status != 200 or len(body) != descriptor['bytes'] or body != path.read_bytes()
                or 'sha256:' + hashlib.sha256(body).hexdigest() != descriptor['digest']
                or headers.get_content_type() != descriptor['mime']):
            raise RuntimeError('Installed Treehouse public fixture byte/MIME descriptor differs: ' + descriptor['id'])
        total += len(body)
    return {'count': len(records), 'bytes': total,
            'source_manifest_package_revision': canonical['packageRevision'],
            'source_manifest_package_digest': canonical['packageDigest']}


def qualify_global_response(journey):
    """Current shared conceptual work, honestly submitted without a grader."""
    j = journey
    course, unit = 'ai:models', 'ai:models:identity'
    path = (PREFIX + '/courses/' + urllib.parse.quote(course, safe='') + '/activities/'
            + urllib.parse.quote(unit, safe='') + '?source=system&sourceId=openclank%3Atreehouse-core')
    before = j.request(path, opener=j.learner)
    assignment = next(a for a in before['assignments'] if a['id'] == 'ai:models:assessment:identity')
    aid = assignment['id']
    if assignment.get('completionEvidence') != 'response' or assignment.get('graded') is not False or aid in before['submissions']:
        raise RuntimeError('Global conceptual qualification requires a fresh ungraded response')
    reread = j.request(path, opener=j.learner)
    if (reread['progress'] != before['progress'] or reread['progressRevision'] != before['progressRevision']
            or aid not in before['progress']['missingAssignmentIds']):
        raise RuntimeError('Reading a global lesson incorrectly earned work')
    owner_before = j.request(path)
    payload = {'courseId': course, 'sourceRef': before['sourceRef'], 'packageRevision': before['packageRevision'],
               'resetGeneration': before['resetGeneration'], 'assignmentId': aid,
               'assignmentRequirementDigest': assignment['requirementDigest']}
    envelope = {'type': 'activity.complete', 'commandId': 'global-click-' + str(uuid.uuid4()),
                'expectedRevision': before['progressRevision'], 'payload': {**payload, 'activityId': unit}}
    j.denied(PREFIX + '/commands', envelope, status=403, opener=j.learner)
    response = ('The blueprint defines the architecture; a checkpoint contains learned weights; '
                'the website is the application interface and harness. Changing its system prompt, '
                'retrieval or enabled tools can change behavior without changing the weights.')
    envelope = {**envelope, 'type': 'submission.submit', 'commandId': 'global-response-' + str(uuid.uuid4()),
                'payload': {**payload, 'answer': response}}
    result = j.request(PREFIX + '/commands', envelope, opener=j.learner)
    if result.get('ok') is not True: raise RuntimeError('Global response did not commit')
    after = j.request(path, opener=j.learner)
    submitted = after['submissions'].get(aid, {})
    if (submitted.get('status') != 'submitted' or submitted.get('answer') != response
            or submitted.get('answerRequirementDigest') != assignment['requirementDigest']
            or submitted.get('gradedAt') or aid in after['progress']['missingAssignmentIds']):
        raise RuntimeError('Global response evidence/progress was not honest submitted self-report')
    replay = j.request(PREFIX + '/commands', envelope, opener=j.learner)
    reread = j.request(path, opener=j.learner)
    if (replay.get('replayed') is not True or replay.get('changed') is not False
            or reread['progressRevision'] != after['progressRevision'] or reread['submissions'] != after['submissions']):
        raise RuntimeError('Global response exact replay changed learner evidence')
    owner_after = j.request(path)
    if owner_after['submissions'] != owner_before['submissions'] or owner_after['progress'] != owner_before['progress']:
        raise RuntimeError('Global response leaked into another account')
    return {'assignmentId': aid, 'evidence': 'submitted-self-report', 'reviewedOrAutomaticMastery': False,
            'readingEarnedNoWork': True, 'clickCompletionDenied': True, 'exactReplay': True, 'accountIsolation': True}


class Journey:
    """Private in-memory account references; receipt contains no cookies/IDs."""
    def __init__(self, base, opener):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port in {7777, 7796, 7797}:
            raise RuntimeError('Treehouse qualification requires an isolated loopback fixture')
        self.base, self.opener = base, opener
        self.receipt = {'checks': [], 'coverage': 'basic', 'resource_count': 0}
        self.persisted = None

    def raw(self, path, body=None, *, opener=None, method=None, headers=None):
        selected = opener or self.opener
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=payload, method=method,
                                         headers={'Origin': self.base, 'Content-Type': 'application/json', **(headers or {})})
        with selected.open(request, timeout=30) as response:
            data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise RuntimeError('Treehouse fixture response exceeded its bound')
            return response.status, response.headers, data

    def request(self, path, body=None, **kwargs):
        return json.loads(self.raw(path, body, **kwargs)[2])

    def snapshot(self, opener=None):
        return self.request(PREFIX, opener=opener)

    def command(self, kind, payload, opener=None):
        snapshot = self.snapshot(opener)
        result = self.request(PREFIX + '/commands', {
            'type': kind, 'commandId': 'native-qualification-' + str(uuid.uuid4()),
            'expectedRevision': snapshot['state']['revision'], 'payload': payload}, opener=opener)
        if result.get('ok') is not True:
            raise RuntimeError('Treehouse public command did not succeed: ' + kind)
        return result

    def denied(self, path, body=None, *, status, opener=None):
        try:
            self.raw(path, body, opener=opener)
        except urllib.error.HTTPError as error:
            if error.code == status:
                return
            raise RuntimeError('Treehouse denial status differs from expected') from None
        raise RuntimeError('Treehouse allowed a forbidden fixture operation')

    @staticmethod
    def progress(snapshot, course):
        return snapshot['projection']['learners'][snapshot['accountId']]['courses'][course]

    def assert_complete(self, opener, course, expected):
        value = self.progress(self.snapshot(opener), course)
        if value.get('complete') is not expected:
            raise RuntimeError('Tutorial/Quest completion boundary differs from expected')

    @staticmethod
    def restart_state(snapshot):
        state = dict(snapshot['state'])
        # aggregate_visible_state builds these two read timestamps afresh on
        # every GET. Exact native same-read/restart diagnosis binds this
        # exception; all nested timestamps and business records stay strict.
        created, updated = state['createdAt'], state['updatedAt']
        for field in ('createdAt', 'updatedAt'):
            value = state.pop(field)
            if not isinstance(value, str) or not value:
                raise RuntimeError('Treehouse aggregate read timestamp is malformed')
        profile = state['profiles'].get(snapshot['accountId'])
        # An author with no persisted progress has an ensure_account_profile
        # timestamp derived from this fresh aggregate. Persisted timestamps
        # never match this condition and remain part of the exact comparison.
        synthesized_profile = bool(isinstance(profile, dict)
                                   and profile.get('createdAt') == created == updated)
        if synthesized_profile:
            state['profiles'] = dict(state['profiles'])
            current = dict(profile)
            current.pop('createdAt')
            state['profiles'][snapshot['accountId']] = current
        # Include the classification: synthesized -> persisted is a change,
        # even if removing the read-only timestamp made other values equal.
        return state, synthesized_profile

    def capture(self):
        owner, learner = self.snapshot(), self.snapshot(self.learner)
        self.persisted = (self.restart_state(owner), self.restart_state(learner), self.request(PREFIX + '/stats'))

    def verify_restart(self):
        if self.persisted is None:
            raise RuntimeError('Treehouse journey did not capture a restart checkpoint')
        if getattr(self, 'file_proof', None):
            self.verify_file_proof()
            self.receipt['checks'].append('installed-treehouse-cold-instructor-file-after-restart')
        current = (self.restart_state(self.snapshot()), self.restart_state(self.snapshot(self.learner)), self.request(PREFIX + '/stats'))
        if current != self.persisted:
            raise RuntimeError('Installed Treehouse learning/stats changed across restart')
        self.receipt['checks'].append(RESTART_CHECK)
        self.receipt['restart_comparison'] = {
            'excluded_synthesized_aggregate_fields': [
                'owner.state.createdAt', 'owner.state.updatedAt',
                'learner.state.createdAt', 'learner.state.updatedAt',
            ],
            'conditional_synthesized_profile_fields': [
                label + '.state.profiles.<current-account>.createdAt'
                for label, captured in zip(('owner', 'learner'), self.persisted[:2])
                if captured[1]
            ],
            'profile_condition': 'createdAt-equals-both-fresh-aggregate-timestamps',
            'synthesized_or_persisted_classification': 'exact-across-restart',
            'nested_state': 'exact-except-listed-synthesized-fields',
            'full_stats': 'exact',
        }

    def verify_file_proof(self):
        path, expected = self.file_proof
        status, headers, body = self.raw(path)
        if (status != 200 or body != expected or headers.get('X-Content-Type-Options') != 'nosniff'
                or headers.get('Cache-Control') != 'private, no-store'
                or not headers.get('Content-Disposition', '').startswith('attachment;')):
            raise RuntimeError('Cold instructor file evidence differs')
        status, _, body = self.raw(path, method='HEAD')
        if status != 200 or body:
            raise RuntimeError('Instructor HEAD differs')
        status, _, body = self.raw(path, headers={'Range': 'bytes=0-7'})
        if status != 206 or body != expected[:8]:
            raise RuntimeError('Instructor range differs')
        self.denied(path, status=403, opener=self.learner)
        self.denied(path, status=401, opener=urllib.request.build_opener())
        self.denied(path.split('?')[0] + '?attemptId=wrong-native-attempt', status=409)


def qualify_treehouse(base: str, opener, resource_root: Path, *, detailed: bool = False) -> Journey:
    journey = Journey(base, opener)
    j = journey
    anonymous = urllib.request.build_opener()
    j.denied(PREFIX, status=401, opener=anonymous)
    initial = j.snapshot()
    catalogue = initial.get('globalCatalogue', {})
    roots = catalogue.get('mainQuests', [])
    if len(roots) != 4 or {row.get('id') for row in roots} != ROOT_IDS or initial.get('globalAvailability', {}).get('status') != 'ready':
        raise RuntimeError('Installed Treehouse did not expose exactly four available global Main Quests')
    if initial['state']['courses']:
        raise RuntimeError('Fresh qualification account unexpectedly owns private default courses')
    for row in roots:
        detail = j.request(PREFIX + '/main-quests/' + urllib.parse.quote(row['id'], safe=''))
        if not detail.get('subjects'):
            raise RuntimeError('Global Main Quest has no subject projection')
    j.receipt['checks'].append('four-global-main-quests-with-no-private-defaults')
    stats = j.request(PREFIX + '/stats')
    if stats.get('status') != 'partial' or not isinstance(stats.get('coverageStart'), (int, float)):
        raise RuntimeError('Fresh installed Treehouse stats were not initialized')
    j.receipt['checks'].append('fresh-installed-treehouse-stats-initialized')
    # A real registered, separately authenticated non-admin account.
    username, password = 'nativelearner' + secrets.token_hex(4), secrets.token_urlsafe(24)
    if j.request('/api/auth/users', {'username': username, 'password': password, 'is_admin': False}).get('ok') is not True:
        raise RuntimeError('Fixture learner creation failed')
    jar = http.cookiejar.CookieJar()
    j.learner = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    if j.request('/api/auth/login', {'username': username, 'password': password}, opener=j.learner).get('ok') is not True:
        raise RuntimeError('Fixture learner normal login failed')
    password = None
    j.snapshot(j.learner)

    root = resource_root.resolve()
    files = sorted((root / 'static/js/copal').glob('treehouse*'))
    required = {'treehouse.js', 'treehouseLearner.js', 'treehouseAssessment.js', 'treehouseTopicWeb.js', 'treehouseLearning.css'}
    if not required.issubset({p.name for p in files}):
        raise RuntimeError('Installed Treehouse JS/CSS resource closure is incomplete')
    files += [root / 'static/icons/treehouse-achievements/manifest.json',
              root / 'static/icons/treehouse-achievements/fallback.svg',
              root / 'static/icons/treehouse-achievements/mascot-kit.zip']
    manifest = json.loads((root / 'src/openclank/treehouse_curriculum/manifest.json').read_text(encoding='utf-8'))
    j.receipt['public_fixtures'] = verify_public_resources(j, root, manifest)
    j.receipt['checks'].append('all-canonical-public-resource-bytes-and-MIME-match-source-bound-descriptors')
    private_python = root / 'python/bin/python3'
    if private_python.is_file():
        with tempfile.TemporaryDirectory(prefix='openclank-packaged-treehouse-storage-') as work:
            j.receipt['storage_activation'] = qualify_storage_activation(root, private_python, Path(work))
        j.receipt['checks'].append('offline-packaged-child-true-legacy-schema-only-activation')
    files += [root / asset['path'] for asset in manifest.get('assets', [])]
    for path in files:
        relative = path.relative_to(root).as_posix()
        status, headers, body = j.raw('/' + relative)
        if status != 200 or hashlib.sha256(body).digest() != hashlib.sha256(path.read_bytes()).digest():
            raise RuntimeError('Installed Treehouse served resource differs: ' + relative)
        if path.suffix in {'.svg', '.webp'} and headers.get_content_type() != {'svg': 'image/svg+xml', 'webp': 'image/webp'}[path.suffix[1:]]:
            raise RuntimeError('Installed Treehouse artwork MIME differs')
    j.receipt['resource_count'] = len(files)
    docs = j.request('/api/copal/official/docs')['articles']
    article = next(row for row in docs if row.get('docId') == 'openclank-docs-treehouse')
    if article.get('read_only') is not True:
        raise RuntimeError('Installed shared Treehouse handbook is not read-only')
    gallery = j.request(PREFIX + '/achievements')
    if not gallery.get('entries') or not isinstance(gallery.get('normalTotal'), int):
        raise RuntimeError('Installed achievement gallery response is empty')

    def create(course_type, *, work=False):
        course = j.command('course.create', {'title': 'Native package ' + course_type, 'courseType': course_type})['result']['courseId']
        module = j.command('module.create', {'courseId': course, 'title': 'Installed journey'})['result']['moduleId']
        lesson = j.command('activity.create', {'moduleId': module, 'title': 'Actual Markdown lesson',
            'activityType': 'markdown', 'content': '# Installed learning\nRead this actual packaged lesson.', 'status': 'published'})['result']['activityId']
        assignment = None
        if work:
            assignment = j.command('assignment.create', {'moduleId': module, 'title': 'Required evidence',
                'assessmentType': 'file', 'passPercent': 60})['result']['assignmentId']
            j.command('assignment.publish', {'assignmentId': assignment})
        # Current draft preview and acknowledgment are real producer APIs.
        preview = j.request(PREFIX + '/courses/' + urllib.parse.quote(course, safe='') + '/learner-preview')
        j.request(PREFIX + '/courses/' + urllib.parse.quote(course, safe='') + '/learner-preview',
                  {'visible': True, 'revision': preview['preview']['revision'], 'accountId': preview['accountId']})
        j.command('course.publish', {'courseId': course})
        shared = j.command('course.share', {'courseId': course, 'recipientId': username, 'capability': 'learn'})['result']
        j.command('course.accept_share', {'shareToken': shared['shareToken']}, j.learner)
        j.command('enrollment.enroll', {'courseId': course}, j.learner)
        j.command('course.open', {'courseId': course, 'activityId': lesson}, j.learner)
        j.command('activity.complete', {'courseId': course, 'activityId': lesson}, j.learner)
        return course, assignment

    tutorial, _ = create('tutorial')
    j.assert_complete(j.learner, tutorial, True)
    quest, assignment = create('quest', work=True)
    j.assert_complete(j.learner, quest, False)
    j.receipt['checks'].append(CHECK)
    if detailed:
        j.receipt['coverage'] = 'detailed'
        evidence = '# Package qualification\nActual learner bytes.\n'
        document = j.request('/api/copal/documents', {'name': 'Native learning evidence.md', 'kind': 'markdown',
            'content': evidence, 'corpus': 'notes', 'actionId': str(uuid.uuid4())}, opener=j.learner)
        snapshot = j.snapshot(j.learner)
        resource = j.request('/api/files-v1/resolve-resource', {'resource_key': {'provider': 'copal',
            'resource_id': document['doc']['id'], 'account_id': snapshot['accountId'], 'workspace_id': snapshot['workspace']}}, opener=j.learner)
        state = snapshot['state']
        grant = next(row for row in state['courseGrants'].values() if row['courseId'] == quest and row['recipientId'] == snapshot['accountId'])
        generation = j.request('/api/files-v1/roots', opener=j.learner)['policy_generation']
        prepared = j.request('/api/files-v1/attachments/prepare', {
            'operation_id': 'native-file-' + str(uuid.uuid4()), 'generation': generation, 'mode': 'link',
            'source': {'resource_ref': resource['ref'], 'expected_revision': resource['revision']},
            'target': {'kind': 'treehouse_submission', 'course_id': quest, 'assignment_id': assignment,
                'expected_revision': {'kind': 'treehouse', 'value': json.dumps({'grantRevision': grant['revision'],
                'catalogueRevision': state['courses'][quest]['curriculumRevision']})}}}, opener=j.learner)
        answer = {'fileReceipts': [{'operationId': prepared['operation_id'],
            'preparationReceiptId': prepared['preparation_receipt_id'], 'name': prepared['insertion']['label']}]}
        submission = j.command('submission.submit', {'courseId': quest, 'assignmentId': assignment, 'answer': answer}, j.learner)['result']['submissionId']
        j.assert_complete(j.learner, quest, False)
        attempt = j.snapshot(j.learner)['state']['submissions'][submission]['attemptId']
        path = PREFIX + '/courses/' + urllib.parse.quote(quest, safe='') + '/submissions/' + urllib.parse.quote(submission, safe='') + '/files/' + urllib.parse.quote(prepared['operation_id'], safe='') + '?attemptId=' + urllib.parse.quote(attempt, safe='')
        j.file_proof = (path, evidence.encode())
        j.verify_file_proof()
        j.receipt['checks'].append('installed-treehouse-cold-instructor-file-head-range-and-denial')
        grade = {'courseId': quest, 'submissionId': submission, 'attemptId': attempt, 'score': 85}
        j.command('submission.grade', grade)
        j.assert_complete(j.learner, quest, True)
        j.denied(PREFIX + '/commands', {'type': 'submission.grade', 'commandId': str(uuid.uuid4()),
            'payload': grade}, status=403, opener=j.learner)
        j.command('submission.grade', {**grade, 'score': 30, 'correctionReason': 'Native correction qualification'})
        j.assert_complete(j.learner, quest, False)
        j.receipt['global_response'] = qualify_global_response(j)
        j.receipt['checks'].append(DETAILED_CHECK)
    return j
