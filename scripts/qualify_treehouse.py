"""Bounded normal-auth Treehouse journeys against an actual installed server.

No application imports, database access, auth bypass or fabricated focus receipts.
Only synthetic fixture accounts/data may be passed by the package qualifiers.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CHECK = 'installed-treehouse-normal-auth-tutorial-quest-and-resource-closure'
DETAILED_CHECK = 'installed-treehouse-quest-review-correction-and-marked-producer'
RESTART_CHECK = 'installed-treehouse-learning-and-stats-persist-after-restart'
PREFIX = '/api/copal/treehouse'


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
    if len(initial['state']['courses']) < 5 or not initial['state']['activities']:
        raise RuntimeError('Installed Treehouse did not provision its Field Guide')
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
    if len(files) != 18:
        raise RuntimeError('Installed Treehouse JS/CSS resource closure is incomplete')
    files += [root / 'static/icons/treehouse-achievements/manifest.json',
              root / 'static/icons/treehouse-achievements/fallback.svg',
              root / 'static/icons/treehouse-achievements/mascot-kit.zip',
              root / 'static/docs/media/treehouse-player-20261007/preview.webp',
              root / 'static/docs/media/treehouse-achievements-20261007/preview.webp']
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
        state = j.snapshot()['state']
        self_check = next(row for row in state['activities'].values() if row.get('completion') == 'self-check')
        j.command('activity.complete', {'courseId': self_check['courseId'], 'activityId': self_check['id']})
        marked = next(row for row in state['activities'].values() if row.get('fieldGuideKey') == 'house-stewardship.teach-a-class')
        result = j.command('activity.complete', {'courseId': marked['courseId'], 'activityId': marked['id']})
        event = next(row for row in result['state']['events'] if row['id'] == result['result']['eventId'])
        proof = event['data']
        if (proof.get('verifierVersion') != 'field-guide-producer-digest-v2'
                or proof.get('workBasis') != 'canonical-producer-receipts'
                or not proof.get('verificationSourceEventIds')):
            raise RuntimeError('Canonical mission did not carry marked producer proof')
        j.receipt['checks'].append(DETAILED_CHECK)
        j.receipt['marked_verifier'] = proof['verifierVersion']
    return j
