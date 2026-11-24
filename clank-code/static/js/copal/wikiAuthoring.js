import { wireDialog } from './overlays.js';
import { resolveDocumentLink } from './notesModel.js';
import { resolveReference, mediaKind } from './markdownResources.js';

// All edits enter the captured shared CodeMirror transaction stream. This
// controller owns presentation only: no content cache, save API or serializer.
export function createWikiAuthoring({ h, capture, isCurrent, documents, notify, insertMedia }) {
  const status = message => notify?.(message);
  function editable(target) {
    if (!target || target.readOnly || !target.editor || !isCurrent(target)) {
      status('Open an editable page in Rich or Source view first.');
      return false;
    }
    return true;
  }
  function action(run) {
    const target = capture();
    if (editable(target)) run(target.editor, target);
  }
  function button(label, run, text = label) {
    return h('button', { type:'button', class:'copal-wiki-format', title:label, 'aria-label':label,
      text, onmousedown:event => event.preventDefault(), onclick:run });
  }
  function lines(prefix, heading = false) {
    action(editor => {
      const view = editor.view;
      const selection = view.state.selection.main;
      const start = view.state.doc.lineAt(selection.from);
      const end = view.state.doc.lineAt(selection.to);
      const text = view.state.doc.sliceString(start.from, end.to);
      const next = text.split('\n').map(line => `${prefix}${heading ? line.replace(/^#{1,6}\s+/, '') : line}`).join('\n');
      editor.replaceRange(start.from, end.to, next);
      editor.focus();
    });
  }
  function dialog(title, build, apply) {
    const target = capture();
    if (!editable(target)) return;
    const form = h('form', { class:'copal-wiki-insert-form' });
    const modal = h('dialog', { class:'copal-dialog copal-wiki-dialog' }, h('h2', { text:title }), form);
    const fields = build(form, target);
    const error = h('p', { class:'copal-wiki-insert-error', role:'alert' });
    form.append(error, h('footer', { class:'copal-dialog-actions' },
      h('button', { type:'button', class:'copal-btn', text:'Cancel', onclick:() => modal.close() }),
      h('button', { type:'submit', class:'copal-btn', text:'Insert' })));
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (!editable(target)) { modal.close(); return; }
      const result = apply(fields, target);
      if (result?.error) { error.textContent = result.error; return; }
      modal.close(); target.editor.focus();
    });
    modal.addEventListener('close', () => modal.remove(), { once:true });
    wireDialog(modal); document.body.append(modal); modal.showModal();
    form.querySelector('input,select')?.focus();
  }
  function field(form, label, attrs) {
    const input = h('input', attrs);
    form.append(h('label', {}, h('span', { text:label }), input));
    return input;
  }
  function link() {
    dialog('Insert link', (form, target) => {
      const label = field(form, 'Link text', { value:target.editor.getSelectedText(), required:true });
      const mode = h('select', {}, h('option', { value:'page', text:'Wiki page' }), h('option', { value:'url', text:'Web address' }));
      const page = h('select', {}, (documents() || []).filter(doc => doc.id !== target.docId)
        .map(doc => h('option', { value:doc.id, text:doc.name || doc.id })));
      const address = field(form, 'Web address', { type:'url', placeholder:'https://…', hidden:true });
      form.append(h('label', {}, h('span', { text:'Link target' }), mode), h('label', {}, h('span', { text:'Page' }), page));
      mode.addEventListener('change', () => { page.parentElement.hidden = mode.value !== 'page'; address.parentElement.hidden = mode.value !== 'url'; address.hidden = mode.value !== 'url'; });
      address.parentElement.hidden = true;
      return { label, mode, page, address };
    }, ({ label, mode, page, address }, target) => {
      const caption = label.value.trim();
      if (!caption) return { error:'Enter link text.' };
      if (mode.value === 'page') {
        if (/[\]\n|]/.test(caption)) return { error:'Wiki link text cannot contain a closing bracket, vertical bar or newline.' };
        const doc = documents().find(item => item.id === page.value);
        if (!doc) return { error:'This page is no longer available.' };
        if (resolveDocumentLink(documents(), doc.name)?.id !== doc.id || /[\]\n|]/.test(doc.name)) return { error:'This page name is ambiguous or cannot be expressed as a Wiki link. Rename it before linking.' };
        target.editor.insertText(`[[${doc.name}|${caption}]]`);
      } else {
        let url;
        try { url = new URL(address.value); } catch { return { error:'Enter a complete web address.' }; }
        if (!['https:', 'http:', 'mailto:'].includes(url.protocol)) return { error:'Use an HTTP, HTTPS or mail address.' };
        target.editor.insertText(`[${caption.replace(/[\\\[\]]/g, '\\$&')}](${url.href.replace(/[()]/g, encodeURIComponent)})`);
      }
    });
  }
  function table() {
    dialog('Insert table', form => ({
      columns:field(form, 'Columns', { type:'number', min:1, max:12, value:3, required:true }),
      rows:field(form, 'Body rows', { type:'number', min:1, max:30, value:3, required:true }),
    }), ({ columns, rows }, target) => {
      const count = Number(columns.value), height = Number(rows.value);
      if (!Number.isInteger(count) || count < 1 || count > 12 || !Number.isInteger(height) || height < 1 || height > 30) return { error:'Choose 1–12 columns and 1–30 rows.' };
      const row = values => `| ${values.join(' | ')} |`;
      const source = [row(Array.from({ length:count }, (_, i) => `Column ${i + 1}`)), row(Array(count).fill('---')), ...Array.from({ length:height }, () => row(Array(count).fill('')))];
      target.editor.insertText(`\n${source.join('\n')}\n`);
    });
  }
  function media() {
    dialog('Insert media', (form, target) => {
      const assets = documents().filter(doc => doc.kind === 'asset');
      const selected = h('select', { 'aria-label':'Existing adopted media' },
        ...assets.map(doc => h('option', { value:doc.id, text:`${doc.name} · ${mediaKind(doc)}` })));
      form.append(h('label', {}, h('span', { text:'Existing adopted asset' }), selected));
      if (!assets.length) form.append(h('p', { text:'No adopted assets in this workspace yet. Upload a file, paste media, or drag an authorized item from Files.' }));
      const upload = h('button', { type:'button', class:'copal-btn', text:'Upload a new file…', onclick:() => {
        if (!editable(target)) return;
        form.closest('dialog').close(); insertMedia(target);
      } });
      form.append(upload);
      const caption = field(form, 'Caption / alternative text', { value:'' });
      return { selected, caption };
    }, ({ selected, caption }, target) => {
      const asset = documents().find(doc => doc.id === selected.value && doc.kind === 'asset');
      if (!asset) return { error:'Choose an existing asset or upload a file.' };
      if (/[\]\n|]/.test(asset.name) || /[\]\n|]/.test(caption.value)) return { error:'The asset name or caption cannot be expressed as a Wiki media reference.' };
      const resolved = resolveReference({ target:asset.name }, { documents:documents(), origin:target.doc });
      if (resolved.status !== 'resolved' || resolved.target?.id !== asset.id) return { error:'This asset name is ambiguous in the current page. Rename it before inserting.' };
      target.editor.insertText(`![[${asset.name}${caption.value ? `|${caption.value}` : ''}]]`);
    });
  }
  function toolbar() {
    return h('div', { class:'copal-wiki-authoring', role:'toolbar', 'aria-label':'Page formatting' },
      button('Heading 1', () => lines('# ', true), 'H1'), button('Heading 2', () => lines('## ', true), 'H2'), button('Heading 3', () => lines('### ', true), 'H3'),
      button('Bold', () => action(editor => editor.formatSelections('**')), 'B'),
      button('Italic', () => action(editor => editor.formatSelections('*')), 'I'),
      button('Strikethrough', () => action(editor => editor.formatSelections('~~')), 'S̶'),
      button('Inline code', () => action(editor => editor.formatSelections('`')), 'Code'),
      button('Bullet list', () => lines('- '), '• List'), button('Numbered list', () => lines('1. '), '1. List'),
      button('Checklist', () => lines('- [ ] '), '☑ List'), button('Quote', () => lines('> '), 'Quote'),
      button('Link', link), button('Table', table), button('Media', media),
      button('Undo', () => action(editor => editor.undo())), button('Redo', () => action(editor => editor.redo())));
  }
  return { toolbar };
}
