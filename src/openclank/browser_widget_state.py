"""Extract an optional, inert widget declaration from leased source bytes."""
import hashlib
import json
import math
import re
from html.parser import HTMLParser

_ID = re.compile(r'^[A-Za-z][A-Za-z0-9_.:-]{0,63}$')


def declaration(html):
    try:
        source = html.decode('utf-8', 'strict')
        class Scanner(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)
                self.found = 0; self.inside = False; self.parts = []; self.invalid = False
            def handle_starttag(self, tag, attrs):
                if self.inside:
                    self.invalid = True; return
                if tag != 'script': return
                ids = [v for k, v in attrs if k == 'id']
                if 'openclank-widget-state' not in ids: return
                self.found += 1
                if len(attrs) != 2 or len(ids) != 1 or sorted(k for k, _ in attrs) != ['id', 'type'] or dict(attrs).get('type') != 'application/json': self.invalid = True
                self.inside = True
            def handle_endtag(self, tag):
                if self.inside:
                    if tag != 'script': self.invalid = True
                    self.inside = False
            def handle_data(self, data):
                if self.inside: self.parts.append(data)
        scan = Scanner(); scan.feed(source); scan.close()
        if scan.invalid or scan.inside or scan.found != 1: return None
        raw = ''.join(scan.parts).encode('utf-8')
        if not raw or len(raw) > 16384: return None
        def pairs(items):
            out = {}
            for key, value in items:
                if key in out: raise ValueError('duplicate key')
                out[key] = value
            return out
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
        if type(value) is not dict or set(value) != {'version','widgetId','fields'} or (type(value['version']) is not int or value['version'] != 1) or not isinstance(value['widgetId'], str) or not _ID.fullmatch(value['widgetId']): return None
        fields = value['fields']
        if type(fields) is not list or not 1 <= len(fields) <= 32: return None
        seen = set()
        for f in fields:
            if type(f) is not dict or not isinstance(f.get('id'), str) or not _ID.fullmatch(f['id']) or f['id'] in seen: return None
            seen.add(f['id'])
            if not isinstance(f.get('label'), str) or not 1 <= len(f['label']) <= 128 or any(ord(x) < 32 for x in f['label']): return None
            kind = f.get('type')
            if kind == 'boolean': valid = set(f) == {'id','label','type'}
            elif kind == 'number':
                valid = set(f) == {'id','label','type','min','max'} and all(type(f.get(k)) in (int,float) and math.isfinite(f[k]) and abs(f[k]) <= 1e9 for k in ('min','max')) and f['min'] <= f['max']
            elif kind == 'text': valid = set(f) == {'id','label','type','maxLength'} and type(f['maxLength']) is int and 1 <= f['maxLength'] <= 1024
            elif kind == 'enum':
                choices = f.get('choices'); valid = set(f) == {'id','label','type','choices'} and type(choices) is list and 1 <= len(choices) <= 32 and all(isinstance(x,str) and 1 <= len(x) <= 128 for x in choices) and len(set(choices)) == len(choices)
            else: valid = False
            if not valid: return None
        return {'schema':value, 'declaration_digest':'sha256:' + hashlib.sha256(raw).hexdigest(), 'source_digest':'sha256:' + hashlib.sha256(html).hexdigest()}
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
