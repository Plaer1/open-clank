"""Deterministic General defaults and immutable operation-boundary snapshots."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from src.general_hex_library import GeneralHexRepository, GeneralHexError, _json, _identity, tag_keys


def ensure_general_hex_snapshot_schema(db_path):
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS fm_general_hex_snapshots (
            owner_id TEXT NOT NULL, boundary_id TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, PRIMARY KEY(owner_id,boundary_id))''')


def workspace_source(owner_id, workspace, db_path):
    if not workspace:
        return None
    from src.project_hex import project_for_root, resolve_hex, hex_activation_current
    project = project_for_root(workspace, owner=owner_id, db_path=db_path)
    if not project:
        return {'state': 'unregistered', 'project_id': None, 'contract_hash': None, 'rules': []}
    resolution = resolve_hex(workspace, workspace_root=project['canonical_root'])
    active = hex_activation_current(resolution, owner=owner_id, project_id=project['project_id'], db_path=db_path)
    return dict(state='active' if active else 'inactive', project_id=project['project_id'],
                workspace_id=project['workspace_id'], contract_hash=resolution.contract_hash,
                contract_path=resolution.contract_path, engine_version=resolution.engine_version,
                rules=(resolution.contract or {}).get('hexes', (resolution.contract or {}).get('henxels', [])) if active else [])


def compose_general_hexes(inputs, *, owner_id, workspace=None, budget_chars=24000):
    """Pure explanation used identically by preview and pinned runtime turns.

    Budget applies only to General defaults. Existing workspace enforcement and
    its instruction path remain separately authoritative and are never cut here.
    """
    owner_id = _identity(owner_id)
    if type(budget_chars) is not int or budget_chars < 0 or budget_chars > 200000:
        raise GeneralHexError('budget_chars must be between 0 and 200000')
    contexts = {kind: inputs.get(kind) for kind in ('workspace_tags', 'task_tags')}
    keys = set()
    for record in contexts.values():
        if record:
            keys.update(tag_keys(record['tags']))
    seen, decisions, rendered, used = set(), [], [], 0
    for entry in sorted(inputs['entries'], key=lambda item: (item['hex_id'], item['revision'])):
        if entry['owner_id'] != owner_id:
            raise GeneralHexError('composition inputs belong to another owner')
        identity = (entry['hex_id'], entry['revision'])
        matches = [label for label in entry['tags'] if label.casefold() in keys]
        decision = dict(hex_id=entry['hex_id'], revision=entry['revision'], hash=entry['hash'],
                        title=entry['title'], tags=entry['tags'], matching_tags=matches,
                        provenance=entry['provenance'], reason='untagged-global' if not entry['tags'] else 'positive-any')
        if identity in seen:
            decision['state'] = 'deduplicated'
        elif not entry['enabled']:
            decision['state'] = 'disabled'
        elif not entry['accepted']:
            decision['state'] = 'unaccepted'
        elif entry['tags'] and not matches:
            decision['state'] = 'unmatched'
        else:
            block = f"General default: {entry['title']} [revision {entry['revision']}]\n{entry['body']}"
            cost = len(block) + (2 if rendered else 0)
            if used + cost > budget_chars:
                decision['state'] = 'omitted-for-budget'
            else:
                decision['state'] = 'included'
                rendered.append(block)
                used += cost
        seen.add(identity)
        decisions.append(decision)
    snapshot = dict(schema='open-clank.hex-composition.v1', owner_id=owner_id,
                    context_tags=contexts, workspace=workspace, entries=decisions,
                    budget=dict(limit_chars=budget_chars, used_chars=used,
                                omitted_count=sum(item['state']=='omitted-for-budget' for item in decisions)),
                    defaults='\n\n'.join(rendered),
                    precedence='Current explicit user instructions take precedence over saved General defaults. Existing workspace enforcement remains authoritative.')
    snapshot['snapshot_hash'] = hashlib.sha256(_json(snapshot).encode()).hexdigest()
    return snapshot


class GeneralHexComposition:
    def __init__(self, db_path):
        if not db_path:
            raise GeneralHexError('General Hex authority is unavailable')
        self.repository = GeneralHexRepository(db_path)

    def preview(self, owner_id, *, workspace=None, workspace_id=None, task_id=None, budget_chars=24000):
        source = workspace_source(owner_id, workspace, self.repository.db_path)
        workspace_id = workspace_id or ((source or {}).get('workspace_id'))
        inputs = self.repository.read_composition_inputs(owner_id, workspace_id=workspace_id, task_id=task_id)
        return compose_general_hexes(inputs, owner_id=owner_id, workspace=source, budget_chars=budget_chars)

    def snapshot(self, owner_id, boundary_id, **context):
        owner_id, boundary_id = _identity(owner_id), _identity(boundary_id)
        with self.repository._connection() as conn:
            row = conn.execute('SELECT snapshot_json FROM fm_general_hex_snapshots WHERE owner_id=? AND boundary_id=?', (owner_id,boundary_id)).fetchone()
        if row:
            saved = json.loads(row[0])
            if saved['owner_id'] != owner_id:
                saved['historical_owner_id'] = saved['owner_id']
                saved['owner_id'] = owner_id
            return saved
        snapshot = self.preview(owner_id, **context)
        snapshot['boundary_id'] = boundary_id
        with self.repository._connection(write=True) as conn:
            conn.execute('INSERT OR IGNORE INTO fm_general_hex_snapshots VALUES(?,?,?,?)', (owner_id,boundary_id,snapshot['snapshot_hash'],_json(snapshot)))
            saved = conn.execute('SELECT snapshot_json FROM fm_general_hex_snapshots WHERE owner_id=? AND boundary_id=?', (owner_id,boundary_id)).fetchone()
        return json.loads(saved[0])


def defaults_instruction(snapshot):
    # Workspace declarations are already supplied by the native workspace
    # AGENTS/policy path. Keep their identity visible without a duplicate
    # all-rule rendering that would bypass the General instruction budget.
    if not snapshot['defaults']:
        return ''
    source = snapshot.get('workspace') or {}
    reference = ''
    if source.get('contract_hash'):
        reference = '\nWorkspace policy source: ' + str(source.get('project_id')) + ' / ' + str(source['contract_hash']) + ' (' + str(source.get('state')) + '). Existing workspace instructions and enforced scopes remain authoritative.'
    return '[Saved General defaults]\n' + snapshot['precedence'] + reference + '\n\n' + snapshot['defaults'] + '\n[/Saved General defaults]'
