#!/usr/bin/env python3
"""Prepare O03/tool-view on a distinct offline Mimo copy; never alter its source."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / 'packages/mimo-code/packages/cli/src/storage'
LAYOUT = json.loads((STORAGE / 'openclank-profile-layout.json').read_text())
SCHEMA = (STORAGE / 'current-schema.ts').read_text()
OBJECTS = json.loads(re.search(r'^export const currentObjects = (\[.*\])$', SCHEMA, re.M).group(1))
STAMP = int(re.search(r'^export const currentStamp = (\d+)', SCHEMA, re.M).group(1))
FTS = {f'{kind}_fts_idx_{suffix}' for kind in ('memory','history') for suffix in ('data','idx','docsize','config')}

def norm(sql): return ' '.join((sql or '').split())
def column(name, kind='TEXT', notnull=0, default=None):
    return {'name':name, 'type':kind, 'notnull':notnull, 'dflt_value':default, 'pk':0}
def object_name(sql):
    match = re.match(r'CREATE (?:UNIQUE )?(?:INDEX|TRIGGER) (\S+)', sql)
    if not match: raise ValueError('invalid profile definition')
    return match.group(1)
def shape(values): return sorted(values,key=lambda c:c['name'])
def validate(db):
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('integrity unavailable')
    if db.execute('PRAGMA foreign_key_check').fetchone(): raise ValueError('foreign keys unavailable')
    flags={}; retired=set(); selected=False; workflow=False
    for obj in OBJECTS:
        name=obj['name']; row=db.execute('SELECT sql FROM sqlite_schema WHERE type=? AND name=?',(obj['type'],name)).fetchone()
        if not row: raise ValueError(f'missing schema object: {name}')
        if obj['type'] != 'table':
            if norm(row[0]) != norm(obj['sql']): raise ValueError(f'unsupported schema object: {name}')
            continue
        escaped=name.replace("'","''")
        actual=shape([{'name':c[1],'type':c[2],'notnull':c[3],'dflt_value':c[4],'pk':c[5]} for c in db.execute(f"PRAGMA table_info('{escaped}')")])
        marker=column(LAYOUT['retirementColumn'])
        if marker in actual and name in ('session','actor_registry'):
            retired.add(name); actual.remove(marker)
        selected_column=column(LAYOUT['selectedInputColumn'],'INTEGER')
        candidates=[c for c in actual if c['name']==LAYOUT['selectedInputColumn']]
        if candidates:
            if name!='user_input_receipt' or candidates != [selected_column]: raise ValueError('invalid selected input column')
            selected=True; actual.remove(selected_column)
        witness=column(LAYOUT['workflowWitnessColumn'])
        witnesses=[c for c in actual if c['name']==LAYOUT['workflowWitnessColumn']]
        if witnesses:
            if name!='workflow_run' or witnesses != [witness]: raise ValueError('invalid workflow witness column')
            workflow=True; actual.remove(witness)
        base=shape(obj['columns']); choices=[('base',base)]
        if name=='user_input_receipt':
            producer=[column(n) for n in LAYOUT['queuedColumns']]
            choices += [('producer',shape(base+producer)),('guarded',shape(base+producer+[column(LAYOUT['queuedGuardColumn'])]))]
        if name=='session_prefix_snapshot': choices.append(('prefix',shape(base+[column(LAYOUT['prefixColumn'])])))
        if name=='part': choices += [('view',shape(base+[column(LAYOUT['viewColumn'])])), ('view_recovery',shape(base+[column(LAYOUT['viewColumn']),column(LAYOUT['stepRecoveryColumn'])]))]
        if name=='session': choices.append(('label_session',shape(base+[column('next_display_ordinal','INTEGER',1,'1')])))
        if name=='actor_registry':
            actor_label=[column(n,'INTEGER' if n in ('display_ordinal','display_revision') else 'TEXT') for n in LAYOUT['labelColumns']]
            choices += [('label_actor',shape(base+actor_label)),('label_actor_v2',shape(base+actor_label+[column(LAYOUT['labelOwnerColumn'])]))]
        matched=[kind for kind,expected in choices if actual==expected]
        if len(matched)!=1: raise ValueError(f'unsupported or partial profile: {name}')
        flags[name]=matched[0]
    if retired and retired != {'session','actor_registry'}: raise ValueError('partial retirement profile')
    flags['retirement']='present' if retired else 'absent'
    flags['selected_input']='present' if selected else 'absent'
    flags['workflow_witness']='present' if workflow else 'absent'
    if (flags['session']=='label_session') != (flags['actor_registry'] in ('label_actor','label_actor_v2')): raise ValueError('partial actor-label profile')
    expected_extra=[]
    if flags['user_input_receipt']=='guarded': expected_extra += LAYOUT['queuedGuards']
    if flags['actor_registry'] in ('label_actor','label_actor_v2'): expected_extra += LAYOUT['labelObjects']
    if flags['actor_registry']=='label_actor_v2': expected_extra += LAYOUT['labelOwnerObjects']
    if retired: expected_extra += LAYOUT['retirementObjects']
    if selected: expected_extra += LAYOUT['selectedInputObjects']
    if workflow: expected_extra += LAYOUT['workflowWitnessObjects']
    extra_names=set()
    for stmt in expected_extra:
        name=object_name(stmt);extra_names.add(name)
        row=db.execute('SELECT sql FROM sqlite_schema WHERE name=?',(name,)).fetchone()
        if not row or norm(row[0])!=norm(stmt): raise ValueError(f'profile guard mismatch: {name}')
    actual_names={row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")}
    if actual_names != {obj['name'] for obj in OBJECTS} | FTS | extra_names: raise ValueError('unknown profile object')
    if db.execute('SELECT created_at FROM __drizzle_migrations ORDER BY created_at DESC LIMIT 1').fetchone() != (STAMP,): raise ValueError('schema journal mismatch')
    if flags['actor_registry'] in ('label_actor','label_actor_v2'):
        invalid=db.execute("""SELECT 1 FROM actor_registry a LEFT JOIN session s ON s.id=a.session_id WHERE
            CASE WHEN ((a.display_ordinal IS NULL AND a.display_label IS NULL AND a.display_revision IS NULL AND a.display_origin IS NULL)
            OR (typeof(a.display_ordinal)='integer' AND a.display_ordinal>0 AND a.display_ordinal<9007199254740991
            AND typeof(a.display_label)='text' AND length(CAST(a.display_label AS BLOB)) BETWEEN 1 AND 200
            AND typeof(a.display_revision)='integer' AND a.display_revision>=0 AND a.display_revision<=9007199254740991
            AND a.display_origin IN ('deterministic','helper','manual'))) THEN 0 ELSE 1 END = 1
            OR s.next_display_ordinal<=a.display_ordinal LIMIT 1""").fetchone()
        if invalid: raise ValueError('invalid actor label data')
        if flags['actor_registry']=='label_actor_v2' and db.execute("SELECT 1 FROM actor_registry WHERE display_owner_account_id IS NOT NULL AND (typeof(display_owner_account_id)!='text' OR length(CAST(display_owner_account_id AS BLOB)) NOT BETWEEN 1 AND 256 OR display_ordinal IS NULL) LIMIT 1").fetchone(): raise ValueError('invalid display owner')
        if db.execute("SELECT 1 FROM session WHERE typeof(next_display_ordinal)!='integer' OR next_display_ordinal<1 OR next_display_ordinal>9007199254740991 LIMIT 1").fetchone(): raise ValueError('invalid actor counter')
    if retired:
        for table in ('session','actor_registry'):
            if db.execute(f"SELECT 1 FROM {table} WHERE openclank_directory_retired_by IS NOT NULL AND (typeof(openclank_directory_retired_by)!='text' OR length(openclank_directory_retired_by)!=64 OR openclank_directory_retired_by GLOB '*[^0-9a-f]*') LIMIT 1").fetchone(): raise ValueError('invalid retirement data')
    if selected and db.execute("SELECT 1 FROM user_input_receipt WHERE selected_execution_revision IS NOT NULL AND (typeof(selected_execution_revision)!='integer' OR selected_execution_revision<0 OR selected_execution_revision>9007199254740991 OR selected_execution_revision != execution_revision) LIMIT 1").fetchone(): raise ValueError('invalid selected input execution data')
    if workflow:
        keys={'version','account_id','workspace_id','session_id','run_id','root_operation_id','actor_id','execution_revision','inner_turn_id'}
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value: raise ValueError('duplicate workflow witness key')
                value[key]=item
            return value
        for session_id,run_id,raw in db.execute('SELECT session_id,id,openclank_launch_witness FROM workflow_run WHERE openclank_launch_witness IS NOT NULL'):
            if type(raw) is not str or len(raw.encode('utf-8'))>4096: raise ValueError('invalid workflow witness')
            value=json.loads(raw,object_pairs_hook=unique)
            if (type(value) is not dict or set(value)!=keys or type(value['version']) is not int or value['version']!=1 or value['actor_id']!='main'
                or type(value['execution_revision']) is not int or not 0<=value['execution_revision']<=9007199254740991
                or any(type(value[k]) is not str or not value[k] or value[k]!=value[k].strip() or '\0' in value[k] or len(value[k].encode('utf-8'))>256 for k in keys-{'version','execution_revision'})
                or value['session_id']!=session_id or value['run_id']!=run_id or json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))!=raw): raise ValueError('invalid workflow witness')
    return flags

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','backup','destination'):parser.add_argument(name,type=Path)
    parser.add_argument('--writers-stopped',action='store_true',required=True)
    parser.add_argument('--prefix-provenance',action='store_true',help='add the nullable prefix receipt column on this offline copy')
    parser.add_argument('--selected-input',action='store_true',help='add the selected-execution receipt profile to this offline copy')
    parser.add_argument('--workflow-launch-witness',action='store_true',help='add the forward workflow witness profile on this offline copy')
    args=parser.parse_args()
    source=args.source.resolve(strict=True);backup=args.backup.resolve();dest=args.destination.resolve()
    if len({source,backup,dest})!=3 or not source.is_file() or backup.exists() or dest.exists(): raise ValueError('distinct new backup and destination required')
    initial=source.stat(); identity=(initial.st_dev,initial.st_ino)
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src:
        version=src.execute('PRAGMA data_version').fetchone()[0];profile=validate(src)
        fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        with sqlite3.connect(backup) as saved:src.backup(saved)
        current=source.stat()
        if (current.st_dev,current.st_ino)!=identity or src.execute('PRAGMA data_version').fetchone()[0]!=version:raise ValueError('source changed during backup')
    fd=os.open(dest,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    with sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True) as saved, sqlite3.connect(dest) as target:
        if validate(saved)!=profile:raise ValueError('backup profile changed')
        saved.backup(target)
        if validate(target)!=profile:raise ValueError('copy profile changed')
        with target:
            target.execute('BEGIN IMMEDIATE')
            if args.prefix_provenance and profile['session_prefix_snapshot']=='base':
                target.execute('ALTER TABLE session_prefix_snapshot ADD COLUMN provenance TEXT')
            if profile['part']=='base':target.execute('ALTER TABLE part ADD COLUMN openclank_view_decision TEXT')
            if profile['part'] in ('base','view'):target.execute('ALTER TABLE part ADD COLUMN openclank_step_recovery TEXT')
            if profile['actor_registry']=='base':
                for stmt in [*LAYOUT['labelDDL'],*LAYOUT['labelObjects']]:target.execute(stmt)
            if profile['actor_registry'] in ('base','label_actor'):
                for stmt in [*LAYOUT['labelOwnerDDL'],*LAYOUT['labelOwnerObjects']]:target.execute(stmt)
            if profile['retirement']=='absent':
                for stmt in [*LAYOUT['retirementDDL'],*LAYOUT['retirementObjects']]:target.execute(stmt)
            if args.selected_input and profile['selected_input']=='absent':
                for stmt in [*LAYOUT['selectedInputDDL'],*LAYOUT['selectedInputObjects']]:target.execute(stmt)
            if args.workflow_launch_witness and profile['workflow_witness']=='absent':
                for stmt in [*LAYOUT['workflowWitnessDDL'],*LAYOUT['workflowWitnessObjects']]:target.execute(stmt)
            expected={**profile,'part':'view_recovery','actor_registry':'label_actor_v2','session':'label_session','retirement':'present'}
            if args.prefix_provenance: expected['session_prefix_snapshot']='prefix'
            if args.selected_input: expected['selected_input']='present'
            if args.workflow_launch_witness: expected['workflow_witness']='present'
            if validate(target)!=expected:raise ValueError('prepared profile mismatch')
    print('Prepared a separate O03/tool-view shadow; source unchanged.')
if __name__=='__main__':main()
