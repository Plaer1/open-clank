"""Assessment values shared by the existing Treehouse command/grade authority.

Answer snapshots never enter stats. File work accepts only adopted, prepared
Treehouse resources; opaque Files locators alone do not confer access.
"""
from __future__ import annotations
import copy
import json


def assessment_fields(payload, existing=None):
    result = copy.deepcopy(existing or {})
    for field in ('assessmentType', 'graded', 'passPercent', 'availableAt', 'availableUntil', 'objectives', 'rubric', 'questions'):
        if field in payload:
            result[field] = copy.deepcopy(payload[field])
    result.setdefault('assessmentType', 'text')
    result.setdefault('graded', True)
    result.setdefault('passPercent', 0)
    if result['assessmentType'] not in {'text', 'file', 'quiz'}:
        raise ValueError('Supported assessments are text tasks, prepared file tasks and choice quizzes')
    if not isinstance(result['graded'], bool):
        raise ValueError('graded must be a boolean')
    if isinstance(result['passPercent'], bool) or not isinstance(result['passPercent'], (int, float)) or not 0 <= result['passPercent'] <= 100:
        raise ValueError('passPercent must be between 0 and 100')
    objectives = result.get('objectives', [])
    if not isinstance(objectives, list) or len(objectives) > 32 or any(not isinstance(x, str) or len(x) > 2048 for x in objectives):
        raise ValueError('Objectives must be up to 32 short statements')
    rubric = result.get('rubric', [])
    if not isinstance(rubric, list) or len(rubric) > 32 or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) or not x['id'] or len(x['id']) > 128 or not isinstance(x.get('title'), str) or len(x['title']) > 512 for x in rubric):
        raise ValueError('Rubric criteria need unique IDs and short titles')
    if len({x['id'] for x in rubric}) != len(rubric):
        raise ValueError('Rubric criterion IDs must be unique')
    questions = result.get('questions', [])
    if not isinstance(questions, list) or len(questions) > 100:
        raise ValueError('Quiz may contain at most 100 questions')
    ids = set()
    for q in questions:
        if not isinstance(q, dict) or not isinstance(q.get('id'), str) or q['id'] in ids or not isinstance(q.get('prompt'), str) or not q['prompt'].strip() or len(q['prompt']) > 8192:
            raise ValueError('Each quiz question needs a unique ID and prompt')
        ids.add(q['id'])
        options = q.get('options', [])
        if not isinstance(options, list) or not 2 <= len(options) <= 20 or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) or not isinstance(x.get('text'), str) or len(x['text']) > 2048 for x in options):
            raise ValueError('Each quiz question needs 2–20 labelled options')
        option_ids = {x['id'] for x in options}
        correct = q.get('correctOptionIds', [])
        if len(option_ids) != len(options) or not isinstance(correct, list) or not correct or any(not isinstance(x, str) for x in correct) or not set(correct).issubset(option_ids):
            raise ValueError('Quiz answer key must identify available options')
    if len(json.dumps(result, ensure_ascii=False)) > 262144:
        raise ValueError('Assessment configuration is too large')
    return result


def validate_answer(assignment, answer, state, course_id, draft=False):
    if len(json.dumps(answer, ensure_ascii=False)) > 262144:
        raise ValueError('Answer is too large')
    if 'assessmentType' not in assignment:
        return copy.deepcopy(answer)
    kind = assignment.get('assessmentType', 'text')
    if kind == 'text':
        if not isinstance(answer, str) or (not draft and not answer.strip()):
            raise ValueError('Enter a text response')
    elif kind == 'quiz':
        if not isinstance(answer, dict):
            raise ValueError('Quiz response must select question options')
        questions = {q['id']: q for q in assignment.get('questions', [])}
        if not questions:
            raise ValueError('Quiz has no published questions')
        if set(answer) - set(questions):
            raise ValueError('Response references a missing question')
        for key, q in questions.items():
            selected = answer.get(key, [])
            if not isinstance(selected, list) or any(not isinstance(x, str) for x in selected) or not set(selected).issubset({x['id'] for x in q['options']}) or (not draft and not selected):
                raise ValueError('Select available answers for every question')
    else:
        receipts = answer.get('fileReceipts', []) if isinstance(answer, dict) else []
        if not isinstance(answer, dict) or not isinstance(receipts, list) or (not draft and not receipts) or len(receipts) > 16:
            raise ValueError('Select up to 16 prepared files as evidence')
        authorized = state.get('_validatedSubmissionReceipts', set())
        for receipt in receipts:
            if not isinstance(receipt, dict) or set(receipt) != {'operationId', 'preparationReceiptId', 'name'} or receipt.get('preparationReceiptId') not in authorized:
                raise ValueError('File evidence requires a current authorized submission preparation receipt')
    return copy.deepcopy(answer)


def quiz_percent(assignment, answer):
    questions = assignment.get('questions', [])
    return round(sum(set(answer.get(q['id'], [])) == set(q['correctOptionIds']) for q in questions) / len(questions) * 100, 2)


def review_values(submission, assignment, payload):
    feedback = payload.get('criterionFeedback', {})
    allowed = {x['id'] for x in assignment.get('rubric', [])}
    if not isinstance(feedback, dict) or set(feedback) - allowed or any(not isinstance(x, str) or len(x) > 8192 for x in feedback.values()):
        raise ValueError('Criterion feedback must reference the current rubric')
    correction = submission.get('status') == 'graded'
    if correction and not str(payload.get('correctionReason', '')).strip():
        raise ValueError('A grade correction requires a reason')
    if (payload.get('attemptId') is not None or correction) and payload.get('attemptId') != submission.get('attemptId'):
        raise ValueError('Review attempt changed; reopen the current attempt')
    return copy.deepcopy(feedback), correction


# These are explicit first-party lesson work contracts, not mutable achievement
# hint links. The award is an index to a qualified predicate; the underlying
# committed producer receipts remain the proof. UI-only observations cannot
# claim a verified work result through this adapter.
BUILTIN_RECEIPT_VERIFIERS = {
    'house-collaborate.chat-identity': ('S02',),
    'house-collaborate.durable-goals': ('N04',),
    'house-collaborate.scheduled-work': ('N03',),
    'house-documents.new-documents': ('N07', 'N08'),
    'house-documents.rich-markdown': ('N10',),
    'house-documents.typed-tables': ('N11',),
    'house-documents.wiki-within': ('N12',),
    'house-documents.images-attached': ('N19', 'N20'),
    'house-connections.tasks-timeline': ('N17', 'N18'),
    'house-making.imps-layers': ('N21',),
    'house-making.project-export': ('N22',),
    'house-making.lore-restore': ('N23',),
    'house-making.scoped-exports': ('N24',),
    'house-stewardship.teach-a-class': ('N28',),
}


def builtin_activity_verification(repository, account_id, workspace_id, activity, *, guide_owner_id=None):
    """Join lifetime committed proof into an explicit current-generation action.

    Progress reset clears course completion, not the canonical action history.
    The command/repository still guard the current learning reset generation.
    """
    from src.openclank.treehouse_field_guide import field_guide_manifest
    from src.openclank.treehouse_achievements import BY_ID, PREDICATE_VERSION, QUALIFYING_ACTOR_KINDS, ActivityEvent, PredicateState, _SINGLE_CHECKERS, event_qualifies_for_scoring, EventValidationError
    key = activity.get('fieldGuideKey')
    official = next((row for row in field_guide_manifest()['lessons'] if row['key'] == key and row['completion'] == 'verified'), None)
    requirements = BUILTIN_RECEIPT_VERIFIERS.get(key)
    if not official or not requirements or activity.get('verifierSpec'):
        raise ValueError('No trusted result adapter is registered for this verified activity. Pure UI observations and Bases work require a dedicated authoritative source verifier.')
    import hashlib
    owner_suffix = hashlib.sha256(str(guide_owner_id or account_id).encode('utf-8')).hexdigest()[:12]
    if activity.get('id') != f'activity:{key}:{owner_suffix}' or activity.get('courseId') != f'course:{official["classKey"]}:{owner_suffix}':
        raise ValueError('This result adapter belongs to the canonical provisioned Field Guide, not a custom copied lesson.')
    source_ids = []
    for predicate_id in requirements:
        receipts = repository.canonical_producer_receipts(account_id, BY_ID[predicate_id].event_families, workspace_id)
        predicate_state = PredicateState()
        durable = False
        qualified = False
        supporting_ids = []
        for receipt in receipts:
            event = ActivityEvent(**receipt)
            try:
                event_qualifies_for_scoring(event)
            except EventValidationError:
                continue
            if receipt.get('actor_kind') not in QUALIFYING_ACTOR_KINDS:
                continue
            if receipt.get('kind') == 'R' and receipt.get('result') == 'committed':
                durable = True
            supporting_ids.append(receipt['source_event_id'])
            if _SINGLE_CHECKERS[predicate_id](predicate_state, event) is not None and durable:
                qualified = True
                break
        if not qualified:
            raise ValueError('Needs fresh practice: complete the lesson action again in its destination. Historical awards or browser claims alone do not prove a server-delivered result.')
        source_ids.extend(supporting_ids)
    return {'accountId': account_id, 'courseId': activity['courseId'], 'activityId': activity['id'],
            'basis': 'canonical-producer-receipts', 'sourceEventIds': sorted(set(source_ids)),
            'verifierVersion': 'field-guide-producer-digest-v2', 'predicateVersion': PREDICATE_VERSION}
