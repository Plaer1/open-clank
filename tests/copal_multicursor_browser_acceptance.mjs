import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><main id="app"></main></body>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  const result = await evaluate(`(async () => {
    const [module, workspace] = await Promise.all([
      import('/static/js/copal/codemirror.js?multicursor=' + Date.now()),
      import('/static/js/copal/notesWorkspace.js?multicursor=' + Date.now()),
    ]);
    const { createSourceEditor } = module;
    const host = document.createElement('div');
    host.style.cssText = 'width:800px;height:180px';
    document.body.append(host);
    const editor = createSourceEditor({
      parent:host,
      doc:'alpha beta alpha\\nalpha beta',
      language:'Plain text',
      selection:{ version:1, ranges:[{ anchor:0, head:5 }, { anchor:11, head:16 }], mainIndex:1 },
    });
    await editor.languageReady;
    const initial = editor.getSelection();
    editor.insertText('X');
    const inserted = { text:editor.getValue(), selection:editor.getSelection() };
    editor.undo();
    const undone = { text:editor.getValue(), selection:editor.getSelection() };
    editor.redo();
    const redone = { text:editor.getValue(), selection:editor.getSelection() };
    editor.setSelection({ ranges:[{ anchor:2, head:6 }], mainIndex:0 });
    const selectedAll = editor.runCommand('select-all-matches');
    const allMatches = editor.getSelection();
    editor.insertText('Q');
    const afterCommandEdit = { text:editor.getValue(), selection:editor.getSelection() };
    editor.undo();
    const afterCommandUndo = { text:editor.getValue(), selection:editor.getSelection() };
    editor.applyValue(editor.getValue(), { ranges:[{ anchor:1, head:1 }, { anchor:8, head:8 }], mainIndex:1 });
    const applied = editor.getSelection();
    editor.setValue(editor.getValue(), { ranges:[{ anchor:1, head:1 }, { anchor:8, head:8 }], mainIndex:1 });
    const reset = editor.getSelection();
    editor.setSelection({ ranges:[{ anchor:1, head:2 }, { anchor:8, head:8 }], mainIndex:0 });
    editor.replaceRange(1, 2, 'ATTACH');
    const attachment = { text:editor.getValue(), selection:editor.getSelection() };
    editor.undo();
    const afterAttachmentUndo = { text:editor.getValue(), selection:editor.getSelection() };
    editor.duplicateLines();
    const duplicated = { text:editor.getValue(), selection:editor.getSelection() };
    editor.undo();
    const afterDuplicateUndo = { text:editor.getValue(), selection:editor.getSelection() };
    const richHost = document.createElement('div');
    richHost.style.cssText = 'width:800px;height:180px';
    document.body.append(richHost);
    const richSource = '// # Heading\\nconst literal = "// not a comment";\\n/* **bold** */';
    const rich = createSourceEditor({
      parent:richHost,
      doc:richSource,
      language:'JavaScript',
      richComments:true,
      renderPreview:(markdown) => { const node = document.createElement('strong'); node.textContent = markdown; return node; },
    });
    await rich.languageReady;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const commentMap = rich.getCommentSourceMap();
    const renderedComment = richHost.querySelector('.cm-rich-comment-widget');
    const renderedCommentText = renderedComment ? renderedComment.textContent : '';
    const grammarFixtures = {
      JavaScript:'// # Café 😀\\r\\nconst s = "// LOOKALIKE"; const r = /x+/; const t = \`// LOOKALIKE\`;', TypeScript:'// # Café 😀\\r\\nconst s = "// LOOKALIKE"; const r = /x+/;', JSX:'// # Café 😀\\r\\nconst s = "// LOOKALIKE";',
      CSS:'/* # Café 😀 */\\r\\n.x { content:"/* LOOKALIKE */"; }', HTML:'<!-- # Café 😀 -->\\r\\n<div title="<!-- LOOKALIKE -->"></div>', XML:'<!-- # Café 😀 -->\\r\\n<root value="<!-- LOOKALIKE -->"/>',
      Java:'// # Café 😀\\r\\nclass A { String s = "// LOOKALIKE"; }', C:'// # Café 😀\\r\\nconst char *s = "// LOOKALIKE";', 'C++':'// # Café 😀\\r\\nconst char *s = "// LOOKALIKE";',
      Go:'// # Café 😀\\r\\npackage main', Rust:'/* # Café 😀\\r\\n /* nested documented */\\r\\n*/\\r\\nfn main() {}', Python:'# # Café 😀\\r\\nvalue = "# LOOKALIKE"\\r\\ndoc = """# LOOKALIKE"""',
      PHP:'<?php // # Café 😀\\r\\necho "// LOOKALIKE";', SQL:"-- # Café 😀\\r\\nSELECT '-- LOOKALIKE';", YAML:'# # Café 😀\\r\\nvalue: "# LOOKALIKE"',
    };
    const grammarResults = [];
    for (const [language, source] of Object.entries(grammarFixtures)) {
      const grammarHost = document.createElement('div'); grammarHost.style.cssText = 'width:600px;height:100px'; document.body.append(grammarHost);
      const grammarEditor = createSourceEditor({ parent:grammarHost, doc:source, language, mode:'source', richComments:true });
      await grammarEditor.languageReady;
      await new Promise(resolve => requestAnimationFrame(resolve));
      const maps = grammarEditor.getCommentSourceMap();
      grammarResults.push({ language, source:grammarEditor.getValue(), comments:maps.map(item => item.markdown.trim()) });
      grammarEditor.destroy(); grammarHost.remove();
    }
    const malformedHost = document.createElement('div'); malformedHost.style.cssText = 'width:600px;height:100px'; document.body.append(malformedHost);
    const malformedEditor = createSourceEditor({ parent:malformedHost, doc:'/* # Café 😀\\r\\nconst value = 1;', language:'JavaScript', mode:'source', richComments:true });
    await malformedEditor.languageReady;
    await new Promise(resolve => requestAnimationFrame(resolve));
    const malformedMap = malformedEditor.getCommentSourceMap();
    const malformedSource = malformedEditor.getValue();
    malformedEditor.destroy(); malformedHost.remove();
    grammarResults.push({ language:'JavaScript malformed', source:malformedSource, comments:malformedMap.map(item => item.markdown.trim()) });
    const richBytes = rich.getValue();
    rich.destroy(); richHost.remove();
    editor.destroy(); host.remove();
    const docs = [{ id:'note', name:'note.md', kind:'note', text:'alpha beta alpha\\nalpha beta' }];
    const saved = workspace.normalizeNotesWorkspace({ version:workspace.NOTES_WORKSPACE_VERSION, root:{ type:'group', tabs:[{ type:'leaf', id:'leaf', docId:'note', selection:initial }] }, activeLeafId:'leaf' }, docs, 'note');
    const persisted = workspace.serializeNotesWorkspace(saved);
    const restored = workspace.normalizeNotesWorkspace(JSON.parse(persisted), docs, 'note');
    const qualified = Object.fromEntries(['JavaScript', 'TypeScript JSX', 'C/C++ header', 'Java', 'Go', 'Rust', 'Python', 'CSS', 'HTML', 'XML', 'PHP', 'SQL', 'YAML', 'C#', 'Ruby', 'Shell', 'Kotlin', 'Swift', 'TOML', 'Mermaid', 'Dockerfile', 'SCSS'].map(language => [language, module.isRichCommentLanguageQualified(language)]));
    return { initial, inserted, undone, redone, selectedAll, allMatches, afterCommandEdit, afterCommandUndo, applied, reset, attachment, afterAttachmentUndo, duplicated, afterDuplicateUndo, restored:workspace.workspaceLeaves(restored)[0].selection, richBytes, commentMap, renderedComment:!!renderedComment, renderedCommentText, grammarResults, qualified };
  })()`);
  assert.deepEqual(result.initial.ranges, [{ anchor:0, head:5 }, { anchor:11, head:16 }]);
  assert.equal(result.initial.mainIndex, 1);
  assert.equal(result.inserted.text, 'X beta X\nalpha beta');
  assert.equal(result.inserted.selection.ranges.length, 2);
  assert.equal(result.undone.text, 'alpha beta alpha\nalpha beta');
  assert.equal(result.redone.text, 'X beta X\nalpha beta');
  assert.equal(result.selectedAll, true);
  assert.deepEqual(result.allMatches.ranges, [{ anchor:2, head:6 }, { anchor:15, head:19 }]);
  assert.equal(result.afterCommandEdit.text, 'X Q X\nalpha Q');
  assert.equal(result.afterCommandEdit.selection.ranges.length, 2);
  assert.equal(result.afterCommandUndo.text, result.redone.text);
  assert.deepEqual(result.afterCommandUndo.selection.ranges, result.allMatches.ranges);
  assert.deepEqual(result.applied.ranges, [{ anchor:1, head:1 }, { anchor:8, head:8 }]);
  assert.deepEqual(result.reset.ranges, [{ anchor:1, head:1 }, { anchor:8, head:8 }]);
  assert.equal(result.attachment.text, 'XATTACHbeta X\nalpha beta');
  assert.deepEqual(result.attachment.selection.ranges, [{ anchor:7, head:7 }, { anchor:13, head:13 }], 'attachment maps the edited range and preserves the extra cursor');
  assert.equal(result.afterAttachmentUndo.text, result.redone.text);
  assert.deepEqual(result.afterAttachmentUndo.selection.ranges, [{ anchor:1, head:2 }, { anchor:8, head:8 }]);
  assert.equal(result.duplicated.text, 'X beta X\nX beta X\nalpha beta');
  assert.deepEqual(result.duplicated.selection.ranges, result.afterAttachmentUndo.selection.ranges, 'duplicate line maps all extra cursors');
  assert.equal(result.afterDuplicateUndo.text, result.redone.text);
  assert.deepEqual(result.afterDuplicateUndo.selection.ranges, result.afterAttachmentUndo.selection.ranges, 'duplicate undo restores all ranges');
  assert.deepEqual(result.restored.ranges, result.initial.ranges);
  assert.equal(result.restored.mainIndex, result.initial.mainIndex);
  assert.equal(result.richBytes, '// # Heading\nconst literal = "// not a comment";\n/* **bold** */');
  assert.equal(result.renderedComment, true, 'opt-in parser comments use the renderer for inactive regions');
  assert.equal(result.renderedCommentText.trim(), '**bold**');
  for (const language of ['JavaScript', 'TypeScript JSX', 'C/C++ header', 'Java', 'Go', 'Rust', 'Python', 'CSS', 'HTML', 'XML', 'PHP', 'SQL', 'YAML']) assert.equal(result.qualified[language], true, `${language} rich comments should be qualified`);
  // S16: every advertised comment-capable language qualifies, including the
  // stream-adapter rows that previously stayed unadvertised.
  for (const language of ['C#', 'Ruby', 'Shell', 'Kotlin', 'Swift', 'TOML', 'Mermaid', 'Dockerfile', 'SCSS']) assert.equal(result.qualified[language], true, `${language} rich comments should be advertised`);
  assert.equal(result.grammarResults.length, 16);
  for (const fixture of result.grammarResults) {
    assert.equal(fixture.source.includes('Café 😀'), true, `${fixture.language} preserves Unicode source`);
    assert.equal(fixture.source.includes('\r\n'), true, `${fixture.language} preserves CRLF source`);
    assert.equal(fixture.comments.length > 0, true, `${fixture.language} recognizes a real comment`);
    assert.equal(fixture.comments[0].includes('Café 😀'), true, `${fixture.language} maps comment content`);
    assert.equal(fixture.comments.some(comment => comment.includes('LOOKALIKE')), false, `${fixture.language} excludes string comment lookalikes`);
  }
  assert.deepEqual(result.commentMap.map(item => item.markdown.trim()), ['# Heading', '**bold**']);
  assert.equal(result.commentMap[0].from, 0);
  assert.ok(result.commentMap[0].contentRanges[0].from > result.commentMap[0].from);
  console.log('Copal multi-range editor: insertion, undo/redo, selection migration, and workspace persistence passed.');
});
