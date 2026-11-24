"""Canonical identity for the bounded native-analysis entry sample."""
import hashlib
import json
import re


def disassembly_input_sha(source, revision, target):
    return _entry_input_sha(source,revision,target,xref=False)


def xref_input_sha(source, revision, target):
    return _entry_input_sha(source,revision,target,xref=True)


def instruction_xref_input_sha(source,revision,target):
    if type(target) is not dict or set(target)!={'dossier_id','expected_dossier_revision','selector','max_references'}:raise ValueError('analysis input unavailable')
    row=target['selector'];count=target['max_references']
    if (type(row) is not dict or set(row)!={'address','size','opcode','addressSpace'} or not isinstance(row.get('opcode'),str)
        or len(row['opcode'].encode('utf-8','strict'))>1024 or row.get('addressSpace')!='provider_virtual'
        or type(row.get('address')) is not int or type(row.get('size')) is not int or row['address']<0 or row['size']<=0
        or row['address']+row['size']>2**53-1 or type(count) is not int or not 1<=count<=128):raise ValueError('analysis input unavailable')
    fake={'dossier_id':target['dossier_id'],'expected_dossier_revision':target['expected_dossier_revision'],
        'selector':{'name':'','address':row['address'],'size':row['size'],'addressSpace':row['addressSpace']},'max_references':count}
    _entry_input_sha(source,revision,fake,xref=True)
    rev=target['expected_dossier_revision']
    values=['openclank.native-analysis.xrefs-from-instruction.v1',source,revision['kind'],revision['value'],target['dossier_id'],rev['kind'],rev['value'],row['address'],row['size'],row['opcode'],row['addressSpace'],count]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode('utf-8','strict')).hexdigest()


def decompile_input_sha(source, revision, target, profile):
    if type(target) is not dict or set(target)!={'dossier_id','expected_dossier_revision','selector'}:
        raise ValueError('analysis input unavailable')
    # Reuse the existing strict source/target grammar without using its digest.
    _entry_input_sha(source,revision,{**target,'max_instructions':1},xref=False)
    if (type(profile) is not dict or set(profile)!={'providerSHA256','pluginSHA256','rootSelectorSHA256','manifestSHA256'}
        or any(not isinstance(profile[k],str) or not re.fullmatch(r'[a-f0-9]{64}',profile[k]) for k in ('providerSHA256','pluginSHA256','rootSelectorSHA256'))
        or profile['manifestSHA256'] is not None and (not isinstance(profile['manifestSHA256'],str) or not re.fullmatch(r'[a-f0-9]{64}',profile['manifestSHA256']))):
        raise ValueError('analysis profile unavailable')
    rev,row=target['expected_dossier_revision'],target['selector']
    values=['openclank.native-analysis.decompile.v1',source,revision['kind'],revision['value'],target['dossier_id'],rev['kind'],rev['value'],row['name'],row['address'],row['size'],row['addressSpace'],profile['providerSHA256'],profile['pluginSHA256'],profile['rootSelectorSHA256'],profile['manifestSHA256']]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode('utf-8','strict')).hexdigest()


def _entry_input_sha(source, revision, target, *, xref):
    count_key='max_references' if xref else 'max_instructions'
    if (not isinstance(source, str) or not 8 <= len(source) <= 16384 or not re.fullmatch(r'[\x21-\x7e]+', source)
        or type(revision) is not dict or set(revision) != {'kind', 'value'}
        or any(not isinstance(revision[k], str) or not re.fullmatch(r'[\x21-\x7e]+', revision[k]) for k in revision)
        or len(revision['kind']) > 64 or len(revision['value']) > 256
        or type(target) is not dict or set(target) != {'dossier_id','expected_dossier_revision','selector',count_key}):
        raise ValueError('analysis input unavailable')
    dossier, rev, row, count = target['dossier_id'], target['expected_dossier_revision'], target['selector'], target[count_key]
    if (not isinstance(dossier,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',dossier)
        or type(rev) is not dict or set(rev) != {'kind','value'} or rev.get('kind') != 'hostFingerprint'
        or not isinstance(rev.get('value'),str) or len(rev['value'])>256 or not re.fullmatch(r'sha256:[a-f0-9]{64}:[0-9]+',rev['value'])
        or type(row) is not dict or set(row) != {'name','address','size','addressSpace'}
        or not isinstance(row.get('name'),str) or len(row['name'].encode('utf-8','strict'))>512
        or row.get('addressSpace')!='provider_virtual' or type(row.get('address')) is not int or type(row.get('size')) is not int
        or row['address']<0 or row['size']<=0 or row['address']+row['size']>2**53-1
        or type(count) is not int or not 1<=count<=128):
        raise ValueError('analysis input unavailable')
    values=['openclank.native-analysis.xrefs-to-entry.v1' if xref else 'openclank.native-analysis.disassembly.v1',source,revision['kind'],revision['value'],dossier,rev['kind'],rev['value'],row['name'],row['address'],row['size'],row['addressSpace'],count]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode('utf-8','strict')).hexdigest()
