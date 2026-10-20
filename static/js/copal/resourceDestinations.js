// Presentation dispatch only. Resource refs remain opaque and provider-owned.
export function isChatResource(resource) {
  const row = resource?.resource || resource || {};
  const target = row.open_target || row.openTarget || resource?.target || {};
  return target.app === 'chat' || row.kind === 'chat' || row.navigationRole === 'chat'
    || row.provenance?.domain === 'chats' || /^chats(?::|$)/.test(String(row.provenance?.view || ''));
}

export function editorDestinationReason(resource) {
  if (isChatResource(resource)) return 'Chats belong in Chats or Library, outside Editor.';
  if (['folder', 'directory', 'virtual_folder', 'provider_root'].includes(resource?.kind)) return '';
  const app = resource?.target?.app || resource?.open_target?.app || resource?.openTarget?.app;
  if (app && !['editor', 'copal_notes'].includes(app)) {
    return `Open this item in ${destinationLabel(app)}; this dialog selects Editor documents.`;
  }
  return '';
}

export function destinationLabel(app) {
  return ({ editor:'Editor', copal_notes:'Editor', document_editor:'Documents', library:'Library', research:'Research', imps:'Image Editor', chat:'Chats' })[app] || String(app || 'its source applet');
}

export async function openExactDestination(app, resourceRef) {
  if (!resourceRef) throw new Error('An exact resource reference is required. Refresh this item.');
  if (app === 'editor' || app === 'copal_notes') {
    const editor = globalThis.copalModule || await import('../copal.js');
    if (typeof editor.openResource !== 'function') throw new Error('Editor exact-open is unavailable.');
    return editor.openResource(resourceRef);
  }
  if (app === 'imps') {
    const editor = globalThis.impsModule || await import('../imps.js');
    if (typeof editor.openResource !== 'function') throw new Error('Image Editor exact-open is unavailable.');
    return editor.openResource(resourceRef);
  }
  const library = globalThis.documentModule;
  const handler = app === 'document_editor' ? library?.openResource
    : ['chat', 'research', 'library'].includes(app) ? library?.openLibraryResource : null;
  if (typeof handler !== 'function') throw new Error(`${destinationLabel(app)} exact-open is unavailable. Open it from Files.`);
  return handler.call(library, resourceRef);
}

/** Only an advertised Editor payload may enter its handle normalizer. */
export async function dispatchFilesDestination(response, { resourceRef, surface = 'files', selectionOnly = false, openEditor } = {}) {
  const app = response?.target?.app;
  if (surface === 'editor' && (app === 'chat' || isChatResource(response))) throw new Error('Chats belong in Chats or Library, outside Editor.');
  const ref = response?.resource?.ref || response?.resource?.resource_ref || resourceRef;
  if (app === 'editor' || app === 'copal_notes') {
    if (typeof openEditor !== 'function') return openExactDestination(app, ref);
    const handle = response?.payload?.resource;
    if (!handle?.representation || !handle?.key) throw new Error('This item has no Editor document payload. Refresh the item or open its source applet.');
    return openEditor(handle, response.payload);
  }
  if (selectionOnly) throw new Error(editorDestinationReason(response) || 'This item is not an Editor document.');
  return openExactDestination(app, ref);
}
