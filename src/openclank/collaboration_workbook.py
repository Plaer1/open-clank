"""Private bounded workbook source validation and exact token replacement.

The result is data only; the caller owns authorization, fresh CAS and History.
"""
from __future__ import annotations
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from src.openclank.collaboration_text import MAX_INSERT_BYTES

MAX_BYTES = 262144
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')

class WorkbookError(ValueError): pass
class WorkbookConflict(WorkbookError): pass

def _utf(value):
    if not isinstance(value, str): raise WorkbookError('invalid workbook text')
    try: data = value.encode('utf-8', 'strict')
    except UnicodeError as exc: raise WorkbookError('invalid workbook text') from exc
    if len(data)>MAX_BYTES or b'\x00' in data or b'\r' in data or data.startswith(b'\xef\xbb\xbf'):
        raise WorkbookError('unsupported workbook text')
    return data

def validate_cells(cells):
    if not isinstance(cells,list) or not 1<=len(cells)<=64: raise WorkbookError('invalid cells')
    seen=set(); total=0
    for cell in cells:
        if not isinstance(cell,Mapping) or set(cell)!={'sheet_id','cell_id','raw'}: raise WorkbookError('invalid cells')
        sheet, ident, raw=cell['sheet_id'],cell['cell_id'],cell['raw']
        if not isinstance(sheet,str) or not _ID.fullmatch(sheet) or not isinstance(ident,str) or not _ID.fullmatch(ident) or (sheet,ident) in seen or not isinstance(raw,str): raise WorkbookError('invalid cells')
        try: encoded=raw.encode('utf-8','strict'); units=len(raw.encode('utf-16-le','strict'))//2
        except UnicodeError as exc: raise WorkbookError('invalid cells') from exc
        if units>65536: raise WorkbookError('invalid cells')
        total+=len(json.dumps(raw,ensure_ascii=False,separators=(',',':')).encode('utf-8','strict'))
        if total>MAX_INSERT_BYTES: raise WorkbookError('invalid cells')
        seen.add((sheet,ident))

class _Parser:
    def __init__(self,text): self.s=text; self.i=0; self.spans={}
    def ws(self):
        while self.i<len(self.s) and self.s[self.i] in ' \t\n\r': self.i+=1
    def parse(self,path=(),depth=0):
        if depth>128: raise WorkbookError('workbook nesting exceeds bound')
        self.ws(); start=self.i
        if start>=len(self.s): raise WorkbookError('invalid JSON')
        c=self.s[self.i]
        if c=='"':
            try: value,end=json.decoder.scanstring(self.s,self.i+1,True)
            except (ValueError,UnicodeError) as exc: raise WorkbookError('invalid JSON string') from exc
            try: value.encode('utf-8','strict')
            except UnicodeError as exc: raise WorkbookError('invalid Unicode') from exc
            self.i=end; self.spans[path]=(start,end); return value
        if c in '{[':
            self.i+=1; self.ws(); obj={} if c=='{' else []; close='}' if c=='{' else ']'
            if self.i<len(self.s) and self.s[self.i]==close: self.i+=1; return obj
            while True:
                if c=='{':
                    self.ws()
                    if self.i>=len(self.s) or self.s[self.i]!='"': raise WorkbookError('invalid JSON object')
                    key=self.parse(path+('key',len(obj)),depth+1)
                    if key in obj: raise WorkbookError('duplicate JSON key')
                    self.ws()
                    if self.i>=len(self.s) or self.s[self.i]!=':': raise WorkbookError('invalid JSON object')
                    self.i+=1; obj[key]=self.parse(path+(key,),depth+1)
                else: obj.append(self.parse(path+(len(obj),),depth+1))
                self.ws()
                if self.i>=len(self.s): raise WorkbookError('invalid JSON')
                sep=self.s[self.i]; self.i+=1
                if sep==close: return obj
                if sep!=',': raise WorkbookError('invalid JSON')
        for literal,value in [('true',True),('false',False),('null',None)]:
            if self.s.startswith(literal,self.i): self.i+=len(literal); return value
        match=_NUMBER.match(self.s,self.i)
        if not match: raise WorkbookError('invalid JSON')
        self.i=match.end(); return _Numeric(match.group())

class _Numeric(str): pass

def _book(text):
    _utf(text); p=_Parser(text); book=p.parse(); p.ws()
    if p.i!=len(text) or not isinstance(book,dict) or book.get('schema')!='openclank.workbook' or not isinstance(book.get('version'),_Numeric): raise WorkbookError('invalid workbook')
    try: valid_version = Decimal(book['version']) == 1
    except InvalidOperation as exc: raise WorkbookError('invalid workbook version') from exc
    if not valid_version: raise WorkbookError('invalid workbook version')
    sheets=book.get('sheets'); ranges=book.get('ranges'); charts=book.get('charts')
    if not isinstance(sheets,list) or not 1<=len(sheets)<=32 or not isinstance(ranges,list) or len(ranges)>256 or not isinstance(charts,list) or len(charts)>256: raise WorkbookError('invalid workbook')
    ids=set(); positions={}; sheet_ids=set()
    def take(item):
        if not isinstance(item,dict): raise WorkbookError('invalid workbook identity')
        ident=item.get('id')
        if not isinstance(ident,str) or isinstance(ident,_Numeric) or not _ID.fullmatch(ident) or ident in ids: raise WorkbookError('invalid workbook identity')
        ids.add(ident); return ident
    for si,sheet in enumerate(sheets):
        sid=take(sheet); sheet_ids.add(sid); name=sheet.get('name'); rows=sheet.get('rows')
        if not isinstance(name,str) or isinstance(name,_Numeric) or not name.strip() or not isinstance(rows,list) or not 1<=len(rows)<=500 or not isinstance(rows[0],list) or not 1<=len(rows[0])<=100: raise WorkbookError('invalid workbook sheet')
        width=len(rows[0])
        for ri,row in enumerate(rows):
            if not isinstance(row,list) or len(row)!=width: raise WorkbookError('invalid workbook rows')
            for ci,cell in enumerate(row):
                cid=take(cell); raw=cell.get('raw')
                if not isinstance(raw,str) or isinstance(raw,_Numeric) or len(raw.encode('utf-16-le'))//2>65536: raise WorkbookError('invalid workbook cell')
                positions[cid]=(sid,si,ri,ci,raw,p.spans[('sheets',si,'rows',ri,ci,'raw')])
                if len(positions)>10000: raise WorkbookError('too many cells')
    rids=set()
    for r in ranges:
        rid=take(r); start_id=r.get('startCellId'); end_id=r.get('endCellId'); start=positions.get(start_id) if isinstance(start_id,str) and not isinstance(start_id,_Numeric) else None; end=positions.get(end_id) if isinstance(end_id,str) and not isinstance(end_id,_Numeric) else None; sid=r.get('sheetId')
        if not isinstance(sid,str) or isinstance(sid,_Numeric) or sid not in sheet_ids or not start or not end or start[0]!=sid or end[0]!=sid: raise WorkbookError('invalid workbook range')
        rids.add(rid)
    for c in charts:
        take(c); title=c.get('title')
        if not isinstance(c.get('rangeId'),str) or isinstance(c.get('rangeId'),_Numeric) or c.get('rangeId') not in rids or c.get('type') not in ('bar','line') or not isinstance(title,str) or isinstance(title,_Numeric) or len(title.encode('utf-16-le'))//2>512: raise WorkbookError('invalid workbook chart')
    return positions

def validate_workbook(text):
    _book(text)


def verify_workbook_cell(text, sheet_id, cell_id):
    positions = _book(text)
    cell = positions.get(cell_id) if isinstance(cell_id, str) else None
    if not cell or cell[0] != sheet_id: raise WorkbookError('workbook cell unavailable')
    return True

def verify_workbook_range(text, sheet_id, start_cell_id, end_cell_id):
    positions = _book(text)
    for cell_id in (start_cell_id, end_cell_id):
        cell = positions.get(cell_id) if isinstance(cell_id, str) else None
        if not cell or cell[0] != sheet_id: raise WorkbookError('workbook range unavailable')
    return True


def prepare_workbook(base_text, base_sha256, current_text, cells):
    base=_utf(base_text)
    if not isinstance(base_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',base_sha256) or hashlib.sha256(base).hexdigest()!=base_sha256: raise WorkbookError('invalid base fingerprint')
    validate_cells(cells); before=_book(base_text); current=_book(current_text); changes=[]
    for item in cells:
        original=before.get(item['cell_id']); live=current.get(item['cell_id'])
        if not original or not live or original[:5]!=live[:5] or original[0]!=item['sheet_id']: raise WorkbookConflict('workbook cell changed')
        if live[4]!=item['raw']:
            changes.append((live[5],json.dumps(item['raw'],ensure_ascii=False,separators=(',',':'))))
    if not changes: raise WorkbookError('no workbook changes')
    result=current_text
    for (start,end),value in sorted(changes,reverse=True): result=result[:start]+value+result[end:]
    data=_utf(result); _book(result)
    return {'text':result,'sha256':hashlib.sha256(data).hexdigest()}
