// Presentation adapter only: Files owns trees, Places, views and navigation.
// Load that browser lazily so resource normalization stays usable without DOM.
export function installEditorFilesStyles() {
  if (document.querySelector('[data-editor-files-workbench-style]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../../css/editorFilesWorkbench.css', import.meta.url).href;
  link.dataset.editorFilesWorkbenchStyle = '';
  document.head.append(link);
}

export async function mountEditorFilesBrowser(options, isCurrent = () => true) {
  installEditorFilesStyles();
  const { createFilesBrowser } = await import('../files.js');
  if (!isCurrent()) return null;
  return createFilesBrowser({ ...options, surface:'editor', pickerMode:true, editorRootProjection:true });
}

// Files columns use resourceId; normalization still owns the complete key.
export function filesBrowserResource(row) {
  if (!row) return null;
  return {
    ...row,
    ref:row.ref || row.resource_ref || row.resourceRef,
    id:row.id || row.resource_id || row.resourceId,
    capabilities:Array.isArray(row.capabilities) ? row.capabilities : Object.keys(row.capabilities || {}).filter(key => row.capabilities[key] === true),
  };
}

// Restore navigation using freshly listed folders. Stored IDs identify rows;
// only a new ref from the current Files session can authorize the next read.
export async function resolveEditorHostFolder(client, savedFolder, trail, signal) {
  const id = savedFolder?.resourceId;
  if (!id) return null;
  const steps = Array.isArray(trail) && trail.length <= 64 && trail.at(-1)?.id === id &&
    trail.every(step => typeof step?.id === 'string' && typeof step.name === 'string')
    ? trail : [{id, name:''}];
  const [catalog, places] = await Promise.all([client.roots({signal}), client.places({signal})]);
  const host = catalog.entries.find(row => row.provider === 'host' && row.kind === 'provider_root');
  if (!host) {
    if (catalog.providers?.host?.available === false) throw new Error('Host folders are temporarily unavailable.');
    return null;
  }
  const folder = row => row.provider === 'host' && row.kind === 'folder' && row.capabilities?.includes('children');
  const anchors = places.entries.filter(folder);
  let cursor = null;
  do {
    const page = await client.children(host.ref, {signal, cursor, limit:200});
    anchors.push(...page.entries.filter(folder));
    cursor = page.next_cursor;
  } while (cursor);
  const start = steps.findLastIndex(step => anchors.some(row => row.id === step.id));
  if (start < 0) return null;
  let current = anchors.find(row => row.id === steps[start].id);
  for (const step of steps.slice(start + 1)) {
    const parent = current;
    do {
      const page = await client.children(parent.ref, {signal, cursor, query:step.name, limit:200});
      current = page.entries.find(row => row.id === step.id && folder(row));
      cursor = page.next_cursor;
    } while (!current && cursor);
    if (!current) return null;
    cursor = null;
  }
  return current;
}

export async function runFilesNavigation(browser, command) {
  if (!browser) return false;
  const snapshot = browser.captureCommands();
  const action = browser.commandDescriptors(snapshot).find(item => item.id === `files.${command}`);
  if (!action || action.disabledReason || !browser.isCommandCaptureCurrent(snapshot)) return false;
  return action.run();
}
