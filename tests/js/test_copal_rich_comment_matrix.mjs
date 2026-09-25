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
// specialCases probes: every advertised matrix special is executable and real.
// A manifest entry without a probe fails the run, so the manifest cannot
// advertise an unproven special.
// ---------------------------------------------------------------------------
const only = async (source, label, expectedText, name) => {
  const regions = await mod.documentationRegionsAsync(source, label);
  eq(regions.length, 1, `${label} ${name}: expected exactly one region in ${JSON.stringify(source)}`);
  if (regions.length === 1) eq(mod.regionMarkdown(source, regions[0]), expectedText, `${label} ${name}: markdown projection`);
  return regions;
};
const count = async (source, label, expected, name) => {
  const regions = await mod.documentationRegionsAsync(source, label);
  eq(regions.length, expected, `${label} ${name}: expected ${expected} regions in ${JSON.stringify(source)}, got ${regions.length} ${JSON.stringify(regions.map((r) => source.slice(r.from, r.to)))}`);
  return regions;
};

const specialCaseProbes = {
  'strings-lookalike': async (label) => {
    const src = 'const a = "// not"; const b = \'/* not */\'; // real\n';
    await only(src, label, 'real', 'strings-lookalike');
  },
  'regex-lookalike': async (label) => {
    // `/*` … `*/` bytes inside a regex literal are data, not a block comment.
    const src = 'const re = /[/*][*/]/; // real\n';
    await only(src, label, 'real', 'regex-lookalike');
  },
  'jsdoc-block': async (label) => {
    const src = '/**\n * Doc\n */\nfunction f() {}\n';
    const regions = await count(src, label, 1, 'jsdoc-block');
    if (regions.length === 1) {
      eq(regions[0].delimiterKind, 'doc-block', `${label} jsdoc-block: kind`);
      ok(mod.regionMarkdown(src, regions[0]).includes('Doc'), `${label} jsdoc-block: content preserved`);
      ok(!mod.regionMarkdown(src, regions[0]).includes('*/'), `${label} jsdoc-block: close delimiter stripped`);
    }
  },
  'jsx-expression-comment': async (label) => {
    const src = 'const el = <div>{/* c */}</div>;\n';
    const regions = await count(src, label, 1, 'jsx-expression-comment');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), ' c ', `${label} jsx-expression-comment: markdown`);
  },
  'jsx-text-not-comment': async (label) => {
    const src = 'const el = <div>/* not */</div>;\n// real\n';
    const regions = await count(src, label, 1, 'jsx-text-not-comment');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'real', `${label} jsx-text-not-comment: only real comment`);
  },
  'typed-declarations': async (label) => {
    const src = 'function f(x: string): number { return 1; } // real\n';
    await only(src, label, 'real', 'typed-declarations');
  },
  'jsx-generic-lookalike': async (label) => {
    // `<T>` generic parameters are not JSX tags.
    const src = 'function id<T>(x: T): T { return x; } // real\n';
    await only(src, label, 'real', 'jsx-generic-lookalike');
  },
  'module-docstring': async (label) => {
    const src = '"""Module **doc**"""\nx = 1\n';
    const regions = await mod.documentationRegionsAsync(src, label);
    const docstrings = regions.filter((r) => r.kind === 'docstring');
    eq(docstrings.length, 1, `${label} module-docstring: one module docstring`);
    eq(mod.regionMarkdown(src, docstrings[0]), 'Module **doc**', `${label} module-docstring: markdown`);
  },
  'function-docstring': async (label) => {
    const src = 'def f():\n    "func doc"\n';
    const regions = await mod.documentationRegionsAsync(src, label);
    const docstrings = regions.filter((r) => r.kind === 'docstring');
    eq(docstrings.length, 1, `${label} function-docstring: one function docstring`);
    eq(mod.regionMarkdown(src, docstrings[0]), 'func doc', `${label} function-docstring: markdown`);
  },
  'assigned-string-excluded': async (label) => {
    const src = 'x = "assigned"\n';
    const regions = await mod.documentationRegionsAsync(src, label);
    eq(regions.filter((r) => r.kind === 'docstring').length, 0, `${label} assigned-string-excluded: no docstring`);
  },
  'nested-block': async (label) => {
    const src = '/* a /* b */ c */\n// real\n';
    const regions = await count(src, label, 2, 'nested-block');
    if (regions.length === 2) {
      eq(regions[0].delimiterKind, 'block', `${label} nested-block: one outer block region`);
      ok(!mod.regionMarkdown(src, regions[0]).startsWith('/*'), `${label} nested-block: outer open stripped`);
      ok(mod.regionMarkdown(src, regions[0]).includes('a /* b */ c'), `${label} nested-block: inner bytes preserved`);
    }
  },
  'raw-string-lookalike': async (label) => {
    const src = label === 'Rust'
      ? 'let s = r"\n// not\n";\n// real\n'
      : 's := `\n// not\n`\n// real\n';
    const regions = await count(src, label, 1, 'raw-string-lookalike');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'real', `${label} raw-string-lookalike: only real comment`);
  },
  'inner-doc': async (label) => {
    const src = '//! inner\nfn f() {}\n';
    const regions = await count(src, label, 1, 'inner-doc');
    if (regions.length === 1) {
      eq(regions[0].delimiterKind, 'doc-line', `${label} inner-doc: kind`);
      eq(mod.regionMarkdown(src, regions[0]), 'inner', `${label} inner-doc: markdown`);
    }
  },
  'outer-doc': async (label) => {
    const src = '/// outer\nfn f() {}\n';
    const regions = await count(src, label, 1, 'outer-doc');
    if (regions.length === 1) {
      eq(regions[0].delimiterKind, 'doc-line', `${label} outer-doc: kind`);
      eq(mod.regionMarkdown(src, regions[0]), 'outer', `${label} outer-doc: markdown`);
    }
  },
  'directive-comment-bytes': async (label) => {
    const src = '//go:generate foo --bar\npackage x\n';
    const regions = await count(src, label, 1, 'directive-comment-bytes');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'go:generate foo --bar', `${label} directive-comment-bytes: bytes preserved`);
  },
  'javadoc-marker-strip': async (label) => {
    const src = '/**\n * Doc line\n */\nclass C {}\n';
    const regions = await count(src, label, 1, 'javadoc-marker-strip');
    if (regions.length === 1) ok(mod.regionMarkdown(src, regions[0]).includes('Doc line'), `${label} javadoc-marker-strip: star continuation stripped`);
  },
  'text-block-string': async (label) => {
    const src = 'String s = """\n// not\n""";\n// real\n';
    const regions = await count(src, label, 1, 'text-block-string');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'real', `${label} text-block-string: only real comment`);
  },
  'triple-string': async (label) => {
    const src = 'val s = """\n# not\n"""\n// real\n';
    const regions = await count(src, label, 1, 'triple-string');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'real', `${label} triple-string: only real comment`);
  },
  'interpolation': async (label) => {
    const src = label === 'Swift'
      ? 'let s = "\\(x) y"\n// real\n'
      : label === 'Kotlin'
        ? 'val s = "x ${1} y"\n// real\n'
        : '#{"x"} { color: red; }\n// real\n';
    await only(src, label, 'real', 'interpolation');
  },
  'escaped-newline-continuation': async (label) => {
    const src = '// comment \\\n  still\nint x;\n';
    const regions = await count(src, label, 1, 'escaped-newline-continuation');
    if (regions.length === 1) {
      ok(regions[0].to > src.indexOf('\n'), `${label} escaped-newline-continuation: region spans the spliced line`);
      ok(mod.regionMarkdown(src, regions[0]).includes('still'), `${label} escaped-newline-continuation: continued text kept`);
    }
  },
  'preprocessor-boundary': async (label) => {
    const src = '#define MAX(a,b) ((a)>(b)) // compare\n// real\n';
    // Adjacent line comments group; the `//` tail of the directive is a comment.
    const regions = await count(src, label, 1, 'preprocessor-boundary');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'compare\nreal', `${label} preprocessor-boundary: tail stays a comment`);
  },
  'extension-routing': async (label) => {
    eq(mod.advertisedLabel(label), label, `${label} extension-routing: label resolves`);
    ok([...mod.ADVERTISED_LANGUAGE_LABELS].includes(label), `${label} extension-routing: advertised label set`);
    eq(mod.supportsRichComments(label), true, `${label} extension-routing: rich comments apply`);
    const regions = await count('// real\nint x;\n', label, 1, 'extension-routing');
    if (regions.length === 1) eq(regions[0].language, label, `${label} extension-routing: regions carry the label`);
  },
  'h-vs-hpp': async (label) => {
    eq(mod.advertisedLabel('c header'), 'C/C++ header', `${label} h-vs-hpp: .h routes to C/C++ header`);
    eq(mod.advertisedLabel('hpp'), 'C++ header', `${label} h-vs-hpp: .hpp routes to C++ header`);
    ok(mod.advertisedLabel('c header') !== mod.advertisedLabel('hpp'), `${label} h-vs-hpp: distinct rows`);
  },
  'hpp-extension': async (label) => {
    eq(mod.advertisedLabel('hpp', { path: 'src/util.hpp' }), 'C++ header', `${label} hpp-extension: path routing`);
    eq(mod.supportsRichComments('C++ header'), true, `${label} hpp-extension: spec present`);
    const regions = await count('// real\nint x;\n', label, 1, 'hpp-extension');
    if (regions.length === 1) eq(regions[0].open, '//', `${label} hpp-extension: line comment form`);
  },
  'doc-line-marker': async (label) => {
    const src = '/// doc line\nlet x = 1;\n';
    const regions = await count(src, label, 1, 'doc-line-marker');
    if (regions.length === 1) {
      eq(regions[0].delimiterKind, 'doc-line', `${label} doc-line-marker: kind`);
      eq(mod.regionMarkdown(src, regions[0]), 'doc line', `${label} doc-line-marker: marker stripped`);
    }
  },
  'raw-string-delimiter': async (label) => {
    const src = 'auto s = R"(\n// not\n)";\n// real\n';
    const regions = await count(src, label, 1, 'raw-string-delimiter');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'real', `${label} raw-string-delimiter: only real comment`);
  },
  'macro-comment': async (label) => {
    const src = '#define FOO /* keep */ 1\n// real\n';
    await count(src, label, 2, 'macro-comment');
  },
  'xml-doc-comment': async (label) => {
    const src = '/// <summary>Doc</summary>\nclass C {}\n';
    const regions = await count(src, label, 1, 'xml-doc-comment');
    if (regions.length === 1) {
      eq(regions[0].delimiterKind, 'doc-line', `${label} xml-doc-comment: kind`);
      ok(mod.regionMarkdown(src, regions[0]).includes('<summary>Doc</summary>'), `${label} xml-doc-comment: xml bytes preserved`);
    }
  },
  'verbatim-string': async (label) => {
    const src = 'var s = @"// not";\n// real\n';
    await only(src, label, 'real', 'verbatim-string');
  },
  'interpolated-string': async (label) => {
    const src = 'var s = $"x {1} y";\n// real\n';
    await only(src, label, 'real', 'interpolated-string');
  },
  'raw-string': async (label) => {
    const src = label === 'C#'
      ? 'var s = """\n// not\n""";\n// real\n'
      : 'let s = #"\n// not\n"#\n// real\n';
    await only(src, label, 'real', 'raw-string');
  },
  'begin-end-column-sensitive': async (label) => {
    const src = '  =begin\n  not a block\n  =end\n# real\n';
    const regions = await count(src, label, 1, 'begin-end-column-sensitive');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'real', `${label} begin-end-column-sensitive: indented =begin is not a block`);
  },
  'heredoc': async (label) => {
    const src = label === 'Ruby'
      ? 's = <<~EOS\n  # not\n  // not\nEOS\n# real\n'
      : label === 'PHP'
        ? '<?php\n$x = <<<EOT\n# not\n// not\nEOT;\n// real\n'
        : 'cat <<EOF\n# not\nEOF\n# real\n';
    const regions = await count(src, label, 1, 'heredoc');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'real', `${label} heredoc: body prefixes are data`);
  },
  'percent-string': async (label) => {
    const src = 's = %w[# not]\n# real\n';
    await only(src, label, 'real', 'percent-string');
  },
  'regex': async (label) => {
    const src = 're = /# not/\n# real\n';
    await only(src, label, 'real', 'regex');
  },
  'nowdoc': async (label) => {
    const src = "<?php\n$x = <<<'EOT'\n# not\nEOT;\n// real\n";
    const regions = await count(src, label, 1, 'nowdoc');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'real', `${label} nowdoc: body prefixes are data`);
  },
  'mixed-html-js-css': async (label) => {
    const src = '<?php\n// php\n?>\n<!-- html -->\n<script>\n// js\n</script>\n';
    const regions = await count(src, label, 3, 'mixed-html-js-css');
    const opens = regions.map((r) => r.open).sort();
    ok(opens.includes('<!--'), `${label} mixed-html-js-css: html comment found`);
    ok(opens.includes('//'), `${label} mixed-html-js-css: php/js comment found`);
  },
  'multiline-string': async (label) => {
    const src = 'let s = """\n// not\n"""\n// real\n';
    await only(src, label, 'real', 'multiline-string');
  },
  'shebang': async (label) => {
    const src = '#!/bin/bash\necho hi\n# real\n';
    const regions = await count(src, label, 2, 'shebang');
    if (regions.length === 2) eq(mod.regionMarkdown(src, regions[0]), '!/bin/bash', `${label} shebang: opener stripped`);
  },
  'quote-lookalike': async (label) => {
    const src = 'echo "# not"\necho \'# not\'\n# real\n';
    await only(src, label, 'real', 'quote-lookalike');
  },
  'backslash-continuation': async (label) => {
    // A shell comment ends at the newline even when it ends with a backslash.
    const src = 'echo a # note \\\necho b\n';
    const regions = await count(src, label, 1, 'backslash-continuation');
    if (regions.length === 1) {
      eq(regions[0].to, src.indexOf('\n'), `${label} backslash-continuation: comment stops at the newline`);
    }
  },
  'bash-zsh-aliases': async (label) => {
    const src = 'alias x=\'# not\'\nalias y="# not"\n# real\n';
    await only(src, label, 'real', 'bash-zsh-aliases');
  },
  'strict-json-no-comments': async (label) => {
    eq(mod.documentationRegions('{"a":1}', label).length, 0, `${label} strict-json-no-comments: no regions`);
    const placement = mod.safeCommentInsertion('{"a":1}', label, 3);
    eq(placement.ok, false, `${label} strict-json-no-comments: insertion refused`);
    ok(String(placement.error || '').includes('Strict JSON'), `${label} strict-json-no-comments: limitation explained`);
  },
  'jsonc-alias-rich': async (label) => {
    eq(mod.supportsRichComments(label, { dialect: 'jsonc' }), true, `${label} jsonc-alias-rich: dialect is rich`);
    const src = '{\n  // real\n  "a": 1\n}\n';
    const regions = await mod.documentationRegionsAsync(src, label, { dialect: 'jsonc' });
    eq(regions.length, 1, `${label} jsonc-alias-rich: dialect scans comments`);
  },
  'quoted-string': async (label) => {
    const src = 'key: "# not"\n# real\n';
    await only(src, label, 'real', 'quoted-string');
  },
  'block-scalar': async (label) => {
    const src = 'key: |\n  # not\n# real\n';
    await only(src, label, 'real', 'block-scalar');
  },
  'multiline-basic-string': async (label) => {
    const src = 's = """\n# not\n"""\n# real\n';
    await only(src, label, 'real', 'multiline-basic-string');
  },
  'multiline-literal-string': async (label) => {
    const src = "s = '''\n# not\n'''\n# real\n";
    await only(src, label, 'real', 'multiline-literal-string');
  },
  'cdata': async (label) => {
    const src = '<![CDATA[<!-- not -->]]>\n<!-- real -->\n';
    const regions = await count(src, label, 1, 'cdata');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), ' real ', `${label} cdata: CDATA lookalike stays source`);
    const solo = '<![CDATA[<!-- not -->]]>\n';
    await count(solo, label, 0, 'cdata solo');
  },
  'declaration': async (label) => {
    const src = '<?xml version="1.0"?>\n<!-- real -->\n';
    await only(src, label, ' real ', 'declaration');
  },
  'attribute-value': async (label) => {
    const src = '<root attr="<!-- not -->"/>\n<!-- real -->\n';
    const regions = await count(src, label, 1, 'attribute-value');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), ' real ', `${label} attribute-value: attribute lookalike stays source`);
  },
  'nested-js-comment': async (label) => {
    const src = '<script>\n// js\n</script>\n<!-- real -->\n';
    const regions = await count(src, label, 2, 'nested-js-comment');
    ok(regions.some((r) => r.open === '//'), `${label} nested-js-comment: js comment found`);
    ok(regions.some((r) => r.open === '<!--'), `${label} nested-js-comment: html comment found`);
  },
  'nested-css-comment': async (label) => {
    const src = '<style>\n/* css */\n</style>\n<!-- real -->\n';
    const regions = await count(src, label, 2, 'nested-css-comment');
    ok(regions.some((r) => r.open === '/*'), `${label} nested-css-comment: css comment found`);
    ok(regions.some((r) => r.open === '<!--'), `${label} nested-css-comment: html comment found`);
  },
  'script-style-strings': async (label) => {
    const src = '<script>\nconst s = "// not";\n</script>\n<!-- real -->\n';
    const regions = await count(src, label, 1, 'script-style-strings');
    if (regions.length === 1) eq(regions[0].open, '<!--', `${label} script-style-strings: strings stay data`);
  },
  'string-url-token': async (label) => {
    const src = 'a { background: url("/* not */"); }\n/* real */\n';
    const regions = await count(src, label, 1, 'string-url-token');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), ' real ', `${label} string-url-token: url string stays data`);
  },
  'official-sass-parser': async (label) => {
    eq(mod.supportsRichComments(label), true, `${label} official-sass-parser: rich comments apply`);
    ok([...mod.ADVERTISED_LANGUAGE_LABELS].includes(label), `${label} official-sass-parser: advertised label set`);
    await only('$x: 1; // real\n', label, 'real', 'official-sass-parser');
  },
  'full-document-preview': async (label) => {
    eq(mod.supportsRichComments(label), false, `${label} full-document-preview: not a comment language`);
    eq(mod.documentationRegions('# title\n', label).length, 0, `${label} full-document-preview: no comment regions`);
  },
  'ordinary-image-insertion': async (label) => {
    const placement = mod.safeCommentInsertion('# title\n', label, 3);
    eq(placement.ok, true, `${label} ordinary-image-insertion: direct embed allowed`);
    eq(placement.comment, '', `${label} ordinary-image-insertion: no comment wrapper`);
  },
  'dialect-quotes': async (label) => {
    const src = "SELECT 'it''s' FROM t; -- real\n";
    await only(src, label, 'real', 'dialect-quotes');
  },
  'string-lookalike': async (label) => {
    const src = label === 'SQL'
      ? "SELECT '-- not' FROM t;\n-- real\n"
      : 'a { content: "/* not */"; }\n/* real */\n';
    const regions = await count(src, label, 1, 'string-lookalike');
    if (regions.length === 1) ok(mod.regionMarkdown(src, regions[0]).includes('real'), `${label} string-lookalike: only real comment`);
  },
  'directive-metadata-not-comment': async (label) => {
    const src = '%%{init: {"x":1}}%%\n%% real\n';
    await only(src, label, 'real', 'directive-metadata-not-comment');
  },
  'quoted-label': async (label) => {
    const src = 'A["# not"] --> B\n%% real\n';
    await only(src, label, 'real', 'quoted-label');
  },
  'parser-directive': async (label) => {
    const src = '# syntax=docker/dockerfile:1\n# real\n';
    const regions = await count(src, label, 1, 'parser-directive');
    if (regions.length === 1) eq(mod.regionMarkdown(src, regions[0]), 'real', `${label} parser-directive: directive excluded`);
  },
  'heredoc-body': async (label) => {
    const src = 'RUN <<EOF\n# not\nEOF\n# real\n';
    await only(src, label, 'real', 'heredoc-body');
  },
  'continued-instruction': async (label) => {
    // Dockerfile `#` must be line-leading; a mid-instruction `#` is not a comment.
    const src = 'RUN echo a \\\n    && echo b # not\n';
    await count(src, label, 0, 'continued-instruction');
  },
  'no-invented-syntax': async (label) => {
    eq(mod.documentationRegions('anything at all\n', label).length, 0, `${label} no-invented-syntax: no regions`);
    eq(mod.supportsRichComments(label), false, `${label} no-invented-syntax: not rich`);
  },
  'explicit-attachment-representation': async (label) => {
    const placement = mod.safeCommentInsertion('hello\n', label, 2);
    eq(placement.ok, false, `${label} explicit-attachment-representation: insertion refused`);
    ok(String(placement.error || '').length > 0, `${label} explicit-attachment-representation: refusal explains the limit`);
  },
};

let specialProbesRun = 0;
for (const row of manifest.rows) {
  for (const special of row.specialCases) {
    ok(typeof specialCaseProbes[special] === 'function', `${row.label}: special case ${special} has an executable probe`);
    if (typeof specialCaseProbes[special] === 'function') {
      await specialCaseProbes[special](row.label);
      specialProbesRun += 1;
    }
  }
}
eq(specialProbesRun, manifest.rows.reduce((sum, row) => sum + row.specialCases.length, 0), 'every specialCases entry ran its probe');

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
