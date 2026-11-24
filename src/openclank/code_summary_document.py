"""Render one verified completed code summary as a bounded document section."""
from __future__ import annotations

import hashlib
import html
import re


def _label(value, limit=2048):
    if type(value) is not str or not value or len(value.encode('utf-8', 'strict')) > limit:
        raise ValueError('Summary provenance unavailable')
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Summary provenance unavailable')
    # Entity-escape Markdown metacharacters as well as HTML delimiters so
    # source-controlled names cannot manufacture links or markup.
    return ''.join(f'&#{ord(c)};' if c in r'\\`*_{}[]<>()#+-.!|&' else c for c in value)


def _digest(value):
    if type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('Summary identity unavailable')
    return value


def _source(item):
    if type(item) is not dict or set(item) != {'id', 'source'} or type(item['source']) is not dict:
        raise ValueError('Summary provenance unavailable')
    source = item['source']
    if set(source) != {'file_id', 'rel_path', 'source_hash', 'span'}:
        raise ValueError('Summary provenance unavailable')
    span = source['span']
    fields = ('start_byte', 'end_byte', 'start_line', 'start_column', 'end_line', 'end_column')
    if type(span) is not dict or set(span) != set(fields) or any(type(span[k]) is not int or span[k] < 0 for k in fields):
        raise ValueError('Summary provenance unavailable')
    if span['end_byte'] < span['start_byte'] or span['start_line'] < 1 or span['end_line'] < span['start_line']:
        raise ValueError('Summary provenance unavailable')
    return (f"- ID: {_label(item['id'])}; file ID: {_label(source['file_id'])}; path: {_label(source['rel_path'])}; "
            f"indexed BLAKE3: {_digest(source['source_hash'])}; bytes: {span['start_byte']}–{span['end_byte']}; "
            f"lines/columns: {span['start_line']}:{span['start_column']}–{span['end_line']}:{span['end_column']}")


async def document_summary_section(*, repository, provider, auth_manager, owner, body, current):
    """Read the existing verified result; return content, never a document grant."""
    from src.openclank.generated_summary_result import read_completed_summary, is_verified_summary

    if await current() is not True:
        raise ValueError('Summary authority unavailable')
    result = await read_completed_summary(repository=repository, provider=provider, auth_manager=auth_manager,
                                          owner=owner, body=body, current=current)
    if await current() is not True or not is_verified_summary(result):
        raise ValueError('Summary authority unavailable')
    if type(result.text) is not bytes or not result.text or len(result.text) > 262144:
        raise ValueError('Summary text unavailable')
    text = result.text.decode('utf-8', 'strict')
    output_hash = _digest(result.output_sha256)
    if hashlib.sha256(result.text).hexdigest() != output_hash:
        raise ValueError('Summary text changed')
    operation = _label(result.operation_id, 512)
    request = _label(result.request_id, 512)
    artifact = _label(result.artifact_id, 512)
    artifact_hash, preview_hash = _digest(result.artifact_sha256), _digest(result.preview_hash)
    if type(result.sources) not in (list, tuple) or not 1 <= len(result.sources) <= 16 or type(result.omissions) not in (list, tuple) or len(result.omissions) > 128:
        raise ValueError('Summary provenance unavailable')
    sources = '\n'.join(_source(item) for item in result.sources)
    omissions = '\n'.join('- ' + _label(item, 4096) for item in result.omissions) or '- None recorded.'
    marker_hash = hashlib.sha256(('openclank.summary-section.v1\0' + result.operation_id + '\0' + output_hash).encode('utf-8')).hexdigest()
    marker = f'<!-- openclank-summary-section:v1:{marker_hash} -->'
    section = (f'{marker}\n## Generated code summary\n\n'
               f'Generated interpretation of captured source; verify against the current source.\n\n'
               f'{text}\n\n### Captured source provenance\n\n{sources}\n\n'
               f'### Recorded omissions\n\n{omissions}\n\n'
               f'Operation: {operation}; request: {request}; preview SHA-256: {preview_hash}; '
               f'output SHA-256: {output_hash}; artifact: {artifact}; artifact SHA-256: {artifact_hash}\n')
    encoded = section.encode('utf-8', 'strict')
    if len(encoded) > 262144 or await current() is not True:
        raise ValueError('Summary section unavailable')
    return {'version': 1, 'operation_id': result.operation_id, 'request_id': result.request_id,
            'artifact_id': result.artifact_id, 'artifact_sha256': artifact_hash, 'preview_hash': preview_hash,
            'output_sha256': output_hash, 'section': section, 'section_sha256': hashlib.sha256(encoded).hexdigest(), 'marker': marker}
