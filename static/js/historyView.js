import { createOpenClankWindow } from './copal/windows.js';

const PAGE_LIMIT = 50;
const READ_CHUNK_BYTES = 64 * 1024;
const PREVIEW_LIMIT_BYTES = 256 * 1024;

function element(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') node.textContent = String(value ?? '');
    else if (key === 'class') node.className = String(value ?? '');
    else if (key === 'onclick' && typeof value === 'function') node.addEventListener('click', value);
    else if (typeof value === 'boolean') { if (value) node.setAttribute(key, ''); }
    else if (value != null) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function actionButton(label, action, { disabled = false, focusKey = '' } = {}) {
  const button = element('button', {
    type: 'button',
    class: 'copal-btn',
    text: label,
    onclick: action,
    ...(focusKey ? { 'data-history-focus': focusKey } : {}),
  });
  button.disabled = disabled;
  return button;
}

function millisLabel(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return 'Time unavailable';
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? 'Time unavailable' : date.toLocaleString();
}

function descriptorLabel(version, fallback) {
  const operation = String(version?.operation || 'change').replaceAll('_', ' ');
  const phase = String(version?.phase || 'version').replaceAll('_', ' ');
  const state = String(version?.action_state || 'state unavailable').replaceAll('_', ' ');
  return operation + ' · ' + phase + ' · ' + state;
}

class ResourceHistoryView {
  constructor() {
    this.generation = 0;
    this.target = null;
    this.pendingRestoreVerification = null;
    this.items = [];
    this.nextCursor = null;
    this.pageCursors = [null];
    this.pageIndex = 0;
    this.selectedRef = '';
    this.selectedVersion = null;
    this.listBusy = false;
    this.listError = '';
    this.previewBusy = false;
    this.previewError = '';
    this.previewText = '';
    this.previewNote = '';
    this.restorePreview = null;
    this.restorePreviewBusy = false;
    this.restoring = false;
    this.contextInvalid = false;
    this.statusMessage = '';
    this.statusBad = false;
    this.listController = null;
    this.previewController = null;
    this.restoreController = null;
    this.restorePreviewController = null;
    this.previewGeneration = 0;
    this.window = createOpenClankWindow({
      id: 'history-window',
      label: 'History',
      subtitle: 'Files · Version history',
      minWidth: 720,
      minHeight: 520,
      className: 'history-resource-window',
      onBeforeClose: () => {
        if (this.restoring) {
          this.statusMessage = 'A restore is in progress for the selected file.';
          this.statusBad = true;
          this.render();
          return false;
        }
        this.reset();
        return true;
      },
      onClosed: () => this.reset(),
    });
  }

  reset() {
    this.generation += 1;
    this.cancelReads();
    this.target = null;
    this.pendingRestoreVerification = null;
    this.items = [];
    this.nextCursor = null;
    this.pageCursors = [null];
    this.pageIndex = 0;
    this.selectedRef = '';
    this.selectedVersion = null;
    this.listBusy = false;
    this.listError = '';
    this.previewBusy = false;
    this.previewError = '';
    this.previewText = '';
    this.previewNote = '';
    this.restorePreview = null;
    this.restorePreviewBusy = false;
    this.contextInvalid = false;
    this.statusMessage = '';
    this.statusBad = false;
  }

  cancelReads() {
    this.listController?.abort();
    this.previewController?.abort();
    this.restoreController?.abort();
    this.restorePreviewController?.abort();
    this.listController = null;
    this.previewController = null;
    this.restoreController = null;
    this.restorePreviewController = null;
  }

  open(input = {}, trigger = null) {
    if (this.restoring) {
      this.statusMessage = 'A restore is in progress for the selected file. Wait for it to finish before changing History targets.';
      this.statusBad = true;
      this.render();
      return false;
    }
    const resourceId = String(input.resourceId || '').trim();
    if (!resourceId) {
      this.window.setStatus('This resource has no registered History identity.', true);
      return false;
    }
    this.cancelReads();
    this.generation += 1;
    this.target = {
      resourceId,
      name: String(input.name || 'Selected file'),
      provider: String(input.provider || ''),
      isContextCurrent: typeof input.isContextCurrent === 'function' ? input.isContextCurrent : null,
      onRestored: typeof input.onRestored === 'function' ? input.onRestored : null,
    };
    this.items = [];
    this.nextCursor = null;
    this.pageCursors = [null];
    this.pageIndex = 0;
    this.selectedRef = '';
    this.selectedVersion = null;
    this.listBusy = false;
    this.listError = '';
    this.previewBusy = false;
    this.previewError = '';
    this.previewText = '';
    this.previewNote = '';
    this.restorePreview = null;
    this.restorePreviewBusy = false;
    this.contextInvalid = false;
    this.statusMessage = '';
    this.statusBad = false;
    this.window.setTitle('History · ' + this.target.name);
    this.window.setSubtitle('Files · Version history');
    this.window.show(trigger || document.activeElement);
    this.render();
    void this.loadPage(null);
    return true;
  }

  isCurrent(token, versionRef = null) {
    return token === this.generation
      && this.target != null
      && (versionRef == null || this.selectedRef === versionRef);
  }

  contextMatches() {
    if (this.contextInvalid || !this.target) return false;
    if (!this.target.isContextCurrent) return true;
    try { return this.target.isContextCurrent() !== false; }
    catch (_) { return false; }
  }

  guardContext(token) {
    if (!this.isCurrent(token)) return false;
    if (this.contextMatches()) return true;
    this.contextInvalid = true;
    this.listController?.abort();
    this.previewController?.abort();
    this.restorePreviewController?.abort();
    this.statusMessage = 'The Files or Editor target changed. Reopen History from the resource you want to review.';
    this.statusBad = true;
    this.render();
    return false;
  }

  async requestJson(url, options = {}) {
    const response = await fetch(url, { credentials: 'same-origin', ...options });
    let payload = null;
    const text = await response.text();
    if (text) {
      try { payload = JSON.parse(text); }
      catch (_) {
        if (response.ok) throw new Error('History returned an unreadable response.');
      }
    }
    if (!response.ok) {
      const detail = payload?.detail || payload?.message || payload?.error || '';
      const error = new Error(String((detail && typeof detail === 'object' ? detail.message : detail) || response.statusText || 'History request failed.'));
      error.status = response.status;
      throw error;
    }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new Error('History returned an invalid response.');
    }
    return payload;
  }

  versionsUrl(cursor = null) {
    const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
    if (cursor) params.set('cursor', cursor);
    return '/api/history/resources/' + encodeURIComponent(this.target.resourceId) + '/versions?' + params.toString();
  }

  async loadPage(cursor = null) {
    const token = this.generation;
    if (!this.guardContext(token)) return;
    this.listController?.abort();
    const controller = new AbortController();
    this.listController = controller;
    this.listBusy = true;
    this.listError = '';
    this.render();
    try {
      const response = await this.requestJson(this.versionsUrl(cursor), { signal: controller.signal });
      if (!this.guardContext(token) || controller.signal.aborted) return;
      if (response.resource_id !== this.target.resourceId || !Array.isArray(response.items)
        || response.items.length > PAGE_LIMIT
        || (response.next_cursor != null && typeof response.next_cursor !== 'string')
        || response.items.some((item) => !item || typeof item !== 'object' || typeof item.id !== 'string' || !item.id)) {
        throw new Error('History returned an invalid resource-version page.');
      }
      this.items = response.items;
      this.nextCursor = response.next_cursor || null;
      if (this.selectedRef) {
        const updated = this.items.find((item) => item.id === this.selectedRef);
        if (updated) this.selectedVersion = updated;
      }
      this.listError = '';
    } catch (error) {
      if (error?.name !== 'AbortError' && this.guardContext(token)) {
        this.listError = this.errorMessage(error, 'list');
      }
    } finally {
      if (this.listController === controller) this.listController = null;
      if (this.isCurrent(token)) {
        this.listBusy = false;
        this.render();
      }
    }
  }

  errorMessage(error, operation) {
    const status = Number(error?.status || 0);
    const detail = String(error?.message || '');
    if (operation === 'restore' && status >= 500) return 'History service did not return a restore receipt. The outcome is unknown; inspect the resource before retrying.';
    if (status === 503 || status === 502 || status === 504) return 'History service is unavailable. Retry when the service is available.';
    if (status === 401 || status === 403) return 'This Files resource is no longer authorized for History.';
    if (status === 410) return 'Retention has pruned this version. Its content can no longer be read or restored.';
    if (status === 404) return 'This resource or version is no longer registered or available to History.';
    if (status === 409 && operation === 'restore') return 'The destination changed after review. No restore was applied; review the current destination again.';
    if (status === 409) return 'This version is changing or unavailable. Refresh History and review it again.';
    if (!status && operation === 'restore') return 'History did not return a restore receipt. The restore outcome is unknown; review the resource before retrying.';
    return detail || 'History could not complete this request.';
  }

  movePage(direction) {
    if (this.listBusy || this.restoring || this.contextInvalid) return;
    if (direction < 0 && this.pageIndex > 0) {
      this.pageIndex -= 1;
      void this.loadPage(this.pageCursors[this.pageIndex]);
      return;
    }
    if (direction > 0 && this.nextCursor) {
      this.pageCursors = this.pageCursors.slice(0, this.pageIndex + 1);
      this.pageCursors.push(this.nextCursor);
      this.pageIndex += 1;
      void this.loadPage(this.nextCursor);
    }
  }

  selectVersion(version, index) {
    if (this.restoring || this.contextInvalid || !version?.id) return;
    this.previewController?.abort();
    this.previewController = null;
    this.restorePreviewController?.abort();
    this.restorePreviewController = null;
    this.previewGeneration += 1;
    this.selectedRef = version.id;
    this.selectedVersion = version;
    this.previewBusy = false;
    this.previewError = '';
    this.previewText = '';
    this.previewNote = '';
    this.restorePreview = null;
    this.restorePreviewBusy = false;
    this.render();
    void this.loadPreview(this.generation, version.id, version);
  }

  async loadPreview(token, versionRef, version) {
    if (!this.guardContext(token) || !this.isCurrent(token, versionRef)) return;
    if (version.can_read !== true || version.availability !== 'available') {
      this.previewNote = this.availabilityNote(version);
      this.render();
      return;
    }
    const previewToken = ++this.previewGeneration;
    const controller = new AbortController();
    this.previewController = controller;
    this.previewBusy = true;
    this.previewError = '';
    this.previewNote = '';
    this.previewText = '';
    this.render();
    try {
      const bytes = [];
      let offset = 0;
      let eof = false;
      while (offset < PREVIEW_LIMIT_BYTES && !eof) {
        if (!this.guardContext(token) || !this.isCurrent(token, versionRef) || previewToken !== this.previewGeneration) return;
        const limit = Math.min(READ_CHUNK_BYTES, PREVIEW_LIMIT_BYTES - offset);
        const url = '/api/history/resources/' + encodeURIComponent(this.target.resourceId)
          + '/versions/' + encodeURIComponent(versionRef)
          + '?offset=' + String(offset) + '&limit=' + String(limit);
        const response = await this.requestJson(url, { signal: controller.signal });
        if (!this.guardContext(token) || controller.signal.aborted || !this.isCurrent(token, versionRef)
          || previewToken !== this.previewGeneration) return;
        if (response.resource_id !== this.target.resourceId || response.version?.id !== versionRef
          || response.offset !== offset || response.content_encoding !== 'base64'
          || (response.content != null && typeof response.content !== 'string')) {
          throw new Error('History returned an invalid version preview.');
        }
        if (response.content == null) {
          if (!response.eof) throw new Error('History returned an incomplete version preview.');
          eof = true;
          break;
        }
        const raw = atob(response.content);
        const chunk = new Uint8Array(raw.length);
        for (let index = 0; index < raw.length; index += 1) chunk[index] = raw.charCodeAt(index);
        if (chunk.length > limit) throw new Error('History returned an oversized version preview.');
        bytes.push(chunk);
        offset += chunk.length;
        eof = response.eof === true;
        if (!chunk.length && !eof) throw new Error('History returned an incomplete version preview.');
      }
      const combined = new Uint8Array(offset);
      let cursor = 0;
      for (const chunk of bytes) { combined.set(chunk, cursor); cursor += chunk.length; }
      if (combined.includes(0)) {
        this.previewNote = 'This version is binary; text preview is unavailable.';
      } else {
        try {
          this.previewText = new TextDecoder('utf-8', { fatal: true }).decode(combined);
          if (!this.previewText && eof) this.previewNote = 'This version contains an empty file.';
        } catch (_) {
          this.previewNote = 'This version is not readable as UTF-8 text.';
        }
      }
      if (!eof && offset >= PREVIEW_LIMIT_BYTES) this.previewNote = 'Preview is limited to the first 256 KiB.';
    } catch (error) {
      if (error?.name !== 'AbortError' && this.guardContext(token) && this.isCurrent(token, versionRef)
        && previewToken === this.previewGeneration) {
        this.previewError = this.errorMessage(error, 'read');
      }
    } finally {
      if (this.previewController === controller) this.previewController = null;
      if (this.isCurrent(token, versionRef) && previewToken === this.previewGeneration) {
        this.previewBusy = false;
        this.render();
      }
    }
  }

  availabilityNote(version) {
    if (version?.availability === 'absent') return 'This entry records an absence. It has no payload to preview or restore.';
    if (version?.availability === 'expired') return 'Retention has pruned this version. Its content can no longer be read or restored.';
    if (version?.availability === 'expiring') return 'This version is being removed by retention and is temporarily unavailable.';
    return 'This version is unavailable for preview. It cannot be restored unless the service marks it available.';
  }

  async reviewRestore() {
    const token = this.generation;
    const versionRef = this.selectedRef;
    const version = this.selectedVersion;
    if (!this.guardContext(token) || !this.isCurrent(token, versionRef) || this.restoring
      || version?.can_restore !== true || !versionRef) return;
    this.restorePreview = null;
    this.restorePreviewBusy = true;
    this.statusMessage = '';
    this.statusBad = false;
    this.render();
    const controller = this.createRestorePreviewController(token);
    try {
      const url = '/api/history/resources/' + encodeURIComponent(this.target.resourceId)
        + '/versions/' + encodeURIComponent(versionRef) + '/restore-preview';
      const response = await this.requestJson(url, { signal: controller.signal });
      if (!this.guardContext(token) || !this.isCurrent(token, versionRef)) return;
      const preview = response;
      const expected = preview?.destination?.expected_fingerprint;
      const exists = preview?.destination?.exists;
      if (String(preview.resource?.resource_id || '') !== this.target.resourceId
        || preview.source?.id !== versionRef
        || (preview.effect !== 'create' && preview.effect !== 'replace')
        || preview.requires_confirmation !== true
        || preview.captures_current_destination !== true
        || typeof expected !== 'string' || !expected
        || typeof exists !== 'boolean'
        || (!exists && expected !== 'missing')
        || (exists && expected === 'missing')) {
        throw new Error('History returned an incomplete restore preview. Nothing was restored.');
      }
      this.restorePreview = {
        resourceId: this.target.resourceId,
        versionRef,
        destination: preview.destination,
        effect: preview.effect,
      };
    } catch (error) {
      if (error?.name !== 'AbortError' && this.guardContext(token) && this.isCurrent(token, versionRef)) {
        this.statusMessage = this.errorMessage(error, 'preview');
        this.statusBad = true;
      }
    } finally {
      const ownsPreview = this.restorePreviewController === controller;
      if (ownsPreview) this.restorePreviewController = null;
      if (ownsPreview && this.isCurrent(token, versionRef)) {
        this.restorePreviewBusy = false;
        this.render();
      }
    }
  }

  createRestorePreviewController(token) {
    this.restorePreviewController?.abort();
    const controller = new AbortController();
    this.restorePreviewController = controller;
    if (!this.isCurrent(token)) controller.abort();
    return controller;
  }

  async confirmRestore() {
    const token = this.generation;
    const versionRef = this.selectedRef;
    const version = this.selectedVersion;
    const target = this.target;
    const review = this.restorePreview;
    const expected = review?.destination?.expected_fingerprint;
    if (!this.guardContext(token) || !this.isCurrent(token, versionRef) || this.restoring
      || version?.can_restore !== true || !review || review.resourceId !== target?.resourceId
      || review.versionRef !== versionRef || typeof expected !== 'string' || !expected) return;
    const restoreId = globalThis.crypto?.randomUUID?.()
      || 'history-' + Date.now() + '-' + Math.random().toString(16).slice(2);
    const body = {
      restore_id: restoreId,
      resource_id: target.resourceId,
      version_ref: versionRef,
      expected_destination_fingerprint: expected,
    };
    this.restoring = true;
    this.statusMessage = 'Restoring the reviewed version…';
    this.statusBad = false;
    this.render();
    const controller = new AbortController();
    this.restoreController = controller;
    try {
      const response = await this.requestJson('/api/history/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!this.isCurrent(token, versionRef)) return;
      const receipt = response?.Restore;
      const restoredResource = Array.isArray(receipt?.resources)
        && receipt.resources.some((item) => item?.resource_id === target.resourceId && item?.outcome === 'Complete');
      if (receipt?.outcome !== 'Complete' || !restoredResource) {
        if (receipt?.outcome === 'Conflict') {
          this.restorePreview = null;
          this.statusMessage = this.errorMessage({ status: 409 }, 'restore');
          this.statusBad = true;
          return;
        }
        if (receipt && typeof receipt.outcome === 'string' && receipt.outcome !== 'Complete') {
          this.restorePreview = null;
          this.statusMessage = 'History reported restore outcome ' + receipt.outcome + '. It did not report a complete restore; inspect the resource before retrying.';
          this.statusBad = true;
          return;
        }
        throw new Error('History did not return a complete restore receipt. The restore outcome is unknown; review the resource before retrying.');
      }
      this.pendingRestoreVerification = receipt.verification?.status === 'Pending' ? body : null;
      this.restorePreview = null;
      if (!this.contextMatches()) {
        this.statusMessage = 'Restored ' + target.name + '. The source context changed, so its open view was left untouched.';
        this.statusBad = false;
        return;
      }
      this.statusMessage = 'Restored ' + target.name + '. Refreshing the open view…';
      this.statusBad = false;
      this.render();
      let refresh = null;
      if (target.onRestored) {
        try {
          refresh = await target.onRestored({ resourceId: target.resourceId, versionRef, receipt });
        } catch (error) {
          refresh = { status: 'refresh_failed', message: String(error?.message || error || '') };
        }
      }
      if (!this.isCurrent(token, versionRef)) return;
      if (refresh?.status === 'refreshed') {
        this.statusMessage = 'Restored ' + target.name + ' and refreshed its open view.';
      } else if (refresh?.status === 'draft_preserved') {
        this.statusMessage = 'Restored ' + target.name + '. An unsaved Editor draft was preserved; reopen the file to view the restored bytes.';
      } else if (refresh?.status === 'context_changed' || !this.contextMatches()) {
        this.statusMessage = 'Restored ' + target.name + '. The source context changed, so its open view was left untouched.';
      } else if (refresh?.status === 'refresh_failed') {
        this.statusMessage = 'Restored ' + target.name + ', but the open view could not be refreshed'
          + (refresh.message ? ': ' + refresh.message : '. Reopen the resource to see its current bytes.');
        this.statusBad = true;
      } else if (!target.onRestored) {
        this.statusMessage = 'Restored ' + target.name + '.';
      } else {
        this.statusMessage = 'Restored ' + target.name + '. Reopen the resource to see its current bytes.';
        this.statusBad = true;
      }
      const proof = receipt.verification;
      if (proof?.status === 'Verified') this.statusMessage += ' Source and restored content hashes verified.';
      else if (proof) this.statusMessage += ' Restore committed; content verification ' + (proof.status === 'Mismatch' ? 'found a mismatch.' : 'is pending.');
    } catch (error) {
      if (error?.name !== 'AbortError' && this.isCurrent(token, versionRef)) {
        if (Number(error?.status) === 409) {
          this.restorePreview = null;
          this.statusMessage = this.errorMessage(error, 'restore');
        } else {
          this.restorePreview = null;
          this.statusMessage = this.errorMessage(error, 'restore');
        }
        this.statusBad = true;
      }
    } finally {
      if (this.restoreController === controller) this.restoreController = null;
      this.restoring = false;
      if (this.isCurrent(token)) this.render();
    }
  }

  render() {
    if (!this.window?.body) return;
    const body = this.window.body;
    const focused = body.contains(document.activeElement)
      ? document.activeElement.getAttribute('data-history-focus') || ''
      : '';
    this.window.setStatus(this.statusMessage, this.statusBad);
    const shell = element('div', { class: 'history-browser' });
    if (!this.target) {
      shell.append(element('p', { class: 'history-state', text: 'Open History from a supported Files resource to inspect its captured versions.' }));
      body.replaceChildren(shell);
      return;
    }
    shell.append(
      element('div', { class: 'history-browser-context' },
        element('strong', { text: this.target.name }),
        element('span', { text: 'History is scoped to this exact Files resource.' })),
    );
    if (this.statusMessage) shell.append(element('p', {
      class: 'history-status' + (this.statusBad ? ' error' : ''),
      role: 'status',
      'aria-live': 'polite',
      text: this.statusMessage,
    }));
    const toolbar = element('div', { class: 'history-browser-toolbar' },
      actionButton('Refresh versions', () => { void this.loadPage(this.pageCursors[this.pageIndex] || null); }, {
        disabled: this.listBusy || this.restoring || this.contextInvalid,
        focusKey: 'refresh',
      }),
      actionButton('Previous', () => this.movePage(-1), {
        disabled: this.pageIndex <= 0 || this.listBusy || this.restoring || this.contextInvalid,
        focusKey: 'previous',
      }),
      actionButton('Next', () => this.movePage(1), {
        disabled: !this.nextCursor || this.listBusy || this.restoring || this.contextInvalid,
        focusKey: 'next',
      }),
      element('span', { class: 'history-page-label', text: 'Page ' + String(this.pageIndex + 1) }));
    shell.append(toolbar);
    const repairAction = element('input', { type: 'text', placeholder: 'Recovery action ID from the saved receipt', 'aria-label': 'Recovery action ID' });
    repairAction.value = this.repairActionId || '';
    repairAction.maxLength = 256;
    repairAction.style.maxWidth = '100%';
    const repair = actionButton(this.repairBusy ? 'Repairing recovery copy…' : 'Repair recovery copy', async () => {
      const actionId = repairAction.value.trim();
      const token = this.generation;
      if (!actionId || this.repairBusy || !this.guardContext(token)) return;
      this.repairActionId = actionId;
      this.repairBusy = true;
      this.render();
      try {
        const response = await this.requestJson('/api/history/capture-repair/' + encodeURIComponent(actionId), { method: 'POST' });
        if (!this.guardContext(token)) return;
        this.statusMessage = response.message || 'Recovery copy repaired; saved operation was not repeated.';
        this.statusBad = false;
        void this.loadPage(this.pageCursors[this.pageIndex] || null);
      } catch (error) {
        if (!this.guardContext(token)) return;
        this.statusMessage = error.message || 'Recovery gap remains. The saved operation was not repeated.';
        this.statusBad = true;
      } finally {
        if (this.isCurrent(token)) { this.repairBusy = false; this.render(); }
      }
    }, { disabled: this.repairBusy || this.restoring || this.contextInvalid, focusKey: 'repair-capture' });
    shell.append(element('div', { class: 'history-browser-toolbar' }, repairAction, repair));
    if (this.pendingRestoreVerification?.resource_id === this.target?.resourceId) {
      shell.append(actionButton('Verify committed restore', async () => {
        const token = this.generation;
        if (!this.guardContext(token) || this.restoring) return;
        this.restoring = true;
        this.render();
        try {
          const response = await this.requestJson('/api/history/restore', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(this.pendingRestoreVerification),
          });
          if (!this.guardContext(token)) return;
          const proof = response?.Restore?.verification;
          if (response?.Restore?.outcome !== 'Complete') throw new Error('Committed restore verification is unavailable.');
          this.statusMessage = proof?.status === 'Verified' ? 'Committed restore verified against its selected source content.'
            : proof?.status === 'Mismatch' ? 'Restore committed; current content differs from its selected source.' : 'Restore committed; verification is still pending.';
          if (proof?.status !== 'Pending') this.pendingRestoreVerification = null;
          this.statusBad = proof?.status === 'Mismatch';
        } catch (error) {
          if (this.guardContext(token)) { this.statusMessage = 'Restore remains committed; verification is unavailable.'; this.statusBad = true; }
        } finally {
          if (this.isCurrent(token)) { this.restoring = false; this.render(); }
        }
      }, { disabled: this.restoring || this.contextInvalid, focusKey: 'verify-restore' }));
    }
    const layout = element('div', { class: 'history-browser-layout' });
    const listPanel = element('section', { class: 'history-version-list-panel', 'aria-label': 'Captured versions' });
    const list = element('div', { class: 'history-version-list' });
    if (this.listBusy && !this.items.length) {
      list.append(element('p', { class: 'history-state', text: 'Loading captured versions…' }));
    } else if (this.listError) {
      list.append(element('p', { class: 'history-state error', text: this.listError }));
      list.append(actionButton('Retry', () => { void this.loadPage(this.pageCursors[this.pageIndex] || null); }, {
        disabled: this.listBusy || this.contextInvalid,
        focusKey: 'retry-list',
      }));
    } else if (!this.items.length) {
      list.append(element('p', {
        class: 'history-state',
        text: this.pageIndex
          ? 'There are no more versions on this page. Older versions may have expired or been pruned by retention.'
          : 'No versions are currently available. History may not have captured this resource yet, or retention may have pruned older versions.',
      }));
    } else {
      this.items.forEach((version, index) => {
        const title = String(version.display_name || this.target.name);
        const row = element('button', {
          type: 'button',
          class: 'history-version-row' + (version.id === this.selectedRef ? ' selected' : ''),
          'aria-pressed': String(version.id === this.selectedRef),
          'data-history-focus': 'version-' + String(index),
          onclick: () => this.selectVersion(version, index),
        });
        row.disabled = this.restoring || this.contextInvalid;
        row.append(
          element('span', { class: 'history-version-name', text: title }),
          element('span', { class: 'history-version-time', text: millisLabel(version.timestamp_millis) }),
          element('span', { class: 'history-version-meta', text: descriptorLabel(version, this.target.name) }),
          element('span', { class: 'history-version-availability', text: this.versionAvailabilityLabel(version) }),
        );
        list.append(row);
      });
    }
    listPanel.append(element('h3', { text: 'Versions' }), list);
    const detailPanel = element('section', { class: 'history-version-detail', 'aria-label': 'Selected version' });
    detailPanel.append(element('h3', { text: 'Preview and restore' }));
    this.renderSelectedVersion(detailPanel);
    layout.append(listPanel, detailPanel);
    shell.append(layout);
    body.replaceChildren(shell);
    if (focused) {
      const nextFocus = [...body.querySelectorAll('[data-history-focus]')]
        .find((node) => node.getAttribute('data-history-focus') === focused);
      nextFocus?.focus({ preventScroll: true });
    }
  }

  versionAvailabilityLabel(version) {
    if (version?.availability === 'expired') return 'Pruned by retention';
    if (version?.availability === 'expiring') return 'Retention pending';
    if (version?.availability === 'absent') return 'Absent entry';
    if (version?.availability === 'available') return 'Available';
    return 'Unavailable';
  }

  renderSelectedVersion(panel) {
    const version = this.selectedVersion;
    const canAct = !this.contextInvalid && !this.restoring;
    if (!version) {
      panel.append(element('p', { class: 'history-state', text: 'Choose a version to inspect its content or review restoring it.' }));
      return;
    }
    panel.append(
      element('div', { class: 'history-selected-summary' },
        element('strong', { text: String(version.display_name || this.target.name) }),
        element('span', { text: millisLabel(version.timestamp_millis) }),
        element('span', { text: descriptorLabel(version, this.target.name) })),
    );
    const preview = element('section', { class: 'history-preview-panel', 'aria-label': 'Version content preview' });
    preview.append(element('h4', { text: 'Content preview' }));
    if (this.previewBusy) preview.append(element('p', { class: 'history-state', text: 'Loading a bounded preview…' }));
    else if (this.previewError) preview.append(element('p', { class: 'history-state error', text: this.previewError }));
    else {
      if (this.previewNote) preview.append(element('p', { class: 'history-state', text: this.previewNote }));
      if (this.previewText) preview.append(element('pre', { class: 'history-preview-text', text: this.previewText }));
      else if (!this.previewNote && version.can_read === true) preview.append(element('p', { class: 'history-state', text: 'No preview content is loaded.' }));
      else if (!this.previewNote) preview.append(element('p', { class: 'history-state', text: this.availabilityNote(version) }));
    }
    panel.append(preview);
    if (version.can_restore === true) {
      panel.append(actionButton(this.restorePreviewBusy ? 'Loading restore preview…' : 'Review restore', () => { void this.reviewRestore(); }, {
        disabled: !canAct || this.restorePreviewBusy,
        focusKey: 'review-restore',
      }));
    } else {
      panel.append(element('p', { class: 'history-state', text: 'This version cannot be restored.' }));
    }
    if (this.restorePreview) {
      const replacing = this.restorePreview.effect === 'replace';
      const review = element('section', { class: 'history-restore-review', 'aria-label': 'Restore confirmation' });
      review.append(element('h4', { text: 'Review restore' }));
      review.append(element('p', {
        text: replacing
          ? 'This will replace the current file with the selected historical version.'
          : 'This will create the file from the selected historical version.',
      }));
      review.append(element('p', { text: 'The current destination will be captured in History before restoration.' }));
      review.append(element('p', { text: 'Confirm only if this effect matches what you want.' }));
      review.append(
        actionButton('Confirm restore', () => { void this.confirmRestore(); }, {
          disabled: !canAct || this.restoring,
          focusKey: 'confirm-restore',
        }),
        actionButton('Cancel review', () => { this.restorePreview = null; this.render(); }, {
          disabled: this.restoring,
          focusKey: 'cancel-review',
        }),
      );
      panel.append(review);
    }
  }
}

let view = null;

export function openResourceHistory(target, trigger = null) {
  if (!view) view = new ResourceHistoryView();
  return view.open(target, trigger);
}
