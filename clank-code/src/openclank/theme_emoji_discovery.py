"""Bounded, inert source validation for the local emoji acquisition service.

No network or storage is opened at import. Publication and inventory ownership
remain disabled until the consent-fenced overlay service is connected.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import io
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.etree import ElementTree

async def cancellable_parse(function, *args, **kwargs):
    """Drain parser-owned resources even if cancellation is repeated."""
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(function, *args, cancel=cancel, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
            except Exception: break
        if not task.cancelled(): task.exception()
        raise

def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise ValueError('Emoji metadata parsing cancelled')

def _decode_json(data, *, cancel=None):
    import json
    _check_cancel(cancel)
    value = json.loads(data)
    _check_cancel(cancel)
    return value

MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024
NOTO_REVISION = 'e20cbc2bbec1926686be9f9bee7d1d2cfa1fea0e'
_ALLOWED_HOSTS = frozenset({'raw.githubusercontent.com', 'www.gstatic.com'})
_SAFE_ID = re.compile(r'^[a-zA-Z0-9_:-]{1,160}$')

class SourceHTTPError(ValueError):
    def __init__(self, status: int, retry_after: str | None = None):
        import math
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        self.status = status
        delay = 1200  # Local fallback when the server supplied no valid hint.
        if isinstance(retry_after, str) and len(retry_after) <= 128:
            try:
                delay = int(retry_after) if retry_after.isdigit() else math.ceil((parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
        # A longer valid hint pauses automatic updates instead of retrying early.
        self.retry_after = None if delay > 10 * 365 * 86400 else max(60, delay)
        super().__init__(f'Emoji source returned HTTP {status}')


@dataclass(frozen=True)
class Candidate:
    identity: str
    source: str
    path: str
    mime: str
    revision: str
    git_blob: str | None = None
    source_bytes: int | None = None
    render_id: str | None = None

    def url(self) -> str:
        if (not _SAFE_ID.fullmatch(self.identity) or self.mime not in {'image/png', 'image/svg+xml'}
                or not self.path.endswith('.png' if self.mime == 'image/png' else '.svg')):
            raise ValueError('Invalid emoji candidate')
        if self.render_id is not None and not re.fullmatch(r'[gk]:[0-9a-f]+(?:-[0-9a-f]+)*(?:_[0-9a-f]+(?:-[0-9a-f]+)*)?', self.render_id):
            raise ValueError('Invalid render identity')
        if (self.git_blob is not None and not re.fullmatch(r'[0-9a-f]{40}', self.git_blob)) or (self.source_bytes is not None and (type(self.source_bytes) is not int or not 0 < self.source_bytes <= 9007199254740991)):
            raise ValueError('Invalid source proof')
        if not self.path or len(self.path) > 512 or any(part in {'', '.', '..'} for part in self.path.split('/')):
            raise ValueError('Invalid emoji path')
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', self.path):
            raise ValueError('Invalid emoji path')
        if self.source == 'noto' and re.fullmatch(r'[0-9a-f]{40}', self.revision) and self.path.startswith(('2D/svg/', '2D/png/128/', 'third_party/region-flags/waved-svg/', 'third_party/region-flags/png/')):
            return f'https://raw.githubusercontent.com/googlefonts/noto-emoji/{self.revision}/{self.path}'
        if (self.source == 'kitchen' and re.fullmatch(r'[0-9a-f]{40}', self.revision)
                and re.fullmatch(r'[0-9]{8}/u[0-9a-fu-]+/u[0-9a-fu-]+_u[0-9a-fu-]+\.png', self.path)):
            return f'https://www.gstatic.com/android/keyboard/emojikitchen/{self.path}'
        raise ValueError('Unsupported emoji source')


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in _ALLOWED_HOSTS or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment or '%' in parsed.path):
        raise ValueError('Unsupported emoji URL')


def validate_original(candidate: Candidate, data: bytes) -> str:
    """Validate original bytes and return their digest without rewriting them."""
    validate_url(candidate.url())
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_ASSET_BYTES:
        raise ValueError('Emoji asset exceeds bounds')
    if candidate.source_bytes is not None and len(data) != candidate.source_bytes:
        raise ValueError('Emoji source size changed')
    if candidate.git_blob is not None:
        git_hash = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if git_hash != candidate.git_blob:
            raise ValueError('Emoji source digest changed')
    if candidate.mime == 'image/png':
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'PNG' or image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_PIXELS:
                raise ValueError('Invalid PNG dimensions')
            image.verify()
    else:
        try:
            data.decode('utf-8-sig')
        except UnicodeError as exc:
            raise ValueError('SVG must be UTF-8') from exc
        if b'\0' in data:
            raise ValueError('Invalid SVG encoding')
        # Permit a normal leading XML declaration and comments, but never
        # permit DTDs, entities, CDATA or any processing instruction. Original
        # bytes remain unchanged; this copy is only for declaration screening.
        screened = re.sub(rb'<!--(?:[^-]|-(?!-))*-->', b'', data)
        screened = re.sub(rb'^(?:\xef\xbb\xbf)?\s*<\?xml\s+[^?]*\?>', b'', screened, count=1)
        if b'<!' in screened or b'<?' in screened:
            raise ValueError('Unsupported SVG declaration')
        try:
            root = ElementTree.fromstring(data)
        except (ElementTree.ParseError, LookupError, UnicodeError) as exc:
            raise ValueError('Invalid SVG') from exc
        namespace = '{http://www.w3.org/2000/svg}'
        allowed_tags = {'svg', 'g', 'path', 'circle', 'ellipse', 'rect', 'line',
                        'polyline', 'polygon', 'defs', 'linearGradient', 'radialGradient',
                        'stop', 'clipPath', 'mask'}
        allowed_attributes = {'id', 'viewBox', 'width', 'height', 'x', 'y', 'x1', 'y1',
                              'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'd', 'points',
                              'fill', 'fill-rule', 'fill-opacity', 'stroke', 'stroke-width',
                              'stroke-linecap', 'stroke-linejoin', 'stroke-miterlimit',
                              'stroke-opacity', 'opacity', 'transform', 'gradientUnits',
                              'gradientTransform', 'offset', 'stop-color', 'stop-opacity',
                              'clip-path', 'clip-rule', 'mask', 'preserveAspectRatio'}
        if root.tag != namespace + 'svg':
            raise ValueError('Invalid SVG root')
        box = root.attrib.get('viewBox', '').replace(',', ' ').split()
        try:
            dimensions = [float(value) for value in box]
        except ValueError as exc:
            raise ValueError('Invalid SVG dimensions') from exc
        if len(dimensions) != 4 or not all(__import__('math').isfinite(x) for x in dimensions) or not (0 < dimensions[2] <= 4096 and 0 < dimensions[3] <= 4096 and dimensions[2] * dimensions[3] <= MAX_PIXELS):
            raise ValueError('Invalid SVG dimensions')
        for dimension in ('width', 'height'):
            if dimension in root.attrib:
                value = root.attrib[dimension]
                if not re.fullmatch(r'(?:[0-9]+(?:\.[0-9]+)?)(?:px)?', value):
                    raise ValueError('Invalid SVG intrinsic dimensions')
                if not 0 < float(value.removesuffix('px')) <= 4096:
                    raise ValueError('Invalid SVG intrinsic dimensions')
        elements = list(root.iter())
        if len(elements) > 10000:
            raise ValueError('SVG exceeds element limit')
        for node in elements:
            if not node.tag.startswith(namespace) or node.tag[len(namespace):] not in allowed_tags:
                raise ValueError('Unsupported SVG element')
            if (node.text and node.text.strip()) or (node.tail and node.tail.strip()):
                raise ValueError('Unsupported SVG text')
            for key, value in node.attrib.items():
                if key not in allowed_attributes or len(value) > 100000 or any(ord(c) < 32 and c not in '\t\n\r' for c in value):
                    raise ValueError('Unsupported SVG attribute')
                if re.search(r'url\s*\(', value, re.I) and not re.fullmatch(r'url\(#[A-Za-z_][A-Za-z0-9_.-]*\)', value):
                    raise ValueError('External SVG reference')
                if any(token in value.lower() for token in ('javascript:', 'data:', 'http:', 'https:', '@import', 'expression(')):
                    raise ValueError('Unsupported SVG reference')
    return hashlib.sha256(data).hexdigest()

async def fetch_candidate(candidate: Candidate, client) -> tuple[bytes, str]:
    """Fetch one fixed candidate with an injected no-redirect client.

    The caller owns consent, rate limits, cancellation and publication. This
    primitive never creates a client or writes a file.
    """
    url = candidate.url()
    validate_url(url)
    if candidate.source_bytes is not None and candidate.source_bytes > MAX_ASSET_BYTES:
        raise ValueError('Emoji asset exceeds bounds')
    async with client.stream('GET', url, follow_redirects=False) as response:
        if response.status_code != 200:
            raise SourceHTTPError(response.status_code, response.headers.get('retry-after'))
        length = response.headers.get('content-length')
        if length is not None:
            try:
                if int(length) < 0 or int(length) > MAX_ASSET_BYTES:
                    raise ValueError('Emoji asset exceeds bounds')
            except (TypeError, OverflowError) as exc:
                raise ValueError('Invalid emoji length') from exc
        data = bytearray()
        async for part in response.aiter_bytes():
            data.extend(part)
            if len(data) > MAX_ASSET_BYTES:
                raise ValueError('Emoji asset exceeds bounds')
        original = bytes(data)
    return original, validate_original(candidate, original)


def noto_candidates(tree: list[dict], revision: str = NOTO_REVISION, *, cancel=None) -> list[Candidate]:
    """Convert a bounded pinned tree response into expected original assets.

    The caller must independently verify the response is complete and for the
    pinned revision; a truncated or missing response is never retirement proof.
    """
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not isinstance(tree, list) or len(tree) > 100000:
        raise ValueError('Invalid Noto inventory')
    candidates = []
    seen = set()
    for entry in tree:
        _check_cancel(cancel)
        if not isinstance(entry, dict) or entry.get('type') != 'blob':
            continue
        path = entry.get('path')
        if not isinstance(path, str):
            raise ValueError('Invalid Noto path')
        if not (path.startswith(('2D/svg/', '2D/png/128/', 'third_party/region-flags/waved-svg/', 'third_party/region-flags/png/'))
                and path.endswith(('.svg', '.png'))):
            continue
        if entry.get('mode') != '100644':
            continue
        stem = path.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        codepoints = re.findall(r'(?i)(?:^|[_-])u?([0-9a-f]{4,6})(?=$|[_-])', stem)
        if any(0x1F3FB <= int(code, 16) <= 0x1F3FF for code in codepoints):
            continue
        identity = 'noto:' + hashlib.sha256(path.encode()).hexdigest()
        if identity in seen:
            raise ValueError('Duplicate Noto source path')
        seen.add(identity)
        render_id = None
        if stem.startswith('emoji_u'):
            parts = stem[7:].split('_')
        elif re.fullmatch(r'u[0-9a-f]+(?:-u[0-9a-f]+)*', stem):
            parts = [part[1:] for part in stem.split('-')]
        elif path.startswith('third_party/region-flags/png/') and re.fullmatch(r'[A-Z]{2}', stem):
            parts = [format(0x1f1e6 + ord(ch) - ord('A'), 'x') for ch in stem]
        else:
            parts = []
        if parts and all(re.fullmatch(r'[0-9a-f]+', part) for part in parts):
            render_id = 'g:' + '-'.join(format(int(part,16),'x') for part in parts if int(part,16) != 0xfe0f)
        candidate = Candidate(identity, 'noto', path,
                              'image/svg+xml' if path.endswith('.svg') else 'image/png',
                              revision, entry.get('sha'), entry.get('size'), render_id)
        if candidate.git_blob is None or candidate.source_bytes is None:
            raise ValueError('Missing Noto source proof')
        candidate.url()
        candidates.append(candidate)
    return candidates

KITCHEN_REVISION = '062ffca53643d36946336cdc2c7655e2576f0a42'

def kitchen_candidates(lines):
    """Yield expected pinned Kitchen entries; absence never retires old entries."""
    seen = set()
    for line in lines:
        if not isinstance(line, str) or len(line) > 1024:
            raise ValueError('Invalid Kitchen inventory row')
        if not line.strip():
            continue
        parts = line.rstrip('\r\n').split('\t')
        if len(parts) != 3:
            raise ValueError('Invalid Kitchen inventory row')
        identity, date, path = parts
        match = re.fullmatch(r'k:([0-9a-f]+(?:-[0-9a-f]+)*)_([0-9a-f]+(?:-[0-9a-f]+)*)', identity)
        if not match or not re.fullmatch(r'[0-9]{8}', date) or not path.startswith(date + '/'):
            raise ValueError('Invalid Kitchen identity')
        codes = [int(code, 16) for side in match.groups() for code in side.split('-')]
        if any(code > 0x10ffff or 0x1f3fb <= code <= 0x1f3ff for code in codes):
            raise ValueError('Unsupported Kitchen identity')
        if identity in seen:
            raise ValueError('Duplicate Kitchen identity')
        seen.add(identity)
        candidate = Candidate(identity, 'kitchen', path, 'image/png', KITCHEN_REVISION, render_id=identity)
        candidate.url()
        yield candidate

async def read_noto_tree(client, *, latest: bool = False):
    """Read a complete pinned GitHub tree with bounded, no-redirect requests."""
    import json
    async def get_json(url):
        async with client.stream('GET', url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise SourceHTTPError(response.status_code, response.headers.get('retry-after'))
            body = bytearray()
            async for part in response.aiter_bytes():
                body.extend(part)
                if len(body) > 32 * 1024 * 1024:
                    raise ValueError('Noto metadata exceeds bounds')
        try:
            return await cancellable_parse(_decode_json, body)
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Invalid Noto metadata') from exc
    base = 'https://api.github.com/repos/googlefonts/noto-emoji/git'
    commit = await get_json(f'{base}/commits/{"main" if latest else NOTO_REVISION}')
    revision = commit.get('sha') if isinstance(commit, dict) else None
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision) or (not latest and revision != NOTO_REVISION):
        raise ValueError('Noto revision changed')
    tree_id = (commit.get('tree') or {}).get('sha')
    if not isinstance(tree_id, str) or not re.fullmatch(r'[0-9a-f]{40}', tree_id):
        raise ValueError('Invalid Noto tree identity')
    tree = await get_json(f'{base}/trees/{tree_id}?recursive=1')
    if not isinstance(tree, dict) or tree.get('sha') != tree_id or tree.get('truncated') is not False:
        raise ValueError('Noto tree is incomplete')
    return await cancellable_parse(noto_candidates, tree.get('tree'), revision)

class _BoundedJSONReader:
    """Incrementally decode one bounded JSON value at a time from text."""
    def __init__(self, stream, cancel=None):
        import json
        self.cancel = cancel
        self.stream, self.decoder = stream, json.JSONDecoder()
        self.buffer, self.offset, self.eof = '', 0, False

    def _fill(self):
        _check_cancel(self.cancel)
        if self.eof:
            return False
        self.buffer = self.buffer[self.offset:]
        self.offset = 0
        chunk = self.stream.read(65536)
        self.eof = not chunk
        self.buffer += chunk
        if len(self.buffer) > 4 * 1024 * 1024:
            raise ValueError('Kitchen metadata item exceeds bounds')
        return not self.eof

    def peek(self):
        while True:
            while self.offset < len(self.buffer) and self.buffer[self.offset].isspace():
                self.offset += 1
            if self.offset < len(self.buffer):
                return self.buffer[self.offset]
            if not self._fill():
                return ''

    def expect(self, token):
        if self.peek() != token:
            raise ValueError('Invalid Kitchen metadata structure')
        self.offset += 1

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.offset)
                self.offset = end
                return value
            except ValueError as exc:
                if not self._fill():
                    raise ValueError('Invalid Kitchen metadata value') from exc


def kitchen_metadata_candidates(stream, revision: str, *, cancel=None) -> list[Candidate]:
    """Parse complete metadata without materializing its large root object."""
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Invalid Kitchen metadata revision')
    reader = _BoundedJSONReader(stream, cancel)
    selected = {}
    root_keys = set()
    reader.expect('{')
    while reader.peek() != '}':
        if root_keys:
            reader.expect(',')
        key = reader.value()
        if not isinstance(key, str) or key in root_keys:
            raise ValueError('Invalid Kitchen metadata key')
        root_keys.add(key)
        reader.expect(':')
        if key != 'data':
            reader.value()
            continue
        reader.expect('{')
        count = 0
        while reader.peek() != '}':
            if count:
                reader.expect(',')
            _check_cancel(cancel)
            source_key = reader.value(); reader.expect(':'); entry = reader.value(); count += 1
            if not isinstance(source_key, str) or not isinstance(entry, dict) or count > 10000:
                raise ValueError('Invalid Kitchen metadata entry')
            combinations = entry.get('combinations')
            if not isinstance(combinations, dict) or len(combinations) > 10000:
                raise ValueError('Invalid Kitchen combinations')
            for variants in combinations.values():
                _check_cancel(cancel)
                if not isinstance(variants, list) or len(variants) > 100:
                    raise ValueError('Invalid Kitchen variants')
                for item in variants:
                    if not isinstance(item, dict) or item.get('isLatest') is not True:
                        continue
                    pair = [item.get('leftEmojiCodepoint'), item.get('rightEmojiCodepoint')]
                    if not all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]+(?:-[0-9a-f]+)*', v) for v in pair):
                        raise ValueError('Invalid Kitchen codepoints')
                    values = [[int(part, 16) for part in v.split('-')] for v in pair]
                    if any(n > 0x10ffff or 0x1f3fb <= n <= 0x1f3ff for side in values for n in side):
                        continue
                    normalized = ['-'.join(format(n, 'x') for n in side if n != 0xfe0f) for side in values]
                    if not all(normalized):
                        raise ValueError('Invalid Kitchen codepoints')
                    identity = 'k:' + '_'.join(sorted(normalized))
                    source = item.get('gStaticUrl')
                    if not isinstance(source, str):
                        raise ValueError('Missing Kitchen source')
                    validate_url(source)
                    prefix = 'https://www.gstatic.com/android/keyboard/emojikitchen/'
                    if not source.startswith(prefix):
                        raise ValueError('Invalid Kitchen source')
                    path = source[len(prefix):]
                    candidate = Candidate(identity, 'kitchen', path, 'image/png', revision, render_id=identity)
                    candidate.url()
                    prior = selected.get(identity)
                    if prior is None or candidate.path > prior.path:
                        selected[identity] = candidate
                    if len(selected) > 250000:
                        raise ValueError('Kitchen inventory exceeds bounds')
        reader.expect('}')
    reader.expect('}')
    if 'data' not in root_keys or reader.peek():
        raise ValueError('Incomplete Kitchen metadata')
    return list(selected.values())

async def read_kitchen_metadata(client) -> list[Candidate]:
    """Read the latest complete Kitchen metadata with Git blob proof."""
    import json
    import tempfile
    async def small_json(url):
        async with client.stream('GET', url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise SourceHTTPError(response.status_code, response.headers.get('retry-after'))
            body = bytearray()
            async for part in response.aiter_bytes():
                body.extend(part)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError('Kitchen metadata index exceeds bounds')
        try:
            return await cancellable_parse(_decode_json, body)
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Invalid Kitchen metadata index') from exc
    base = 'https://api.github.com/repos/xsalazar/emoji-kitchen-backend/git'
    commit = await small_json(base + '/commits/main')
    revision = commit.get('sha') if isinstance(commit, dict) else None
    tree_id = (commit.get('tree') or {}).get('sha') if isinstance(commit, dict) else None
    if not all(isinstance(x, str) and re.fullmatch(r'[0-9a-f]{40}', x) for x in (revision, tree_id)):
        raise ValueError('Invalid Kitchen metadata revision')
    tree = await small_json(f'{base}/trees/{tree_id}?recursive=1')
    if not isinstance(tree, dict) or tree.get('sha') != tree_id or tree.get('truncated') is not False or not isinstance(tree.get('tree'), list):
        raise ValueError('Incomplete Kitchen metadata index')
    matches = [row for row in tree['tree'] if isinstance(row, dict) and row.get('path') == 'app/metadata.json']
    if len(matches) != 1:
        raise ValueError('Kitchen metadata identity unavailable')
    row = matches[0]; size = row.get('size'); oid = row.get('sha')
    if (row.get('type') != 'blob' or row.get('mode') != '100644' or type(size) is not int
            or not 0 < size <= 128 * 1024 * 1024 or not isinstance(oid, str) or not re.fullmatch(r'[0-9a-f]{40}', oid)):
        raise ValueError('Invalid Kitchen metadata proof')
    import shutil
    if shutil.disk_usage(tempfile.gettempdir()).free < size + 128 * 1024 * 1024:
        raise ValueError('Kitchen metadata disk reserve unavailable')
    url = f'https://raw.githubusercontent.com/xsalazar/emoji-kitchen-backend/{revision}/app/metadata.json'
    digest = hashlib.sha1(b'blob ' + str(size).encode() + b'\0'); received = 0
    with tempfile.TemporaryFile(mode='w+b') as stream:
        async with client.stream('GET', url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise SourceHTTPError(response.status_code, response.headers.get('retry-after'))
            async for part in response.aiter_bytes():
                received += len(part)
                if received > size:
                    raise ValueError('Kitchen metadata exceeds source size')
                digest.update(part); stream.write(part)
        if received != size or digest.hexdigest() != oid:
            raise ValueError('Kitchen metadata source changed')
        stream.seek(0)
        with io.TextIOWrapper(stream, encoding='utf-8', errors='strict') as text:
            return await cancellable_parse(kitchen_metadata_candidates, text, revision)
