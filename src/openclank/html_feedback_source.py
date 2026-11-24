"""Closed source-only HTML node mapping for revision-bound O15 feedback."""
import hashlib
import re
from html.parser import HTMLParser

_TAGS = {'html','head','body','title','main','section','div','h1','h2','h3','p','span','button','ul','ol','li','strong','em'}
_TAG = re.compile(r'<([a-zA-Z][a-zA-Z0-9]*)(?:\s+(?:id|class|style)\s*=\s*(?:"[^"<>&]*"|\'[^\'<>&]*\'))*\s*>', re.I)
_INLINE = {'span','strong','em'}
_BLOCK = {'main','section','div','h1','h2','h3','p','ul','ol'}
_CHILDREN = {'html':{'head','body'},'head':{'title'},'title':set(), 'body':_BLOCK | _INLINE, 'main':_BLOCK | _INLINE, 'section':_BLOCK | _INLINE, 'div':_BLOCK | _INLINE, 'li':_BLOCK | _INLINE, 'ul':{'li'}, 'ol':{'li'}, 'p':_INLINE | {'button'}, 'h1':_INLINE, 'h2':_INLINE, 'h3':_INLINE, 'span':_INLINE, 'strong':_INLINE, 'em':_INLINE, 'button':_INLINE}
_LOCATOR = re.compile(r'v1:(0|[1-9][0-9]*):(0|[1-9][0-9]*):([0-9a-f]{64})')


def verify_html_feedback_node(data, node_id):
    if not isinstance(data, bytes) or not 0 < len(data) <= 262144 or b'\r' in data or data.startswith(b'\xef\xbb\xbf'):
        raise ValueError('HTML source mapping unavailable')
    try:
        text = data.decode('utf-8', 'strict')
    except UnicodeDecodeError as exc:
        raise ValueError('HTML source mapping unavailable') from exc
    match = _LOCATOR.fullmatch(node_id) if isinstance(node_id, str) else None
    if not match: raise ValueError('HTML node mapping unavailable')
    start, end, expected = int(match[1]), int(match[2]), match[3]
    if not 0 <= start < end <= len(data) or hashlib.sha256(data[start:end]).hexdigest() != expected:
        raise ValueError('HTML node mapping changed')
    lines = text.splitlines(keepends=True); starts = [0]
    for line in lines: starts.append(starts[-1] + len(line))
    byte_offsets=[0]
    for ch in text: byte_offsets.append(byte_offsets[-1]+len(ch.encode('utf-8')))
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False); self.stack=[]; self.matches=0; self.nodes=0; self.roots=0; self.html_children=[]; self.declared=False
        def handle_starttag(self, tag, attrs):
            raw=self.get_starttag_text(); self.nodes+=1
            if tag not in _TAGS or self.nodes>8192 or len(self.stack)>=64 or not _TAG.fullmatch(raw) or len({x[0] for x in attrs})!=len(attrs): raise ValueError('HTML source mapping unavailable')
            if not self.stack:
                self.roots+=1
                if tag!='html': raise ValueError('HTML source mapping unavailable')
            elif tag not in _CHILDREN[self.stack[-1]]: raise ValueError('HTML source mapping unavailable')
            if self.stack and self.stack[-1]=='html':
                self.html_children.append(tag)
                if self.html_children not in (['head'],['body'],['head','body']): raise ValueError('HTML source mapping unavailable')
            line,col=self.getpos(); position=starts[line-1]+col; prefix=byte_offsets[position]; raw_bytes=raw.encode('utf-8')
            if prefix==start and prefix+len(raw_bytes)==end and raw_bytes==data[start:end]: self.matches+=1
            self.stack.append(tag)
        def handle_endtag(self,tag):
            line,col=self.getpos(); position=starts[line-1]+col; end=text.find('>',position)
            if end<0 or not re.fullmatch(r'</\s*'+re.escape(tag)+r'\s*>',text[position:end+1],re.I) or not self.stack or self.stack.pop()!=tag: raise ValueError('HTML source mapping unavailable')
        def handle_startendtag(self,*_): raise ValueError('HTML source mapping unavailable')
        def handle_data(self,value):
            if '<' in value or '&' in value or (not self.stack and value.strip()): raise ValueError('HTML source mapping unavailable')
        def handle_entityref(self,*_): raise ValueError('HTML source mapping unavailable')
        def handle_charref(self,*_): raise ValueError('HTML source mapping unavailable')
        def handle_comment(self,*_): raise ValueError('HTML source mapping unavailable')
        def handle_decl(self,value):
            if value.lower()!='doctype html' or self.nodes or self.declared: raise ValueError('HTML source mapping unavailable')
            self.declared=True
        def handle_pi(self,*_): raise ValueError('HTML source mapping unavailable')
        def unknown_decl(self,*_): raise ValueError('HTML source mapping unavailable')
    parser=Parser(); parser.feed(text); parser.close()
    if parser.stack or parser.roots!=1 or 'body' not in parser.html_children or parser.matches!=1: raise ValueError('HTML source mapping unavailable')
    return (start,end)
