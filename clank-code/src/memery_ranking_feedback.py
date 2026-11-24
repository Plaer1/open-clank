"""S07 shadow ranking: pure replay core. SHADOW ONLY.

Given already-loaded `found.features v1` receipts and explicit `helped` labels,
compute on demand what a linear (LinUCB with exploration 0, i.e. ridge) scorer
WOULD have ordered, and how that compares with the order actually delivered.
It never changes recall, never stores a model, never reads or writes anything:
the scorer is rebuilt by replay on every call, so erasing or resetting the
receipts invalidates it automatically. Standard library only; no clock, no
randomness. Outputs carry ids, ordinals, counts and scores, never text.
"""
from __future__ import annotations

import math

FEATURES = ('fts_ordinal', 'route_kind', 'directness', 'entity_match', 'freshness')
RIDGE = 1.0            # lambda in A = lambda*I + sum(x x^T)
EXPLORATION = 0        # LinUCB alpha; 0 means plain ridge scoring, no exploration bonus
# Floor for not printing noise: below this many labelled items an agreement
# figure is withheld because it would mostly reflect chance.
MIN_LABELLED = 20
FEATURE_VERSION = 1    # the found.features v1 receipt layout encode_features() reads
LABEL_VERSION = 'host_human_review.v1'   # the helped producer string the Native page labels carry
_ORDINAL_CAP = 19      # automatic lane asks for at most 20 candidates (positions 0..19)
_POLARITY = {'positive': 1.0, 'negative': 0.0}


def solve_linear(a, b):
    """Solve a x = b by Gaussian elimination with partial pivoting (small dense lists)."""
    n = len(b)
    m = [list(map(float, a[i])) + [float(b[i])] for i in range(n)]  # augmented copy
    for col in range(n):
        # Pivot: swap up the row with the largest magnitude in this column.
        p = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[p][col]) < 1e-12:
            raise ValueError('singular system')
        m[col], m[p] = m[p], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):  # back substitution
        x[i] = (m[i][n] - sum(m[i][j] * x[j] for j in range(i + 1, n))) / m[i][i]
    return x


def ridge_solve(gram, b, lam=RIDGE):
    """Solve (gram + lam*I) x = b. lam > 0 makes a singular gram solvable."""
    n = len(b)
    a = [[gram[i][j] + (lam if i == j else 0.0) for j in range(n)] for i in range(n)]
    return solve_linear(a, b)


def _num01(v, name):
    if type(v) not in (int, float) or isinstance(v, bool) or not 0.0 <= v <= 1.0:
        raise ValueError(f'{name} must be None or a number in [0,1]')
    return float(v)


def _check_receipt(r):
    if type(r) is not dict or r.get('v') != 1 or type(r.get('v')) is not int:
        raise ValueError('receipt must be a found.features v1 dict')
    o = r.get('fts_ordinal')
    if type(o) is not int or o < 0:
        raise ValueError('fts_ordinal must be an int >= 0')
    if type(r.get('route_kind')) is not str or not r['route_kind']:
        raise ValueError('route_kind must be a non-empty str')
    for k in ('directness', 'freshness'):
        if r.get(k) is not None:
            _num01(r[k], k)
    if r.get('entity_match') is not None and type(r['entity_match']) is not bool:
        raise ValueError('entity_match must be None or bool')
    for k in ('owner', 'found_event_id', 'recall_attempt_id'):
        if type(r.get(k)) is not str or not r[k]:
            raise ValueError(f'{k} must be a non-empty str')


def encode_features(receipt, route_kinds):
    """Vector: bias, ordinal, then (known, value) per optional feature, then route one-hot.

    Unknown (None) gives known=0 AND value=0, so unknown is a distinct state,
    never "zero". `route_kinds` is the sorted list of kinds seen in the input.
    """
    _check_receipt_features(receipt)
    x = [1.0, min(receipt['fts_ordinal'], _ORDINAL_CAP) / _ORDINAL_CAP]
    for k in ('directness', 'entity_match', 'freshness'):
        v = receipt[k]
        x += [0.0, 0.0] if v is None else [1.0, float(v)]
    x += [1.0 if receipt['route_kind'] == rk else 0.0 for rk in route_kinds]
    return x


def _check_receipt_features(r):
    # Feature-only validation for callers encoding without identifiers.
    o = r.get('fts_ordinal')
    if type(o) is not int or o < 0 or type(r.get('route_kind')) is not str:
        raise ValueError('bad feature receipt')
    for k in ('directness', 'freshness'):
        if r.get(k) is not None:
            _num01(r[k], k)
    if r.get('entity_match') is not None and type(r['entity_match']) is not bool:
        raise ValueError('entity_match must be None or bool')


def _fit(examples):
    """Replay: accumulate A and b per update in a fixed order, then solve for theta."""
    d = len(examples[0][0]) if examples else 0
    if d == 0:
        return []
    a = [[0.0] * d for _ in range(d)]
    b = [0.0] * d
    for x, y in examples:
        for i in range(d):
            b[i] += y * x[i]
            for j in range(d):
                a[i][j] += x[i] * x[j]
    return ridge_solve(a, b)  # A = lam*I + sum x x^T; theta = A^-1 b


def _score(theta, x):
    return sum(t * v for t, v in zip(theta, x)) if theta else 0.0


def _load(scope, receipts, labels):
    """Validate, scope-check and dedupe. Returns (sorted receipts, label map, stats)."""
    if type(scope) is not str or not scope:
        raise ValueError('scope must be a non-empty str')
    by_id = {}
    for r in receipts:
        _check_receipt(r)
        if r['owner'] != scope:
            raise ValueError('receipt scope differs from requested scope')
        key = tuple(r[k] for k in FEATURES) + (r['recall_attempt_id'],)
        old = by_id.setdefault(r['found_event_id'], key)
        if old != key:
            raise ValueError('conflicting receipts for one found_event_id')  # replay must be idempotent
    seen = {}
    for lab in labels:
        if type(lab) is not dict or lab.get('polarity') not in _POLARITY or type(lab.get('found_event_id')) is not str:
            raise ValueError('label must carry found_event_id and polarity')
        if lab.get('owner') != scope:
            raise ValueError('label scope differs from requested scope')
        seen.setdefault(lab['found_event_id'], set()).add(lab['polarity'])
    uniq = {}
    for r in receipts:
        uniq[r['found_event_id']] = r
    rows = [uniq[k] for k in sorted(uniq, key=lambda k: (uniq[k]['recall_attempt_id'], k))]
    conflicting = sorted(k for k, v in seen.items() if len(v) > 1)  # contradictory labels: never guess
    lab_map = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1 and k in uniq}
    stats = {'conflicting_labels': len(conflicting),
             'labels_without_receipt': sum(1 for k in seen if k not in uniq)}
    return rows, lab_map, stats


def _uninformative(rows):
    out = {}
    for f in FEATURES:
        vals = {r[f] for r in rows}
        if vals == {None}:
            out[f] = 'always_unknown'
        elif len(vals) == 1:
            out[f] = 'constant'
    return out


def _pairs_agreement(rows, lab_map, vecs):
    """Leave-one-attempt-out: each attempt is scored by a model that never saw its labels."""
    attempts = {}
    for r, x in zip(rows, vecs):
        attempts.setdefault(r['recall_attempt_id'], []).append((r, x))
    pairs = sh = dl = 0.0
    same = n_multi = 0
    for aid in sorted(attempts):
        group = attempts[aid]
        train = [(x, _POLARITY[lab_map[r['found_event_id']]]) for r, x in zip(rows, vecs)
                 if r['recall_attempt_id'] != aid and r['found_event_id'] in lab_map]
        theta = _fit(train)
        sc = {r['found_event_id']: _score(theta, x) for r, x in group}
        if len(group) > 1:
            n_multi += 1
            actual = [g[0]['found_event_id'] for g in sorted(group, key=lambda g: (g[0]['fts_ordinal'], g[0]['found_event_id']))]
            same += _order(group, sc) == actual
        lbl = [r for r, _ in group if r['found_event_id'] in lab_map]
        for p in lbl:
            for q in lbl:
                if lab_map[p['found_event_id']] == 'positive' and lab_map[q['found_event_id']] == 'negative':
                    pairs += 1
                    s1, s2 = sc[p['found_event_id']], sc[q['found_event_id']]
                    sh += 1.0 if s1 > s2 else 0.5 if s1 == s2 else 0.0
                    o1, o2 = p['fts_ordinal'], q['fts_ordinal']
                    dl += 1.0 if o1 < o2 else 0.5 if o1 == o2 else 0.0
    return int(pairs), (sh / pairs if pairs else None), (dl / pairs if pairs else None), same, n_multi


def _order(group, scores):
    """Shadow order: higher score first; ties fall back to the delivered order."""
    return [r['found_event_id'] for r, _ in sorted(
        group, key=lambda g: (-scores[g[0]['found_event_id']], g[0]['fts_ordinal'], g[0]['found_event_id']))]


def shadow_report(scope, receipts, labels):
    """Content-free shadow report for one owner scope. Raises ValueError on foreign-scope rows."""
    rows, lab_map, stats = _load(scope, receipts, labels)
    kinds = sorted({r['route_kind'] for r in rows})
    vecs = [encode_features(r, kinds) for r in rows]
    train = [(x, _POLARITY[lab_map[r['found_event_id']]]) for r, x in zip(rows, vecs) if r['found_event_id'] in lab_map]
    theta = _fit(train)
    groups = {}
    for r, x in zip(rows, vecs):
        groups.setdefault(r['recall_attempt_id'], []).append((r, x))
    order = []
    for aid in sorted(groups):
        sc = {r['found_event_id']: _score(theta, x) for r, x in groups[aid]}
        order.append({'recall_attempt_id': aid, 'found_event_ids': _order(groups[aid], sc),
                      'scores': [round(sc[i], 9) for i in _order(groups[aid], sc)]})
    n_lab = len(train)
    report = {
        'v': 1, 'mode': 'shadow', 'exploration': EXPLORATION,
        'state': 'no_receipts' if not rows else 'ok' if n_lab >= MIN_LABELLED else 'insufficient_labels',
        'receipts': len(rows), 'attempts': len(groups), 'labelled': n_lab,
        'positive': sum(1 for _, y in train if y == 1.0), 'negative': sum(1 for _, y in train if y == 0.0),
        'distinct_feature_vectors': len({tuple(r[f] for f in FEATURES) for r in rows}),
        'uninformative_features': _uninformative(rows),
        'min_labelled': MIN_LABELLED, 'agreement': None, 'agreement_withheld': None,
        'route_kinds': kinds, 'shadow_order': order, **stats,
    }
    if n_lab < MIN_LABELLED:
        report['agreement_withheld'] = 'below_min_labelled'
        return report
    pairs, sh, dl, same, multi = _pairs_agreement(rows, lab_map, vecs)
    if not pairs:
        report['agreement_withheld'] = 'no_mixed_pairs'
        return report
    report['agreement'] = {'basis': 'leave_one_attempt_out', 'mixed_pairs': pairs,
                           'shadow_pair_accuracy': sh, 'delivered_pair_accuracy': dl,
                           'attempts_same_order': same, 'attempts_multi_item': multi}
    return report
