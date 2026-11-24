"""M6(a): one Markdown file per memory in a zip; same authority, redaction and private/no-store handling."""
import io
import zipfile

from src import memery_formats as f
from tests.test_markdown_export_route import NOW, make, row


def names_and_docs(response):
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    return {name: archive.read(name).decode('utf-8') for name in archive.namelist()}


def test_round_trip_of_text_kind_and_stable_safe_names():
    client, seen = make([row('Buy milk\nand eggs'), row('Second memory', kind='project')])
    response = client.get('/api/memory/export/markdown?layout=files')
    assert response.status_code == 200 and response.headers['content-type'] == 'application/zip'
    assert response.headers['cache-control'] == 'private, no-store' and 'openclank-memory-20261110.zip' in response.headers['content-disposition']
    files = names_and_docs(response)
    assert len(files) == 2 and all(n.endswith('.md') and '/' not in n and '\\' not in n and n.isascii() for n in files)
    texts = {}
    for name, doc in files.items():
        bundle = f.parse_markdown(doc)          # each file is a valid single-item export: re-importable
        assert len(bundle.items) == 1 and name.endswith(bundle.items[0].id + '.md')
        texts[bundle.items[0].text] = bundle.items[0].kind
    assert texts == {'Buy milk\nand eggs': 'fact', 'Second memory': 'project'}
    again = names_and_docs(client.get('/api/memory/export/markdown?layout=files'))
    assert again == files, 'names and bytes are stable'


def test_unsafe_titles_never_reach_the_file_name():
    unsafe = ['../../etc/passwd', 'CON:\\x00weird<>|"*?', '‮evil.md', 'x' * 500, '   \n  ']
    client, _ = make([row(t) for t in unsafe])
    files = names_and_docs(client.get('/api/memory/export/markdown?layout=files'))
    assert files, 'safe records are exported'
    for name in (n for n in files if n != 'OMISSIONS.json'):
        assert name.isascii() and set(name) <= set('abcdefghijklmnopqrstuvwxyz0123456789-.') and not name.startswith('.') and len(name) < 90, name


def test_empty_store_is_an_empty_archive_and_redaction_still_applies():
    client, _ = make([])
    assert names_and_docs(client.get('/api/memory/export/markdown?layout=files')) == {}
    client, _ = make([row('api key sk-ant-api03-' + 'a' * 40), row('plain')])
    files = names_and_docs(client.get('/api/memory/export/markdown?layout=files'))
    assert len(files) == 1 + ('OMISSIONS.json' in files) and 'sk-ant' not in ''.join(files.values())


def test_default_layout_and_auth_are_unchanged():
    client, seen = make([row('a')])
    assert client.get('/api/memory/export/markdown').headers['content-type'].startswith('text/markdown')
    client, seen = make([row('a')], authed=False)
    assert client.get('/api/memory/export/markdown?layout=files').status_code == 403 and not seen['lists']
