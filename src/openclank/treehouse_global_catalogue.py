"""Immutable first-party curriculum; native personal work, never owner copies."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from datetime import datetime, UTC
from fractions import Fraction
from pathlib import Path
from typing import Any

from src.openclank.copal_treehouse import (
    TreeHouseError, apply_treehouse_command, compute_treehouse_projections,
    new_treehouse_state, validate_treehouse_state,
)
from src.openclank.copal_treehouse_contracts import validate_course_contract
from src.openclank.treehouse_curriculum_validation import (
    ROOT_IDS, validate_fragments, validate_public_strings, validate_resources, validate_sources,
)
from src.openclank.treehouse_course_files import (
    content_head, hydrate_activity, hydrate_assignment, read_content, validate_content_references,
)

SOURCE_ID = 'openclank:treehouse-core'
KINDS = ('courses', 'modules', 'activities', 'assignments', 'skills', 'badges', 'quests')
PROGRESS_FIELDS = ('profiles', 'enrollments', 'submissions', 'evidence', 'events', 'processedCommands', 'progressResets')


def canonical_digest(value: Any) -> str:
    data = (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def assignment_requirement_digest(assignment: dict) -> str:
    return canonical_digest({'assignmentId': assignment['id'], 'unitId': assignment['unitId'],
                             'completionEvidence': assignment['completionEvidence'],
                             'requirementRevision': assignment['requirementRevision']})


def subject_requirements_digest(course: dict, assignments: dict) -> str:
    criteria = course['completionCriteria']
    required = sorted(criteria['assignmentIds'])
    return canonical_digest({'completionCriteria': {'activityIds': sorted(criteria['activityIds']),
        'assignmentIds': required, 'assignmentMode': criteria['assignmentMode']},
        'requirementRevisions': [{'id': k, 'requirementRevision': assignments[k]['requirementRevision'],
                                 'completionEvidence': assignments[k]['completionEvidence']} for k in required]})


def error(code: str, message: str, status: int = 409, **details):
    raise TreeHouseError(message, code=code, status=status, details=details)


class GlobalCatalogue:
    def __init__(self, package_root: str | Path, *, project_root: str | Path | None = None, manifest_path: str | Path = 'manifest.json'):
        self.path = Path(package_root).resolve()
        manifest_file = (self.path / manifest_path).resolve()
        if not manifest_file.is_relative_to(self.path):
            error('invalid_global_path','Index manifest escapes source root')
        self.manifest_path = manifest_file
        self.manifest = json.loads(manifest_file.read_text())
        m = self.manifest
        if m.get('format') != 'openclank-treehouse-global-v1' or m.get('schemaVersion') != 1 or m.get('sourceId') != SOURCE_ID:
            error('invalid_global_package', 'Unsupported system curriculum package')
        if m.get('packageDigest') != canonical_digest({k: v for k, v in m.items() if k != 'packageDigest'}):
            error('global_package_digest_mismatch', 'System curriculum manifest changed')
        if set(m.get('mainQuests', {})) != ROOT_IDS or not isinstance(m.get('packageRevision'), str):
            error('invalid_global_roots', 'System curriculum needs exactly Programming, Games, AI and Open Clank')
        self.source_id, self.revision = SOURCE_ID, m['packageRevision']
        self.course_files = m.get('contentFormat') == 'course-files-v1'
        self.active_root = False
        if m.get('contentFormat') not in (None, 'course-files-v1'):
            error('invalid_global_package', 'Unsupported course file format')
        self._states, self._projection_states = {}, {}
        ids = set()
        for course_id, metadata in m['subjects'].items():
            relative = Path(metadata['definitionPath'])
            path = (self.path / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(self.path):
                error('invalid_global_path', 'Definition escapes package')
            raw = path.read_bytes()
            if metadata.get('definitionDigest') != 'sha256:' + hashlib.sha256(raw).hexdigest():
                error('global_package_digest_mismatch', 'Subject content changed', courseId=course_id)
            definitions = json.loads(raw)
            if self.course_files:
                try:
                    validate_content_references(self.path, definitions)
                except (ValueError, TypeError, KeyError) as exc:
                    error('invalid_global_definition', str(exc), courseId=course_id)
            if set(definitions) != set(KINDS) or set(definitions['courses']) != {course_id}:
                error('invalid_global_definition', 'Subject must contain only native definition maps')
            state = new_treehouse_state('system curriculum')
            state['profiles'] = {}
            for kind in KINDS:
                if not isinstance(definitions[kind], dict):
                    error('invalid_global_definition', 'Definition map is not an object')
                for key, item in definitions[kind].items():
                    if key in ids or item.get('id') != key or any(f in item for f in ('ownerId', 'authorIds', 'profiles', 'shareToken', 'sourceAttachments')):
                        error('invalid_global_definition', 'Duplicate ID or private authority in system definition')
                    ids.add(key)
                    if kind != 'courses' and item.get('courseId') != course_id:
                        error('invalid_global_ownership', 'Foreign work in subject')
                state[kind] = definitions[kind]
            course = state['courses'][course_id]
            if course.get('courseType') != 'quest' or course.get('learningWebVersion') != 1 or course.get('freeExploration') is not True or course.get('status') != 'published':
                error('invalid_global_definition', 'Subjects need published freely explorable native Quests')
            if course.get('completionCriteria') != metadata.get('completionCriteria') or course.get('curriculumRevision') != metadata.get('curriculumRevision'):
                error('global_summary_mismatch', 'Summary does not match native work authority')
            for assignment in state['assignments'].values():
                if type(assignment.get('promptInBody', False)) is not bool:
                    error('invalid_global_work', 'promptInBody must be boolean')
                if assignment.get('completionEvidence') not in {'response', 'observed-success'} or type(assignment.get('requirementRevision')) is not int or assignment['requirementRevision'] < 1:
                    error('invalid_global_work', 'Work needs reviewed semantic revision and evidence kind')
                if assignment.get('requirementDigest') != assignment_requirement_digest(assignment):
                    error('invalid_global_work', 'Work semantic digest differs')
                if assignment['unitId'] not in state['activities'] or assignment['id'] not in state['activities'][assignment['unitId']].get('assignmentIds', []):
                    error('invalid_global_ownership', 'Assignment must belong to its canonical unit')
            if course.get('requirementsDigest') != subject_requirements_digest(course, state['assignments']):
                error('invalid_global_work', 'Subject semantic digest differs')
            validate_treehouse_state(state)
            validate_course_contract(state, course_id, publishing=True)
            for module in state['modules'].values():
                for kind, field in (('activities', 'activityIds'), ('assignments', 'assignmentIds')):
                    if any(k not in state[kind] or state[kind][k].get('moduleId') != module['id'] for k in module.get(field, [])):
                        error('invalid_global_ownership', 'Module work is missing or foreign')
            if set(metadata['unitIds']) != set(state['activities']):
                error('global_summary_mismatch', 'Subject unit summary differs')
            for key, unit in state['activities'].items():
                thin = m['units'].get(key, {})
                if thin.get('courseId') != course_id or thin.get('assignmentIds') != unit.get('assignmentIds'):
                    error('global_summary_mismatch', 'Unit summary differs from canonical work')
                if not unit.get('assignmentIds') and (unit.get('readingOnly') is not True or key in course['completionCriteria'].get('activityIds', [])):
                    error('invalid_global_work', 'Assignment-free route cannot grant completion')
                for field in ('sourceKeys', 'resourceIds', 'readingOnly'):
                    if thin.get(field) != unit.get(field):
                        error('global_summary_mismatch', 'Unit metadata differs from canonical definition')
            self._states[course_id] = state
            # Native projection needs identity/work metadata, never learner bodies.
            thin = copy.deepcopy(state)
            for kind in ('activities', 'assignments'):
                for item in thin[kind].values():
                    for field in ('content', 'optionalSections', 'prompt', 'successConditions', 'failureFeedback', 'hint', 'sourceKeys', 'verificationLimit', 'action'):
                        item.pop(field, None)
            self._projection_states[course_id] = thin
        if set(m['units']) != {k for s in self._states.values() for k in s['activities']}:
            error('invalid_global_definition', 'Unit index contains unknown or missing units')
        try:
            validate_public_strings(m)
            validate_sources(m.get('sources', {}), m['units'])
            for state in self._states.values():
                validate_public_strings(state)
                validate_fragments(state, m['units'])
        except (ValueError, TypeError, KeyError) as exc:
            error('invalid_global_definition', str(exc))
        members = []
        for root_id, root in m['mainQuests'].items():
            required = [x for x in root['members'] if x['required'] is True]
            if not required or root.get('rollupRuleVersion') != 1:
                error('invalid_global_roots', 'Root needs nonempty required subjects and supported rule')
            for item in root['members']:
                if type(item.get('required')) is not bool or item['courseId'] not in self._states or m['subjects'][item['courseId']]['mainQuestId'] != root_id:
                    error('invalid_global_ownership', 'Wrong root membership')
                members.append(item['courseId'])
        if len(members) != len(set(members)) or set(members) != set(self._states):
            error('invalid_global_ownership', 'Subjects need exactly one owning root')
        graph, hard = {k: set() for k in m['units']}, {k: set() for k in m['units']}
        edge_ids = set()
        for edge in m['topicLinks']:
            a, b = edge['fromId'], edge['toId']
            if a not in graph or b not in graph or edge['type'] not in {'requires', 'recommended', 'related'} or not str(edge.get('reason', '')).strip():
                error('invalid_topic_link', 'Topic edge is missing, dangling or unjustified')
            identity = (a, b, edge['type'])
            if identity in edge_ids:
                error('invalid_topic_link', 'Duplicate typed topic edge')
            edge_ids.add(identity)
            graph[a].add(b); graph[b].add(a)
            if edge['type'] == 'requires': hard[a].add(b)
        visiting, visited = set(), set()
        def walk(node):
            if node in visiting: error('prerequisite_cycle', 'Hard topic dependency cycle')
            if node in visited: return
            visiting.add(node)
            for target in hard[node]: walk(target)
            visiting.remove(node); visited.add(node)
        for node in hard: walk(node)
        for root_id in ROOT_IDS:
            own = {k for k, unit in m['units'].items() if m['subjects'][unit['courseId']]['mainQuestId'] == root_id}
            reached, pending = set(), [next(iter(own))]
            while pending:
                node = pending.pop()
                if node in reached: continue
                reached.add(node); pending.extend((graph[node] & own) - reached)
            if reached != own: error('disconnected_topic_web', 'Root topic web is disconnected')
        asset_ids = set()
        project_root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[2]
        asset_root = (project_root / 'static/treehouse/assets').resolve()
        for asset in m.get('assets', []):
            path = (project_root / str(asset.get('path', ''))).resolve()
            if asset.get('id') in asset_ids or not path.is_relative_to(asset_root) or asset.get('mime') != 'application/json' or asset.get('renderer') != 'bounded-native-v1' or not str(asset.get('license', '')).strip():
                error('invalid_global_asset', 'Asset identity, path, renderer or license is invalid')
            if not isinstance(asset.get('unitIds'), list) or not asset['unitIds'] or not set(asset['unitIds']).issubset(m['units']):
                error('invalid_global_asset', 'Asset has unknown unit ownership')
            raw = path.read_bytes()
            if asset.get('digest') != 'sha256:' + hashlib.sha256(raw).hexdigest():
                error('global_asset_digest_mismatch', 'Published asset bytes changed')
            if not isinstance(json.loads(raw), dict): error('invalid_global_asset', 'Native asset configuration is not an object')
            asset_ids.add(asset['id'])
        for unit in m['units'].values():
            if not set(unit.get('assetIds', [])).issubset(asset_ids): error('invalid_global_asset', 'Unit references missing asset')
            for key in unit.get('assetIds', []):
                if unit['id'] not in next(a['unitIds'] for a in m['assets'] if a['id'] == key): error('invalid_global_asset', 'Asset belongs to another unit')
        try:
            self.resources = validate_resources(m, project_root)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            error('invalid_global_resource', str(exc))

    def summary_index(self): return copy.deepcopy(self.manifest)

    def completed_generation_stamp(self):
        directory = self.path / '.indexes' / 'commits'
        paths = sorted(p for p in directory.glob('*.json') if len(p.stem) == 20 and p.stem.isdigit())
        if not paths:
            return None
        latest = paths[-1]
        stat = latest.stat()
        return (latest.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def assert_source_current(self, course_id=None, *, check_catalogue=True):
        index = self.manifest.get('sourceIndex')
        if index is None:
            return
        try:
            if check_catalogue:
                _, head = read_content(self.path, {'path':index['cataloguePath'],'originDigest':index['catalogueHead']}, suffix='.json')
                if head != index['catalogueHead']:
                    raise ValueError('Catalogue source changed')
            if course_id is not None:
                item = index['subjects'][course_id]
                _, head = read_content(self.path, {'path':item['path'],'originDigest':item['head']}, suffix='.json')
                if head != item['head']:
                    raise ValueError('Course source changed')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            error('source_index_pending','Course metadata changed or needs repair; explicitly reindex the shared source',409,
                  **({'courseId':course_id} if course_id else {}))

    def source_status(self, course_id=None, *, check_catalogue=True):
        try:
            self.assert_source_current(course_id, check_catalogue=check_catalogue)
            return {'status':'ready'}
        except TreeHouseError as exc:
            if exc.code != 'source_index_pending': raise
            return {'status':'source-index-pending','code':exc.code}
    def root(self, root_id):
        if root_id not in ROOT_IDS: error('main_quest_not_found', 'Main Quest not found', 404)
        return copy.deepcopy(self.manifest['mainQuests'][root_id])
    def subject_metadata(self, course_id):
        if course_id not in self._states: error('course_not_found', 'System subject not found', 404)
        return copy.deepcopy(self.manifest['subjects'][course_id])
    def subject_state(self, course_id):
        self.subject_metadata(course_id)
        state = copy.deepcopy(self._states[course_id])
        if self.course_files:
            state['activities'] = {key: self.unit(course_id, key) for key in state['activities']}
            state['assignments'] = {key: self.requirements(key) for key in state['assignments']}
        return state
    def projection_state(self, course_id):
        self.subject_metadata(course_id)
        return copy.deepcopy(self._projection_states[course_id])
    def unit(self, course_id, unit_id):
        self.subject_metadata(course_id)
        self.assert_source_current(course_id)
        state = self._states[course_id]
        if unit_id not in state['activities']: error('activity_not_found', 'Unit not found in subject', 404)
        if not self.course_files:
            return copy.deepcopy(state['activities'][unit_id])
        try:
            value, _ = hydrate_activity(self.path, state['activities'][unit_id])
            validate_public_strings(value)
            validate_fragments(value, self.manifest['units'])
            return value
        except (ValueError, OSError, TypeError, KeyError) as exc:
            error('course_content_unavailable', 'Lesson source needs repair', 409, unitId=unit_id, reason=type(exc).__name__)

    def content_source(self, course_id, unit_id, *, unit=None):
        if not self.course_files:
            return None
        value = self.unit(course_id, unit_id) if unit is None else unit
        descriptor = self._states[course_id]['activities'][unit_id]['contentFile']
        return {'kind': 'course-files', 'contentHead': content_head(value['content'].encode('utf-8')),
                'originDigest': descriptor['originDigest']}
    def topic_links(self, entity_id):
        result = []
        for edge in self.manifest['topicLinks']:
            if edge['fromId'] != entity_id: continue
            target = self.manifest['units'][edge['toId']]
            result.append({**edge, 'targetTitle': target['title'], 'targetSubjectId': target['courseId']})
        return copy.deepcopy(result)
    def requirements(self, assignment_id):
        return self.assignment_with_source(assignment_id)[0]

    def assignment_with_source(self, assignment_id):
        for state in self._states.values():
            if assignment_id not in state['assignments']:
                continue
            self.assert_source_current(state['assignments'][assignment_id]['courseId'])
            if not self.course_files:
                return copy.deepcopy(state['assignments'][assignment_id]), None
            try:
                value, source = hydrate_assignment(self.path, state['assignments'][assignment_id], with_source=True)
                validate_public_strings(value)
                validate_fragments(value, self.manifest['units'])
                return value, source
            except (ValueError, OSError, TypeError, KeyError) as exc:
                error('course_content_unavailable', 'Work source needs repair', 409, assignmentId=assignment_id, reason=type(exc).__name__)
        error('assignment_not_found', 'System assignment not found', 404)


def load_global_catalogue(package_root=None):
    if package_root is not None:
        return GlobalCatalogue(package_root)
    from src.constants import DATA_DIR
    active = Path(os.environ.get('TREEHOUSE_CONTENT_ROOT') or Path(DATA_DIR) / 'treehouse-content/shared/openclank-treehouse-core')
    marker = active / '.authority.json'
    if marker.exists():
        value = json.loads(marker.read_text())
        if value.get('format') != 'treehouse-course-authority-v1' or value.get('sourceId') != SOURCE_ID:
            error('invalid_global_package', 'Shared course authority marker needs repair')
        from src.openclank.treehouse_shared_course_index import latest_shared_index
        index = latest_shared_index(active)
        if not index:
            error('invalid_global_package','Shared source has no completed admitted metadata index')
        catalogue = GlobalCatalogue(active,manifest_path=index['manifestPath'])
        catalogue.active_root = True
        return catalogue
    return GlobalCatalogue(Path(__file__).with_name('treehouse_curriculum'))


class GlobalLearning:
    """Thin source-aware adapter around native command/evidence semantics."""
    def __init__(self, catalogue, repository, learner, workspace):
        self.catalogue, self.repo, self.learner, self.workspace = catalogue, repository, learner, workspace
        self.rows = repository.get_global_progress(learner, SOURCE_ID)
        self._source_health = {}

    def source_status(self, course_id):
        if None not in self._source_health:
            self._source_health[None] = self.catalogue.source_status()
        if course_id not in self._source_health:
            self._source_health[course_id] = (self.catalogue.source_status(course_id,check_catalogue=False)
                if self._source_health[None]['status'] == 'ready' else self._source_health[None])
        return self._source_health[course_id]

    def source_ref(self, course_id=None):
        return {'kind': 'system', 'sourceId': SOURCE_ID, **({'courseId': course_id} if course_id else {})}

    def state(self, course_id):
        state = self.catalogue.projection_state(course_id)
        row = self.rows.get(course_id, {'state': {}, 'revision': 0, 'resetGeneration': 0})
        for field in PROGRESS_FIELDS:
            if field in row['state']: state[field] = copy.deepcopy(row['state'][field])
        state['profiles'] = {self.learner: {'id': self.learner, 'displayName': self.learner, 'roles': ['learner'], 'active': True}}
        state['_systemSource'] = SOURCE_ID
        state['revision'] = row['revision']
        state['progressResets'][f'{self.learner}:{course_id}'] = row['resetGeneration']
        if self.source_status(course_id)['status'] != 'ready':
            state['courses'][course_id]['contentError'] = {'code':'source_index_pending'}
        return state

    def progress(self, course_id):
        state = self.state(course_id)
        value = compute_treehouse_projections(state)['learners'].get(self.learner, {}).get('courses', {}).get(course_id, {})
        if self.source_status(course_id)['status'] != 'ready':
            value.update(complete=False, verified=False, availability='source-unavailable')
        return value

    def subject_summary(self, course_id, *, progress=None):
        metadata = self.catalogue.subject_metadata(course_id)
        row = self.rows.get(course_id, {'revision': 0, 'resetGeneration': 0})
        root = self.catalogue.root(metadata['mainQuestId'])
        required = next(x['required'] for x in root['members'] if x['courseId'] == course_id)
        return {k: metadata[k] for k in ('id', 'mainQuestId', 'title', 'objective') if k in metadata} | {'required': required, 'sourceRef': self.source_ref(course_id), 'progress': self.progress(course_id) if progress is None else progress, 'progressRevision': row['revision'], 'resetGeneration': row['resetGeneration'],'sourceStatus':self.source_status(course_id)}

    def root_progress(self, root_id, *, progress_by_course=None, include_history=True):
        root = self.catalogue.root(root_id)
        values = [(x, self.progress(x['courseId']) if progress_by_course is None else progress_by_course[x['courseId']]) for x in root['members']]
        required = [(x, p) for x, p in values if x['required']]
        fractions = [Fraction(p['completed'], p['total']) for _, p in required]
        exact = sum(fractions, Fraction(0)) / len(required)
        complete = all(p['complete'] for _, p in required)
        return {'id': root_id, 'title': root['title'], 'curriculumRevision': root['curriculumRevision'], 'rollupRuleVersion': 1,
                'requiredTotal': len(required), 'requiredComplete': sum(p['complete'] for _, p in required),
                'electiveTotal': len(values) - len(required), 'electiveComplete': sum(p['complete'] for x, p in values if not x['required']),
                'allSubjectTotal': len(values), 'allSubjectComplete': sum(p['complete'] for _, p in values),
                'progressNumerator': exact.numerator, 'progressDenominator': exact.denominator,
                'percent': 100 if complete else min(99, round(float(exact) * 100)), 'complete': complete,
                'status': 'completed' if complete else 'available', 'sourceRef': self.source_ref(),
                **({'historicalCompletions': self.repo.global_completion_history(self.learner, SOURCE_ID, root_id)} if include_history else {})}

    def discovery(self):
        """One body-free view of canonical learning, not another skill registry.

        Progress is computed once per subject for this request and reused by the
        existing root rollup. Never cache personal rows across requests/accounts.
        """
        manifest = self.catalogue.manifest
        progress = {cid: self.progress(cid) for cid in manifest['subjects']}
        public_progress = ('courseType', 'completionBasis', 'completionRuleVersion', 'state',
                           'completed', 'total', 'percent', 'complete', 'requiredActivityIds',
                           'requiredAssignmentIds', 'missingActivityIds', 'missingAssignmentIds',
                           'resumeActivityId', 'availability', 'requirementsDigest')
        quests = []
        for cid in manifest['subjects']:
            summary = self.subject_summary(cid, progress=progress[cid])
            summary['progress'] = {key: copy.deepcopy(progress[cid][key]) for key in public_progress if key in progress[cid]}
            quests.append(summary)
        lessons = []
        for unit in manifest['units'].values():
            cid = unit['courseId']
            required = set(progress[cid]['requiredAssignmentIds'])
            lessons.append({key: copy.deepcopy(unit[key]) for key in ('id', 'courseId', 'title', 'outcome', 'assignmentIds', 'readingOnly') if key in unit}
                           | {'sourceRef': self.source_ref(cid), 'requiredAssignmentIds': [aid for aid in unit['assignmentIds'] if aid in required]})
        vector = [{'courseId': cid, 'progressRevision': self.rows.get(cid, {}).get('revision', 0),
                   'resetGeneration': self.rows.get(cid, {}).get('resetGeneration', 0)} for cid in sorted(manifest['subjects'])]
        return {'sourceId': SOURCE_ID, 'sourceRef': self.source_ref(), 'packageRevision': self.catalogue.revision,
                'packageDigest': manifest['packageDigest'], 'progressFingerprint': canonical_digest(vector),
                'mainQuests': [self.root_progress(root_id, progress_by_course=progress, include_history=False) for root_id in sorted(ROOT_IDS)],
                'quests': quests, 'lessons': lessons, 'topicLinks': copy.deepcopy(manifest['topicLinks'])}

    def summary(self):
        return {'sourceId': SOURCE_ID, 'packageRevision': self.catalogue.revision, 'mainQuests': [self.root_progress(k) for k in sorted(ROOT_IDS)]}

    def root_detail(self, root_id):
        root = self.catalogue.root(root_id)
        return {'sourceRef': self.source_ref(), 'packageRevision': self.catalogue.revision, 'mainQuest': self.root_progress(root_id), 'subjects': [self.subject_summary(x['courseId']) for x in root['members']]}

    def subject_detail(self, course_id):
        self.catalogue.assert_source_current(course_id)
        subject = self.subject_summary(course_id)
        units = [dict(self.catalogue.manifest['units'][k], topicLinks=self.catalogue.topic_links(k)) for k in self.catalogue.subject_metadata(course_id)['unitIds']]
        work = {k: {field: a[field] for field in ('unitId', 'requirementDigest', 'completionEvidence')}
                for k, a in self.catalogue._states[course_id]['assignments'].items()}
        return {'sourceRef': self.source_ref(course_id), 'packageRevision': self.catalogue.revision, 'subject': subject, 'units': units, 'workRequirements': work, **{k: subject[k] for k in ('progress', 'progressRevision', 'resetGeneration')}}

    def availability(self, unit_id):
        unmet = []
        for edge in self.catalogue.topic_links(unit_id):
            if edge['type'] != 'requires': continue
            target = self.catalogue.manifest['units'][edge['toId']]
            state = self.state(target['courseId'])
            progress = self.progress(target['courseId'])
            submitted = {e['data'].get('assignmentId') for e in state['events'] if e['type'] == 'submission.submitted' and e['data'].get('generation') == progress.get('resetGeneration') and e['data'].get('assignmentRequirementDigest') == state['assignments'].get(e['data'].get('assignmentId'), {}).get('requirementDigest')}
            targets = target.get('assignmentIds', [])
            conceptual = [k for k in targets if state['assignments'][k].get('completionEvidence') == 'response']
            required = conceptual or targets
            if not required or not set(required).issubset(submitted):
                unmet.append({'targetId': edge['toId'], 'targetSubjectId': target['courseId'], 'targetTitle': target['title'], 'reason': edge['reason']})
        return {'status': 'blocked' if unmet else 'available', 'unmetPrerequisites': unmet}

    def assignment_availability(self, assignment):
        if assignment.get('completionEvidence') != 'observed-success':
            return {'status': 'available', 'unmetPrerequisites': []}
        return self.availability(assignment['unitId'])

    def unit_detail(self, course_id, unit_id):
        unit = self.catalogue.unit(course_id, unit_id)
        state, subject = self.state(course_id), self.subject_summary(course_id)
        submissions = {}
        for value in state['submissions'].values():
            if value['profileId'] != self.learner:
                continue
            value = copy.deepcopy(value)
            aid = value['assignmentId']
            assignment = state['assignments'].get(aid, {})
            if not value.get('answerRequirementDigest'):
                event = next((e for e in reversed(state['events']) if e['type'] == 'submission.submitted' and e['data'].get('assignmentId') == aid and e['data'].get('generation') == value.get('resetGeneration')), None)
                if event:
                    value['answerRequirementDigest'] = event['data'].get('assignmentRequirementDigest')
            for prefix in ('draft', 'answer'):
                digest = value.get(prefix + 'RequirementDigest')
                value[prefix + 'RecoveryStatus'] = ('reset' if value.get('status') == 'reset' or value.get('resetGeneration') != subject['resetGeneration'] else
                    'unknown' if not digest else 'current' if digest == assignment.get('requirementDigest') else 'previous')
            submissions[aid] = value
        assignments = []
        for key in unit['assignmentIds']:
            assignment, source = self.catalogue.assignment_with_source(key)
            assignments.append({**assignment,'contentSource':source,'actionAvailability':self.assignment_availability(assignment)})
        return {'sourceRef': self.source_ref(course_id), 'packageRevision': self.catalogue.revision, 'subjectId': course_id,
                'unit': {k: unit[k] for k in ('id','title','outcome','content','optionalSections','assignmentIds','assetIds','resourceIds','readingOnly') if k in unit}, 'assignments': assignments,
                'sources': [{k: source[k] for k in ('title','url','supportedClaim') if k in source} for key in unit.get('sourceKeys', []) if isinstance((source := self.catalogue.manifest.get('sources', {}).get(key)), dict) and str(source.get('url', '')).startswith('https://')],
                'submissions': submissions,
                'contentSource': self.catalogue.content_source(course_id, unit_id, unit=unit),
                'resources': [{**copy.deepcopy(self.catalogue.resources[key]), 'url': '/' + self.catalogue.resources[key]['path']} for key in unit.get('resourceIds', [])],
                'actionAvailability': self.availability(unit_id), 'assetRegistry': copy.deepcopy(self.catalogue.manifest.get('assets', [])),
                **{k: subject[k] for k in ('progress', 'progressRevision', 'resetGeneration')}}

    def command(self, envelope):
        payload, kind = envelope['payload'], envelope['type']
        course_id = payload.get('courseId')
        if payload.get('sourceRef') != self.source_ref(course_id): error('source_mismatch', 'Command source does not match canonical system identity', 403)
        if any(payload.get(k) not in (None, self.learner) for k in ('profileId', 'learnerId', 'ownerId', 'ownerAccountId')):
            error('forbidden', 'Cannot act as another account', 403)
        if envelope.get('actorId') not in (None, 'owner', self.learner): error('forbidden', 'Cannot act as another learner', 403)
        prior = self.repo.global_command_receipt(self.learner, SOURCE_ID, envelope['commandId'], envelope)
        if prior: return {**prior, 'changed': False, 'replayed': True}
        if payload.get('packageRevision') != self.catalogue.revision: error('stale_curriculum', 'Curriculum changed; reload')
        self.catalogue.assert_source_current(course_id)
        if self.catalogue.active_root and kind in {'submission.submit','submission.draft'}:
            _, source = self.catalogue.assignment_with_source(payload.get('assignmentId'))
            if not source or payload.get('workBase') != source['contentHead']:
                error('stale_work_source','Work source changed; keep your response and reopen the assignment')
        if kind not in {'course.open', 'enrollment.enroll', 'enrollment.unenroll', 'submission.submit', 'submission.draft', 'progress.reset'}:
            error('system_curriculum_read_only', 'System content cannot be edited or generically completed', 403)
        reset_many = kind == 'progress.reset' and not course_id
        if reset_many:
            root_id = payload.get('mainQuestId')
            members = self.catalogue.root(root_id)['members'] if root_id else [{'courseId': k} for k in self.catalogue.manifest['subjects']] if payload.get('scope') == 'all-global' else []
            scopes = payload.get('resetScopes')
            if not members or not isinstance(scopes, list) or any(not isinstance(s, dict) for s in scopes) or {s.get('courseId') for s in scopes} != {m['courseId'] for m in members} or len(scopes) != len(members):
                error('invalid_reset_scope', 'Reset vector must include exactly the current membership')
            by_id = {s['courseId']: s for s in scopes}
            course_ids = list(by_id)
        else:
            self.catalogue.subject_metadata(course_id)
            course_ids = [course_id]
            by_id = {course_id: {'progressRevision': envelope.get('expectedRevision'), 'resetGeneration': payload.get('resetGeneration')}}
        updates, result, read_checks = [], None, {}
        for ident in course_ids:
            self.catalogue.assert_source_current(ident)
            expected = by_id[ident]
            row = self.rows.get(ident, {'revision': 0, 'resetGeneration': 0})
            if expected.get('progressRevision') != row['revision'] or expected.get('resetGeneration') != row['resetGeneration']:
                error('stale', 'Learning state changed; reload', progressRevision=row['revision'], resetGeneration=row['resetGeneration'])
            state = self.state(ident)
            work = {**payload, 'courseId': ident, 'resetGeneration': row['resetGeneration'], 'originWorkspaceId': self.workspace}
            if kind in {'submission.submit', 'submission.draft'}:
                assignment = state['assignments'].get(payload.get('assignmentId'))
                if not assignment: error('assignment_not_found', 'Assignment belongs to another subject', 404)
                if payload.get('assignmentRequirementDigest') != assignment['requirementDigest']:
                    error('stale_requirement', 'Displayed work changed or lacks its requirement identity; reload before saving')
                if self.catalogue.active_root:
                    state['_validatedWorkHeads'] = {assignment['id']:payload['workBase']}
                answer = payload.get('answer')
                outcome = answer.get('outcome') if isinstance(answer, dict) else None
                success_path = kind == 'submission.submit' and assignment.get('completionEvidence') == 'observed-success' and isinstance(outcome, dict) and outcome.get('status') == 'success'
                available = self.assignment_availability(assignment)
                if success_path and available['status'] == 'blocked': error('prerequisites_unmet', 'Necessary concepts are missing', **available)
                if success_path:
                    for edge in self.catalogue.topic_links(assignment['unitId']):
                        if edge['type'] != 'requires': continue
                        dependency = self.catalogue.manifest['units'][edge['toId']]['courseId']
                        dep_row = self.rows.get(dependency, {'revision': 0, 'resetGeneration': 0})
                        read_checks[dependency] = {'courseId': dependency, 'progressRevision': dep_row['revision'], 'resetGeneration': dep_row['resetGeneration']}
                # Formal enrollment is optional intent, not a mastery gate.
                key = ident + ':' + self.learner
                state['enrollments'].setdefault(key, {'id': key, 'courseId': ident, 'profileId': self.learner, 'status': 'active'})
            next_state, result, changed = apply_treehouse_command(state, {'type': kind, 'payload': work}, actor_id=self.learner, command_id=envelope['commandId'], expected_revision=row['revision'])
            partition = {k: next_state[k] for k in PROGRESS_FIELDS}
            epoch = next_state['progressResets'].get(self.learner + ':' + ident, row['resetGeneration'])
            updates.append({'courseId': ident, 'state': partition, 'context': next_state, 'expectedRevision': row['revision'], 'expectedGeneration': row['resetGeneration'], 'resetGeneration': epoch})
            self.rows[ident] = {'state': partition, 'revision': row['revision'] + 1, 'resetGeneration': epoch}
        root_ids = {self.catalogue.subject_metadata(x)['mainQuestId'] for x in course_ids}
        def response(current_rows):
            self._source_health.clear()
            self.catalogue.assert_source_current(course_id)
            for ident in course_ids:
                self.catalogue.assert_source_current(ident)
            for ident in read_checks:
                self.catalogue.assert_source_current(ident)
            if self.catalogue.active_root and kind in {'submission.submit','submission.draft'}:
                _, current_source = self.catalogue.assignment_with_source(payload.get('assignmentId'))
                if current_source['contentHead'] != payload.get('workBase'):
                    error('stale_work_source','Work source changed before the learning transaction committed')
            self.rows = current_rows
            value = {'ok': True, 'changed': True, 'result': result, 'sourceRef': self.source_ref(course_id), 'packageRevision': self.catalogue.revision,
                     'mainQuestProgress': [self.root_progress(k) for k in sorted(root_ids)], 'globalCatalogue': self.summary()}
            if reset_many: value['subjects'] = [self.subject_summary(k) for k in course_ids]
            else:
                subject = self.subject_summary(course_id)
                value.update({k: subject[k] for k in ('progress', 'progressRevision', 'resetGeneration')})
            receipts = []
            for root_id in root_ids:
                if not self.root_progress(root_id)['complete']: continue
                for member in self.catalogue.root(root_id)['members']:
                    if member['required']:
                        self.catalogue.assert_source_current(member['courseId'])
                root = self.catalogue.root(root_id)
                children = [{'courseId': x['courseId'], 'requirementsDigest': self.catalogue._states[x['courseId']]['courses'][x['courseId']]['requirementsDigest'], 'resetGeneration': self.rows.get(x['courseId'], {}).get('resetGeneration', 0), 'evidenceEventIds': self.progress(x['courseId'])['evidenceEventIds']} for x in root['members'] if x['required']]
                identity = {'mainQuestId': root_id, 'rollupRuleVersion': 1, 'children': [{k: v for k, v in child.items() if k != 'evidenceEventIds'} for child in children]}
                receipts.append({**identity, 'children': children, 'receiptDigest': canonical_digest(identity), 'packageRevision': self.catalogue.revision, 'curriculumRevision': root['curriculumRevision']})
            value['_completionReceipts'] = receipts
            return value
        return self.repo.put_global_updates(self.learner, SOURCE_ID, envelope['commandId'], envelope, updates, response, read_checks=list(read_checks.values()))
