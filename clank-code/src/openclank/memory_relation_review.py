"""Human-reviewed suggestions over the existing scoped Memory records and audit."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from contextlib import closing

KINDS = frozenset({'duplicate','conflict','lesson'})
REASON = 'memory_relation_declined.v1'


def _material(account, workspace, kind, left, right):
    # A decline applies to both views of the same exact pair and classification.
    pair=sorted(((left['block_id'],left['revision']), (right['block_id'],right['revision'])))
    return {'version':1,'account':account,'workspace':workspace,'kind':kind,'pair':[list(ref) for ref in pair]}


def _id(material):
    raw=json.dumps(material,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    return 'cs_v2_'+hashlib.sha256(raw).hexdigest()[:32]


def _decline_row(db, owner, material):
    change=_id(material)
    row=db.execute('SELECT operation_metadata_json,content_hash FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason=?',
                   (owner,change,REASON)).fetchone()
    if row is None:
        return None
    raw=row[0]; metadata=json.loads(raw)
    if hashlib.sha256(raw.encode('utf-8')).hexdigest()!=row[1] or metadata.get('pair_material')!=material or metadata.get('decision')!='declined':
        raise ValueError('Invalid relation review receipt')
    return {'state':'declined','receipt_id':change,'evidence_sha256':metadata['evidence_sha256']}


def read_declines(db_path, owner, account, workspace, pairs):
    from pathlib import Path
    import sqlite3
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
        return [_decline_row(db,owner,_material(account,workspace,kind,left,right)) for kind,left,right in pairs]


def retain_decline(db_path, owner, account, workspace, evidence, current):
    from src.memery_v2 import V2Repository, _now
    material=_material(account,workspace,evidence['kind'],evidence['left'],evidence['right'])
    digest=evidence['evidence_sha256']
    if evidence['kind'] not in KINDS or not re.fullmatch(r'[0-9a-f]{64}',digest) or current() is not True:
        raise ValueError('Invalid relation review decision')
    repository=V2Repository(db_path,guarded_v17=True)
    with closing(repository._connect()) as db:
        db.execute('PRAGMA busy_timeout=500')
        db.execute('BEGIN IMMEDIATE')
        try:
            prior=_decline_row(db,owner,material)
            if prior is not None:
                if current() is not True:
                    raise ValueError('Relation review authority changed')
                db.rollback()
                return prior
            for block, revision in material['pair']:
                present=db.execute('SELECT 1 FROM fm_v2_knowledge_blocks WHERE owner_id=? AND workspace_key=? AND project_key=\'\' AND block_id=? AND current_revision=?',
                                   (owner,workspace,block,revision)).fetchone()
                if present is None:
                    raise ValueError('Memory revisions changed')
            metadata={'pair_material':material,'decision':'declined','evidence_sha256':digest}
            receipt_id=repository._change_set(db,owner=owner,actor_type='user',actor_id=owner,reason=REASON,
                source_event_id=None,payload={},now=_now(),operation_metadata=metadata,operation_change_set_id=_id(material))
            if current() is not True:
                raise ValueError('Relation review authority changed')
            db.commit()
            return {'state':'declined','receipt_id':receipt_id,'evidence_sha256':digest}
        except BaseException:
            db.rollback()
            raise


def _normalized(text):
    return ' '.join(unicodedata.normalize('NFKC',text).casefold().split())


def _sources(record):
    allowed=set(record['current'].get('evidence_ids') or ())
    return {(item['source_id'],item['source_revision']) for item in record.get('evidence',())
            if item.get('id') in allowed and item.get('forget_state')=='active'
            and isinstance(item.get('source_id'),str) and type(item.get('source_revision')) is int}


def classify_pair(left, right):
    """Return evidence-backed hypotheses. Native conflicts are never findings."""
    a,b=left['current'],right['current']
    av,bv=a.get('value'),b.get('value')
    at,bt=(_normalized(item.get('text') or '') for item in (a,b))
    if not at or not bt or a.get('status')!='active' or b.get('status')!='active':
        return None
    if at==bt:
        return 'duplicate','same_normalized_claim'
    shared=_sources(left)&_sources(right)
    aw,bw=(set(re.findall(r'[^\W_]+',text)) for text in (at,bt))
    if shared and len(aw&bw)>=5 and 5*len(aw&bw)>=4*len(aw|bw):
        return 'duplicate','shared_source_and_wording'
    same_claim=a.get('subject_entity_id') and (a.get('subject_entity_id'),a.get('predicate'))==(b.get('subject_entity_id'),b.get('predicate'))
    if same_claim and av!=bv:
        competing=any(item.get('state')=='open' and isinstance(item.get('competing_payload'),dict)
                      and item['competing_payload'].get('value')==other
                      for record,other in ((left,bv),(right,av)) for item in record.get('conflicts',())[:16])
        if competing:
            return 'conflict','matching_native_conflict'
        prior_end,new_start=b.get('valid_to'),a.get('valid_from')
        if isinstance(prior_end,str) and isinstance(new_start,str) and prior_end and prior_end<=new_start:
            return 'lesson','successive_claim_revisions'
        reverse_end,reverse_start=a.get('valid_to'),b.get('valid_from')
        if isinstance(reverse_end,str) and isinstance(reverse_start,str) and reverse_end and reverse_end<=reverse_start:
            return None
        if a.get('cardinality')==b.get('cardinality')=='single':
            return 'conflict','different_values_for_single_claim'
    lesson_kinds={'instruction','procedural','summary','journal'}
    if shared and a.get('kind')!=b.get('kind') and (a.get('kind') in lesson_kinds or b.get('kind') in lesson_kinds):
        return 'lesson','shared_source_with_guidance'
    return None
