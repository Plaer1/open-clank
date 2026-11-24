"""O15-shaped annotations owned by an existing live publication.

The optional row profile is installed only on a stopped copy. This module
never creates a database, grant, chat, or separate annotation authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.openclank.plan_feedback import _anchor, _utf8, MAX_ENTRIES, MAX_BYTES
from src.secret_storage import decrypt, encrypt, is_encrypted
from src.published_files import _typed_target


def _same_database(store, service):
    session = service.session_factory()
    try:
        url = session.get_bind().url
        if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:' or str(url.database).startswith('file:'):
            raise ValueError('Shared feedback setup unavailable')
        policy = Path(store.db_path).resolve(strict=True)
        publication = Path(url.database).resolve(strict=True)
        if policy != publication or not os.path.samefile(policy, publication):
            raise ValueError('Shared feedback setup unavailable')
    finally:
        session.close()


def _profile(connection):
    columns = {row['name']: row for row in connection.execute('PRAGMA table_info(published_files)')}
    cipher=columns.get('annotations_ciphertext'); revision=columns.get('annotations_revision')
    if (cipher is None or revision is None or str(cipher['type']).upper()!='TEXT' or cipher['notnull']!=0
            or str(revision['type']).upper()!='INTEGER' or revision['notnull']!=1 or str(revision['dflt_value'])!='0'):
        raise ValueError('Shared feedback setup unavailable')


def _active_grant(connection, publication_id):
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=' ', timespec='microseconds')
    return connection.execute('SELECT 1 FROM published_file_grants WHERE file_id=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?) LIMIT 1', (publication_id, now)).fetchone() is not None


def _load(raw, target):
    if raw is None: return {'version': 1, 'entries': []}
    if not isinstance(raw, str) or not is_encrypted(raw): raise ValueError('Shared feedback unavailable')
    value = json.loads(decrypt(raw))
    if type(value) is not dict or set(value) != {'version', 'entries'} or type(value['version']) is not int or value['version'] != 1 or type(value['entries']) is not list or len(value['entries']) > MAX_ENTRIES:
        raise ValueError('Shared feedback unavailable')
    keys={'id','author','publication_id','resource_id','text','content_sha256','comment_revision','anchor','source','request_key','intent_hash','publication_revision'}
    seen=set()
    for item in value['entries']:
        if type(item) is not dict or set(item) not in (keys,keys|{'requests'}) or not re.fullmatch(r'[a-f0-9]{32}',str(item.get('id',''))) or item['id'] in seen:
            raise ValueError('Shared feedback unavailable')
        seen.add(item['id'])
        if (item['publication_id']!=target.publication_id or item['resource_id']!=target.resource['resource_id']
                or not isinstance(item['author'],str) or not 0<len(item['author'].encode('utf-8'))<=256
                or type(item['comment_revision']) is not int or item['comment_revision']<1
                or type(item['publication_revision']) is not int or item['publication_revision']<1
                or hashlib.sha256(_utf8(item['text'],4096)).hexdigest()!=item['content_sha256']
                or not re.fullmatch(r'[0-9a-f]{64}',str(item['intent_hash']))): raise ValueError('Shared feedback unavailable')
        requests=item.get('requests',[])
        if type(requests) is not list or len(requests)>64: raise ValueError('Shared feedback unavailable')
        for request in requests:
            if type(request) is not dict or set(request)!={'key','hash','publication_revision','entry'} or not re.fullmatch(r'[0-9a-f]{64}',str(request['hash'])) or type(request['publication_revision']) is not int or request['publication_revision']<1 or type(request['entry']) is not dict: raise ValueError('Shared feedback unavailable')
            _utf8(request['key'],128)
        _utf8(item['request_key'],128);_anchor(item['anchor'],item['resource_id'])
        source=item['source']
        if type(source) is not dict or set(source)!={'revision','sha256'} or type(source['revision']) is not dict or set(source['revision'])!={'kind','value'} or source['revision']['kind']!='hostFingerprint' or not re.fullmatch(r'[0-9a-f]{64}',str(source['sha256'])) or not re.fullmatch(r'sha256:'+source['sha256']+r':[0-9]+',str(source['revision']['value'])): raise ValueError('Shared feedback unavailable')
    return value


def _public(item):
    return {key:item[key] for key in ('id','author','resource_id','text','content_sha256','comment_revision','anchor','source')}


def _transaction(store, service, target, current, apply):
    """Serialize policy and publication changes on their one canonical SQLite DB."""
    _same_database(store, service)
    with store._connect() as connection:
        store._begin(connection)
        _profile(connection)
        if current() is not True or store._generation(connection) != target.context.policy_generation:
            raise ValueError('Shared feedback authority changed')
        row = connection.execute('SELECT owner, owner_subject_id, target_ciphertext, annotations_ciphertext, annotations_revision FROM published_files WHERE id=?', (target.publication_id,)).fetchone()
        if not row or row['owner'] != target.owner or row['owner_subject_id'] != target.owner_subject_id or not row['target_ciphertext'] or not is_encrypted(row['target_ciphertext']) or not _active_grant(connection, target.publication_id):
            raise ValueError('Shared feedback unavailable')
        publication = _typed_target(json.loads(decrypt(row['target_ciphertext'])), owner_subject_id=target.owner_subject_id)
        if publication.get('mode') != 'live' or publication['resource'] != target.resource:
            raise ValueError('Shared feedback target changed')
        role = 'owner' if target.recipient_subject_id == target.owner_subject_id else store.resolve_resource_role(owner_subject_id=target.owner_subject_id, recipient_subject_id=target.recipient_subject_id, location_id=target.resource['location_id'], resource_id=target.resource['resource_id'], connection=connection)
        if role != target.role or role not in {'owner', 'commenter', 'editor'}:
            raise ValueError('Shared feedback unavailable')
        revision = row['annotations_revision']
        if type(revision) is not int or revision < 0: raise ValueError('Shared feedback setup unavailable')
        value = _load(row['annotations_ciphertext'], target)
        result, changed = apply(value, revision, role)
        if changed:
            body = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
            if len(body.encode('utf-8')) > MAX_BYTES: raise ValueError('Shared feedback exceeds its size limit')
            sealed = encrypt(body)
            if not is_encrypted(sealed): raise ValueError('Shared feedback unavailable')
            if current() is not True or store._generation(connection) != target.context.policy_generation or not _active_grant(connection, target.publication_id): raise ValueError('Shared feedback authority changed')
            updated = connection.execute('UPDATE published_files SET annotations_ciphertext=?, annotations_revision=? WHERE id=? AND annotations_revision=? AND target_ciphertext=?', (sealed, revision + 1, target.publication_id, revision, row['target_ciphertext']))
            if updated.rowcount != 1: raise ValueError('Shared feedback changed')
            connection.commit()
        else:
            connection.rollback()
        return result


def list_annotations(store, service, target, current):
    def read(value, revision, role):
        entries = value['entries'] if role == 'owner' else [item for item in value['entries'] if item.get('author') == target.recipient_subject_id]
        return {'version': 1, 'revision': revision, 'entries': [_public(item) for item in entries]}, False
    return _transaction(store, service, target, current, read)


def save_annotation(store, service, target, current, *, request_key, text, anchor, source, expected_revision, feedback_id=None, comment_revision=None):
    _utf8(request_key, 128); digest = hashlib.sha256(_utf8(text, 4096)).hexdigest()
    if type(expected_revision) is not int or expected_revision < 0: raise ValueError('Shared feedback revision unavailable')
    if (feedback_id is None) != (comment_revision is None) or (feedback_id is not None and (not isinstance(feedback_id,str) or not re.fullmatch(r'[a-f0-9]{32}',feedback_id) or type(comment_revision) is not int or comment_revision < 1)):
        raise ValueError('Shared feedback revision unavailable')
    checked = _anchor(anchor, target.resource['resource_id'])
    if checked['kind'] not in {'whole','text','cell','slide','image'}: raise ValueError('Shared feedback anchor unavailable')
    if type(source) is not dict or set(source) != {'revision', 'sha256'} or type(source['revision']) is not dict or not re.fullmatch(r'[0-9a-f]{64}', str(source['sha256'])):
        raise ValueError('Shared feedback source unavailable')
    intent = {'text': text, 'sha256': digest, 'anchor': checked, 'source': source}
    if feedback_id is not None: intent.update(feedback_id=feedback_id,comment_revision=comment_revision)
    intent_hash = hashlib.sha256(json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    def write(value, revision, role):
        if role not in {'commenter', 'editor'}: raise ValueError('Shared feedback unavailable')
        for item in value['entries']:
            if item.get('author') != target.recipient_subject_id: continue
            for prior in item.get('requests',[]):
                if prior['key']==request_key:
                    if prior['hash']!=intent_hash: raise ValueError('Shared feedback request changed')
                    return {'version':1,'revision':prior['publication_revision'],'entry':prior['entry']},False
            if item['request_key']==request_key:
                if item['intent_hash']!=intent_hash: raise ValueError('Shared feedback request changed')
                return {'version':1,'revision':item['publication_revision'],'entry':_public(item)},False
        if revision != expected_revision: raise ValueError('Shared feedback changed')
        if feedback_id is not None:
            item=next((item for item in value['entries'] if item['id']==feedback_id and item['author']==target.recipient_subject_id),None)
            if item is None or item['comment_revision']!=comment_revision or item['source']!=source or item['anchor']!=checked:
                raise ValueError('Shared feedback changed')
            requests=item.setdefault('requests',[])
            if len(requests)>=63: raise ValueError('Shared feedback request history is full')
            if not requests:
                requests.append({'key':item['request_key'],'hash':item['intent_hash'],'publication_revision':item['publication_revision'],'entry':_public(item)})
            item.update(text=text,content_sha256=digest,comment_revision=comment_revision+1,publication_revision=revision+1)
            requests.append({'key':request_key,'hash':intent_hash,'publication_revision':revision+1,'entry':_public(item)})
        else:
            if len(value['entries']) >= MAX_ENTRIES: raise ValueError('Shared feedback is full')
            item = {'id': uuid.uuid4().hex, 'author': target.recipient_subject_id, 'publication_id': target.publication_id,
                    'resource_id': target.resource['resource_id'], 'text': text, 'content_sha256': digest,
                    'comment_revision': 1, 'anchor': checked, 'source': source, 'request_key': request_key,
                    'intent_hash': intent_hash, 'publication_revision': revision + 1}
            value['entries'].append(item)
        return {'version': 1, 'revision': revision + 1, 'entry': _public(item)}, True
    return _transaction(store, service, target, current, write)


async def capture_source(target, anchor, expected_revision, *, image_read=False):
    """Capture existing Files content and an O15 anchor under the live grant."""
    from src.openclank.files_facade import FilesFacade
    from src.openclank.files_host_provider import _path
    checked = _anchor(anchor, target.resource['resource_id'])
    if checked['kind'] not in {'whole','text','cell','slide','image'}: raise ValueError('Shared feedback anchor unavailable')
    origin = target.resource['origin_id']; provider = target.provider; ctx = target.context
    path = _path(origin)
    before_path = os.stat(path, follow_symlinks=False)
    meta = await provider.stat_inspection(ctx, origin_id=origin)
    entry = await provider.stat(ctx, origin_id=origin)
    revision = FilesFacade._provider_revision(entry)
    content = await provider.content(ctx, origin_id=origin)
    if (not await target.current() or meta.kind != 'file' or meta.origin_id != origin or type(meta.size) is not int or not 0 <= meta.size <= 16*1024*1024
            or (checked['kind'] in {'cell','slide'} and meta.size > 262144)
            or (checked['kind'] == 'image' and not 0 < meta.size <= 4*1024*1024)
            or (expected_revision is not None and revision != expected_revision) or content.origin_id != origin or content.size != meta.size
            or content.etag != meta.revision.get('value') or content.stream is None):
        raise ValueError('Shared feedback source changed')
    digest=hashlib.sha256(); size=0; chunks=[]
    async for chunk in content.stream(0, meta.size):
        if type(chunk) is not bytes: raise ValueError('Shared feedback source unavailable')
        size += len(chunk)
        if size > meta.size: raise ValueError('Shared feedback source changed')
        digest.update(chunk)
        if checked['kind'] in {'text','cell','slide','image'}: chunks.append(chunk)
    hexdigest=digest.hexdigest()
    if size != meta.size or revision != {'kind':'hostFingerprint','value':f'sha256:{hexdigest}:{size}'}: raise ValueError('Shared feedback source changed')
    if checked['kind'] == 'text':
        data=b''.join(chunks); start=checked['start']; end=checked['end']
        if end > len(data): raise ValueError('Shared feedback anchor changed')
        data.decode('utf-8','strict');data[:start].decode('utf-8','strict');data[start:end].decode('utf-8','strict')
        if hashlib.sha256(data[start:end]).hexdigest() != checked['sha256']: raise ValueError('Shared feedback anchor changed')
    if checked['kind'] == 'cell':
        from src.openclank.collaboration_workbook import verify_workbook_range
        try: text=b''.join(chunks).decode('utf-8','strict')
        except UnicodeDecodeError as exc: raise ValueError('Shared feedback anchor unavailable') from exc
        verify_workbook_range(text,checked['sheetID'],checked['range']['startCellID'],checked['range']['endCellID'])
    if checked['kind'] == 'slide':
        from src.openclank.collaboration_slides import verify_slide_text_object
        try: text=b''.join(chunks).decode('utf-8','strict')
        except UnicodeDecodeError as exc: raise ValueError('Shared feedback anchor unavailable') from exc
        verify_slide_text_object(text,checked['deckID'],checked['slideID'],checked['objectID'])
    image_info = None
    if checked['kind'] == 'image':
        import asyncio
        import io
        import warnings
        from PIL import Image
        data = b''.join(chunks)
        def decode():
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    kind, (width, height) = image.format, image.size
                    if (kind not in ('PNG','JPEG') or not 1 <= width <= 4096 or not 1 <= height <= 4096
                            or width * height > 16777216 or getattr(image,'n_frames',1) != 1
                            or getattr(image,'is_animated',False)):
                        raise ValueError('Shared image unavailable')
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    if image.getexif().get(274,1) != 1: raise ValueError('Shared image orientation unavailable')
                    image.load()
                return {'mime':'image/png' if kind == 'PNG' else 'image/jpeg','width':width,'height':height}
        task=asyncio.create_task(asyncio.to_thread(decode)); cancelled=False
        while True:
            try:
                image_info=await asyncio.shield(task); break
            except asyncio.CancelledError:
                cancelled=True
                if task.done():
                    try: task.result()
                    except Exception: pass
                    raise
            except Exception as exc:
                if cancelled: raise asyncio.CancelledError from None
                raise ValueError('Shared image unavailable') from exc
        if cancelled: raise asyncio.CancelledError
    after_path=os.stat(path,follow_symlinks=False)
    def identity(st): return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns,st.st_mode)
    if identity(before_path) != identity(after_path) or not await target.current(): raise ValueError('Shared feedback source changed')
    def source_current():
        try:return identity(os.stat(path,follow_symlinks=False)) == identity(before_path)
        except OSError:return False
    source={'revision':dict(revision),'sha256':hexdigest}
    if image_read:
        if checked['kind'] != 'image': raise ValueError('Shared image unavailable')
        return source,source_current,image_info,data
    return source,source_current
