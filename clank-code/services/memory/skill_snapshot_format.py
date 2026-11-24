"""Data-only portable snapshot of one published skill; no access authority."""
from __future__ import annotations
import copy
import hashlib
import json
import re
from services.memory.skill_lifecycle import _safe_bundle_path

FORMAT='openclank.skill-snapshot.v1'
_MAX_TEXT=256*1024
_MAX_TOTAL=2*1024*1024
_MAX_PACKAGE=4*1024*1024
_HEX=re.compile(r'[0-9a-f]{64}\Z')
_NAME=re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
_RESERVED=re.compile(r'(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z',re.I)
_INTERNAL={'_lifecycle.json','_revisions','_usage_events.jsonl','_promotions.json','_manifest.json','.lifecycle.lock'}


def canonical_bytes(value):
    try:
        return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8','strict')
    except (TypeError,ValueError,UnicodeError,RecursionError) as exc:
        raise ValueError('Invalid snapshot JSON') from exc


def _string(value, *, limit=_MAX_TEXT, nonempty=False):
    if type(value) is not str: raise ValueError('Invalid snapshot text')
    try: data=value.encode('utf-8','strict')
    except UnicodeError as exc: raise ValueError('Invalid snapshot text') from exc
    if len(data)>limit or '\x00' in value or (nonempty and not value.strip()): raise ValueError('Invalid snapshot text')
    return value


def _name(value):
    _string(value,limit=60,nonempty=True)
    if not _NAME.fullmatch(value): raise ValueError('Invalid skill name')
    return value


def _sha(value):
    if type(value) is not str or not _HEX.fullmatch(value): raise ValueError('Invalid snapshot hash')
    return value


def _path(value):
    _string(value,limit=512,nonempty=True)
    if _safe_bundle_path(value)!=value or any(c in value for c in '\\:<>"|?*'): raise ValueError('Invalid snapshot path')
    parts=value.split('/')
    if len(parts)>8 or any(not part or part in ('.','..') or part.endswith(('.',' ')) or
        _RESERVED.fullmatch(part) or part.casefold() in _INTERNAL or part.casefold().endswith('.lock') or
        any(ord(c)<32 or ord(c)==127 for c in part) for part in parts): raise ValueError('Invalid snapshot path')
    if any(part.casefold()=='skill.md' for part in parts): raise ValueError('Invalid snapshot path')
    return value


def _array(value):
    if type(value) is not list or len(value)>64: raise ValueError('Invalid snapshot list')
    return [_string(x) for x in value]


def validate_package(package):
    if type(package) is not dict or set(package)!={'format','origin','skill','files'} or package['format']!=FORMAT:
        raise ValueError('Invalid snapshot package')
    origin=package['origin']; skill=package['skill']; files=package['files']
    if type(origin) is not dict or set(origin)!={'item_ref','revision','content_sha256','snapshot_sha256','bundle_sha256','declared_scope'}:
        raise ValueError('Invalid snapshot origin')
    if type(origin['item_ref']) is not str or not re.fullmatch(r'skill-[0-9a-f]{64}',origin['item_ref']) or type(origin['revision']) is not int or not 1<=origin['revision']<=9007199254740991:
        raise ValueError('Invalid snapshot origin')
    for key in ('content_sha256','snapshot_sha256','bundle_sha256'): _sha(origin[key])
    scope=origin['declared_scope']
    if type(scope) is not dict or set(scope)!={'kind','platforms'} or scope['kind']!='owner': raise ValueError('Invalid snapshot scope')
    platforms=_array(scope['platforms'])
    keys={'name','description','version','tags','platforms','when_to_use','procedure','pitfalls','verification','body_extra'}
    if type(skill) is not dict or set(skill)!=keys: raise ValueError('Invalid snapshot skill')
    _name(skill['name'])
    for key in ('description','version','when_to_use','body_extra'): _string(skill[key])
    for key in ('tags','platforms','procedure','pitfalls','verification'): _array(skill[key])
    if platforms!=skill['platforms']: raise ValueError('Snapshot scope changed')
    total=sum(len(skill[k].encode('utf-8')) for k in ('name','description','version','when_to_use','body_extra'))
    total+=sum(len(x.encode('utf-8')) for k in ('tags','platforms','procedure','pitfalls','verification') for x in skill[k])
    if type(files) is not list or len(files)>63: raise ValueError('Invalid snapshot files')
    seen=set(); paths=[]
    for item in files:
        if type(item) is not dict or set(item)!={'path','utf8','sha256','size'}: raise ValueError('Invalid snapshot file')
        path=_path(item['path']); folded=path.casefold()
        if folded in seen or any(folded.startswith(old+'/') or old.startswith(folded+'/') for old in seen): raise ValueError('Ambiguous snapshot paths')
        seen.add(folded); paths.append(path)
        raw=_string(item['utf8']).encode('utf-8')
        if type(item['size']) is not int or item['size']!=len(raw) or _sha(item['sha256'])!=hashlib.sha256(raw).hexdigest(): raise ValueError('Snapshot file changed')
        total+=len(raw)
    if paths!=sorted(paths) or total>_MAX_TOTAL or len(canonical_bytes(package))>_MAX_PACKAGE: raise ValueError('Snapshot exceeds bounds')
    return copy.deepcopy(package)


def parse_package(raw):
    if type(raw) is not bytes or len(raw)>_MAX_PACKAGE: raise ValueError('Invalid snapshot package')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('Duplicate snapshot key')
            result[key]=value
        return result
    try: value=json.loads(raw.decode('utf-8','strict'),object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON')))
    except (ValueError,UnicodeError,RecursionError) as exc: raise ValueError('Invalid snapshot package') from exc
    return validate_package(value)


def package_sha256(package):
    return hashlib.sha256(canonical_bytes(validate_package(package))).hexdigest()


def review_package(package, *, destination_name):
    package=validate_package(package); _name(destination_name)
    review={'version':1,'package_sha256':package_sha256(package),'destination':{'name':destination_name,'category':'imported'},
            'origin':copy.deepcopy(package['origin']),'skill':copy.deepcopy(package['skill']),
            'files':[{'path':x['path'],'size':x['size'],'sha256':x['sha256']} for x in package['files']]}
    review['review_sha256']=hashlib.sha256(canonical_bytes(review)).hexdigest()
    return review


def from_published_skill(skill, *, reference, companions):
    from services.memory.skill_format import Skill
    if type(skill) is not Skill or type(reference) is not dict or set(reference)!={'skill_id','revision','content_sha256','snapshot_sha256','bundle_sha256'} or type(companions) is not dict:
        raise ValueError('Published skill unavailable')
    if any(getattr(skill,key) for key in ('source_uri','source_revision','source_memory_ids','source_status','unknown_frontmatter_raw','requires_toolsets','fallback_for_toolsets')) or skill.resource_metadata is not None:
        raise ValueError('Unsupported skill metadata')
    sid=_string(reference['skill_id'],limit=512,nonempty=True)
    if skill.skill_id!=sid or type(reference['revision']) is not int or skill.revision!=reference['revision']:
        raise ValueError('Published skill changed')
    names=('name','description','version','tags','platforms','when_to_use','procedure','pitfalls','verification','body_extra')
    authored={key:copy.deepcopy(getattr(skill,key)) for key in names}
    origin={'item_ref':'skill-'+hashlib.sha256(canonical_bytes([sid])).hexdigest(),'revision':reference['revision'],
            'content_sha256':reference['content_sha256'],'snapshot_sha256':reference['snapshot_sha256'],
            'bundle_sha256':reference['bundle_sha256'],'declared_scope':{'kind':'owner','platforms':copy.deepcopy(skill.platforms)}}
    files=[]
    for path,value in sorted(companions.items()):
        _path(path); raw=_string(value).encode('utf-8')
        files.append({'path':path,'utf8':value,'sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw)})
    return validate_package({'format':FORMAT,'origin':origin,'skill':authored,'files':files})
