"""Closed original-source identity and terminal facts for local WebM trim."""
import re
from src.openclank.recording_contract import MAX_BYTES, revision, text

ADAPTER='trim-webm-v1'
_HASH=re.compile(r'^[0-9a-f]{64}$')


def validate_binding(value, *, account, hashes):
    fields={'interface','ownerAccountID','source_ref','source_resource_id','source_revision','source_sha256','source_size','source_object_tag',
            'destination_ref','destination_revision','destination_name','helper_sha256','start_ms','end_ms'}
    if (not isinstance(value,dict) or set(value)!=fields or value['interface']!=ADAPTER or value['ownerAccountID']!=account
        or not isinstance(value['source_sha256'],str) or not _HASH.fullmatch(value['source_sha256'])
        or hashes != [value['source_sha256']] or type(value['source_size']) is not int or not 0<value['source_size']<=MAX_BYTES
        or not isinstance(value['source_object_tag'],str) or not _HASH.fullmatch(value['source_object_tag'])
        or not isinstance(value['helper_sha256'],str) or not _HASH.fullmatch(value['helper_sha256'])
        or type(value['start_ms']) is not int or type(value['end_ms']) is not int
        or not 0<=value['start_ms']<value['end_ms']<=86400000 or value['end_ms']-value['start_ms']>60000):
        raise ValueError('trim binding unavailable')
    name=text(value['destination_name'],240)
    if not name.lower().endswith('.webm') or name in ('.','..') or any(c in name for c in ('/','\\')): raise ValueError('trim destination unavailable')
    return {'interface':ADAPTER,'ownerAccountID':account,'source_ref':text(value['source_ref'],16384),
        'source_resource_id':text(value['source_resource_id'],256),'source_revision':revision(value['source_revision']),
        'source_sha256':value['source_sha256'],'source_size':value['source_size'],'source_object_tag':value['source_object_tag'],
        'destination_ref':text(value['destination_ref'],16384),'destination_revision':revision(value['destination_revision']),
        'destination_name':name,'helper_sha256':value['helper_sha256'],'start_ms':value['start_ms'],'end_ms':value['end_ms']}


def validate_receipt(value, *, start_ms, end_ms):
    if not isinstance(value,dict) or value.get('outcome') not in {'finalized','partial'}: raise ValueError('trim receipt unavailable')
    fields={'outcome','finalized','exit_code','reason','generation','metadata','artifact_id','sha256','size','mime_type'}
    if (set(value)!=fields or type(value['exit_code']) is not int or not -255<=value['exit_code']<=255
        or not isinstance(value['generation'],str) or not re.fullmatch(r'[0-9a-f]{32}',value['generation'])
        or value['reason'] not in {'eos','output_limit','cancelled','unsupported','error'}
        or not isinstance(value['artifact_id'],str) or not value['artifact_id']
        or not isinstance(value['sha256'],str) or not _HASH.fullmatch(value['sha256'])
        or type(value['size']) is not int or not 0<value['size']<=MAX_BYTES): raise ValueError('trim receipt unavailable')
    if value['metadata'] is None:
        if value['outcome']=='finalized': raise ValueError('trim receipt unavailable')
    else:
        from src.openclank.native_capture_session_linux import validate_trim_reply
        meta=validate_trim_reply(value['metadata'],value['generation'],start_ms,end_ms)
        if meta.get('state') not in ('finalized','partial') or meta['reason']!=value['reason'] or meta['bytes_written']!=value['size']:
            raise ValueError('trim receipt unavailable')
    if value['outcome']=='finalized':
        if value['finalized'] is not True or value['exit_code']!=0 or value['reason']!='eos' or value['mime_type']!='video/webm' or value['metadata']['state']!='finalized':
            raise ValueError('trim receipt unavailable')
    elif value['finalized'] is not False or value['mime_type']!='application/octet-stream': raise ValueError('trim receipt unavailable')
    return dict(value)


def source_evidence(identity, job):
    """Project only the exact positively settled original v4 child receipt."""
    if not isinstance(identity,dict) or not isinstance(job,dict) or job.get('identity')!=identity:
        raise ValueError('trim evidence unavailable')
    binding=validate_binding(identity.get('resource_binding'),account=identity.get('account_id'),hashes=identity.get('input_hashes'))
    child=job.get('local_child');artifact=job.get('artifact')
    if (not isinstance(child,dict) or child.get('state')!='terminal' or child.get('account_id')!=identity['account_id']
        or child.get('job_id')!=job.get('id') or child.get('identity_digest')!=job.get('identity_digest')
        or child.get('claim_token')!=job.get('submission_token') or not isinstance(artifact,dict)):
        raise ValueError('trim evidence unavailable')
    receipt=validate_receipt(child.get('receipt'),start_ms=binding['start_ms'],end_ms=binding['end_ms'])
    if (receipt['outcome']!='finalized' or receipt['sha256']!=artifact.get('sha256')
        or receipt['size']!=artifact.get('size') or receipt['artifact_id']!=artifact.get('artifact_id')
        or artifact.get('mime_type')!='video/webm'):
        raise ValueError('trim evidence unavailable')
    meta=receipt['metadata']
    return {'version':1,'kind':'video_trim','source':{'resource_id':binding['source_resource_id'],
        'revision':dict(binding['source_revision']),'sha256':binding['source_sha256'],'size':binding['source_size']},
        'requested':{'start_ms':binding['start_ms'],'end_ms':binding['end_ms'],'timebase':'playback_hint'},
        'observed':{key:meta[key] for key in ('timebase','first_pts_ns','last_pts_ns','last_duration_ns','selected_frames','encoded_frames','coverage')},
        'output_sha256':receipt['sha256']}
