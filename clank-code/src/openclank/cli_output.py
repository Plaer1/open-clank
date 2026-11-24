"""Bounded, terminal-safe output for a one-shot host run."""
import json
import re

_LIMIT = 2 * 1024 * 1024
_ESCAPE = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|.)')


def clean_text(value):
    value = _ESCAPE.sub('', value)
    return ''.join(c for c in value if c in '\n\t' or ord(c) >= 32 and not 127 <= ord(c) <= 159)


def bounded_chunk(value, used):
    available = max(0, _LIMIT - used)
    raw = value.encode('utf-8')
    accepted = raw[:available].decode('utf-8', 'ignore')
    return accepted, used + len(accepted.encode('utf-8')), len(raw) > available


def json_line(stream, value):
    stream.write(json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n')
    stream.flush()
