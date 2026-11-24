"""Pure tests for src/memery_ranking_feedback.py (runs under plain Python or pytest)."""
import ast
import copy
import importlib.util
import math
import pathlib

_SRC = pathlib.Path(__file__).resolve().parent.parent / 'src' / 'memery_ranking_feedback.py'
_spec = importlib.util.spec_from_file_location('frankenmemory_ranking_feedback_under_test', _SRC)
rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rf)

OWNER = 'owner-a'


def receipt(eid, attempt, ordinal, **kw):
    r = {'v': 1, 'fts_ordinal': ordinal, 'route_kind': 'curated_fts', 'directness': None,
         'entity_match': None, 'freshness': None, 'owner': OWNER,
         'found_event_id': eid, 'recall_attempt_id': attempt}
    r.update(kw)
    return r


def label(eid, polarity, owner=OWNER):
    return {'owner': owner, 'found_event_id': eid, 'polarity': polarity}


def synthetic(n_attempts=12):
    """Each attempt delivers ordinals 0,2,5; only ordinal 0 is helpful."""
    rs, ls = [], []
    for a in range(n_attempts):
        for o in (0, 2, 5):
            eid = f'f{a:02d}-{o}'
            rs.append(receipt(eid, f'a{a:02d}', o, freshness=1.0 if a % 2 else None))
            ls.append(label(eid, 'positive' if o == 0 else 'negative'))
    return rs, ls


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings(v)


def test_encoding_unknown_is_not_zero():
    unknown = rf.encode_features(receipt('e', 'a', 3), ['curated_fts'])
    zero = rf.encode_features(receipt('e', 'a', 3, directness=0.0, entity_match=False, freshness=0.0), ['curated_fts'])
    known = rf.encode_features(receipt('e', 'a', 3, directness=0.5, entity_match=True, freshness=1.0), ['curated_fts'])
    assert unknown != zero
    assert unknown[2:8] == [0.0] * 6
    assert zero[2:8] == [1.0, 0.0, 1.0, 0.0, 1.0, 0.0]
    assert known[2:8] == [1.0, 0.5, 1.0, 1.0, 1.0, 1.0]
    assert unknown[-1] == 1.0 and len(unknown) == 9


def test_invalid_receipts_refused():
    for bad in (receipt('e', 'a', -1), receipt('e', 'a', True), receipt('e', 'a', 1, directness=2.0),
                receipt('e', 'a', 1, entity_match=1), receipt('e', 'a', 1, v=2)):
        try:
            rf.shadow_report(OWNER, [bad], [])
        except ValueError:
            continue
        raise AssertionError('invalid receipt accepted')


def test_replay_deterministic_and_order_independent():
    rs, ls = synthetic()
    first = rf.shadow_report(OWNER, rs, ls)
    assert first == rf.shadow_report(OWNER, rs, ls)
    # Independent events may arrive in any order; replay sorts them first.
    assert first == rf.shadow_report(OWNER, list(reversed(rs)), list(reversed(ls)))


def test_low_ordinal_ranked_first():
    rs, ls = synthetic()
    rep = rf.shadow_report(OWNER, rs, ls)
    assert rep['state'] == 'ok' and rep['labelled'] == 36
    for entry in rep['shadow_order']:
        assert [i.split('-')[1] for i in entry['found_event_ids']] == ['0', '2', '5']
    ag = rep['agreement']
    assert ag['basis'] == 'leave_one_attempt_out'
    assert ag['mixed_pairs'] == 12 * 2 and ag['shadow_pair_accuracy'] == 1.0
    assert rep['exploration'] == 0 and rep['mode'] == 'shadow'


def test_shadow_prefers_high_ordinal_when_labels_say_so():
    rs, ls = synthetic()
    flipped = [label(l['found_event_id'], 'negative' if l['polarity'] == 'positive' else 'positive') for l in ls]
    rep = rf.shadow_report(OWNER, rs, flipped)
    assert rep['shadow_order'][0]['found_event_ids'][0].endswith('-5') or rep['shadow_order'][0]['found_event_ids'][0].endswith('-2')


def test_constant_and_unknown_features_reported():
    rs, ls = synthetic()
    rep = rf.shadow_report(OWNER, rs, ls)
    u = rep['uninformative_features']
    assert u['route_kind'] == 'constant'
    assert u['directness'] == 'always_unknown' and u['entity_match'] == 'always_unknown'
    assert 'fts_ordinal' not in u and 'freshness' not in u
    assert rep['distinct_feature_vectors'] == 6  # 3 ordinals x freshness {None,1.0}


def test_below_minimum_no_agreement():
    rs, ls = synthetic(n_attempts=2)
    rep = rf.shadow_report(OWNER, rs, ls)
    assert rep['labelled'] == 6 < rf.MIN_LABELLED
    assert rep['state'] == 'insufficient_labels'
    assert rep['agreement'] is None and rep['agreement_withheld'] == 'below_min_labelled'
    empty = rf.shadow_report(OWNER, [], [])
    assert empty['state'] == 'no_receipts' and empty['agreement'] is None and empty['shadow_order'] == []


def test_unlabelled_and_conflicting_contribute_nothing():
    rs, ls = synthetic()
    rep = rf.shadow_report(OWNER, rs, ls[:-3])
    assert rep['labelled'] == 33
    ls2 = ls + [label('f00-0', 'negative')]  # contradicts an existing label: dropped, not guessed
    rep2 = rf.shadow_report(OWNER, rs, ls2)
    assert rep2['labelled'] == 35 and rep2['conflicting_labels'] == 1
    rep3 = rf.shadow_report(OWNER, rs, ls + [label('nope', 'positive')])
    assert rep3['labels_without_receipt'] == 1 and rep3['labelled'] == 36
    assert rf.shadow_report(OWNER, rs, ls + [ls[0]]) == rf.shadow_report(OWNER, rs, ls)  # duplicate is idempotent


def test_mixed_scope_refused():
    rs, ls = synthetic(2)
    for args in ((OWNER, rs + [receipt('x', 'z', 0, owner='owner-b')], ls),
                 (OWNER, rs, ls + [label('f00-0', 'positive', owner='owner-b')]),
                 ('owner-b', rs, ls)):
        try:
            rf.shadow_report(*args)
        except ValueError:
            continue
        raise AssertionError('foreign scope accepted')


def test_inputs_not_mutated():
    rs, ls = synthetic()
    rs0, ls0 = copy.deepcopy(rs), copy.deepcopy(ls)
    rf.shadow_report(OWNER, rs, ls)
    assert rs == rs0 and ls == ls0


def test_outputs_have_no_text_fields():
    rs, ls = synthetic()
    for r in rs:
        r['text'] = 'SECRET-MEMORY-TEXT'
        r['query'] = 'SECRET-QUERY'
    rep = rf.shadow_report(OWNER, rs, ls)
    flat = repr(rep)
    assert 'SECRET' not in flat and "'text'" not in flat and "'query'" not in flat


def test_solve_hand_computed():
    # [[2,1],[1,3]] x = [3,5]  ->  x = [0.8, 1.4]
    x = rf.solve_linear([[2, 1], [1, 3]], [3, 5])
    assert math.isclose(x[0], 0.8) and math.isclose(x[1], 1.4)
    # [[2,1,1],[1,3,2],[1,0,0]] x = [4,5,6]  ->  x=[6,15,-23]
    y = rf.solve_linear([[2, 1, 1], [1, 3, 2], [1, 0, 0]], [4, 5, 6])
    assert all(math.isclose(a, b) for a, b in zip(y, [6, 15, -23]))
    # needs a row swap (zero leading pivot)
    z = rf.solve_linear([[0, 1], [1, 0]], [2, 3])
    assert z == [3.0, 2.0]


def test_singular_handled_by_ridge():
    sing = [[1.0, 1.0], [1.0, 1.0]]
    try:
        rf.solve_linear(sing, [1.0, 1.0])
        raise AssertionError('plain solve accepted a singular system')
    except ValueError:
        pass
    x = rf.ridge_solve(sing, [1.0, 1.0])
    assert all(math.isfinite(v) for v in x)
    assert math.isclose(x[0], 1 / 3) and math.isclose(x[1], 1 / 3)
    # report on a degenerate input (identical features) is finite too
    rs = [receipt(f'e{i}', f'a{i // 2}', 1) for i in range(40)]
    ls = [label(f'e{i}', 'positive' if i % 2 else 'negative') for i in range(40)]
    rep = rf.shadow_report(OWNER, rs, ls)
    assert all(math.isfinite(s) for e in rep['shadow_order'] for s in e['scores'])
    assert rep['uninformative_features']['fts_ordinal'] == 'constant'


def test_no_forbidden_words_in_strings():
    rs, ls = synthetic()
    outs = [rf.shadow_report(OWNER, rs, ls), rf.shadow_report(OWNER, rs[:6], ls[:6]), rf.shadow_report(OWNER, [], [])]
    for out in outs:
        for s in strings(out):
            low = s.lower()
            assert not any(w in low for w in ('improves', 'learns', 'better')), s
    src = _SRC.read_text().lower()
    assert 'improves' not in src and 'learns' not in src and 'better' not in src


def test_imports_stdlib_only():
    tree = ast.parse(_SRC.read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split('.')[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            assert n.level == 0
            mods.add((n.module or '').split('.')[0])
    assert mods <= {'__future__', 'math'}, mods


def test_report_names_its_route_kinds_and_label_and_feature_versions_without_storing_anything():
    rs, ls = [], []
    rep = rf.shadow_report(OWNER, rs, ls)
    assert rep['route_kinds'] == [] and rf.FEATURE_VERSION == 1 and rf.LABEL_VERSION == 'host_human_review.v1'
