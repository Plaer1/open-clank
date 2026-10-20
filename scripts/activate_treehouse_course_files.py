#!/usr/bin/env python3
"""Explicit stopped-writer conversion to ordinary course files; never run at startup."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.auth import AuthManager
from src.auth_helpers import copal_owner_for_user
from src.openclank.treehouse_course_files import PrivateCourseFiles, canonical_bytes
from src.openclank.treehouse_global_catalogue import GlobalCatalogue
from src.openclank.treehouse_storage_activation import readonly, logical_digest, logical_snapshot

FORMAT = 'treehouse-course-files-activation-v1'
SOURCE = 'openclank:treehouse-core'
KINDS = ('courses', 'modules', 'activities', 'assignments', 'skills', 'badges', 'quests')
OPERATIONS = ('shareToken', 'sourceAttachments', 'retiredAt', 'replacementMainQuestId')


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def inventory(root):
    """Exact existing payload fence; never follows links or reads socket payloads."""
    root = Path(root)
    if not root.exists(): return {'exists': False, 'entries': []}
    if root.is_symlink() or not root.is_dir(): raise ValueError('Expected a real directory')
    records = []
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink(): raise ValueError('Source/store symlink needs explicit operator review: ' + rel)
        if p.is_dir(): records.append([rel, 'directory', p.stat().st_mode & 0o777])
        elif p.is_file(): records.append([rel, 'file', p.stat().st_mode & 0o777, p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()])
        else: raise ValueError('Special store entry needs explicit operator review: ' + rel)
    return {'exists': True, 'entries': records}


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.pending')
    raw = canonical_bytes(value)
    if temp.exists():
        if temp.read_bytes() != raw: raise ValueError('Unresolved partial receipt; preserve it for review')
    else:
        with os.fdopen(os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def locations(db, auth, loose_root, bundle, shared_root):
    values = {k: str(Path(v).resolve()) for k,v in locals().copy().items()}
    for key, value in values.items():
        if Path(locals()[key]).is_symlink(): raise ValueError('Symlink authority root is not accepted')
    bundle, shared, loose = (Path(values[k]) for k in ('bundle','shared_root','loose_root'))
    for a,b in ((bundle,shared),(bundle,loose),(shared,loose)):
        if a == b or a.is_relative_to(b) or b.is_relative_to(a): raise ValueError('Course/store roots overlap')
    return values


def catalogue_rows(connection):
    return list(connection.execute('SELECT owner_account_id,workspace_id,state_json,revision,updated_at FROM treehouse_catalogues ORDER BY owner_account_id,workspace_id'))


def definitions(state):
    """Only the existing course-owned native records; retain unscoped records in DB."""
    for cid in state.get('courses', {}):
        yield cid, {kind: {key:copy.deepcopy(item) for key,item in state.get(kind, {}).items()
                          if (key == cid if kind == 'courses' else item.get('courseId') == cid)} for kind in KINDS}


def auth_head(path):
    path = Path(path)
    if not path.is_file(): raise ValueError('Existing real AuthManager store is required')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def session_head(auth):
    path = Path(auth).with_name('sessions.json')
    if path.is_symlink(): raise ValueError('Session preimage is a symlink; request explicit review')
    return auth_head(path) if path.exists() else None


def identity_mapping(auth):
    # Canonical AuthManager loads/prunes adjacent sessions in its constructor.
    # Validate only an isolated auth-only copy; never point it at live tokens.
    raw = Path(auth).read_bytes()
    with tempfile.TemporaryDirectory(prefix='treehouse-conversion-auth-') as temporary:
        path = Path(temporary)/'auth.json'
        with os.fdopen(os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb') as output: output.write(raw)
        manager = AuthManager(str(path))
        result = {}
        for user in manager.list_users():
            account = user.get('account_id')
            if account:
                username = manager.username_for_account_id(account)
                if username: result[account] = copal_owner_for_user(username)
    return result,hashlib.sha256(raw).hexdigest()


def preview(db, auth, loose_root, bundle, shared_root, operation_id='treehouse-course-files-v1'):
    if not isinstance(operation_id, str) or not operation_id.strip(): raise ValueError('Operation ID required')
    paths = locations(db, auth, loose_root, bundle, shared_root)
    for suffix in ('','-wal','-shm'):
        path = Path(str(db)+suffix)
        if path.is_symlink() or (path.exists() and not path.is_file()): raise ValueError('Nonregular SQLite preimage requires review')
    catalogue = GlobalCatalogue(bundle)
    if not catalogue.course_files: raise ValueError('Bundled course-files publication is required')
    identities,auth_digest = identity_mapping(auth)
    with readonly(db) as connection:
        rows = catalogue_rows(connection)
        for account, _, raw, _, _ in rows:
            state = json.loads(raw)
            if state.get('_courseFileSources'): raise ValueError('Existing private source authority requires separate update review')
            if state.get('courses') and account not in identities: raise ValueError('Catalogue owner has no immutable auth mapping')
        before = logical_digest(connection)
    return {'format':FORMAT,'operationId':operation_id,'paths':paths,'inputDigest':before,
            'authDigest':auth_digest,'sessionsDigest':session_head(auth),'bundleDigest':digest(inventory(bundle)),
            'looseDigest':digest(inventory(loose_root)),'sharedDigest':digest(inventory(shared_root)),
            'packageDigest':catalogue.manifest['packageDigest'],'catalogues':len(rows),
            'courses':sum(len(json.loads(row[2]).get('courses', {})) for row in rows),
            'action':'Stage exact shared/private text; switch only catalogue source pointers; preserve other tables'}


def unchanged_other_tables(before, after):
    if before[0] != after[0]: raise ValueError('Database schema changed during conversion')
    expected = {k:v for k,v in before[1:] if k != 'treehouse_catalogues'}
    actual = {k:v for k,v in after[1:] if k != 'treehouse_catalogues'}
    if expected != actual: raise ValueError('Non-catalogue rows changed during conversion')


def resume_preview(db, plan, prior_backup):
    """Explicit manual recovery from partial extraction with OLD DB authority.

    Nothing is removed or silently reauthorized. A resumed apply must take a
    new exclusive backup, and install still refuses mismatching existing text.
    """
    if plan.get('format') != FORMAT: raise ValueError('Invalid prior preview')
    if Path(db).resolve() != Path(plan['paths']['db']): raise ValueError('Prior preview names another database')
    prior_backup = Path(prior_backup).resolve()
    journal = json.loads((prior_backup/'conversion.json').read_text())
    if journal.get('preview') != plan: raise ValueError('Prior backup names another preview')
    if (Path(plan['paths']['shared_root'])/'.authority.json').exists(): raise ValueError('Source authority is already active')
    with readonly(db) as c:
        if logical_digest(c) != plan['inputDigest']: raise ValueError('Database authority changed; resume preview refused')
    with readonly(prior_backup/'treehouse.sqlite3') as c:
        if logical_digest(c) != plan['inputDigest']: raise ValueError('Prior coherent recovery backup changed')
    if auth_head(plan['paths']['auth']) != plan['authDigest'] or digest(inventory(plan['paths']['bundle'])) != plan['bundleDigest']:
        raise ValueError('Auth or bundled edition changed')
    result = preview(**plan['paths'],operation_id=plan['operationId'])
    result['resume'] = {'priorBackup':str(prior_backup),'priorPreviewDigest':digest(plan)}
    return result


def restore_native(state, store):
    value = copy.deepcopy(state)
    for cid,pointer in value.pop('_courseFileSources', {}).items():
        metadata = store.metadata(pointer, account_id=pointer['accountId'], workspace=pointer['workspaceId'])
        metadata['activities'] = {k:store.selected_activity(pointer,v)[0] for k,v in metadata['activities'].items()}
        metadata['assignments'] = {k:store.selected_assignment(pointer,v) for k,v in metadata['assignments'].items()}
        for kind in KINDS:
            for key,item in metadata[kind].items():
                runtime = {field:value[kind][key][field] for field in OPERATIONS if field in value[kind].get(key,{})}
                value[kind][key] = {**item, **runtime}
    value.pop('_courseFilesEnabled', None)
    return value


def generated_index_file(name):
    return (name in {'catalogue.json','.copal-commit.lock'}
            or re.fullmatch(r'\.indexes/[0-9a-f]{64}/(?:manifest\.json|subjects/[a-z0-9-]+\.json)',name) is not None
            or re.fullmatch(r'\.indexes/commits/[0-9]{20}\.json',name) is not None
            or (Path(name).name.startswith('.index-') and (str(Path(name).parent)=='.' or name.startswith('.indexes/'))))


def reindex(root, *, project_root=None, initial=False):
    """Delegate index semantics; local OS ownership is not a learner grant."""
    selected = Path(root)
    if selected.is_symlink() or not selected.is_dir(): raise ValueError('Select an existing real shared source directory')
    if not hasattr(os,'geteuid') or selected.stat().st_uid != os.geteuid() or not os.access(selected,os.W_OK|os.X_OK):
        raise ValueError('Reindex requires the actual OS owner and write authority for this exact source directory')
    if not initial:
        marker = json.loads((selected/'.authority.json').read_text())
        if marker.get('format')!='treehouse-course-authority-v1' or marker.get('sourceId')!=SOURCE:
            raise ValueError('Shared source authority needs explicit activation/repair')
    from src.openclank.treehouse_shared_course_index import reindex_shared_course_files
    return reindex_shared_course_files(selected,project_root=project_root or Path(__file__).resolve().parents[1])


def apply(db, plan, backup_dir, *, writers_stopped=False, fail_after=None):
    if not writers_stopped: raise ValueError('Stop writers and explicitly confirm --writers-stopped')
    if plan.get('format') != FORMAT: raise ValueError('Invalid conversion preview')
    paths = plan['paths']
    if Path(db).resolve() != Path(paths['db']): raise ValueError('Preview names another database')
    backup = Path(backup_dir).resolve()
    for key in ('bundle','shared_root','loose_root'):
        p = Path(paths[key])
        if backup == p or backup.is_relative_to(p) or p.is_relative_to(backup): raise ValueError('Backup and source/store roots overlap')
    marker = Path(paths['shared_root']) / '.authority.json'
    journal_path = backup / 'conversion.json'
    if marker.exists():
        mark = json.loads(marker.read_text())
        if mark.get('format') != 'treehouse-course-authority-v1' or mark.get('sourceId') != SOURCE:
            raise ValueError('Shared authority marker is invalid; never fall back or reseed')
        if mark.get('operationId') != plan['operationId'] or mark.get('inputDigest') != plan['inputDigest']:
            raise ValueError('Another source edition is active; do not overwrite it')
        if not journal_path.is_file(): raise ValueError('Activation recovery journal missing')
        journal = json.loads(journal_path.read_text())
        if journal['preview'] != plan: raise ValueError('Operation ID conflicts with another preview')
        if 'receipt' not in journal: raise ValueError('Activation recovery journal is incomplete')
        return {**journal['receipt'], 'replayed':True}
    if journal_path.exists():
        journal = json.loads(journal_path.read_text())
        if journal['preview'] != plan: raise ValueError('Recovery journal belongs to another preview')
        if digest(inventory(paths['loose_root'])) != journal['looseDigest'] or digest(inventory(paths['shared_root'])) != journal['sharedDigest']:
            raise ValueError('Staged files changed; preserve and review before resuming')
    else:
        if backup.exists(): raise ValueError('Backup destination exists; preserve it')
        expected = {k:v for k,v in plan.items() if k != 'resume'}
        if preview(**paths, operation_id=plan['operationId']) != expected: raise ValueError('Database, auth or file inventory changed; create a fresh preview')
        if 'resume' in plan:
            prior = Path(plan['resume']['priorBackup'])
            original = json.loads((prior/'conversion.json').read_text())['preview']
            if digest(original) != plan['resume']['priorPreviewDigest'] or resume_preview(db,original,prior) != plan:
                raise ValueError('Resume authority changed')
        backup.mkdir(mode=0o700, parents=True)
        # Exact raw closed generation before SQLite backup; never a user Git blob.
        raw = backup / 'raw'; raw.mkdir()
        raw_files = {}
        for suffix in ('','-wal','-shm'):
            original = Path(str(db)+suffix)
            if original.exists():
                before_hash = hashlib.sha256(original.read_bytes()).hexdigest()
                copied_raw = raw / ('treehouse.sqlite3'+suffix)
                shutil.copy2(original, copied_raw)
                if before_hash != hashlib.sha256(copied_raw.read_bytes()).hexdigest() or before_hash != hashlib.sha256(original.read_bytes()).hexdigest():
                    raise ValueError('Raw closed SQLite preimage changed or copy differs')
                raw_files['treehouse.sqlite3'+suffix] = {'bytes':original.stat().st_size,
                    'sha256':before_hash,'mode':original.stat().st_mode&0o777}
            else: raw_files['treehouse.sqlite3'+suffix] = None
        with readonly(db) as source, sqlite3.connect(backup/'treehouse.sqlite3') as destination: source.backup(destination)
        os.chmod(backup/'treehouse.sqlite3',0o600)
        with readonly(backup/'treehouse.sqlite3') as copied:
            if copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or logical_digest(copied) != plan['inputDigest']:
                raise ValueError('Coherent recovery backup differs')
        for key in ('loose_root','shared_root'):
            source = Path(paths[key])
            if source.exists():
                shutil.copytree(source,backup/key)
                if inventory(source) != inventory(backup/key): raise ValueError('Store backup differs')
        shutil.copy2(paths['auth'],backup/'auth.json')
        sessions = Path(paths['auth']).with_name('sessions.json')
        if sessions.exists(): shutil.copy2(sessions,backup/'sessions.json')
        journal = {'preview':plan,'phase':'backed-up','looseDigest':plan['looseDigest'],'sharedDigest':plan['sharedDigest'],
                   'rawPreimage':raw_files}
        atomic_json(journal_path,journal)
    if auth_head(paths['auth']) != plan['authDigest'] or digest(inventory(paths['bundle'])) != plan['bundleDigest']:
        raise ValueError('Auth or bundled edition changed')
    if session_head(paths['auth']) != plan['sessionsDigest']: raise ValueError('Session preimage changed')
    identities,_ = identity_mapping(backup/'auth.json')
    store = PrivateCourseFiles(Path(paths['loose_root']))
    with readonly(backup/'treehouse.sqlite3') as copied:
        if logical_digest(copied) != plan['inputDigest'] or copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Coherent recovery backup changed')
        old_rows, old_snapshot = catalogue_rows(copied), logical_snapshot(copied)
    with readonly(db) as current: current_digest = logical_digest(current)
    if current_digest not in {plan['inputDigest'],journal.get('outputDigest')}:
        raise ValueError('Database changed since preview/staging')
    if current_digest == plan['inputDigest']:
        shared = Path(paths['shared_root']); shared.mkdir(parents=True,exist_ok=True)
        bundle_files = {p.relative_to(paths['bundle']).as_posix() for p in Path(paths['bundle']).rglob('*') if p.is_file()}
        existing_files = {p.relative_to(shared).as_posix() for p in shared.rglob('*') if p.is_file()}
        if any(not generated_index_file(name) for name in existing_files-bundle_files):
            raise ValueError('Unexpected shared source entries; preserve them for review')
        for source in sorted(Path(paths['bundle']).rglob('*')):
            if not source.is_file(): continue
            target = shared/source.relative_to(paths['bundle'])
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():
                if target.read_bytes() != source.read_bytes(): raise ValueError('Staged shared source differs; never overwrite edits')
            else:
                with target.open('xb') as output: output.write(source.read_bytes())
                if fail_after == 'shared-file': raise RuntimeError('Injected interruption during shared extraction')
        changes = []
        for account,workspace,raw,revision,updated in old_rows:
            original, converted = json.loads(raw), json.loads(raw)
            owner = identities.get(account)
            if not owner:
                if original.get('courses'): raise ValueError('Auth owner mapping disappeared')
                # No files need installation: preserve this entire orphan empty
                # row exactly, including its raw JSON, revision and timestamp.
                continue
            for cid,definition in definitions(original):
                pointer,metadata = store.install(definition,owner=owner,account_id=account,
                                                 workspace=workspace,action_id=plan['operationId']+':'+digest([account,workspace,cid]))
                for kind in KINDS:
                    for key,item in metadata[kind].items():
                        runtime = {field:copy.deepcopy(converted[kind][key][field]) for field in OPERATIONS if field in converted[kind].get(key,{})}
                        converted[kind][key] = {**item,**runtime}
                converted.setdefault('_courseFileSources',{})[cid] = pointer
            converted['_courseFilesEnabled'] = True
            if restore_native(converted,store) != original: raise ValueError('Private native definition/operational data changed')
            changes.append((json.dumps(converted,ensure_ascii=False,separators=(',',':')),account,workspace))
        index = reindex(shared,initial=True)
        journal.update(phase='staged',looseDigest=digest(inventory(paths['loose_root'])),
                       sharedDigest=digest(inventory(shared)),sharedIndex=index)
        atomic_json(journal_path,journal)
        if fail_after == 'staged': raise RuntimeError('Injected interruption after inactive staging')
        with sqlite3.connect(db,isolation_level=None) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                if logical_digest(connection) != plan['inputDigest']: raise ValueError('Database changed after staging')
                connection.executemany('UPDATE treehouse_catalogues SET state_json=? WHERE owner_account_id=? AND workspace_id=?',changes)
                unchanged_other_tables(old_snapshot,logical_snapshot(connection))
                journal.update(phase='committing',outputDigest=logical_digest(connection))
                atomic_json(journal_path,journal)
                connection.commit()
            except BaseException: connection.rollback(); raise
    if fail_after == 'database': raise RuntimeError('Injected interruption after catalogue switch, before shared marker')
    with readonly(db) as current:
        unchanged_other_tables(old_snapshot,logical_snapshot(current))
        if logical_digest(current) != journal['outputDigest']: raise ValueError('Converted database differs')
    marker_value = {'format':'treehouse-course-authority-v1','sourceId':SOURCE,'operationId':plan['operationId'],
                    'inputDigest':plan['inputDigest'],'packageDigest':journal['sharedIndex']['packageDigest'],
                    'originPackageDigest':plan['packageDigest'],'indexGeneration':journal['sharedIndex']['generation']}
    receipt = {'format':FORMAT,'operationId':plan['operationId'],'inputDigest':plan['inputDigest'],
               'outputDigest':journal['outputDigest'],'packageDigest':journal['sharedIndex']['packageDigest'],
               'originPackageDigest':plan['packageDigest'],'sharedIndex':journal['sharedIndex'],
               'catalogues':plan['catalogues'],'courses':plan['courses'],'replayed':False,
               'nonCatalogueRowsExact':True,'privateNativeDefinitionsExact':True,'policyGrantsCreated':0}
    # A crash after marker publication must still have a replayable receipt.
    journal.update(phase='ready-to-activate',receipt=receipt);atomic_json(journal_path,journal)
    # Shared edition becomes active only after all private pointers commit.
    atomic_json(marker,marker_value)
    if fail_after == 'marker': raise RuntimeError('Injected interruption after shared activation')
    journal.update(phase='complete',receipt=receipt);atomic_json(journal_path,journal)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('preview','resume-preview','apply','reindex'))
    help_text={'db':'Existing stopped TreeHouse SQLite store','auth':'Existing immutable-account AuthManager file (validated through an isolated copy)',
               'loose-root':'Real owner/workspace Copal Loose store root','bundle':'Reviewed bundled course-files origin',
               'shared-root':'Exact shared edition source root; never an arbitrary file grant'}
    for name,text in help_text.items(): parser.add_argument('--'+name,type=Path,required=name=='shared-root',help=text)
    parser.add_argument('--project-root',type=Path,help='Existing first-party static asset/resource authority for reindex')
    parser.add_argument('--operation-id',default='treehouse-course-files-v1')
    parser.add_argument('--preview',type=Path,help='Prior saved preview; resume-preview keeps it and fences new partial files')
    parser.add_argument('--backup-dir',type=Path,help='New exclusive recovery directory for apply; ORIGINAL retained directory for resume-preview')
    parser.add_argument('--writers-stopped',action='store_true',help='Confirm writers stopped; this tool never stops services')
    parser.add_argument('--output',type=Path,help='New private JSON result path; never overwrite a prior receipt')
    args=parser.parse_args();paths={k:getattr(args,k) for k in ('db','auth','loose_root','bundle','shared_root')}
    try:
        if args.output and args.output.exists(): raise ValueError('Output exists; preserve it')
        if args.mode!='reindex' and any(value is None for value in paths.values()):
            raise ValueError('Preview/apply require explicit --db, --auth, --loose-root, --bundle and --shared-root')
        if args.mode=='reindex': result=reindex(args.shared_root,project_root=args.project_root)
        elif args.mode=='preview': result=preview(**paths,operation_id=args.operation_id)
        elif args.mode=='resume-preview':
            if not args.preview or not args.backup_dir: raise ValueError('Resume preview requires original preview and retained backup')
            original=json.loads(args.preview.read_text())
            if locations(**paths)!=original['paths']: raise ValueError('CLI paths differ from original preview')
            result=resume_preview(args.db,original,args.backup_dir)
        else:
            if not args.preview or not args.backup_dir: raise ValueError('Apply requires preview and exclusive backup directory')
            plan=json.loads(args.preview.read_text())
            if locations(**paths)!=plan['paths']: raise ValueError('CLI paths differ from preview')
            result=apply(args.db,plan,args.backup_dir,writers_stopped=args.writers_stopped)
        if args.output: atomic_json(args.output,result)
        else: print(json.dumps(result,indent=2))
        return 0
    except (ValueError,OSError,RuntimeError,sqlite3.Error) as exc:
        print('Course file activation refused: '+str(exc),file=sys.stderr);return 1


if __name__=='__main__': raise SystemExit(main())
