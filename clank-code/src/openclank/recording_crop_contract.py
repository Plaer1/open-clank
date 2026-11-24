"""Closed, same-child recording crop selection vocabulary (private v7)."""
import hashlib
import json
import re

from src.openclank.recording_audio_contract import selection as audio_selection

_ID = re.compile(r'^[a-f0-9]{32}$')


def selection(generation, selection_id, microphone, system_output, crop, *, source_width, source_height):
    """Bind one explicit crop and the audio choices to an original selection."""
    audio_selection(generation, selection_id, microphone, system_output)
    if (type(crop) is not dict or set(crop) != {'mode', 'basis_id', 'rect'}
        or crop['mode'] not in ('full', 'crop') or type(crop['basis_id']) is not str
        or not _ID.fullmatch(crop['basis_id'])
        or any(type(n) is not int or not 1 <= n <= 16384 for n in (source_width, source_height))
        or source_width * source_height > 16_000_000):
        raise ValueError('recording crop unavailable')
    rect = crop['rect']
    if crop['mode'] == 'full':
        if rect is not None:
            raise ValueError('recording crop unavailable')
        coords = [None] * 4
    else:
        if (type(rect) is not dict or set(rect) != {'x', 'y', 'width', 'height'}
            or any(type(rect[k]) is not int or rect[k] % 2 for k in ('x', 'y', 'width', 'height'))
            or rect['x'] < 0 or rect['y'] < 0 or rect['width'] <= 0 or rect['height'] <= 0
            or rect['x'] + rect['width'] > source_width or rect['y'] + rect['height'] > source_height):
            raise ValueError('recording crop unavailable')
        coords = [rect[k] for k in ('x', 'y', 'width', 'height')]
    digest = hashlib.sha256(json.dumps(['openclank.recording.selection.v2', generation, selection_id,
        microphone, system_output, crop['mode'], crop['basis_id'], *coords], separators=(',', ':')).encode()).hexdigest()
    return {'version': 7, 'action': 'select', 'generation': generation, 'selection_id': selection_id,
            'selection_digest': digest, 'microphone': microphone, 'system_output': system_output,
            'crop': {'mode': crop['mode'], 'basis_id': crop['basis_id'], 'rect': dict(rect) if rect is not None else None}}


def validate_preview(preview, data):
    """Validate the exact complete bounded PNG from the original preview pipe."""
    import struct
    if (type(preview) is not dict or set(preview) != {'basis_id','source_width','source_height','preview_width','preview_height','bytes','sha256','mime'}
        or type(preview['basis_id']) is not str or not _ID.fullmatch(preview['basis_id'])
        or preview['mime'] != 'image/png' or type(data) is not bytes
        or type(preview['bytes']) is not int or not 24 <= preview['bytes'] <= 2 * 1024 * 1024
        or len(data) != preview['bytes'] or type(preview['sha256']) is not str
        or not re.fullmatch(r'[a-f0-9]{64}',preview['sha256']) or hashlib.sha256(data).hexdigest() != preview['sha256']
        or any(type(preview[k]) is not int or preview[k] < 1 for k in ('source_width','source_height','preview_width','preview_height'))
        or preview['source_width'] > 16384 or preview['source_height'] > 16384
        or preview['source_width'] * preview['source_height'] > 16_000_000
        or preview['preview_width'] > 960 or preview['preview_height'] > 540
        or abs(preview['source_width'] * preview['preview_height'] - preview['source_height'] * preview['preview_width']) > max(preview['source_width'],preview['source_height'])
        or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR'
        or struct.unpack('>II',data[16:24]) != (preview['preview_width'],preview['preview_height'])):
        raise ValueError('recording preview unavailable')
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'PNG' or image.size != (preview['preview_width'],preview['preview_height']) or getattr(image,'n_frames',1) != 1: raise ValueError('recording preview unavailable')
            image.verify()
    except Exception as exc:
        raise ValueError('recording preview unavailable') from exc
    return dict(preview)


def start(generation, deadline_ms, offered):
    from src.openclank.native_capture_session_linux import observed_recording_start
    if type(offered) is not dict or set(offered) != {'microphone','system_output'} or any(type(v) is not bool for v in offered.values()):
        raise ValueError('recording intent unavailable')
    return {**observed_recording_start(generation,deadline_ms),'version':7,'audio_intent':dict(offered),'crop_intent':True}


def validate_reply(value, generation):
    from src.openclank.recording_audio_contract import validate_reply as validate_audio
    if type(value) is not dict or value.get('version') != 7 or value.get('generation') != generation:
        raise ValueError('recording reply unavailable')
    base=dict(value);base['version']=6;state=base.get('state')
    if state == 'choices':
        preview=base.pop('preview',None)
        validate_audio(base,generation)
        if type(preview) is not dict or set(preview) != {'basis_id','source_width','source_height','preview_width','preview_height','bytes','sha256','mime'}:
            raise ValueError('recording preview unavailable')
        return dict(value)
    if state == 'selection_accepted':
        crop=base.pop('crop',None);validate_audio(base,generation)
        if type(crop) is not dict or set(crop) != {'mode','basis_id','rect'}:
            raise ValueError('recording crop acknowledgement unavailable')
        return dict(value)
    if state in ('partial','finalized'):
        crop=base.pop('crop',None);validate_audio(base,generation)
        keys={'basis_id','source_width','source_height','rect','output_width','output_height','transform','basis','pixel_aspect','orientation'}
        if type(crop) is not dict or set(crop) != keys or type(crop['basis_id']) is not str or not _ID.fullmatch(crop['basis_id']):
            raise ValueError('recording crop terminal unavailable')
        if crop['basis'] != 'source_buffer' or crop['pixel_aspect'] != '1:1' or crop['orientation'] != 'identity':
            raise ValueError('recording crop terminal unavailable')
        if (any(type(crop[k]) is not int or not 1 <= crop[k] <= 16384 for k in ('source_width','source_height'))
            or crop['source_width'] * crop['source_height'] > 16_000_000
            or any(type(crop[k]) is not int or not 1 <= crop[k] <= 4096 for k in ('output_width','output_height'))
            or value.get('encoded_size') != [crop['output_width'],crop['output_height']]
            or (value['observations']['source_pixels'] is not None and value['observations']['source_pixels'] != [crop['source_width'],crop['source_height']])):
            raise ValueError('recording crop terminal unavailable')
        rect=crop['rect']
        if (type(rect) is not dict or set(rect) != {'x','y','width','height'}
            or any(type(rect[k]) is not int for k in rect) or rect['x'] < 0 or rect['y'] < 0
            or rect['width'] <= 0 or rect['height'] <= 0 or rect['x']+rect['width'] > crop['source_width']
            or rect['y']+rect['height'] > crop['source_height']
            or crop['transform'] != {'x_numerator':crop['output_width'],'x_denominator':rect['width'],
                'y_numerator':crop['output_height'],'y_denominator':rect['height']}):
            raise ValueError('recording crop terminal unavailable')
        return dict(value)
    validate_audio(base,generation);return dict(value)
