"""Closed first-profile human-owned screen recording bindings."""
import re
ADAPTER='screen-record-v1'
MAX_BYTES=64*1024*1024
LIMITS={'duration_ms':60000,'fps':15,'max_width':1920,'max_height':1080,'max_bytes':MAX_BYTES}
_HASH=re.compile(r'^[0-9a-f]{64}$')

def text(value,maximum):
    if not isinstance(value,str) or not value or len(value.encode('utf-8'))>maximum or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError('recording identity unavailable')
    return value

def revision(value):
    if not isinstance(value,dict) or set(value)!={'kind','value'}:raise ValueError('recording revision unavailable')
    return {key:text(value[key],512) for key in ('kind','value')}

def validate_binding(value, *, account, hashes):
    fields={'interface','ownerAccountID','destination_ref','destination_revision','destination_name','helper_sha256','limits'}
    if (not isinstance(value,dict) or set(value) not in (fields, fields | {'recording_protocol'}, fields | {'recording_protocol','audio_intent'}, fields | {'recording_protocol','control_stream'}) or value.get('recording_protocol','record-v3') not in ('record-v3','record-v5','record-v6','record-v7','record-v8') or (value.get('recording_protocol') in ('record-v6','record-v7')) != ('audio_intent' in value) or (value.get('recording_protocol') == 'record-v8') != ('control_stream' in value) or value['interface']!=ADAPTER or value['ownerAccountID']!=account
        or hashes!=[] or value['limits']!=LIMITS or not isinstance(value['helper_sha256'],str) or not _HASH.fullmatch(value['helper_sha256'])):
        raise ValueError('recording binding unavailable')
    if value.get('recording_protocol') == 'record-v8':
        binding=value['control_stream']
        if type(binding) is not dict or set(binding)!={'id','epoch'} or not isinstance(binding['id'],str) or not _HASH.fullmatch(binding['id']) or type(binding['epoch']) is not int or binding['epoch']<0: raise ValueError('recording stream unavailable')
    if value.get('recording_protocol') == 'record-v6':
        from src.openclank.recording_audio_contract import intent
        intent(value['audio_intent'])
    if value.get('recording_protocol') == 'record-v7':
        offered=value['audio_intent']
        if type(offered) is not dict or set(offered) != {'microphone','system_output'} or any(type(v) is not bool for v in offered.values()): raise ValueError('recording intent unavailable')
    name=text(value['destination_name'],240)
    if not name.lower().endswith('.webm') or name in ('.','..') or any(c in name for c in ('/','\\')):raise ValueError('recording name unavailable')
    return {'interface':ADAPTER,'ownerAccountID':account,'destination_ref':text(value['destination_ref'],16384),
        'destination_revision':revision(value['destination_revision']),'destination_name':name,'helper_sha256':value['helper_sha256'],'limits':dict(LIMITS), **({'recording_protocol':value['recording_protocol']} if 'recording_protocol' in value else {}), **({'audio_intent':dict(value['audio_intent'])} if 'audio_intent' in value else {}), **({'control_stream':dict(value['control_stream'])} if 'control_stream' in value else {})}

def validate_receipt(value, *, protocol=None):
    if not isinstance(value,dict) or value.get('outcome') not in {'finalized','partial','unavailable'}:raise ValueError('recording receipt unavailable')
    fields={'outcome','finalized','exit_code','reason','generation','metadata'}
    if value['outcome']!='unavailable':fields|={'artifact_id','sha256','size','mime_type'}
    if set(value)!=fields or type(value['exit_code']) is not int or not -255<=value['exit_code']<=255 or (value['metadata'] is not None and not isinstance(value['metadata'],dict)):raise ValueError('recording receipt unavailable')
    if not isinstance(value['generation'],str) or not re.fullmatch(r'[0-9a-f]{32}',value['generation']):raise ValueError('recording receipt unavailable')
    if value['reason'] not in {'stopped','duration','cancelled','source_lost','output_limit','error'}:raise ValueError('recording receipt unavailable')
    if value['metadata'] is None:
        if value['outcome'] == 'finalized': raise ValueError('recording receipt unavailable')
    else:
        from src.openclank.native_capture_session_linux import validate_recording_reply, validate_observed_recording_reply
        version = value['metadata'].get('version')
        if version not in (3,5,6,7,8) or (protocol is not None and version != {'record-v3':3,'record-v5':5,'record-v6':6,'record-v7':7,'record-v8':8}.get(protocol)): raise ValueError('recording protocol changed')
        if version == 8:
            from src.openclank.native_capture_session_linux import validate_control_recording_reply
            metadata = validate_control_recording_reply(value['metadata'], value['generation'])
        elif version == 7:
            from src.openclank.recording_crop_contract import validate_reply
            metadata = validate_reply(value['metadata'], value['generation'])
        elif version == 6:
            from src.openclank.recording_audio_contract import validate_reply
            metadata = validate_reply(value['metadata'], value['generation'])
        else:
            metadata = (validate_observed_recording_reply if version == 5 else validate_recording_reply)(value['metadata'], value['generation'])
        if (metadata.get('state') not in {'finalized','partial'} or metadata.get('reason') != value['reason']
            or (value['outcome'] == 'finalized' and metadata['state'] != 'finalized')
            or (value['outcome'] != 'unavailable' and metadata.get('bytes_written') != value['size'])):
            raise ValueError('recording receipt unavailable')
    if value['outcome']=='finalized':
        if value['finalized'] is not True or value['exit_code']!=0 or value['reason'] not in {'stopped','duration'} or value['mime_type']!='video/webm':raise ValueError('recording receipt unavailable')
    elif value['finalized'] is not False:raise ValueError('recording receipt unavailable')
    if value['outcome']=='partial' and value['mime_type']!='application/octet-stream':raise ValueError('recording receipt unavailable')
    if value['outcome']!='unavailable':
        if not isinstance(value['artifact_id'],str) or not value['artifact_id'] or not isinstance(value['sha256'],str) or not _HASH.fullmatch(value['sha256']) or type(value['size']) is not int or not 0<value['size']<=MAX_BYTES:raise ValueError('recording receipt unavailable')
    return dict(value)
