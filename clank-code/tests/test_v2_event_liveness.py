import json
import blake3
import sqlite3
from src.memery_v2 import list_current_records, search_current_records


def test_event_hidden_without_verifier_and_legacy_readable(tmp_path):
    path = str(tmp_path / 'current.db')
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE fm_v2_knowledge_blocks(owner_id TEXT,block_id TEXT,workspace_key TEXT,project_key TEXT,subject_entity_id TEXT,predicate TEXT,claim_slot TEXT,current_revision INTEGER);
CREATE TABLE fm_v2_knowledge_revisions(owner_id TEXT,block_id TEXT,revision INTEGER,value_type TEXT,value_json TEXT,expected_value_type TEXT,kind TEXT,tags_json TEXT,status TEXT,confidence REAL,trust REAL,activation TEXT,activation_rationale TEXT,evidence_json TEXT,source_event_id TEXT,created_at TEXT);
CREATE TABLE fm_v2_trust_assignments(owner_id TEXT,assignment_id TEXT,subject_kind TEXT,subject_id TEXT,subject_revision INTEGER,workspace_key TEXT,project_key TEXT,assignment_revision INTEGER,state TEXT,trust REAL,actor_type TEXT,actor_id TEXT,reason_code TEXT,rationale TEXT,evidence_json TEXT,supersedes_assignment_id TEXT,content_hash TEXT,created_at TEXT);
CREATE TABLE fm_v2_evidence(owner_id TEXT,evidence_id TEXT,source_id TEXT,source_revision INTEGER,locator_type TEXT,locator_json TEXT,quote TEXT,created_at TEXT);
CREATE TABLE fm_v2_sources(owner_id TEXT,source_id TEXT,source_revision INTEGER,source_uri TEXT,content_hash TEXT,forget_state TEXT,workspace_key TEXT,project_key TEXT);
CREATE TABLE fm_v2_privacy_erasure_context(owner_id TEXT,source_id TEXT); CREATE TABLE fm_v2_privacy_erasure_tombstones(owner_id TEXT,source_id TEXT);
CREATE TABLE raw(id TEXT,owner TEXT,workspace_id TEXT,role TEXT,content TEXT,session_id TEXT,metadata TEXT);''')
        for block, evidence in [('event','e1'),('legacy','e2')]:
            db.execute('INSERT INTO fm_v2_knowledge_blocks VALUES (?,?,?,?,?,?,?,1)',('alice',block,'w','','entity','fact',block))
            db.execute('INSERT INTO fm_v2_knowledge_revisions VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)',('alice',block,'string',json.dumps({'value':block+' body'}),None,'fact','[]','active',0.5,0.5,'active','{}',json.dumps([evidence]),'memory:'+block,'2026'))
        digest = lambda value: blake3.blake3(value.encode()).hexdigest()
        raw_text = ' event body '
        raw_id = 'raw_' + digest('event\x1fuser')[:24]
        raw_hash = digest(raw_text)
        locator = {'kind':'native_current_event','raw_id':raw_id,'role':'user','source_event_id':'event','session_id':'session','source_revision':1,'raw_hash_algorithm':'blake3','raw_content_hash':raw_hash,'candidate_hash_algorithm':'blake3','candidate_content_hash':digest('event body'),'transform':'trim'}
        metadata = {'source_event_id':'event','turn_id':'turn_'+digest('event')[:24],'source_uri':'event://alice/event#user','source_revision':1,'content_hash':raw_hash,'source_type':'conversation_message'}
        db.execute("INSERT INTO raw VALUES (?,?,?,?,?,?,?)",(raw_id,'alice','w','user',raw_text,'session',json.dumps(metadata)))
        db.execute("INSERT INTO fm_v2_sources VALUES (?,?,1,?,?,?,'w','')",('alice','s1','event://alice/event#user',raw_hash,'active'))
        db.execute("INSERT INTO fm_v2_sources VALUES ('alice','s2',1,'memory://legacy','hash','active','w','')")
        db.execute("INSERT INTO fm_v2_evidence VALUES (?,?,?,1,?,?,?,?)",('alice','e1','s1','native_current_event',json.dumps(locator),'event body','2026'))
        db.execute("INSERT INTO fm_v2_evidence VALUES ('alice','e2','s2',1,'memory_record','{}','legacy body','2026')")
    results = list_current_records(owner='alice', workspace_id='w', db_path=path)
    assert {row['id'] for row in results} == {'event','legacy'}
    assert search_current_records('legacy',owner='alice',workspace_id='w',db_path=path)[0]['id'] == 'legacy'
    with sqlite3.connect(path) as db:
        db.execute("UPDATE fm_v2_sources SET forget_state='forgotten' WHERE source_id='s1'")
    assert [row['id'] for row in list_current_records(owner='alice',workspace_id='w',db_path=path)] == ['legacy']
