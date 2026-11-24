"""Bounded, source-linked recording bundle; caller owns authentication and import."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import re
import tempfile
import zipfile

from src.openclank import plan_feedback

MAX_SOURCE = 64 * 1024 * 1024
MAX_ARCHIVE = MAX_SOURCE + plan_feedback.MAX_BYTES + 128 * 1024


class BundleUnavailable(ValueError):
    """The selected source or feedback no longer has its original authority."""


class _CappedFile:
    def __init__(self, file):
        self.file = file

    def write(self, data):
        if self.file.tell() + len(data) > MAX_ARCHIVE:
            raise BundleUnavailable('recording bundle exceeds its limit')
        return self.file.write(data)

    def __getattr__(self, name):
        return getattr(self.file, name)


async def _owned_thread(function):
    task = asyncio.create_task(asyncio.to_thread(function))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result



async def _readonly(function, timeout=5):
    # The database read owns no bundle file. A timed-out thread is allowed to
    # finish, but its result is never used to authorize later publication.
    task = asyncio.create_task(asyncio.to_thread(function))
    def discard(done):
        if not done.cancelled():
            done.exception()
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task not in done:
            task.add_done_callback(discard)
            raise BundleUnavailable('feedback read timed out')
        return task.result()
    except asyncio.CancelledError:
        task.add_done_callback(discard)
        raise

def _selected(session_id, owner, account, ids, session_current, evidence):
    if session_current() is not True:
        raise BundleUnavailable('feedback authority changed')
    entries = plan_feedback.read(session_id, owner, account, current_authority=session_current)
    by_id = {entry['id']: entry for entry in entries}
    result = []
    for identifier in ids:
        entry = by_id.get(identifier)
        target = entry.get('resourceTarget') if entry else None
        resource = target.get('resource') if isinstance(target, dict) else None
        anchor = target.get('anchor') if isinstance(target, dict) else None
        if (not isinstance(resource, dict) or not isinstance(anchor, dict)
                or resource.get('resourceID') != evidence['source_resource_id']
                or resource.get('revision') != evidence['source_revision']
                or resource.get('sha256') != evidence['source_sha256']
                or anchor.get('kind') not in ('frame', 'time_range')):
            raise BundleUnavailable('selected feedback is unavailable or changed')
        # Only the closed temporal anchor leaves the O15 private target.
        result.append({key: entry[key] for key in ('id', 'text', 'comment_revision', 'content_sha256')}
                      | {'anchor': dict(anchor), 'anchor_assurance': 'unverified_playback_hint'})
    if session_current() is not True:
        raise BundleUnavailable('feedback authority changed')
    return result


async def _qualified_observations(facade, context, job_id, evidence):
    """Resolve exactly one original job and its committed Files receipt."""
    from src import bg_jobs
    if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise BundleUnavailable('recording job unavailable')
    job = await _readonly(lambda: bg_jobs.get_media(job_id, account_id=context.owner_subject_id))
    stored = await _readonly(lambda: facade._load_operation(context, job_id))
    try:
        identity = job['identity']; child = job['local_child']; result = bg_jobs._media_result_for(job, job['result'], job_id)
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        receipt = stored['receipt']; binding = receipt['_import_binding']; derived = binding['derived_source']
        items = receipt['items']
        if (job['id'] != job_id or job['account_id'] != context.owner_subject_id or job['owner'] != context.owner_username
            or identity['workspace_id'] != context.workspace_id or identity['account_id'] != context.owner_subject_id or identity['owner'] != context.owner_username
            or identity['adapter_id'] != 'screen-record-v1' or job['status'] != 'complete' or job['identity_digest'] != digest
            or child['state'] != 'terminal' or child['identity_digest'] != digest or child['claim_token'] != job['submission_token']
            or child['profile'].get('recording_protocol') not in ('record-v5','record-v6','record-v7','record-v8')
            or child['profile'].get('recording_protocol') != identity.get('resource_binding',{}).get('recording_protocol')
            or child['receipt']['outcome'] != 'finalized'
            or any(job['artifact'].get(k) != child['receipt'].get(k) for k in ('artifact_id','sha256','size','mime_type'))
            or result['sha256'] != evidence['source_sha256'] or result['mime_type'] != 'video/webm'
            or receipt['state'] != 'complete' or len(items) != 1 or items[0].get('item_id') != 'output'
            or items[0].get('outcome') != 'committed' or items[0].get('resource_key') != evidence['source_resource_id']
            or items[0].get('revision') != evidence['source_revision'] or items[0].get('resource_ref') != result['resource_ref']
            or binding['item_id'] != 'output'
            or binding['account_id'] != context.owner_subject_id or binding['workspace_id'] != context.workspace_id
            or binding['upload_digest'] != 'sha256:' + evidence['source_sha256'] or binding['upload_size'] != evidence['source_size']
            or derived != {'media_job_id':job_id, 'identity_digest':digest, 'artifact_sha256':evidence['source_sha256']}):
            raise BundleUnavailable('recording association unavailable')
        if child['profile'].get('recording_protocol') == 'record-v8':
            from src.openclank.recording_contract import validate_binding
            validate_binding(identity['resource_binding'], account=context.owner_subject_id, hashes=identity['input_hashes'])
        observations = bg_jobs._media_recording_observations(job)
        if observations is None: raise BundleUnavailable('recording observations unavailable')
        protocol=child['profile'].get('recording_protocol')
        if protocol in ('record-v6','record-v7'):
            control=job['audio_selection']; metadata=child['receipt']['metadata']
            tracks=bg_jobs._media_recording_audio_tracks(job)
            kinds=control['selected_kinds']; offered=identity['resource_binding']['audio_intent']
            if protocol == 'record-v6':
                from src.openclank.recording_audio_contract import intent
                intent(offered)
            elif (type(offered) is not dict or set(offered) != {'microphone','system_output'}
                  or any(type(v) is not bool for v in offered.values())):
                raise BundleUnavailable('recording audio association unavailable')
            if (control.get('state') != 'accepted' or tracks is None or control.get('generation') != metadata.get('generation')
                or child['profile'].get('audio_intent') != offered or kinds not in ([],['microphone'],['system_output'],['microphone','system_output'])
                or any(offered.get(kind) is not True for kind in kinds)):
                raise BundleUnavailable('recording audio association unavailable')
            audio={'selected_kinds':list(kinds), 'tracks':tracks, 'complete_loss':'unknown', 'physical_sync':'unknown'}
            if protocol == 'record-v6': audio.update(selection_id=control['selection_id'],selection_digest=control['selection_digest'])
            observations={**observations,'audio':audio}
            if protocol == 'record-v7':
                selected=control['crop']; basis=control['source_basis']; actual=metadata['crop']
                handles=control['choice_handles']
                if (type(handles) is not dict or set(handles) != {'microphone','system_output'}
                    or [k for k in ('microphone','system_output') if handles[k] is not None] != kinds):
                    raise BundleUnavailable('recording selection association unavailable')
                if (type(selected) is not dict or set(selected) != {'mode','basis_id','rect'}
                    or type(basis) is not dict or set(basis) != {'source_width','source_height'}
                    or any(type(basis[k]) is not int or not 1<=basis[k]<=16384 for k in basis)
                    or basis['source_width']*basis['source_height']>16000000
                    or selected['mode'] not in ('full','crop') or actual['basis_id'] != selected['basis_id']
                    or any(actual[k] != basis[k] for k in basis)):
                    raise BundleUnavailable('recording crop association unavailable')
                from src.openclank.recording_crop_contract import selection as crop_selection
                original=crop_selection(control['generation'],control['selection_id'],handles['microphone'],handles['system_output'],selected,source_width=basis['source_width'],source_height=basis['source_height'])
                if original['selection_digest'] != control['selection_digest'] or original['selection_digest'] != metadata['selection_digest']:
                    raise BundleUnavailable('recording selection association unavailable')
                expected=(selected['rect'] if selected['mode']=='crop' else
                          {'x':0,'y':0,'width':basis['source_width'],'height':basis['source_height']})
                if (actual['rect'] != expected or (selected['mode']=='full' and selected['rect'] is not None)
                    or (selected['mode']=='crop' and (type(selected['rect']) is not dict
                        or any(type(selected['rect'].get(k)) is not int or selected['rect'][k] % 2 for k in ('x','y','width','height'))))):
                    raise BundleUnavailable('recording crop association unavailable')
                observations={**observations,'crop':{'basis':'source_buffer','assurance':'nominal_transform',
                    'source_width':actual['source_width'],'source_height':actual['source_height'],'rect':dict(actual['rect']),
                    'output_width':actual['output_width'],'output_height':actual['output_height'],
                    'transform':dict(actual['transform']),'pixel_aspect':actual['pixel_aspect'],'orientation':actual['orientation'],
                    'physical_correspondence':'unknown'}}
        return {'job_id':job_id, 'identity_digest':digest, 'artifact_sha256':evidence['source_sha256'],
                'source_resource_id':evidence['source_resource_id'], 'source_revision':evidence['source_revision'],
                'observations':observations}
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleUnavailable('recording association unavailable') from exc


async def fresh_recording_result(*, facade, context, job_id, current):
    """Reopen one exact historical result via its retained Host import marker."""
    from src import bg_jobs
    from src.openclank.resource_refs import stable_resource_id
    if await current() is not True: raise BundleUnavailable('recording authority changed')
    job = await _readonly(lambda: bg_jobs.get_media(job_id, account_id=context.owner_subject_id))
    stored = await _readonly(lambda: facade._load_operation(context, job_id))
    try:
        item = stored['receipt']['items'][0]; artifact = job['artifact']
        evidence = {'source_resource_id':item['resource_key'], 'source_revision':item['revision'],
                    'source_sha256':artifact['sha256'], 'source_size':artifact['size']}
        association = await _qualified_observations(facade, context, job_id, evidence)
        host = facade._providers['host']
        marker = await _readonly(lambda: host._operation_load(context, job_id))
        if (not isinstance(marker,dict) or marker.get('phase') != 'complete' or marker.get('operation_id') != job_id
            or marker.get('item_id') != 'output' or marker.get('workspace_id') != context.workspace_id
            or marker.get('generation') != stored['generation'] or marker.get('revision') != evidence['source_revision']
            or marker.get('digest') != 'sha256:' + evidence['source_sha256'] or marker.get('length') != evidence['source_size']
            or not isinstance(marker.get('target'),str) or not marker['target']):
            raise BundleUnavailable('recording result unavailable')
        if await current() is not True: raise BundleUnavailable('recording authority changed')
        resource = await host.resource_for_path(context, path=marker['target'])
        if (resource.kind != 'file' or stable_resource_id(owner_subject_id=context.owner_subject_id, provider='host', origin_id=resource.origin_id) != evidence['source_resource_id']
            or facade._provider_revision(resource) != evidence['source_revision'] or resource.size != evidence['source_size']):
            raise BundleUnavailable('recording result changed')
        result = facade._resource(context, 'host', resource)
        again = await host.resource_for_path(context, path=marker['target'])
        if (again.origin_id != resource.origin_id or facade._provider_revision(again) != evidence['source_revision']
            or again.size != evidence['source_size'] or await current() is not True
            or await _qualified_observations(facade, context, job_id, evidence) != association
            or await _readonly(lambda: host._operation_load(context, job_id)) != marker):
            raise BundleUnavailable('recording result changed')
        observations = association['observations']
        video = observations['measurements']
        audio = observations.get('audio')
        tracks = [{'kind': track['kind'], 'submittedFrames': track['submitted_frames']}
                  for track in audio['tracks']] if audio is not None else []
        coverage = {
            'version': 1, 'jobID': job_id,
            'video': {'state': 'observed', 'outputPixels': video['output_pixels'],
                      'encodedBuffers': video['encoded']['buffers'],
                      'missingTimestampBuffers': video['encoded']['missing_pts'], 'upstreamLoss': 'unknown'},
            'audio': {'state': 'unavailable' if audio is None else 'observed' if tracks else 'not_selected',
                      'tracks': tracks, 'physicalSync': 'unknown'},
            'inputEvents': 'unavailable',
        }
        return {'resourceRef': result['ref'], 'revision': dict(evidence['source_revision']),
                'recordingCoverage': coverage}
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleUnavailable('recording result unavailable') from exc


class RecordingBundle:
    content_type = 'application/zip'

    def __init__(self, file, name, evidence, notes, current):
        self._file = file
        self.filename = name
        self.source_evidence = evidence
        self.selected_notes = notes
        self.current = current

    async def read(self, size):
        return await _owned_thread(lambda: self._file.read(size))


@asynccontextmanager
async def build_recording_bundle(*, facade, context, source_ref, expected_revision,
                                 session_id, owner, account, feedback_ids, include_source,
                                 name, current, session_current, original_job_id=None):
    """Yield one upload and its precommit check; supplied callbacks are trusted owners.

    The caller must hold the account admission, verify the original browser token
    and session ownership, and pass those checks in both currentness callbacks.
    The yielded upload must only be passed to the existing original-ID importer.
    """
    if (not isinstance(feedback_ids, list) or len(feedback_ids) > plan_feedback.MAX_ENTRIES
            or any(type(item) is not str or not item or len(item) > 512 for item in feedback_ids)
            or len(set(feedback_ids)) != len(feedback_ids) or type(include_source) is not bool
            or not isinstance(name, str) or not name.lower().endswith('.zip')
            or not callable(current) or not callable(session_current)
            or account != context.owner_subject_id or owner != context.owner_username):
        raise BundleUnavailable('invalid recording bundle selection')
    async def strict_current():
        return await current() is True

    with tempfile.TemporaryFile(mode='w+b') as source, tempfile.TemporaryFile(mode='w+b') as output:
        evidence, source_current = await facade._host_video_source(
            context, source_ref=source_ref, expected_revision=expected_revision,
            current=strict_current, snapshot_fd=source.fileno())
        notes = await _readonly(
            lambda: _selected(session_id, owner, account, feedback_ids, session_current, evidence))
        association = None
        if original_job_id is not None:
            try: association = await _qualified_observations(facade, context, original_job_id, evidence)
            except Exception: association = None
        metadata = {'version': 1, 'kind': 'recording_feedback_bundle',
                    'coverage': 'selected_observed', 'source': {'sha256': evidence['source_sha256'],
                    'size': evidence['source_size'], 'included': include_source},
                    'native_observations': ({'state':'qualified', 'component':'observations.json', 'input_events':'unknown', 'audio':('qualified_component' if 'audio' in association['observations'] else 'unknown'), 'transform':('qualified_component' if 'crop' in association['observations'] else 'unknown')}
                                            if association else {'pts': 'unknown', 'input_events': 'unknown', 'drops': 'unknown'}),
                    'feedback': notes}
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        if len(encoded) > plan_feedback.MAX_BYTES:
            raise BundleUnavailable('recording metadata exceeds its limit')

        observation_bytes = (json.dumps(association['observations'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8') if association else None)
        if observation_bytes is not None and len(observation_bytes) > 16384: raise BundleUnavailable('recording observations exceed limit')

        def zip_entry(name):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            return info

        def write_archive():
            source.seek(0)
            with zipfile.ZipFile(_CappedFile(output), 'w', compression=zipfile.ZIP_STORED) as archive:
                if include_source:
                    with archive.open(zip_entry('recording.webm'), 'w', force_zip64=True) as member:
                        while chunk := source.read(1024 * 1024):
                            member.write(chunk)
                archive.writestr(zip_entry('timeline.json'), encoded)
                if observation_bytes is not None: archive.writestr(zip_entry('observations.json'), observation_bytes)
                manifest = {'version': 1, 'components': [
                    {'path': 'timeline.json', 'sha256': hashlib.sha256(encoded).hexdigest(), 'size': len(encoded)}]}
                if observation_bytes is not None:
                    manifest['components'].append({'path':'observations.json', 'sha256':hashlib.sha256(observation_bytes).hexdigest(), 'size':len(observation_bytes)})
                if include_source:
                    manifest['components'].append({'path': 'recording.webm', 'sha256': evidence['source_sha256'], 'size': evidence['source_size']})
                archive.writestr(zip_entry('manifest.json'), json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('utf-8'))
            output.flush()
            output.seek(0)

        await _owned_thread(write_archive)

        async def still_current():
            try:
                if await current() is not True or await source_current() is not True:
                    return False
                observed = await _readonly(
                    lambda: _selected(session_id, owner, account, feedback_ids, session_current, evidence))
                if observed != notes: return False
                return (association is None or await _qualified_observations(facade, context, original_job_id, evidence) == association)
            except Exception:
                return False

        if not await still_current():
            raise BundleUnavailable('recording selection changed')
        binding = {'kind': 'recording_feedback_bundle', 'version': 1,
                   'source': dict(evidence), 'feedback': notes, 'include_source': include_source,
                   **({'recording_association':association} if association else {})}
        yield RecordingBundle(output, name, binding, notes, still_current)
