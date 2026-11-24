"""Pure S02 resolver over trusted snapshots; no storage, grants, or activation.

These internal read DTOs are a proposed projection of S01 knowledge revisions,
not an existing schema or a second ontology. Callers must obtain a coherent,
authorized snapshot and revalidate its generations before using any result.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import json
import math
from typing import Any, Optional

AXES = ('importance', 'connections', 'consolidation')
MODES = frozenset({'rules', 'model', 'hybrid'})
MAX_NODES = 256
MAX_DEPTH = 32
MAX_PATHS = 2048
MAX_POLICY_BYTES = 32768


@dataclass(frozen=True)
class Scope:
    owner: str
    workspace: Optional[str] = None
    project: Optional[str] = None


@dataclass(frozen=True)
class AxisPolicy:
    axis: str
    value: Any  # None means explicit inherit; false and zero are values.
    mode: Optional[str]
    authority: str  # owner or inferred; never an access/trust grant.


@dataclass(frozen=True)
class Definition:
    id: str
    revision: int
    kind: str  # existing revisioned type or tag identity
    scope: Scope
    policies: tuple[AxisPolicy, ...]


@dataclass(frozen=True)
class Edge:
    id: str
    revision: int
    child_id: str
    parent_id: str
    scope: Scope


@dataclass(frozen=True)
class Assignment:
    id: str
    revision: int
    definition_id: str
    scope: Scope


@dataclass(frozen=True)
class Snapshot:
    scope: Scope
    generation: str
    memory_id: str
    memory_revision: int
    definitions: tuple[Definition, ...]
    edges: tuple[Edge, ...]
    assignments: tuple[Assignment, ...]
    manual: tuple[tuple[str, Any], ...] = ()
    manual_unknown: tuple[str, ...] = ()


def _identifier(value):
    return type(value) is str and 0 < len(value) <= 256 and value == value.strip()


def _scope_valid(scope):
    return (type(scope) is Scope and _identifier(scope.owner) and
            (scope.workspace is None or _identifier(scope.workspace)) and
            (scope.project is None or (_identifier(scope.project) and scope.workspace is not None)))


def _visible(scope, target):
    return (scope.owner == target.owner and (scope.workspace is None or scope.workspace == target.workspace) and
            (scope.project is None or scope.project == target.project))


def _value_valid(axis, value):
    if axis == 'importance':
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
    return type(value) is bool


def resolve(snapshot: Snapshot) -> dict[str, Any]:
    """Return all-or-nothing axis resolutions and exact contributing paths."""
    def unavailable(reason):
        return {'state': 'unavailable', 'reason': reason}
    try:
        if type(snapshot) is not Snapshot or not _scope_valid(snapshot.scope) or not _identifier(snapshot.generation) or not _identifier(snapshot.memory_id) or type(snapshot.memory_revision) is not int or snapshot.memory_revision < 1:
            return unavailable('invalid_snapshot')
        if any(type(value) is not tuple for value in (snapshot.definitions, snapshot.edges, snapshot.assignments, snapshot.manual, snapshot.manual_unknown)):
            return unavailable('invalid_snapshot')
        if len(snapshot.definitions) > MAX_NODES or len(snapshot.edges) > MAX_PATHS or len(snapshot.assignments) > MAX_NODES:
            return unavailable('limit_exceeded')
        definitions = {}
        for d in snapshot.definitions:
            if type(d) is not Definition or not _identifier(d.id) or type(d.revision) is not int or d.revision < 1 or d.kind not in {'type', 'tag'} or not _scope_valid(d.scope) or type(d.policies) is not tuple or d.id in definitions:
                return unavailable('invalid_snapshot')
            definitions[d.id] = d
            seen = set()
            for p in d.policies:
                if type(p) is not AxisPolicy or p.axis not in AXES or p.axis in seen or p.authority not in {'owner', 'inferred'} or (p.value is None and p.mode is not None) or (p.value is not None and (p.mode not in MODES or not _value_valid(p.axis, p.value))):
                    return unavailable('invalid_policy')
                seen.add(p.axis)
        payload = json.dumps([[(p.axis, p.value, p.mode, p.authority) for p in d.policies] for d in snapshot.definitions], separators=(',', ':'), allow_nan=False)
        if len(payload.encode()) > MAX_POLICY_BYTES:
            return unavailable('limit_exceeded')
        adjacency, edge_ids = {}, set()
        for edge in snapshot.edges:
            if type(edge) is not Edge or not _identifier(edge.id) or edge.id in edge_ids or type(edge.revision) is not int or edge.revision < 1 or not _identifier(edge.child_id) or not _identifier(edge.parent_id) or not _scope_valid(edge.scope):
                return unavailable('invalid_snapshot')
            edge_ids.add(edge.id)
            adjacency.setdefault(edge.child_id, []).append(edge)
        # Validate the complete supplied graph, not only the winning branch.
        for edge in snapshot.edges:
            child, parent = definitions.get(edge.child_id), definitions.get(edge.parent_id)
            if not child or not parent or not _visible(child.scope, snapshot.scope) or not _visible(parent.scope, snapshot.scope) or not _visible(edge.scope, snapshot.scope):
                return unavailable('inaccessible_parent')
        color, heights = {}, {}
        def visit(node):
            if color.get(node) == 1:
                raise ValueError('cycle')
            if color.get(node) == 2:
                return heights[node]
            color[node] = 1
            height = 0
            for edge in adjacency.get(node, ()):
                height = max(height, 1 + visit(edge.parent_id))
                if height > MAX_DEPTH:
                    raise OverflowError('limit_exceeded')
            color[node] = 2
            heights[node] = height
            return height
        try:
            for node in definitions:
                visit(node)
        except ValueError:
            return unavailable('cycle')
        except OverflowError:
            return unavailable('limit_exceeded')
        assignment_ids = set()
        for a in snapshot.assignments:
            if type(a) is not Assignment or not _identifier(a.id) or a.id in assignment_ids or type(a.revision) is not int or a.revision < 1 or not _identifier(a.definition_id) or not _scope_valid(a.scope) or not _visible(a.scope, snapshot.scope):
                return unavailable('inaccessible_assignment')
            assignment_ids.add(a.id)
        manual = {}
        for item in snapshot.manual:
            if type(item) is not tuple or len(item) != 2 or item[0] not in AXES or item[0] in manual or not _value_valid(item[0], item[1]):
                return unavailable('invalid_manual_value')
            manual[item[0]] = item[1]
        if (len(snapshot.manual_unknown) > len(AXES) or any(axis not in AXES for axis in snapshot.manual_unknown)
                or len(set(snapshot.manual_unknown)) != len(snapshot.manual_unknown)
                or any(axis in manual for axis in snapshot.manual_unknown)):
            return unavailable('invalid_manual_authority')
        paths, queue = [], deque()
        for a in sorted(snapshot.assignments, key=lambda x: x.id):
            if len(paths) + len(queue) >= MAX_PATHS:
                return unavailable('limit_exceeded')
            queue.append((a, a.definition_id, (), ()))
        while queue:
            assignment, node, nodes, edges = queue.popleft()
            if node in nodes:
                return unavailable('cycle')
            if len(nodes) > MAX_DEPTH or len(paths) >= MAX_PATHS:
                return unavailable('limit_exceeded')
            definition = definitions.get(node)
            if definition is None or not _visible(definition.scope, snapshot.scope):
                return unavailable('inaccessible_parent')
            chain = nodes + (node,)
            paths.append((assignment, definition, chain, edges))
            for edge in sorted(adjacency.get(node, ()), key=lambda x: (x.parent_id, x.id)):
                if not _visible(edge.scope, snapshot.scope):
                    return unavailable('inaccessible_parent')
                if len(paths) + len(queue) >= MAX_PATHS:
                    return unavailable('limit_exceeded')
                queue.append((assignment, edge.parent_id, chain, edges + (edge,)))
        axes = {}
        for axis in AXES:
            if axis in snapshot.manual_unknown:
                axes[axis] = {'state': 'unavailable', 'value': None, 'mode': None, 'reason': 'manual_authority_unknown', 'origins': []}
                continue
            if axis in manual:
                axes[axis] = {'state': 'resolved', 'value': manual[axis], 'mode': None, 'reason': 'manual_memory_field', 'origins': []}
                continue
            candidates = []
            for assignment, definition, chain, edges in paths:
                for policy in definition.policies:
                    if policy.axis == axis and policy.value is not None:
                        origin = {'assignment_id': assignment.id, 'assignment_revision': assignment.revision,
                                  'definition_id': definition.id, 'definition_revision': definition.revision,
                                  'path': [{'definition_id': node, 'definition_revision': definitions[node].revision} for node in chain],
                                  'edges': [{'id': edge.id, 'revision': edge.revision} for edge in edges],
                                  'authority': policy.authority,
                                  'scope': {'owner': definition.scope.owner, 'workspace': definition.scope.workspace, 'project': definition.scope.project}}
                        scope_rank = 0 if definition.scope.project is not None else (1 if definition.scope.workspace is not None else 2)
                        candidates.append((len(edges), 0 if policy.authority == 'owner' else 1, scope_rank, policy, origin))
            if not candidates:
                inherited = any(p.axis == axis and p.value is None for _, definition, _, _ in paths for p in definition.policies)
                axes[axis] = {'state': 'unset', 'value': None, 'mode': None, 'reason': 'explicit_inherit' if inherited else 'missing', 'origins': []}
                continue
            rank = min((item[0], item[1], item[2]) for item in candidates)
            winners = [item for item in candidates if (item[0], item[1], item[2]) == rank]
            origins = sorted((item[4] for item in winners), key=lambda x: json.dumps(x, sort_keys=True))
            values = {(json.dumps(item[3].value, sort_keys=True), item[3].mode) for item in winners}
            if len(values) != 1:
                axes[axis] = {'state': 'unresolved', 'value': None, 'mode': None, 'reason': 'equal_specificity_conflict', 'origins': origins}
            else:
                policy = winners[0][3]
                axes[axis] = {'state': 'resolved', 'value': policy.value, 'mode': policy.mode, 'reason': 'nearest_authoritative_policy', 'origins': origins}
        return {'state': 'resolved', 'generation': snapshot.generation, 'memory_id': snapshot.memory_id,
                'memory_revision': snapshot.memory_revision, 'scope': snapshot.scope, 'axes': axes}
    except (TypeError, ValueError, OverflowError):
        return unavailable('invalid_snapshot')


def resolve_native_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """Validate an authorized native snapshot; unknown manual axes stay unavailable."""
    try:
        if type(value) is not dict or set(value)!={'version','state','scope','generation','memory_id','memory_revision','definitions','edges','assignments','manual_authority'} or value['version']!=1 or value['state']!='ready':
            raise ValueError
        def scope(v):
            if type(v) is not dict or set(v)!={'owner','workspace','project'}: raise ValueError
            return Scope(v['owner'],v['workspace'],v['project'])
        def policy(v):
            if type(v) is not dict or set(v)!={'axis','value','mode','authority'}: raise ValueError
            return AxisPolicy(v['axis'],v['value'],v['mode'],v['authority'])
        definitions=[]
        for d in value['definitions']:
            if type(d) is not dict or set(d)!={'id','revision','kind','scope','policies'} or type(d['policies']) is not list: raise ValueError
            definitions.append(Definition(d['id'],d['revision'],d['kind'],scope(d['scope']),tuple(policy(p) for p in d['policies'])))
        edges=[]
        for e in value['edges']:
            if type(e) is not dict or set(e)!={'id','revision','child_id','parent_id','scope'}: raise ValueError
            edges.append(Edge(e['id'],e['revision'],e['child_id'],e['parent_id'],scope(e['scope'])))
        assignments=[]
        for x in value['assignments']:
            if type(x) is not dict or set(x)!={'id','revision','definition_id','scope'}: raise ValueError
            assignments.append(Assignment(x['id'],x['revision'],x['definition_id'],scope(x['scope'])))
        authority=value['manual_authority']
        if type(authority) is not dict or set(authority)!=set(AXES): raise ValueError
        unknown=[];manual=[]
        for axis,entry in authority.items():
            if entry=='unknown':unknown.append(axis)
            elif type(entry) is dict and entry=={'state':'inherit'}:pass
            elif (axis=='importance' and type(entry) is dict and set(entry)=={'state','witness_sha256'}
                and entry['state']=='known_absent' and type(entry['witness_sha256']) is str
                and len(entry['witness_sha256'])==64 and all(c in '0123456789abcdef' for c in entry['witness_sha256'])):pass
            elif type(entry) is dict and set(entry)=={'state','value'} and entry['state']=='value' and _value_valid(axis,entry['value']):manual.append((axis,entry['value']))
            else:raise ValueError
        result=resolve(Snapshot(scope(value['scope']),value['generation'],value['memory_id'],value['memory_revision'],tuple(definitions),tuple(edges),tuple(assignments),manual=tuple(manual),manual_unknown=tuple(unknown)))
        if result.get('state')=='resolved':
            result['scope']=value['scope'];result['manual_authority']=authority
        return result
    except (TypeError,ValueError,KeyError,OverflowError):
        return {'state':'unavailable','reason':'invalid_native_snapshot'}
