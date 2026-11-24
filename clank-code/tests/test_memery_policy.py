from dataclasses import replace
from src.memery_policy import Scope, AxisPolicy, Definition, Edge, Assignment, Snapshot, resolve, MAX_NODES


def test_sparse_policy_diamond_conflict_manual_and_bounds():
    scope = Scope('alice', 'w', 'p')
    base = Definition('base', 1, 'type', scope, (AxisPolicy('importance', 0, 'rules', 'owner'),))
    left = Definition('left', 2, 'tag', scope, (AxisPolicy('connections', False, 'rules', 'owner'),))
    right = Definition('right', 3, 'tag', scope, (AxisPolicy('connections', False, 'rules', 'owner'),))
    top = Definition('top', 4, 'type', scope, ())
    edges = (Edge('a',1,'top','left',scope), Edge('b',1,'top','right',scope), Edge('c',1,'left','base',scope), Edge('d',1,'right','base',scope))
    snapshot = Snapshot(scope, 'graph-v1', 'memory', 7, (base,left,right,top), edges, (Assignment('assign',1,'top',scope),))
    result = resolve(snapshot)
    assert result['axes']['importance']['value'] == 0 and len(result['axes']['importance']['origins']) == 2
    assert result['axes']['connections']['value'] is False and len(result['axes']['connections']['origins']) == 2
    assert resolve(replace(snapshot, edges=tuple(reversed(edges)))) == result
    conflict = replace(right, policies=(AxisPolicy('connections', True, 'rules', 'owner'),))
    changed = replace(snapshot, definitions=(base,left,conflict,top))
    assert resolve(changed)['axes']['connections']['state'] == 'unresolved'
    assert resolve(replace(changed, manual=(('connections', False),)))['axes']['connections']['reason'] == 'manual_memory_field'
    assert resolve(replace(snapshot, edges=edges + (Edge('cycle',1,'base','top',scope),)))['reason'] == 'cycle'
    assert resolve(replace(snapshot, scope=Scope('bob','w','p')))['state'] == 'unavailable'
    assert resolve(replace(snapshot, definitions=(base,) * (MAX_NODES + 1)))['reason'] == 'limit_exceeded'


def test_scope_specificity_applies_after_distance_and_authority():
    scope = Scope('alice', 'workspace', 'project')
    global_def = Definition('global', 1, 'tag', Scope('alice'), (AxisPolicy('connections', False, 'rules', 'owner'),))
    project_def = Definition('project', 2, 'tag', scope, (AxisPolicy('connections', True, 'rules', 'owner'),))
    snap = Snapshot(scope, 'generation', 'memory', 1, (global_def, project_def), (),
                    (Assignment('global-assignment',1,'global',scope), Assignment('project-assignment',1,'project',scope)))
    result = resolve(snap)['axes']['connections']
    assert result['value'] is True and result['origins'][0]['definition_id'] == 'project'
    inferred = replace(project_def, policies=(AxisPolicy('connections', True, 'rules', 'inferred'),))
    assert resolve(replace(snap, definitions=(global_def, inferred)))['axes']['connections']['value'] is False
