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
  return createFilesBrowser({ ...options, surface:'editor', pickerMode:true });
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

export async function runFilesNavigation(browser, command) {
  if (!browser) return false;
  const snapshot = browser.captureCommands();
  const action = browser.commandDescriptors(snapshot).find(item => item.id === `files.${command}`);
  if (!action || action.disabledReason || !browser.isCommandCaptureCurrent(snapshot)) return false;
  return action.run();
}
