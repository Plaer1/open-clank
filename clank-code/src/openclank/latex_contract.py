"""Closed Files identities for the explicit local LaTeX document job."""
import re
from pathlib import PurePosixPath

ADAPTER = 'latex-pdflatex-v1'
MAX_INPUTS = 32
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024
_HASH = re.compile(r'^[0-9a-f]{64}$')
_NAME = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')

def _text(value,maximum):
    if not isinstance(value,str) or not value or len(value.encode('utf-8')) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value): raise ValueError('LaTeX identity unavailable')
    return value

def safe_relative(value):
    value = _text(value,240)
    if '\\' in value or value.startswith('/') or any(not _NAME.fullmatch(part) or part in {'.','..'} for part in value.split('/')):
        raise ValueError('LaTeX dependency name unavailable')
    if str(PurePosixPath(value)) != value or value == 'main.tex': raise ValueError('LaTeX dependency name unavailable')
    return value

def revision(value):
    if not isinstance(value,dict) or set(value) != {'kind','value'}: raise ValueError('LaTeX revision unavailable')
    return {key:_text(value[key],512) for key in ('kind','value')}

def validate_binding(binding, *, account, hashes):
    fields = {'interface','ownerAccountID','inputs','destination_ref','destination_revision','destination_name','compiler_sha256'}
    if not isinstance(binding,dict) or set(binding) != fields or binding['interface'] != ADAPTER or binding['ownerAccountID'] != account:
        raise ValueError('LaTeX binding unavailable')
    inputs=binding['inputs']
    if not isinstance(inputs,list) or not 1 <= len(inputs) <= MAX_INPUTS: raise ValueError('LaTeX inputs unavailable')
    names=set(); output=[]
    for index,item in enumerate(inputs):
        if not isinstance(item,dict) or set(item) != {'ref','resourceID','revision','sha256','relative_name'}: raise ValueError('LaTeX input unavailable')
        name = 'main.tex' if index == 0 and item['relative_name'] == 'main.tex' else safe_relative(item['relative_name'])
        if name in names or any(name.startswith(previous+'/') or previous.startswith(name+'/') for previous in names) or (index != 0 and name == 'main.tex'): raise ValueError('LaTeX input collision')
        names.add(name); digest=_text(item['sha256'],64)
        if not _HASH.fullmatch(digest): raise ValueError('LaTeX input hash unavailable')
        output.append({'ref':_text(item['ref'],16384),'resourceID':_text(item['resourceID'],256),
                       'revision':revision(item['revision']),'sha256':digest,'relative_name':name})
    if output[0]['relative_name'] != 'main.tex' or [item['sha256'] for item in output] != hashes: raise ValueError('LaTeX input identity changed')
    compiler=_text(binding['compiler_sha256'],64)
    if not _HASH.fullmatch(compiler): raise ValueError('LaTeX compiler identity unavailable')
    name=_text(binding['destination_name'],240)
    if not name.lower().endswith('.pdf') or name in {'.','..'} or any(c in name for c in ('/','\\')): raise ValueError('LaTeX destination name unavailable')
    return {'interface':ADAPTER,'ownerAccountID':account,'inputs':output,'destination_ref':_text(binding['destination_ref'],16384),
            'destination_revision':revision(binding['destination_revision']),'destination_name':name,'compiler_sha256':compiler}
