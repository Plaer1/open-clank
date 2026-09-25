import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const mod = await import(path.join(repo, 'static/js/copal/codemirror.js'));

let asserts = 0;
const ok = (condition, message) => { asserts += 1; assert.ok(condition, message); };
const eq = (actual, expected, message) => { asserts += 1; assert.equal(actual, expected, message); };

const regionsOf = async (source, language, options = {}) => {
  const regions = await mod.documentationRegionsAsync(source, language, options);
  eq(mod.reconstructFromRegions(source, regions), source, `round-trip for ${language}`);
  return regions;
};

// --- disposable language samples: byte round-trip + content boundaries ------
{
  const js = 'const x = 1; // # Heading\n/* **block** */\nlet re = /\\/\\/ not/;';
  const regions = await regionsOf(js, 'JavaScript');
  eq(regions.length, 2, 'js finds line + block');
  eq(mod.regionMarkdown(js, regions[0]), '# Heading', 'js line markdown');
  eq(mod.regionMarkdown(js, regions[1]), ' **block** ', 'js block markdown');
}

{
  const rust = 'fn f() {\n    //! inner\n    /// outer\n    /* nested /* in */ */\n    let s = r"// not";\n}';
  const regions = await regionsOf(rust, 'Rust');
  eq(regions.length, 3, 'rust finds three documentation regions');
  eq(regions[0].delimiterKind, 'doc-line', 'rust inner doc');
  eq(regions[2].delimiterKind, 'block', 'rust nested block is one region');
  ok(!mod.regionMarkdown(rust, regions[2]).startsWith('/*'), 'rust nested block strips outer open');
}

{
  const crlf = '// line one\r\n// line two\r\n/* block\r\n * star\r\n */\r\n';
  const regions = await regionsOf(crlf, 'JavaScript');
  ok(regions.length >= 2, 'crlf groups line comments');
  const joined = mod.regionMarkdown(crlf, regions[0]);
  eq(joined, 'line one\nline two', 'crlf line markdown uses logical newlines');
}

{
  const nonAscii = '// café **naïve**\n/* ünïcode */\n';
  const regions = await regionsOf(nonAscii, 'JavaScript');
  eq(regions.length, 2, 'non-ascii comments found');
  eq(mod.regionMarkdown(nonAscii, regions[0]), 'café **naïve**', 'non-ascii content preserved');
}

{
  const lookalikes = 'const s = "// not a comment";\nconst t = \'/* not */\';\nconst u = `// nope`;\n// real\n';
  const regions = await regionsOf(lookalikes, 'JavaScript');
  eq(regions.length, 1, 'string lookalikes are not comments');
  eq(mod.regionMarkdown(lookalikes, regions[0]), 'real', 'only the real comment projects');
}

// --- Python docstrings: structural, not triple-quote heuristics ------------
{
  const py = '"""Module **doc**"""\nclass C:\n    """Cls"""\n    def f(self):\n        "fn"\n        x = "assigned"\n';
  const regions = await regionsOf(py, 'Python');
  const docstrings = regions.filter((r) => r.kind === 'docstring');
  eq(docstrings.length, 3, 'python finds module, class and function docstrings');
  eq(mod.regionMarkdown(py, docstrings[0]), 'Module **doc**', 'module docstring markdown');
  eq(mod.regionMarkdown(py, docstrings[2]), 'fn', 'function docstring markdown');
}

{
  const cases = [
    ['def f():\n    b"bytes"\n', 'bytes'],
    ['def f():\n    f"fmt"\n', 'fstring'],
    ['def f():\n    "a" + "b"\n', 'binary'],
    ['def f():\n    print("call")\n', 'call'],
    ['x = "assigned"\n', 'assigned'],
    ['def f():\n    x = 1\n    "later"\n', 'later'],
  ];
  for (const [source, name] of cases) {
    const regions = await regionsOf(source, 'Python');
    eq(regions.filter((r) => r.kind === 'docstring').length, 0, `python ${name} excluded`);
  }
}

// --- HTML/PHP nested regions use their own grammar delimiters --------------
{
  const html = '<!-- html -->\n<script>\n// js\n</script>\n<style>\n/* css */\n</style>\n';
  const regions = await regionsOf(html, 'HTML');
  eq(regions.length, 3, 'html nested js/css comments found');
  ok(regions.some((r) => r.open === '<!--'), 'html comment region');
  ok(regions.some((r) => r.open === '//'), 'nested js comment region');
  ok(regions.some((r) => r.open === '/*'), 'nested css comment region');
}

// --- See source / insertion helpers ---------------------------------------
{
  const src = 'const x = 1; // **doc**\n';
  const regions = await regionsOf(src, 'JavaScript');
  const map = regions.map((r) => ({ ...r, markdown: mod.regionMarkdown(src, r) }));
  eq(map[0].from, 13, 'see-source range starts at comment');
  eq(map[0].markdown, '**doc**', 'see-source markdown');
}

{
  const insertion = mod.safeCommentInsertion('function f() {\n  return 1;\n}\n', 'JavaScript', 20);
  eq(insertion.ok, true, 'javascript insertion point');
  eq(insertion.comment, '//', 'javascript insertion uses line comment');
  eq(insertion.indent, '  ', 'insertion retains indent');
}

{
  const wrapped = mod.wrapMarkdownAsComment('![img](a.png)', 'Python', '    ');
  eq(wrapped.ok, true, 'python wrap');
  ok(wrapped.text.startsWith('    # '), 'python wrap uses hash comment and indent');
  const round = await regionsOf(`def f():\n${wrapped.text}\n    return 1\n`, 'Python');
  eq(round.length, 1, 'wrapped comment is a documentation region');
  eq(mod.regionMarkdown(`def f():\n${wrapped.text}\n    return 1\n`, round[0]), '![img](a.png)', 'wrapped markdown round-trips');
}

{
  const strict = mod.safeCommentInsertion('{"a": 1}', 'JSON', 2);
  eq(strict.ok, false, 'strict json refuses insertion');
  ok(String(strict.error).includes('Strict JSON'), 'strict json explains the limit');
  const jsonc = mod.safeCommentInsertion('{\n  "a": 1\n}', 'JSONC', 4);
  eq(jsonc.ok, true, 'jsonc accepts insertion');
  eq(jsonc.comment, '//', 'jsonc uses line comments');
}

{
  const plain = mod.wrapMarkdownAsComment('![img](a.png)', 'Plain text', '');
  eq(plain.ok, false, 'plain text cannot wrap comments');
  const markdown = mod.wrapMarkdownAsComment('![img](a.png)', 'Markdown', '');
  eq(markdown.ok, true, 'markdown embeds images directly');
  eq(markdown.text, '![img](a.png)', 'markdown leaves the embed unchanged');
}

// --- SCSS uses real sass applicability; JSON stays split -------------------
{
  ok(mod.supportsRichComments('SCSS'), 'scss rich comments apply');
  ok(mod.supportsRichComments('Kotlin'), 'kotlin rich comments apply (stream adapter)');
  ok(mod.supportsRichComments('C#'), 'csharp rich comments apply (stream adapter)');
  ok(!mod.supportsRichComments('Plain text'), 'plain text has no rich comments');
  ok(!mod.supportsRichComments('JSON'), 'strict json has no rich comments');
  ok(mod.supportsRichComments('JSON', { dialect: 'jsonc' }), 'jsonc dialect has rich comments');
  eq(mod.dialectForPath('src/config.jsonc'), 'jsonc', 'jsonc path dialect');
  eq(mod.dialectForPath('src/config.json'), 'json', 'json path dialect');
}

// --- mixed-language file and multiline docstrings -------------------------
{
  const mixed = '<?php\n// php line\n?>\n<!-- html -->\n<script>\n// js\n</script>\n';
  const regions = await regionsOf(mixed, 'PHP');
  ok(regions.length >= 2, 'php mixed file finds regions');
}

{
  const multi = 'def f():\n    """Multi\n    line **doc**\n    """\n    return 1\n';
  const regions = await regionsOf(multi, 'Python');
  eq(regions.length, 1, 'multiline python docstring is one region');
  ok(mod.regionMarkdown(multi, regions[0]).includes('line **doc**'), 'multiline content preserved');
}

console.log(`copal documentation-region tests passed (${asserts} asserts)`);
