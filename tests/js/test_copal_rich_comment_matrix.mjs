import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const mod = await import(path.join(repo, 'static/js/copal/codemirror.js'));
const manifest = JSON.parse(fs.readFileSync(path.join(here, 'fixtures/rich_comment_coverage_manifest.json'), 'utf8'));

let asserts = 0;
const ok = (condition, message) => {
  asserts += 1;
  assert.ok(condition, message);
};
const eq = (actual, expected, message) => {
  asserts += 1;
  assert.equal(actual, expected, message);
};

const decode = (value) => String(value).replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\r/g, '\r').replace(/\\\\"/g, '"').replace(/\\\\\\\\/g, '\\');

eq(manifest.rows.length, 30, 'coverage manifest must contain all 30 advertised labels');
const labels = manifest.rows.map((row) => row.label);
eq(new Set(labels).size, 30, 'coverage manifest labels must be unique');
const advertised = [...mod.ADVERTISED_LANGUAGE_LABELS];
eq(advertised.length, 30, 'advertised language registry must stay at 30 labels');
for (const label of advertised) {
  ok(labels.includes(label), `manifest is missing advertised label: ${label}`);
}

let rowsPassed = 0;
for (const row of manifest.rows) {
  const source = decode(row.fixture);
  const regions = await mod.documentationRegionsAsync(source, row.label, row.dialect ? { dialect: row.dialect } : {});
  const reconstructed = mod.reconstructFromRegions(source, regions);
  eq(reconstructed, source, `${row.label}: source round-trip must preserve bytes`);
  eq(regions.length, row.expectRegions, `${row.label}: expected ${row.expectRegions} documentation regions, got ${regions.length}`);
  const kinds = regions.map((region) => region.delimiterKind);
  for (const kind of row.expectKinds) {
    ok(kinds.includes(kind), `${row.label}: missing delimiter kind ${kind} in ${JSON.stringify(kinds)}`);
  }
  // Content spans never swallow delimiters or mutate program bytes.
  for (const region of regions) {
    ok(region.contentFrom >= region.from && region.contentTo <= region.to, `${row.label}: content bounds stay inside outer bounds`);
    ok(region.contentRanges.every((span) => span.from >= region.from && span.to <= region.to), `${row.label}: content spans stay inside region`);
    const markdown = mod.regionMarkdown(source, region);
    // The open delimiter is consumed as outer source, not projected as Markdown.
    ok(region.contentFrom >= region.from + region.open.length, `${row.label}: markdown must strip the open delimiter`);
    if (region.close) ok(region.contentTo <= region.to - region.close.length, `${row.label}: markdown must strip the close delimiter`);
    ok(typeof markdown === 'string', `${row.label}: markdown projection is a string`);
  }
  // Rich-comment applicability matches the row's behavior (strict label form).
  const rich = mod.supportsRichComments(row.label, {});
  if (row.behavior === 'comments' || row.behavior === 'comments-and-docstrings') {
    eq(rich, true, `${row.label}: rich comments must be applicable`);
  } else if (row.behavior === 'strict-json-no-comments-jsonc-rich') {
    eq(rich, false, `${row.label}: strict JSON must not claim a comment wrapper`);
    eq(mod.supportsRichComments(row.label, { dialect: 'jsonc' }), true, `${row.label}: JSONC dialect must be rich`);
  } else {
    eq(rich, false, `${row.label}: rich comments must not claim a comment wrapper`);
  }
  // JSONC dialect under the JSON label.
  if (row.dialect && row.dialectExpectRegions != null) {
    const dialectSource = decode(row.dialectFixture);
    const dialectRegions = await mod.documentationRegionsAsync(dialectSource, row.label, { dialect: row.dialect });
    eq(mod.reconstructFromRegions(dialectSource, dialectRegions), dialectSource, `${row.label} ${row.dialect}: dialect round-trip`);
    eq(dialectRegions.length, row.dialectExpectRegions, `${row.label} ${row.dialect}: dialect region count`);
  }
  rowsPassed += 1;
}
eq(rowsPassed, 30, 'every matrix row must pass');

// ---------------------------------------------------------------------------
// Python positive/negative docstring contexts (not just triple quotes)
// ---------------------------------------------------------------------------
const pythonCases = {
  positive: {
    triple: ['"""Module doc"""', 'Module doc'],
    single: ['def f():\n    "func doc"\n', 'func doc'],
    raw: ['def f():\n    r"""raw doc"""\n', 'raw doc'],
    unicode: ['def f():\n    u"uni doc"\n', 'uni doc'],
    parenthesized: ['def f():\n    ("paren doc")\n', 'paren doc'],
    adjacent: ['def f():\n    "a" "b"\n', 'a\nb'],
    class: ['class C:\n    "cls doc"\n    pass\n', 'cls doc'],
    async: ['async def f():\n    "async doc"\n', 'async doc'],
    'doc-after-comment': ['def f():\n    # comment first\n    "doc"\n', 'doc'],
  },
  negative: {
    bytes: 'def f():\n    b"bytes"\n',
    fstring: 'def f():\n    f"fmt"\n',
    binary: 'def f():\n    "a" + "b"\n',
    call: 'def f():\n    print("call")\n',
    assigned: 'x = "assigned"\n',
    later: 'def f():\n    x = 1\n    "later"\n',
  },
};
for (const [name, [source, expected]] of Object.entries(pythonCases.positive)) {
  const regions = await mod.documentationRegionsAsync(source, 'Python');
  const docstrings = regions.filter((region) => region.kind === 'docstring');
  ok(docstrings.length >= 1, `python ${name}: expected a docstring region`);
  eq(mod.regionMarkdown(source, docstrings[0]), expected, `python ${name}: docstring markdown`);
}
for (const [name, source] of Object.entries(pythonCases.negative)) {
  const regions = await mod.documentationRegionsAsync(source, 'Python');
  const docstrings = regions.filter((region) => region.kind === 'docstring');
  eq(docstrings.length, 0, `python ${name}: must not qualify as a docstring`);
}

// ---------------------------------------------------------------------------
// Strict JSON limitation vs JSONC applicability
// ---------------------------------------------------------------------------
const strictJson = mod.safeCommentInsertion('{"a":1}', 'JSON', 3);
eq(strictJson.ok, false, 'strict JSON must refuse invented comment insertion');
ok(String(strictJson.error || '').includes('Strict JSON'), 'strict JSON limitation is explained');
const jsoncInsert = mod.safeCommentInsertion('{\n  "a":1\n}', 'JSONC', 5);
eq(jsoncInsert.ok, true, 'JSONC must accept comment insertion');
eq(jsoncInsert.comment, '//', 'JSONC insertion uses line comments');

const plainInsert = mod.safeCommentInsertion('hello', 'Plain text', 2);
eq(plainInsert.ok, false, 'Plain text must refuse invented comment syntax');

// ---------------------------------------------------------------------------
// Image-paste comment wrapping never splits tokens and retains indentation
// ---------------------------------------------------------------------------
const wrappedJs = mod.wrapMarkdownAsComment('![img](media/a.png)', 'JavaScript', '  ');
eq(wrappedJs.ok, true, 'JavaScript comment wrap succeeds');
ok(wrappedJs.text.includes('  // '), 'JavaScript wrap retains indentation');
const wrappedHtml = mod.wrapMarkdownAsComment('![img](x.png)', 'HTML', '');
eq(wrappedHtml.ok, true, 'HTML comment wrap succeeds');
ok(wrappedHtml.text.startsWith('<!--') && wrappedHtml.text.endsWith('-->'), 'HTML wrap is a complete comment');
const wrappedJson = mod.wrapMarkdownAsComment('![img](x.png)', 'JSON', '');
eq(wrappedJson.ok, false, 'strict JSON cannot wrap images as comments');

// ---------------------------------------------------------------------------
// Language matrix metadata honesty
// ---------------------------------------------------------------------------
const matrix = mod.languageMatrix();
eq(matrix.length, 30, 'languageMatrix lists 30 rows');
const markdownRow = matrix.find((row) => row.label === 'Markdown');
eq(markdownRow.behavior, 'full-document-rich-preview', 'Markdown keeps full-document rich preview');
const plainRow = matrix.find((row) => row.label === 'Plain text');
eq(plainRow.behavior, 'plain-editing-no-invented-comments', 'Plain text invents no comment syntax');
const jsonRow = matrix.find((row) => row.label === 'JSON');
eq(jsonRow.behavior, 'strict-json-no-comments-jsonc-rich', 'JSON row documents strict vs JSONC');

console.log(`copal rich-comment coverage manifest tests passed (${asserts} asserts, 30 rows)`);
