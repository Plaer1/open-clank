import { createWikiAuthoring } from './wikiAuthoring.js';
import { linkedMentions } from './notesModel.js';

let stylesInstalled = false;
function installStyles() {
  if (stylesInstalled) return;
  stylesInstalled = true;
  const link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = '/static/css/copalWiki.css';
  document.head.append(link);
}

/** Wiki presentation over the shared Editor dispatcher. Documents, buffers,
 * selections, pins, recent history and native properties remain core-owned. */
export function createWikiWorkspace({ h, core }) {
  installStyles();
  let query = '', collection = 'all';
  const authoring = createWikiAuthoring({ h,
    capture:() => core.capture(), isCurrent:target => core.isCurrent(target),
    documents:() => core.documents(), notify:message => core.notify(message),
    insertMedia:target => core.insertMedia(target),
  });
  function button(text, run, attrs = {}) {
    return h('button', { type:'button', class:'copal-btn', text, onclick:run, ...attrs });
  }
  function pages() {
    const docs = core.documents().filter(doc => doc.kind === 'wiki' || doc.recordKind === 'wikiArticle' || core.isOfficial?.(doc));
    const ids = collection === 'pinned' ? core.pinned() : collection === 'recent' ? core.recent() : null;
    return (ids ? ids.map(id => docs.find(doc => doc.id === id)).filter(Boolean) : docs)
      .filter(doc => `${doc.name}\n${doc.text || ''}\n${JSON.stringify(doc.properties || {})}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()))
      .sort((a, b) => ids ? ids.indexOf(a.id) - ids.indexOf(b.id) : String(a.name).localeCompare(String(b.name)));
  }
  function renderNavigation(nav) {
    const current = core.capture();
    const list = h('div', { class:'copal-wiki-pages', role:'list', 'aria-label':'Wiki pages' });
    for (const doc of pages()) list.append(h('button', { type:'button', class:'copal-wiki-page', role:'listitem',
      'aria-current':doc.id === current?.docId ? 'page' : null, onclick:() => core.open(doc.id) },
      h('strong', { text:doc.name }), h('span', { text:doc.readOnly || doc.builtin ? 'Read-only' : doc.properties?.summary || 'Article' })));
    if (!list.childElementCount) list.append(h('p', { class:'copal-empty-inline', text:query ? 'No matching pages.' : 'No pages in this collection.' }));
    nav.replaceChildren(list);
  }
  function chrome() {
    const target = core.capture();
    const nav = h('div', { class:'copal-wiki-navigation-results' });
    const search = h('input', { type:'search', class:'copal-wiki-search', placeholder:'Search pages and content', 'aria-label':'Search Wiki', value:query });
    search.addEventListener('input', () => { query = search.value; renderNavigation(nav); });
    const collections = h('div', { class:'copal-wiki-collections', role:'group', 'aria-label':'Page collection' });
    for (const [id, label] of [['all','All pages'], ['recent','Recent'], ['pinned','Pinned']]) collections.append(button(label, () => {
      collection = id;
      for (const child of collections.children) child.setAttribute('aria-pressed', String(child.dataset.collection === id));
      renderNavigation(nav);
    }, { 'data-collection':id, 'aria-pressed':String(collection === id) }));
    const aside = h('aside', { class:'copal-wiki-navigation', 'aria-label':'Wiki navigation' },
      h('header', {}, h('strong', { text:'Wiki' }), button('New page', () => core.create())), search, collections, nav);
    renderNavigation(nav);
    const toolbar = h('div', { class:'copal-wiki-page-actions' },
      h('strong', { class:'copal-wiki-title', text:target?.name || 'Choose a page' }),
      ...[['live','Rich'], ['source','Source'], ['reading','Read']].map(([mode,label]) => button(label, () => core.mode(mode), { 'aria-pressed':String(target?.mode === mode) })),
      button(core.pinned().includes(target?.docId) ? 'Unpin' : 'Pin', () => target?.docId && core.pin(target.docId), { disabled:!target?.docId }),
      button('Save', () => core.save(), { disabled:!target?.docId || target.readOnly }),
      button('History', () => target?.docId && core.history(target.docId), { disabled:!target?.docId }),
      button('Open in Editor', () => target?.docId && core.openEditor(target.docId), { disabled:!target?.docId }));
    const tools = h('div', { class:'copal-wiki-tools' }, toolbar);
    if (target?.readOnly) tools.append(h('div', { class:'copal-wiki-readonly' },
      h('span', { text:'This article is read-only.' }),
      ...(core.canCopy?.(target.doc) ? [button('Make editable copy', () => core.copy(target.doc))] : [])));
    else if (target?.editor) tools.append(authoring.toolbar());
    const backlinks = h('details', { class:'copal-wiki-backlinks' }, h('summary', { text:'Backlinks' }));
    const incoming = target?.doc ? linkedMentions(core.documents(), target.doc) : [];
    for (const mention of incoming) backlinks.append(button(mention.doc.name, () => core.open(mention.doc.id)));
    if (!incoming.length) backlinks.append(h('p', { text:'No recorded incoming links.' }));
    const menu = h('details', { class:'copal-wiki-library-menu' }, h('summary', { text:'Library' }),
      button('Import .memes', () => core.importMemes()), button('Export .memes', () => core.exportMemes()),
      button('Page properties', () => core.properties()), button('Links and relations', () => core.links()));
    tools.append(h('div', { class:'copal-wiki-library-actions' }, backlinks, menu));
    return { aside, tools };
  }
  // Called after core render. Never replace the core editor node or reconstruct
  // it when typing; its mount/unmount and selection ownership stay in the core.
  function decorate(shell) {
    if (!shell) return;
    shell.classList.add('copal-wiki-workspace');
    shell.querySelector(':scope > .copal-wiki-navigation')?.remove();
    shell.querySelector(':scope > .copal-wiki-tools')?.remove();
    const { aside, tools } = chrome();
    shell.prepend(tools, aside);
  }
  return { decorate };
}
