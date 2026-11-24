"""Byte-preserving slide text edits, without resource or authorization access."""
from __future__ import annotations
import hashlib
import json
import math
import re
from collections.abc import Mapping
from src.openclank.collaboration_text import MAX_INSERT_BYTES
from src.openclank.collaboration_workbook import _Parser, _Numeric, _utf, WorkbookError

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')

class SlidesError(ValueError): pass
class SlidesConflict(SlidesError): pass

def _id(value): return type(value) is str and _ID.fullmatch(value) is not None

def _text(value, limit):
    return type(value) is str and len(value.encode('utf-16-le','strict')) // 2 <= limit

def _number(value, minimum, maximum):
    if not isinstance(value,_Numeric): return False
    try: number=float(value)
    except (ValueError,OverflowError): return False
    return math.isfinite(number) and minimum <= number <= maximum

def validate_objects(objects):
    if not isinstance(objects,list) or not 1<=len(objects)<=64: raise SlidesError('invalid slide targets')
    seen=set(); total=0
    for item in objects:
        if not isinstance(item,Mapping) or set(item)!={'slide_id','object_id','text'} or not _id(item['slide_id']) or not _id(item['object_id']) or type(item['text']) is not str:
            raise SlidesError('invalid slide targets')
        key=(item['slide_id'],item['object_id'])
        if key in seen: raise SlidesError('duplicate slide target')
        try:
            if not _text(item['text'],65536): raise SlidesError('invalid slide text')
            total+=len(json.dumps(item['text'],ensure_ascii=False,separators=(',',':')).encode('utf-8','strict'))
        except UnicodeError as exc: raise SlidesError('invalid slide text') from exc
        if total>MAX_INSERT_BYTES: raise SlidesError('slide insertion exceeds bounds')
        seen.add(key)

def _deck(source):
    try:
        _utf(source); parser=_Parser(source); doc=parser.parse(); parser.ws()
        if parser.i!=len(source) or not isinstance(doc,dict) or doc.get('schema')!='openclank.slides' or not _number(doc.get('version'),1,1) or not _id(doc.get('id')) or not _text(doc.get('title'),512): raise SlidesError('invalid slide deck')
        size=doc.get('size'); slides=doc.get('slides')
        if not isinstance(size,dict) or not _number(size.get('width'),1,10000) or not _number(size.get('height'),1,10000) or not isinstance(slides,list) or not 1<=len(slides)<=100: raise SlidesError('invalid slide deck')
        for key in ('appearance','provenance'):
            if doc.get(key) is not None and not isinstance(doc[key],dict): raise SlidesError('invalid slide deck')
        template=doc.get('template')
        if template is not None and (not isinstance(template,dict) or not _text(template.get('ref'),4096) or not template['ref'] or not _text(template.get('version'),128) or not template['version'] or (template.get('recipeRef') is not None and not _text(template['recipeRef'],4096))): raise SlidesError('invalid slide template')
        ids={doc['id']}; targets={}; count=0
        def take(obj):
            ident=obj.get('id')
            if not _id(ident) or ident in ids: raise SlidesError('invalid slide identity')
            ids.add(ident);return ident
        for si,slide in enumerate(slides):
            if not isinstance(slide,dict): raise SlidesError('invalid slide')
            sid=take(slide); items=slide.get('objects')
            if not _text(slide.get('name'),512) or not _text(slide.get('notes'),65536) or not isinstance(items,list): raise SlidesError('invalid slide')
            for oi,item in enumerate(items):
                if not isinstance(item,dict): raise SlidesError('invalid slide object')
                oid=take(item); count+=1; kind=item.get('type')
                if count>3000 or kind not in ('text','vector','image','chart') or not _number(item.get('x'),-10000,10000) or not _number(item.get('y'),-10000,10000) or not _number(item.get('width'),0.01,20000) or not _number(item.get('height'),0.01,20000): raise SlidesError('invalid slide geometry')
                if kind=='text':
                    if not _text(item.get('text'),65536) or (item.get('fontSize') is not None and not _number(item['fontSize'],1,1000)): raise SlidesError('invalid text object')
                    targets[oid]=(doc['id'],sid,si,oi,kind,item['text'],parser.spans[('slides',si,'objects',oi,'text')])
                elif kind=='vector':
                    if item.get('shape') not in ('rect','ellipse','line'): raise SlidesError('invalid vector object')
                else:
                    if not _text(item.get('resourceRef'),4096) or not item['resourceRef'] or item.get('revision') is None: raise SlidesError('invalid slide resource')
                    if kind=='image' and not _text(item.get('alt'),1024): raise SlidesError('invalid slide image')
                    if kind=='chart' and (not _id(item.get('chartId')) or not _text(item.get('title'),512)): raise SlidesError('invalid slide chart')
        return targets
    except (WorkbookError, UnicodeError, RecursionError) as exc:
        raise SlidesError('invalid slide source') from exc

def validate_slides(text): _deck(text)


def verify_slide_text_object(text, deck_id, slide_id, object_id):
    target = _deck(text).get(object_id) if isinstance(object_id, str) else None
    if not target or target[0] != deck_id or target[1] != slide_id: raise SlidesError('slide text object unavailable')
    return True

def verify_slide_object(text, deck_id, slide_id, object_id):
    _deck(text)
    parser = _Parser(text); doc = parser.parse(); parser.ws()
    if doc['id'] != deck_id: raise SlidesError('slide object unavailable')
    matches = [obj for slide in doc['slides'] if slide['id'] == slide_id
               for obj in slide['objects'] if obj['id'] == object_id]
    if len(matches) != 1: raise SlidesError('slide object unavailable')
    return matches[0]['type']


def prepare_slides(base_text,base_sha256,current_text,objects):
    try: base=_utf(base_text)
    except WorkbookError as exc: raise SlidesError('invalid slide base') from exc
    if not isinstance(base_sha256,str) or re.fullmatch(r'[0-9a-f]{64}',base_sha256) is None or hashlib.sha256(base).hexdigest()!=base_sha256: raise SlidesError('invalid base fingerprint')
    validate_objects(objects); before=_deck(base_text); current=_deck(current_text); changes=[]
    for item in objects:
        original=before.get(item['object_id']); live=current.get(item['object_id'])
        if not original or not live or original[:6]!=live[:6] or original[1]!=item['slide_id']: raise SlidesConflict('slide text changed')
        if live[5]!=item['text']: changes.append((live[6],json.dumps(item['text'],ensure_ascii=False,separators=(',',':'))))
    if not changes: raise SlidesError('no slide changes')
    result=current_text
    for (start,end),value in sorted(changes,reverse=True): result=result[:start]+value+result[end:]
    try: data=_utf(result)
    except WorkbookError as exc: raise SlidesError('invalid slide result') from exc
    _deck(result)
    return {'text':result,'sha256':hashlib.sha256(data).hexdigest()}
