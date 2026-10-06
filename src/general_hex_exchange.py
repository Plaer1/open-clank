"""Reviewed copies between General and the existing workspace publication path."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
import yaml
from src.general_hex_library import GeneralHexRepository, GeneralHexError, GeneralHexConflict, _json, normalize_tags, _identity, _revision
from src.project_hex import get_project, require_hex_activation, publish_contract_update, _append_rule_bytes, _read_contract_bytes
from pathlib import Path


def ensure_general_hex_exchange_schema(db_path):
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS fm_general_hex_exchange_drafts (
            owner_id TEXT NOT NULL, draft_id TEXT NOT NULL, revision INTEGER NOT NULL,
            payload_json TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(owner_id,draft_id))''')


def _declarative(rule):
    for key in ('hex','rule','must','description','henxel'):
        if isinstance(rule.get(key),str) and rule[key].strip():
            return rule[key]
    raise GeneralHexError('selected workspace rule has no declarative instruction')


def _scope(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value,list) or not value or len(value)>100 or any(not isinstance(x,str) or not x.strip() or len(x)>1000 for x in value):
        raise GeneralHexError('reviewed static scope must contain positive path patterns')
    return value


class GeneralHexExchange:
    def __init__(self, db_path):
        if not db_path:
            raise GeneralHexError('General Hex authority is unavailable')
        self.repository=GeneralHexRepository(db_path)
        self.db_path=str(db_path)

    def _source(self, owner_id, project_id):
        project=get_project(project_id,owner=owner_id,db_path=self.db_path)
        if not project:
            raise GeneralHexError('owner project not found')
        resolution=require_hex_activation(project['canonical_root'],owner=owner_id,project_id=project_id,db_path=self.db_path,workspace_root=project['canonical_root'])
        return project,resolution

    def _draft(self, owner_id, payload):
        draft_id=uuid.uuid4().hex
        with self.repository._connection(write=True) as conn:
            conn.execute('INSERT INTO fm_general_hex_exchange_drafts VALUES(?,?,1,?,0)',(_identity(owner_id),draft_id,_json(payload)))
        return dict(draft_id=draft_id,draft_revision=1,**payload)

    def _read_draft(self, conn, owner_id, draft_id, expected_draft_revision):
        _revision(expected_draft_revision)
        row=conn.execute('SELECT revision,payload_json,consumed FROM fm_general_hex_exchange_drafts WHERE owner_id=? AND draft_id=?',(_identity(owner_id),_identity(draft_id))).fetchone()
        if not row or row[0]!=expected_draft_revision or row[2]:
            raise GeneralHexConflict('review draft changed or was already consumed')
        return json.loads(row[1])

    def preview_promotion(self, owner_id, *, project_id, rule_index):
        project,resolution=self._source(owner_id,project_id)
        rules=(resolution.contract or {}).get('hexes',(resolution.contract or {}).get('henxels',[]))
        if type(rule_index) is not int or not 0<=rule_index<len(rules):
            raise GeneralHexError('select an existing workspace rule')
        rule=rules[rule_index]
        body=_declarative(rule)
        reserved={'hex','rule','must','description','henxel','in','except','level','why','context','comment'}
        source=dict(project_id=project_id,workspace_id=project['workspace_id'],contract_path=resolution.contract_path,
                    contract_hash=resolution.contract_hash,engine_version=resolution.engine_version,
                    rule_index=rule_index,rule_hash=hashlib.sha256(_json(rule).encode()).hexdigest(),
                    original_in=rule.get('in',['./*']),original_except=rule.get('except',[]),original_body=body,
                    unsupported_checks={k:v for k,v in rule.items() if k not in reserved})
        return self._draft(owner_id,dict(kind='promotion',source=source,title=body[:100],body=body,tags=[],
                           applicability='No tags = owner-global. Positive tags = Experimental ANY explicit workspace/task tag match.',
                           scope_warning='Saving an untagged copy broadens the instruction to all owner contexts. Original workspace scope remains source information; edit the body to retain its conditions.',
                           original_workspace_unchanged=True))

    def save_promotion(self, owner_id, *, draft_id, expected_draft_revision, title, body, tags=(), enabled=True):
        with self.repository._connection() as conn:
            draft=self._read_draft(conn,owner_id,draft_id,expected_draft_revision)
        if draft['kind']!='promotion':
            raise GeneralHexError('promotion review required')
        source=draft['source']; _,resolution=self._source(owner_id,source['project_id'])
        if resolution.contract_hash!=source['contract_hash']:
            raise GeneralHexConflict('source workspace contract changed; review again')
        rule=resolution.contract.get('hexes',resolution.contract.get('henxels',[]))[source['rule_index']]
        if hashlib.sha256(_json(rule).encode()).hexdigest()!=source['rule_hash']:
            raise GeneralHexConflict('selected source rule changed; review again')
        with self.repository._connection(write=True) as conn:
            self._read_draft(conn,owner_id,draft_id,expected_draft_revision)
            saved=self.repository._save(conn,_identity(owner_id),uuid.uuid4().hex,1,dict(title=title,body=body,tags=tags,enabled=enabled,authorship='user',accepted=True,provenance={'workspace_source':source,'promotion_draft_id':draft_id}))
            conn.execute('UPDATE fm_general_hex_exchange_drafts SET consumed=1,revision=revision+1 WHERE owner_id=? AND draft_id=?',(owner_id,draft_id))
        return saved

    def export(self, owner_id, hex_id, *, revision=None):
        entry=self.repository.get(owner_id,hex_id,revision)
        return dict(schema='open-clank.general-hex.export.v1',owner_id=owner_id,entry={k:entry[k] for k in ('title','body','tags','enabled','authorship','provenance')},source=dict(hex_id=hex_id,revision=entry['revision'],hash=entry['hash']))

    def import_payload(self, owner_id, payload, *, accept_assisted=False):
        if not isinstance(payload,dict) or payload.get('schema')!='open-clank.general-hex.export.v1' or payload.get('owner_id')!=owner_id or set(payload)-{'schema','owner_id','entry','source'}:
            raise GeneralHexError('valid versioned export for this owner is required')
        entry=payload.get('entry')
        if not isinstance(entry,dict) or set(entry)-{'title','body','tags','enabled','authorship','provenance'}:
            raise GeneralHexError('invalid General Hex export entry')
        values=dict(entry)
        provenance=values.get('provenance') or {}
        if not isinstance(provenance,dict):
            raise GeneralHexError('provenance must be a JSON object')
        values['provenance']=dict(provenance,import_source=payload.get('source'),import_kind='explicit-file-copy')
        values['accepted']=True if values.get('authorship','user')=='user' else bool(accept_assisted)
        if accept_assisted:
            values['provenance']['acceptance']=dict(owner_id=owner_id,source='explicit-user-import-acceptance')
        return self.repository.create(owner_id,**values)

    def preview_application(self, owner_id, *, hex_id, revision, project_id, scope_in=None, scope_except=None):
        entry=self.repository.get(owner_id,hex_id,revision)
        if not entry['accepted']:
            raise GeneralHexError('accept the General instruction before workspace application')
        _,resolution=self._source(owner_id,project_id)
        source=entry['provenance'].get('workspace_source') or {}
        unchanged=source.get('project_id')==project_id and source.get('original_body')==entry['body']
        if scope_in is None:
            if not unchanged:
                raise GeneralHexError('explicitly review a static workspace in scope; tags are not path rules')
            scope_in=source['original_in']
        if scope_except is None:
            scope_except=source.get('original_except',[]) if unchanged else []
        rule={'hex':entry['body'],'in':_scope(scope_in)}
        if scope_except:
            rule['except']=_scope(scope_except)
        raw=_read_contract_bytes(Path(resolution.contract_path))
        candidate=_append_rule_bytes(raw,resolution.contract,rule).decode()
        return self._draft(owner_id,dict(kind='application',hex_id=hex_id,revision=revision,hex_hash=entry['hash'],
                           project_id=project_id,base_contract_hash=resolution.contract_hash,candidate_text=candidate,
                           candidate_hash=hashlib.sha256(candidate.encode()).hexdigest(),rule=rule,
                           general_tags=entry['tags'],general_tags_are_not_workspace_checks=True))

    def apply(self, owner_id, *, draft_id, expected_draft_revision):
        with self.repository._connection() as conn:
            draft=self._read_draft(conn,owner_id,draft_id,expected_draft_revision)
        if draft['kind']!='application':
            raise GeneralHexError('workspace application review required')
        current=self.repository.get(owner_id,draft['hex_id'])
        if current['revision']!=draft['revision'] or current['hash']!=draft['hex_hash']:
            raise GeneralHexConflict('General Hex changed; review application again')
        result=publish_contract_update(owner=owner_id,project_id=draft['project_id'],expected_contract_hash=draft['base_contract_hash'],candidate_text=draft['candidate_text'],db_path=self.db_path,actor_id=owner_id,general_hex_source={'hex_id':draft['hex_id'],'revision':draft['revision'],'hash':draft['hex_hash']})
        with self.repository._connection(write=True) as conn:
            conn.execute('UPDATE fm_general_hex_exchange_drafts SET consumed=1,revision=revision+1 WHERE owner_id=? AND draft_id=?',(owner_id,draft_id))
        return dict(result,general_source=dict(hex_id=draft['hex_id'],revision=draft['revision'],hash=draft['hex_hash']))
