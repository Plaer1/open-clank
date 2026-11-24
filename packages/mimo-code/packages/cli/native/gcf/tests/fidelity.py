"""Independent generic-wire reader and bounded safe-JSON codec checks.

Grammar fixtures are adapted from gcf-go v1.7.1 (MIT); see ../NOTICE.
This test never imports or executes the study checkout or a Go implementation.
Run with the path to the std-only driver built from tests/driver.rs.
"""
import json, re, subprocess, sys

DRIVER = sys.argv[1]
def run(data):
    result = subprocess.run([DRIVER], input=data, stdout=subprocess.PIPE, check=True).stdout
    head, _, body = result.partition(b'\n')
    return head.decode(), body.decode('utf-8')
def scalar(s):
    if s == '-': return None
    if s == 'true': return True
    if s == 'false': return False
    if s.startswith('"'): return json.loads(s)
    return int(s)
def split(s, delimiter):
    result=[]; start=0; quoted=False; escaped=False
    for i,c in enumerate(s):
        if escaped: escaped=False; continue
        if quoted and c=='\\': escaped=True; continue
        if c=='"': quoted=not quoted
        elif c==delimiter and not quoted: result.append(s[start:i]);start=i+1
    assert not quoted
    result.append(s[start:]);return result
def key(s):
    return json.loads(s) if s.startswith('"') else s

def read_wire(wire):
    lines=wire.split('\n');assert lines.pop()=='';assert lines.pop(0)=='GCF profile=generic'
    def arr(header, depth, idx):
        match=re.fullmatch(r'\[(\d+)\](?:\{(.*)\}|: (.*))?',header);assert match, header
        n=int(match[1]); fields=match[2]; inline=match[3]
        if inline is not None: return [scalar(x) for x in split(inline,',')],idx
        if fields is not None:
            names=[key(x) for x in split(fields,',')];rows=[]
            for _ in range(n):
                line=lines[idx];idx+=1; assert line.startswith('  '*depth)
                cells=split(line[depth*2:],'|');assert len(cells)==len(names)
                rows.append({k:scalar(v) for k,v in zip(names,cells) if v!='~'})
            return rows,idx
        rows=[]
        for i in range(n):
            line=lines[idx];idx+=1;prefix='  '*depth+f'@{i} ';assert line.startswith(prefix),line
            item=line[len(prefix):]
            if item=='{}': value,idx=obj(depth+1,idx)
            elif item.startswith('['): value,idx=arr(item,depth+1,idx)
            else: assert item.startswith('=');value=scalar(item[1:])
            rows.append(value)
        return rows,idx
    def obj(depth,idx):
        result={};prefix='  '*depth
        while idx<len(lines):
            line=lines[idx]
            if not line.startswith(prefix) or (depth and line.startswith(prefix+'  ')):break
            content=line[len(prefix):]
            if content.startswith('@'):break
            idx+=1
            if content.startswith('## '):
                body=content[3:]
                # Keys are either bare identifiers or quoted JSON strings.
                if body.startswith('"'):
                    k,end=json.JSONDecoder().raw_decode(body);rest=body[end:]
                else:
                    match=re.match(r'[A-Za-z_][A-Za-z_0-9]*',body);assert match,body
                    k=match[0];rest=body[match.end():]
                if rest.startswith(' ['): value,idx=arr(rest[1:],depth,idx)
                else: assert rest=='';value,idx=obj(depth+1,idx)
            else:
                if content.startswith('"'):
                    k,end=json.JSONDecoder().raw_decode(content);rest=content[end:]
                else:
                    match=re.match(r'[A-Za-z_][A-Za-z_0-9]*',content);assert match,content
                    k=match[0];rest=content[match.end():]
                if rest.startswith('='):value=scalar(rest[1:])
                else: value,idx=arr(rest,depth,idx)
            assert k not in result;result[k]=value
        return result,idx
    if not lines:return {}
    if lines[0].startswith('='):value=scalar(lines[0][1:]);idx=1
    elif lines[0].startswith('## ['):value,idx=arr(lines[0][3:],0,1)
    else:value,idx=obj(0,0)
    assert idx==len(lines),(idx,lines[idx:]);return value

def emit(value):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode();state,wire=run(raw)
    assert state=='encoded',(state,len(raw));decoded=read_wire(wire);assert decoded==value
    assert json.dumps(decoded,ensure_ascii=False,separators=(',',':')) == json.dumps(value,ensure_ascii=False,separators=(',',':'))
    assert len(wire.encode()) < len(raw);assert run(raw)==(state,wire)
    return wire

rows=[{'long_identifier':i,'label':'item','optional':None} if i%2 else {'long_identifier':i,'label':'item'} for i in range(30)]
value={'records':rows,'nested':{'empty':{},'values':[[],[1,2],{'x':'~','newline':'a\nb','😀':'😀'},True,None]},'different_order':[{'a':1,'b':2},{'b':3,'a':4}],'a>b':'literal','unicode':'\u2028','integer_bounds':[-9007199254740991,9007199254740991]}
wire=emit(value); assert 'long_identifier,optional,label' in wire or 'long_identifier,label,optional' in wire
assert emit(rows).startswith('GCF profile=generic\n## [30]{long_identifier,label,optional}\n')
for bad,reason in [('{"x":1,"x":2}','DuplicateKey'),('{"n":-0}','UnsupportedNumber'),('{"n":1.0}','UnsupportedNumber'),('{"n":1e0}','UnsupportedNumber'),('{"n":9007199254740992}','UnsupportedNumber'),('{"n":-9007199254740992}','UnsupportedNumber'),('{"x":"\\ud800"}','Invalid')]:
    assert run((bad+' '*100).encode())[0]=='bypass:'+reason,bad
assert run(b' ' * (50*1024+1))[0]=='bypass:TooLarge'
assert run(b'['+b'0,'*16383+b'0]')[0]=='bypass:TooManyValues'
assert run(json.dumps([{'long>key':0}]*3500,separators=(',',':')).encode())[0]=='bypass:TooLarge'
assert run(b'{"x":"\xff"}'+b' '*100)[0]=='bypass:Invalid'
assert run(('['*33+'0'+']'*33+' '*100).encode())[0]=='bypass:TooDeep'
assert run(b'{"a":1}')[0]=='bypass:TooSmall'
assert run((json.dumps({'x':'z'*200})).encode())[0]=='bypass:NotSmaller'
assert run((json.dumps({'x':1})+' '*100).encode())[0]=='bypass:NotSmaller'
print('PASS std-only generic v3 wire semantic fidelity, bounded rejection and deterministic fallback')
