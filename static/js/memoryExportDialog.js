// Memory export dialog + per-photo download (memory-export-controls S01,
// EC-D04/EC-D05). The single Export button now opens a small dialog that
// assembles the S00 query string via util/memoryExport.js; submitting with
// untouched defaults reproduces today's full-bundle download exactly.
//
// Dependencies (document/host/fetch/URL/toasts) are injected so the dialog
// is drivable from the node harness in tests/js — the same injection idiom
// as editor/thumbnailLoader.js. memory.js wires it with the real globals.

import {
  MEMORY_EXPORT_KINDS,
  MEMORY_EXPORT_SECTION_CHOICES,
  MemoryExportFilterError,
  buildMemoryExportQuery,
  memoryAssetUrl,
  memoryExportFilename,
} from './util/memoryExport.js';
import { responseError } from './util/httpError.js';

async function _downloadResponse({
  document: doc, fetchImpl, urlApi, showToast, showError,
}, url, filename, { okToast, failMessage }) {
  try {
    const response = await fetchImpl(url, { credentials: 'same-origin' });
    if (!response.ok) throw new Error((await responseError(response, failMessage)).message);
    const blob = await response.blob();
    const objectUrl = urlApi.createObjectURL(blob);
    const anchor = doc.createElement('a');
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.click();
    setTimeout(() => urlApi.revokeObjectURL(objectUrl), 0);
    showToast(okToast);
  } catch (error) {
    showError(error?.message || failMessage);
  }
}

// EC-D05 — single owner-scoped photo download. Errors surface through the
// shared toast/error helpers exactly like the bundle export path.
export async function downloadMemoryAsset(deps, assetId, filename) {
  await _downloadResponse(
    deps,
    memoryAssetUrl(assetId),
    String(filename || '').trim() || 'memory-photo',
    { okToast: 'Downloaded photo', failMessage: 'Photo download failed' },
  );
}

export function createMemoryExportDialog({
  document: doc,
  host,
  fetchImpl = fetch,
  urlApi = URL,
  showToast = () => {},
  showError = () => {},
} = {}) {
  if (!doc) throw new Error('createMemoryExportDialog needs a document');
  const overlay = doc.createElement('div');
  overlay.className = 'modal hidden';
  overlay.id = 'memory-export-modal';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-label', 'Export Memory');

  const content = doc.createElement('div');
  content.className = 'modal-content';

  const header = doc.createElement('div');
  header.className = 'modal-header';
  const title = doc.createElement('h2');
  title.textContent = 'Export Memory';
  const closeBtn = doc.createElement('button');
  closeBtn.className = 'close-btn';
  closeBtn.setAttribute('aria-label', 'Close export dialog');
  closeBtn.textContent = '✖';
  header.append(title, closeBtn);

  const body = doc.createElement('div');
  body.className = 'modal-body';

  const hint = doc.createElement('p');
  hint.className = 'memory-desc doclib-desc';
  hint.textContent = 'Defaults export everything as the canonical v3 bundle — narrow it only if you need to.';

  // Format: bundle (default, zip) or plain JSON (absent format param).
  const formatRow = doc.createElement('div');
  formatRow.className = 'memory-toolbar-row';
  const formatLabel = doc.createElement('span');
  formatLabel.className = 'memory-import-pending-text';
  formatLabel.textContent = 'Format';
  const formatBundle = doc.createElement('input');
  formatBundle.type = 'radio';
  formatBundle.name = 'memory-export-format';
  formatBundle.value = 'bundle';
  formatBundle.checked = true;
  const formatBundleLabel = doc.createElement('label');
  formatBundleLabel.append(formatBundle, doc.createTextNode(' bundle (zip)'));
  const formatJson = doc.createElement('input');
  formatJson.type = 'radio';
  formatJson.name = 'memory-export-format';
  formatJson.value = 'json';
  const formatJsonLabel = doc.createElement('label');
  formatJsonLabel.append(formatJson, doc.createTextNode(' JSON'));
  formatRow.append(formatLabel, formatBundleLabel, formatJsonLabel);

  // Sections: all checked = full export (param omitted).
  const sectionsField = doc.createElement('div');
  const sectionsLabel = doc.createElement('p');
  sectionsLabel.className = 'memory-desc doclib-desc';
  sectionsLabel.textContent = 'Sections';
  const sectionsBox = doc.createElement('div');
  const sectionBoxes = MEMORY_EXPORT_SECTION_CHOICES.map((section) => {
    const box = doc.createElement('input');
    box.type = 'checkbox';
    box.value = section;
    box.checked = true;
    const label = doc.createElement('label');
    label.append(box, doc.createTextNode(` ${section}`));
    sectionsBox.appendChild(label);
    return box;
  });
  sectionsField.append(sectionsLabel, sectionsBox);

  // Kinds: multi-select; nothing selected = every kind (param omitted).
  const kindsLabel = doc.createElement('p');
  kindsLabel.className = 'memory-desc doclib-desc';
  kindsLabel.textContent = 'Kinds (none selected = all)';
  const kindSelect = doc.createElement('select');
  kindSelect.multiple = true;
  kindSelect.className = 'memory-sort-select';
  kindSelect.setAttribute('aria-label', 'Export only these memory kinds');
  for (const kind of MEMORY_EXPORT_KINDS) {
    const option = doc.createElement('option');
    option.value = kind;
    option.textContent = kind;
    kindSelect.appendChild(option);
  }

  // Date range: date inputs produce ISO-8601 dates; 'to' is expanded to the
  // inclusive end of that day so a same-day range is not empty.
  const dateRow = doc.createElement('div');
  dateRow.className = 'memory-toolbar-row';
  const dateFrom = doc.createElement('input');
  dateFrom.type = 'date';
  dateFrom.className = 'memory-add-input';
  dateFrom.setAttribute('aria-label', 'Export memories recorded from this date');
  const dateTo = doc.createElement('input');
  dateTo.type = 'date';
  dateTo.className = 'memory-add-input';
  dateTo.setAttribute('aria-label', 'Export memories recorded until this date');
  dateRow.append(
    doc.createTextNode('From '), dateFrom,
    doc.createTextNode(' to '), dateTo,
  );

  // Content match: backend substring filter `q`.
  const qInput = doc.createElement('input');
  qInput.type = 'text';
  qInput.className = 'memory-add-input';
  qInput.placeholder = 'Content contains…';
  qInput.setAttribute('aria-label', 'Export only memories containing this text');

  // Archived toggle: on by default (backend default), off sends
  // include_archived=false.
  const archivedLabel = doc.createElement('label');
  archivedLabel.className = 'admin-switch';
  archivedLabel.title = 'Include archived memories';
  const archivedToggle = doc.createElement('input');
  archivedToggle.type = 'checkbox';
  archivedToggle.checked = true;
  const archivedSlider = doc.createElement('span');
  archivedSlider.className = 'admin-slider';
  archivedLabel.append(archivedToggle, archivedSlider);
  const archivedRow = doc.createElement('div');
  archivedRow.className = 'memory-toolbar-row';
  const archivedText = doc.createElement('span');
  archivedText.className = 'memory-import-pending-text';
  archivedText.textContent = 'Include archived';
  archivedRow.append(archivedLabel, archivedText);

  // Asset mode: bundle-only controls, disabled for JSON exports.
  const assetsRow = doc.createElement('div');
  assetsRow.className = 'memory-toolbar-row';
  const assetsText = doc.createElement('span');
  assetsText.className = 'memory-import-pending-text';
  assetsText.textContent = 'Photo assets';
  const assetsSelect = doc.createElement('select');
  assetsSelect.className = 'memory-sort-select';
  assetsSelect.setAttribute('aria-label', 'Photo assets to include in the bundle');
  for (const [value, label] of [['all', 'all'], ['images', 'images only'], ['none', 'none']]) {
    const option = doc.createElement('option');
    option.value = value;
    option.textContent = label;
    assetsSelect.appendChild(option);
  }
  const imagesOnlyBtn = doc.createElement('button');
  imagesOnlyBtn.className = 'memory-toolbar-btn';
  imagesOnlyBtn.textContent = 'Images only';
  imagesOnlyBtn.title = 'Download just the photo assets as a restorable bundle';
  assetsRow.append(assetsText, assetsSelect, imagesOnlyBtn);

  const syncAssetControls = () => {
    const bundle = formatBundle.checked;
    assetsSelect.disabled = !bundle;
    imagesOnlyBtn.disabled = !bundle;
  };
  formatBundle.addEventListener('change', syncAssetControls);
  formatJson.addEventListener('change', syncAssetControls);

  const errorLine = doc.createElement('small');
  errorLine.className = 'memory-import-error hidden';
  errorLine.setAttribute('role', 'alert');

  const actions = doc.createElement('div');
  actions.className = 'memory-suggestions-actions';
  const submitBtn = doc.createElement('button');
  submitBtn.className = 'memory-item-btn save';
  submitBtn.textContent = 'Export';
  const cancelBtn = doc.createElement('button');
  cancelBtn.className = 'memory-item-btn';
  cancelBtn.textContent = 'Cancel';
  actions.append(submitBtn, cancelBtn);

  body.append(
    hint, formatRow, sectionsField, kindsLabel, kindSelect,
    dateRow, qInput, archivedRow, assetsRow, errorLine, actions,
  );
  content.append(header, body);
  overlay.appendChild(content);
  (host || doc.body)?.appendChild?.(overlay);

  function _showDialogError(message) {
    errorLine.textContent = message;
    errorLine.classList.remove('hidden');
  }
  function _hideDialogError() {
    errorLine.textContent = '';
    errorLine.classList.add('hidden');
  }

  function _readOptions() {
    const sections = sectionBoxes.every((box) => box.checked)
      ? null
      : sectionBoxes.filter((box) => box.checked).map((box) => box.value);
    const kinds = Array.from(kindSelect.selectedOptions || [])
      .map((option) => option.value);
    const from = String(dateFrom.value || '').trim();
    const to = String(dateTo.value || '').trim();
    return {
      format: formatBundle.checked ? 'bundle' : 'json',
      sections,
      kinds: kinds.length ? kinds : null,
      since: from || null,
      // Inclusive whole-day upper bound for a date-only 'to'.
      until: to ? `${to}T23:59:59Z` : null,
      q: qInput.value,
      includeArchived: archivedToggle.checked,
      assets: assetsSelect.value,
      assetsOnly: false,
    };
  }

  async function _submit(presetOptions) {
    let options;
    let query;
    try {
      options = presetOptions || _readOptions();
      query = buildMemoryExportQuery(options);
    } catch (error) {
      if (error instanceof MemoryExportFilterError) {
        // Client-side mirror of the backend's typed 422 — the request never
        // leaves the dialog when the backend would reject it.
        _showDialogError(error.message);
        return;
      }
      throw error;
    }
    _hideDialogError();
    submitBtn.disabled = true;
    try {
      const bundle = options.format !== 'json';
      await _downloadResponse(
        { document: doc, fetchImpl, urlApi, showToast, showError },
        `/api/memory/export${query}`,
        memoryExportFilename(options),
        {
          okToast: bundle ? 'Exported the canonical Memory bundle' : 'Exported Memory as JSON',
          failMessage: 'Memory export failed',
        },
      );
    } finally {
      submitBtn.disabled = false;
    }
    close();
  }

  submitBtn.addEventListener('click', () => { _submit(); });
  imagesOnlyBtn.addEventListener('click', () => {
    // Shortcut: restorable bundle with only the image asset bytes.
    formatBundle.checked = true;
    formatJson.checked = false;
    assetsSelect.value = 'images';
    syncAssetControls();
    _submit({ ..._readOptions(), format: 'bundle', assets: 'images', assetsOnly: true });
  });
  cancelBtn.addEventListener('click', () => close());
  closeBtn.addEventListener('click', () => close());

  function open() {
    _hideDialogError();
    syncAssetControls();
    overlay.classList.remove('hidden');
  }
  function close() {
    overlay.classList.add('hidden');
  }

  return {
    open,
    close,
    element: overlay,
    // Exposed for wiring tests; not part of the user-facing surface.
    controls: {
      formatBundle, formatJson, sectionBoxes, kindSelect, dateFrom, dateTo,
      qInput, archivedToggle, assetsSelect, imagesOnlyBtn, submitBtn,
      cancelBtn, closeBtn, errorLine,
    },
  };
}
