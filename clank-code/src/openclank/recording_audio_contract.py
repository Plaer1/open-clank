"""Closed original-child v6 recording selection and metadata vocabulary."""
import hashlib
import json
import re
import unicodedata
from src.openclank.native_capture_session_linux import observed_recording_start, observed_recording_command, validate_observed_recording_reply

_ID = re.compile(r'^[a-f0-9]{32}$')
_HASH = re.compile(r'^[a-f0-9]{64}$')
_KINDS = ('microphone', 'system_output')


def intent(value):
    if type(value) is not dict or set(value) != set(_KINDS) or any(type(value[k]) is not bool for k in _KINDS) or not any(value.values()):
        raise ValueError('audio intent unavailable')
    return dict(value)


def start(generation, deadline_ms, offered):
    return {**observed_recording_start(generation, deadline_ms), 'version':6, 'audio_intent':intent(offered)}


def command(action, generation):
    return {**observed_recording_command(action, generation), 'version':6}


def selection(generation, selection_id, microphone, system_output):
    if type(generation) is not str or type(selection_id) is not str or not _ID.fullmatch(generation) or not _ID.fullmatch(selection_id):
        raise ValueError('audio selection unavailable')
    values=(microphone,system_output)
    if any(v is not None and (type(v) is not str or not _ID.fullmatch(v)) for v in values):
        raise ValueError('audio selection unavailable')
    raw=json.dumps(['openclank.recording.audio-selection.v1', generation, selection_id, *values], separators=(',', ':')).encode('utf-8')
    return {'version':6,'action':'select','generation':generation,'selection_id':selection_id,
            'selection_digest':hashlib.sha256(raw).hexdigest(),'microphone':microphone,'system_output':system_output}


def validate_reply(value, generation):
    if type(value) is not dict or value.get('version') != 6 or value.get('generation') != generation or not _ID.fullmatch(str(generation)):
        raise ValueError('audio reply unavailable')
    state=value.get('state')
    if state == 'choices':
        if set(value) != {'version','state','generation','choices'} or type(value['choices']) is not list or len(value['choices'])>24:
            raise ValueError('audio choices unavailable')
        seen=set()
        for item in value['choices']:
            if (type(item) is not dict or set(item) != {'kind','choice_id','label','channels'} or item['kind'] not in _KINDS
                or type(item['choice_id']) is not str or not _ID.fullmatch(item['choice_id']) or item['choice_id'] in seen
                or type(item['label']) is not str or not item['label'] or len(item['label'].encode('utf-8'))>64
                or any(unicodedata.category(c)=='Cc' for c in item['label'])
                or item['channels'] not in (['MONO'],['FL','FR'],['FR','FL'])):
                raise ValueError('audio choices unavailable')
            seen.add(item['choice_id'])
        return dict(value)
    if state == 'selection_accepted':
        if (set(value) != {'version','state','generation','selection_id','selection_digest','selected_kinds'}
            or type(value['selection_id']) is not str or not _ID.fullmatch(value['selection_id'])
            or type(value['selection_digest']) is not str or not _HASH.fullmatch(value['selection_digest'])
            or value['selected_kinds'] not in ([],['microphone'],['system_output'],list(_KINDS))):
            raise ValueError('audio acknowledgement unavailable')
        return dict(value)
    base=dict(value);base['version']=5
    if state in ('starting','recording'):
        if base.get('audio') not in ('none','selected'):raise ValueError('audio phase unavailable')
        base['audio']='none';validate_observed_recording_reply(base,generation)
        return dict(value)
    if state == 'unsupported':
        validate_observed_recording_reply(base,generation);return dict(value)
    if state not in ('finalized','partial'):
        raise ValueError('audio terminal unavailable')
    if not {'selection_id','selection_digest','tracks'} <= set(base):raise ValueError('audio terminal unavailable')
    selection_id=base.pop('selection_id');digest=base.pop('selection_digest');tracks=base.pop('tracks')
    validate_observed_recording_reply(base,generation)
    if type(selection_id) is not str or not _ID.fullmatch(selection_id) or type(digest) is not str or not _HASH.fullmatch(digest) or type(tracks) is not list or len(tracks)>2:
        raise ValueError('audio terminal unavailable')
    keys={'kind','stage','assurance','sample_rate','channels','queued_frames','submitted_frames','first_running_ns','last_running_ns','max_clock_uncertainty_ns','last_graph_delay_ns','discontinuities','missing_metadata','header_buffers','header_absent_buffers','pre_start_discarded_frames','eos'}
    seen=[]
    for row in tracks:
        if (type(row) is not dict or set(row)!=keys or row['kind'] not in _KINDS or row['kind'] in seen
            or row['stage']!='pcm_appsrc_submission' or row['assurance']!='software_correlated_estimate'
            or (row['sample_rate'] is not None and (type(row['sample_rate']) is not int or row['sample_rate']!=48000))
            or (row['channels'] is not None and (type(row['channels']) is not int or row['channels'] not in (1,2))) or type(row['eos']) is not bool
            or any(type(row[k]) is not int or not 0<=row[k]<=2**53-1 for k in ('queued_frames','submitted_frames','discontinuities','missing_metadata','header_buffers','header_absent_buffers','pre_start_discarded_frames'))):
            raise ValueError('audio track unavailable')
        if row['submitted_frames']>row['queued_frames']:raise ValueError('audio track unavailable')
        for key in ('first_running_ns','last_running_ns','max_clock_uncertainty_ns','last_graph_delay_ns'):
            val=row[key]
            if val is None:continue
            signed=key=='last_graph_delay_ns'; pattern=r'0|-?[1-9][0-9]{0,18}' if signed else r'0|[1-9][0-9]{0,19}'
            if type(val) is not str or not re.fullmatch(pattern,val) or not (-(2**63)<=int(val)<=2**63-1 if signed else 0<=int(val)<=2**64-1):
                raise ValueError('audio track unavailable')
        if state=='finalized' and (row['sample_rate'] is None or row['channels'] is None or not row['eos']
            or row['submitted_frames'] <= 0 or row['first_running_ns'] is None or row['last_running_ns'] is None
            or int(row['first_running_ns']) > int(row['last_running_ns'])
            or row['queued_frames'] != row['submitted_frames'] + row['pre_start_discarded_frames']):
            raise ValueError('audio track incomplete')
        seen.append(row['kind'])
    if seen != [kind for kind in _KINDS if kind in seen]:raise ValueError('audio track order unavailable')
    return dict(value)
