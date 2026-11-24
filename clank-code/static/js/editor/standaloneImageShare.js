import { wireDialog } from '../copal/overlays.js';
import { openFilesShare } from '../filesShare.js';
import ui from '../ui.js';

const MAX_PNG_BYTES = 16 * 1024 * 1024;

function element(tag, text) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  return node;
}

export async function identifyImageShareSnapshot(snapshot) {
  const blob = snapshot?.blob;
  if (!(blob instanceof Blob) || blob.type !== 'image/png' || blob.size < 1 || blob.size > MAX_PNG_BYTES) {
    throw new Error('Choose a flattened PNG up to 16 MiB.');
  }
  const hash = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
  const hex = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
  return Object.freeze({ snapshot, expectedRevision:{ kind:'hostFingerprint', value:`sha256:${hex}:${blob.size}` } });
}

export function openStandaloneImageShare({draft, client, current, revisionCurrent, chooseParent, status}) {
  const review = draft.review;
  if (!current() || !review || (!draft.attempt && !revisionCurrent())) throw new Error('The original image changed.');
  if (draft.dialog?.open) return draft.dialog;

  const dialog = element('dialog');
  dialog.className = 'copal-dialog';
  dialog.setAttribute('aria-label', 'Share standalone PNG');
  const heading = element('h2', 'Share standalone PNG');
  const intro = element('p', 'Review the flattened image. To redact anything, return to the image and edit it before sharing.');
  const preview = element('img');
  preview.alt = 'Exact PNG preview';
  preview.hidden = true;
  preview.style.maxWidth = '100%';
  preview.style.maxHeight = '48vh';
  preview.style.objectFit = 'contain';
  const name = element('input');
  name.value = draft.name;
  name.maxLength = 200;
  const nameLabel = element('label', 'Download file name');
  nameLabel.append(name);
  const message = element('p');
  message.setAttribute('role', 'status');
  const footer = element('footer');
  footer.className = 'copal-dialog-actions';

  function button(label, run) {
    const node = element('button', label);
    node.type = 'button';
    node.className = 'copal-btn';
    node.addEventListener('click', run);
    footer.append(node);
    return node;
  }

  let displayed = false;
  let url = null;
  const alive = () => dialog.isConnected && dialog.open && current();
  const sameRevision = value => value?.kind === review.expectedRevision.kind && value.value === review.expectedRevision.value;

  function update() {
    const fresh = revisionCurrent();
    preview.hidden = !displayed || !fresh;
    const saved = draft.saved?.name === name.value.trim();
    name.disabled = draft.busy || Boolean(draft.attempt) || !fresh;
    save.textContent = draft.attempt ? 'Check private copy' : 'Save reviewed private copy';
    save.disabled = draft.busy || (draft.attempt ? false : !displayed || !fresh || saved);
    share.disabled = draft.busy || !displayed || !fresh || !saved;
  }

  const save = button('Save reviewed private copy', async () => {
    if (!alive() || draft.busy || (!draft.attempt && (!displayed || !revisionCurrent()))) return;
    draft.busy = true;
    update();
    try {
      let receipt;
      let attempt = draft.attempt;
      if (attempt) {
        receipt = await client.operationReceipt(attempt.id, {generation:attempt.generation});
      } else {
        const filename = name.value.trim();
        if (!filename.toLowerCase().endsWith('.png') || filename.length > 200 ||
            /[\/\u0000-\u001f\u007f]/.test(filename) || filename.includes(String.fromCharCode(92))) {
          throw new Error('Use a plain file name ending in .png.');
        }
        const destination = await chooseParent(() => alive() && revisionCurrent());
        if (!alive() || !revisionCurrent()) throw new Error('The image changed. Review a fresh PNG.');
        if (!destination) return;
        const response = await client.stat(destination);
        const parent = response?.resource || response;
        if (!alive() || !revisionCurrent() || parent?.provider !== 'host' || parent.kind !== 'folder' ||
            !parent.ref || !parent.capabilities?.includes('write') || !Number.isSafeInteger(parent.policy_generation)) {
          throw new Error('The image or private destination changed.');
        }
        attempt = {id:'standalone-png-'+crypto.randomUUID(), generation:parent.policy_generation, name:filename};
        draft.attempt = attempt;
        try {
          receipt = await client.importFile(review.snapshot.blob, {
            operationId:attempt.id, itemId:attempt.id, generation:attempt.generation,
            destinationRef:parent.ref, name:filename, collision:'fail',
          });
        } catch (error) {
          if (['invalid_resource_request','resource_ref_stale','policy_denied','unsupported_provider_kind','import_too_large'].includes(error?.code)
              && Number(error?.status) >= 400 && Number(error.status) < 500) {
            draft.attempt = null;
            throw error;
          }
          throw new Error('Private copy outcome is unknown. Check the original request.');
        }
      }
      const item = receipt?.items?.find(entry => entry.item_id === attempt.id);
      if (!item || receipt.state === 'pending' || item.outcome === 'pending' ||
          (item.outcome === 'failed' && ['mutation_outcome_unknown','policy_generation_changed','provider_unavailable'].includes(item.code))) {
        throw new Error('Private copy outcome is pending or unknown. Check the original request and the selected Files folder.');
      }
      if (!['committed','unchanged'].includes(item.outcome)) {
        draft.attempt = null;
        throw new Error(item.outcome === 'conflict' ? 'That file name already exists. Choose another.' : 'The private PNG was not saved.');
      }
      if (receipt.state !== 'complete' || !item.resource_key || !item.resource_ref || !sameRevision(item.revision)) {
        draft.attempt = null;
        throw new Error('A Files copy was recorded. Its exact reviewed PNG revision could not be verified.');
      }
      if (!alive()) return;
      if (!revisionCurrent()) {
        draft.attempt = null;
        message.textContent = 'The previous PNG copy was saved in Files. Reopen Share to review the current image.';
        return;
      }
      const response = await client.stat(item.resource_ref);
      const row = response?.resource || response;
      if (!alive()) return;
      if (row?.provider !== 'host' || row.kind !== 'file' || row.id !== item.resource_key || !row.ref ||
          !sameRevision(row.revision) || !row.capabilities?.includes('download')) {
        draft.attempt = null;
        throw new Error('The private copy is saved in Files. Its reviewed bytes are unavailable.');
      }
      draft.saved = {name:attempt.name, provider:'host', kind:'file',
        resource_ref:row.ref, resource_id:row.id, revision:row.revision};
      draft.attempt = null;
      message.textContent = revisionCurrent()
        ? 'Private copy saved. Review the audience before sharing it.'
        : 'Private copy saved in Files. Reopen Share to review the current image.';
    } catch (error) {
      if (alive()) message.textContent = error?.message || 'The private PNG could not be verified.';
    } finally {
      draft.busy = false;
      if (alive()) update();
    }
  });
  const share = button('Share reviewed copy', async () => {
    if (!alive() || !revisionCurrent() || !displayed || draft.busy || draft.saved?.name !== name.value.trim()) return;
    const entry = draft.saved;
    dialog.close();
    try {
      await openFilesShare({entry, client, current:() => current() && revisionCurrent(), ui, status, fixedSnapshot:true});
    } catch (error) {
      if (current()) status(error?.message || 'PNG sharing is unavailable.', true);
    }
  });
  button('Close', () => dialog.close());
  name.addEventListener('input', () => { draft.name = name.value; update(); });

  const scopeListeners = new AbortController();
  const checkScope = () => {
    if (!dialog.open) return;
    if (!current()) dialog.close();
    else if (!revisionCurrent()) {
      update();
      message.textContent = draft.attempt
        ? 'The image changed. You can check the previous private copy request.'
        : 'The image changed. Reopen Share to review a fresh PNG.';
    }
  };
  for (const target of [document, window]) {
    for (const type of ['openclank:editor-feedback-scope-changed','openclank-window-closed','openclank-account-changed',
      'openclank:auth-context-changed','openclank:file-policy-changed','workspace-change']) {
      target.addEventListener(type, checkScope, {signal:scopeListeners.signal});
    }
  }
  dialog.addEventListener('close', () => {
    scopeListeners.abort();
    if (url) URL.revokeObjectURL(url);
    if (draft.dialog === dialog) draft.dialog = null;
  }, {once:true});
  dialog.append(heading, intro, preview, nameLabel, message, footer);
  draft.dialog = dialog;
  wireDialog(dialog);
  document.body.append(dialog);
  dialog.showModal();

  if (revisionCurrent()) {
    message.textContent = 'Preparing the PNG preview…';
    url = URL.createObjectURL(review.snapshot.blob);
    preview.src = url;
    void preview.decode().then(() => {
      if (!alive()) return;
      if (!revisionCurrent()) { checkScope(); return; }
      displayed = true;
      preview.hidden = false;
      message.textContent = `${preview.naturalWidth} × ${preview.naturalHeight} pixels`;
      update();
    }, () => {
      if (alive()) message.textContent = 'The exact PNG preview could not be displayed.';
    });
  } else {
    checkScope();
  }
  update();
  return dialog;
}
