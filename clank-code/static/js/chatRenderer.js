import { capturedResourceButton } from './chatCapturedResource.js';
import { createCapturedHtmlPreview } from './chatHtmlPreview.js';
import { renderSkillUseReceipt, currentSkillSourceButton } from './skillUseReceipt.js';
import { searchHitButtons } from './toolResultReceipt.js';
import { capturedChangeButton, capturedTurnButton } from './historyView.js';
import { exactOperationDetails, verifiedOperationExplanation, hasAdditionalOperationDetails } from './operationExplanation.js';
import { uiIcon } from './uiIcons.js';
import { fileIcon } from './langIcons.js';
// static/js/chatRenderer.js
// Extracted from chat.js — message rendering, sources, images, metrics

import uiModule from './ui.js';
import markdownModule from './markdown.js';
import { svgifyEmoji } from './markdown.js';
import { addAITTSButton } from './tts-ai.js';
import { providerLogo, providerLabel } from './providers.js';
import settingsModule from './settings.js';
import spinnerModule from './spinner.js';
import { bindMenuDismiss } from './escMenuStack.js';
import { matchModelKey } from './model/matchKey.js';
import { normalizeAssistantTranscript } from './chatTranscript.js';
import { openClankHandbook, openImageResource } from './copal.js';
import { filesFacadeClient } from './filesFacadeClient.js';
import { mediaInputNotice } from './mediaInputNotice.js';

/** An untrusted selector is resolved against original custody and current Human Files. */
export function resourceResultButton(selector) {
  if (!selector || selector.version !== 1 || selector.kind !== 'workflow-member' || typeof selector.operationID !== 'string') return null;
  const scope=window.chatModule?.ratingAccountScope?.(), session=window.sessionModule, navigation=session?.getSessionNavigationToken?.();
  const button=document.createElement('button'); button.type='button'; button.className='copal-btn'; button.textContent='Open saved file'; button.dataset.chatArtifact='Saved file';
  const current=()=>{const now=window.chatModule?.ratingAccountScope?.();return button.isConnected && !!scope?.sid && !!scope?.owner && navigation!==undefined && selector.chat===scope.sid && now?.sid===scope.sid && now.owner===scope.owner && now.authEpoch===scope.authEpoch && document.body.dataset.accountId===scope.owner && session?.getCurrentSessionId?.()===scope.sid && session?.getSessionNavigationToken?.()===navigation;};
  button.addEventListener('click',async event=>{event.stopPropagation();if(button.disabled || !current())return;button.disabled=true;
    try {
      const response=await fetch('/api/files-v1/workflow-result/resolve',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(selector)});
      if(!response.ok || !current())throw new Error('Saved file unavailable.');
      const found=await response.json();if(!current() || typeof found?.resourceRef!=='string' || !found.resourceRef || found?.revision?.kind!==selector.revision?.kind || found?.revision?.value!==selector.revision?.value)throw new Error('Saved file unavailable.');
      const editor=await import('./copal.js');if(!current())return;
      try {if(typeof editor.openResource!=='function' || !await editor.openResource(found.resourceRef,{expectedRevision:found.revision,isCurrent:current}))throw new Error('Editor unavailable.');}
      catch (_) {if(!current())return;const files=await import('./files.js');if(!current() || typeof files.revealResource!=='function' || !await files.revealResource(found.resourceRef,{expectedRevision:found.revision,isCurrent:current}))throw new Error('Saved file unavailable.');}
    } catch (_) {if(current())button.textContent='Saved file unavailable';}
    finally {if(button.isConnected)button.disabled=false;}
  });
  return button;
}

/** Offer the exact committed Files result from a completed media tool response. */
export function mediaResultButton(raw) {
  if (!raw || raw.status !== 'completed' || !['submit','status','collect'].includes(raw.rawInput?.action) || raw.rawOutput?.metadata?.truncated === true) return null;
  const text = raw.rawOutput?.output;
  if (typeof text !== 'string' || text.length > 16384) return null;
  let value; try { value = JSON.parse(text); } catch { return null; }
  const item = value?.result;
  if (value?.ok !== true || value.state !== 'complete' || item?.partial !== false ||
      !['image','mesh','archive','audio'].includes(item.kind) || typeof item.resource_ref !== 'string' ||
      !item.resource_ref || item.resource_ref.length > 8192 || /[\u0000-\u001f\u007f]/.test(item.resource_ref) ||
      !/^[a-f0-9]{64}$/.test(item.sha256 || '') || !Number.isSafeInteger(item.size_bytes) || item.size_bytes < 0 || item.size_bytes > 67108864) return null;
  const ref=item.resource_ref, revision={kind:'hostFingerprint',value:`sha256:${item.sha256}:${item.size_bytes}`};
  const scope=window.chatModule?.ratingAccountScope?.(), session=window.sessionModule;
  const navigation=session?.getSessionNavigationToken?.();
  const button=document.createElement('button'); button.type='button'; button.className='copal-btn'; button.textContent='Open saved result'; button.dataset.chatArtifact=item.kind + ' result';
  const current=()=>{const now=window.chatModule?.ratingAccountScope?.(); return button.isConnected && !!scope?.sid && !!scope?.owner && navigation!==undefined && now?.sid===scope.sid && now.owner===scope.owner && now.authEpoch===scope.authEpoch && document.body.dataset.accountId===scope.owner && session?.getCurrentSessionId?.()===scope.sid && session?.getSessionNavigationToken?.()===navigation;};
  button.addEventListener('click',async event=>{event.stopPropagation(); if(button.disabled || !current())return; button.disabled=true;
    try {const editor=await import('./copal.js'); if(!current())return;
      if(item.kind==='image') {
        if (typeof editor.openRetainedHostImage !== 'function' ||
            !await editor.openRetainedHostImage(ref,{expectedRevision:revision,isCurrent:current})) {
          throw new Error('The exact saved image could not be opened.');
        }
      } else {
        const files=await import('./files.js'); if(!current())return;
        if(typeof files.revealResource!=='function' || !await files.revealResource(ref,{expectedRevision:revision,isCurrent:current})) throw new Error('Files result unavailable or changed.');
      }
    } catch(error) {if(current()){button.textContent='Saved result unavailable';button.title=String(error?.message || 'The result changed or is unavailable.');}}
    finally {if(button.isConnected)button.disabled=false;}
  });
  return button;
}

const SEARCH_ICON = uiIcon("search", 14);
const REPORT_ICON = uiIcon("document", 14);
const CHAT_ABOUT_ICON = uiIcon("chat", 14);
const COPY_ICON = uiIcon("copy", 12);
const CHECK_ICON = uiIcon("check", 12, {"role":"success"});
const PAPERCLIP_ICON = uiIcon("attachment", 12);

/** Sanitize a URL for use in href — only allow http(s) and protocol-relative. */
function _safeHref(url) {
  if (!url) return '#';
  try {
    var parsed = new URL(url, window.location.origin);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return uiModule.esc(url);
  } catch(e) { /* invalid URL */ }
  return '#';
}

export function safeToolScreenshotSrc(raw) {
  const src = String(raw || '').trim();
  if (/^data:image\/(?:png|jpe?g|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(src)) {
    return src;
  }
  return '';
}

export function safeDisplayImageSrc(raw) {
  const src = String(raw || '').trim();
  if (!src) return '';
  if (/^data:image\/(?:png|jpe?g|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(src)) {
    return src;
  }
  try {
    const parsed = new URL(src, window.location.origin);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return parsed.href;
    }
  } catch (_) {}
  return '';
}

function _makeActionBtn(className, title, text, handler) {
  const btn = document.createElement('button');
  btn.className = className;
  btn.type = 'button';
  btn.title = title;
  btn.textContent = text;
  btn.addEventListener('click', handler);
  return btn;
}

// Attachment card helpers
function _attachIcon(mimeOrName) {
  const value = String(mimeOrName || '');
  const mime = /^(?:application|audio|font|image|text|video)\//i.test(value) ? value : '';
  return fileIcon({ name: mime ? '' : value, mimeType: mime }, 16, { className: 'attach-card-icon' });
}
function _formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

// Build the `.attach-cards` element for a message's attachment list. Shared by
// addMessage and updateMessageAttachments so a live (optimistic) user bubble
// can be re-rendered with real upload ids once the upload resolves.
export function buildAttachCards(attachments) {
  const attachWrap = document.createElement('div');
  attachWrap.className = 'attach-cards';
  for (const att of attachments) {
    const isImage = (att.mime || '').startsWith('image/') || /\.(png|jpg|jpeg|gif|webp|svg|bmp)$/i.test(att.name || '');
    if (isImage) {
      // Image preview. Shown for both uploaded (att.id present) and still-
      // uploading attachments. A shimmering skeleton + whirlpool fills the
      // space until either the upload resolves (no id yet) or the thumbnail
      // image finishes loading, so the photo doesn't pop in abruptly.
      const imgWrap = document.createElement('div');
      imgWrap.className = 'attach-image-preview';
      imgWrap.style.cursor = att.id ? 'zoom-in' : 'default';
      if (att.id) imgWrap.dataset.fileId = att.id;
      if (att.id) {
        imgWrap.addEventListener('click', (e) => {
          // Tapping the corner OCR button shouldn't also open the lightbox.
          if (e.target.closest('.attach-ocr-btn')) return;
          _openImageLightbox(att);
        });
      }

      let skel = null;
      let sp = null;
      if (!att.previewUrl) {
        // Skeleton placeholder with a centered whirlpool. Self-stops when removed.
        skel = document.createElement('div');
        skel.className = 'attach-image-skeleton';
        // Match the photo's aspect ratio when the backend knew it at upload
        // time, so the skeleton doesn't sit at a 4:3 default and then snap to
        // a portrait shape when the image arrives.
        if (att.width && att.height) {
          skel.style.aspectRatio = att.width + ' / ' + att.height;
          skel.style.width = 'auto';
          skel.style.height = 'auto';
          skel.style.maxWidth = '300px';
          skel.style.maxHeight = '200px';
          skel.style.minWidth = '80px';
        }
        sp = spinnerModule.createWhirlpool(20);
        skel.appendChild(sp.element);
        imgWrap.appendChild(skel);
      }

      if (att.id || att.previewUrl) {
        const img = document.createElement('img');
        // Small cached thumbnail — the preview is tiny, no need to pull the
        // full-resolution photo. Click still opens the full image.
        img.alt = att.name || 'Image';
        img.loading = 'lazy';
        img.style.cssText = 'max-width:300px;max-height:200px;border-radius:6px;display:' + (att.previewUrl ? 'block' : 'none') + ';';
        let _revealed = false;
        let _revealTimer = null;
        const _reveal = () => {
          if (_revealed) return;
          _revealed = true;
          if (_revealTimer) { clearTimeout(_revealTimer); _revealTimer = null; }
          img.style.display = 'block';
          try { sp && sp.stop(); } catch {}
          if (skel) skel.remove();
        };
        img.addEventListener('load', _reveal);
        img.addEventListener('error', _reveal);
        img.src = att.previewUrl || `/api/upload/${att.id}?thumb=1`;
        // Cached images can be complete before the load listener attaches.
        if (img.complete && img.naturalWidth) _reveal();
        // Failsafe: if neither load nor error fires within 8s, reveal anyway.
        // The timer is cleared on reveal AND when updateMessageAttachments
        // replaces the card (which scrubs the img / skel from the DOM), so
        // repeated re-renders don't accumulate stranded timers.
        if (!att.previewUrl) _revealTimer = setTimeout(_reveal, 8000);
        imgWrap.appendChild(img);

        if (att.id) {
          // Small corner button → opens the vision/OCR editor so the user can
          // correct what the vision model extracted. The edit is cached on the
          // server keyed by file id, so any later message referencing this same
          // image picks up the corrected text instead of re-running the model.
          const ocrBtn = document.createElement('button');
          ocrBtn.type = 'button';
          ocrBtn.className = 'attach-ocr-btn';
          ocrBtn.title = 'View / edit OCR text';
          ocrBtn.innerHTML = uiIcon("edit", 12) + "<span class=\"attach-ocr-label\">Caption</span>";
          ocrBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            _openVisionEditor(att, ocrBtn.closest('.msg'));
          });
          imgWrap.appendChild(ocrBtn);
        }
      }

      if (att.vision_model) {
        const visionLabel = document.createElement('div');
        visionLabel.className = 'attach-vision-model';
        visionLabel.textContent = 'Vision: ' + String(att.vision_model).split('/').pop();
        imgWrap.appendChild(visionLabel);
      }
      const omitted = mediaInputNotice(att);
      if (omitted) {
        const note = document.createElement('div');
        note.className = 'attach-media-omitted';
        note.setAttribute('role', 'status');
        note.style.cssText = 'font-size:12px;opacity:0.85;margin-top:4px;max-width:300px;';
        note.textContent = omitted;
        imgWrap.appendChild(note);
      }
      if (att.name) {
        const label = document.createElement('div');
        label.className = 'attach-image-name';
        label.textContent = att.name;
        imgWrap.appendChild(label);
      }
      attachWrap.appendChild(imgWrap);
    } else {
      // Non-image file card
      const card = document.createElement('div');
      card.className = 'attach-card';
      card.dataset.name = att.name;
      if (att.id) {
        card.dataset.fileId = att.id;
        card.style.cursor = 'pointer';
        card.addEventListener('click', () => {
          // PDFs & text/code/markdown → open in the Documents viewer
          // (others fall back to the raw file).
          if (window.chatModule?.openAttachment) window.chatModule.openAttachment(att, false);
          else window.open(`/api/upload/${att.id}`, '_blank');
        });
      }
      const icon = _attachIcon(att.mime || att.name);
      const nameSpan = document.createElement('span');
      nameSpan.className = 'attach-card-name';
      nameSpan.textContent = att.name;
      card.innerHTML = icon;
      card.appendChild(nameSpan);
      if (att.size) {
        const sizeSpan = document.createElement('span');
        sizeSpan.className = 'attach-card-size';
        sizeSpan.textContent = _formatSize(att.size);
        card.appendChild(sizeSpan);
      }
      attachWrap.appendChild(card);
    }
  }
  return attachWrap;
}

// Re-render the attachment cards of an already-rendered message. Used to swap
// in real upload ids (and image thumbnails) on the optimistic user bubble once
// uploadPending() resolves — otherwise image previews only appear after a
// refresh, because the bubble is rendered before the upload assigns ids.
export function updateMessageAttachments(msgWrap, attachments) {
  if (!msgWrap || !attachments?.length) return;
  const body = msgWrap.querySelector('.body') || msgWrap;
  const existing = body.querySelector('.attach-cards');
  const fresh = buildAttachCards(attachments);
  if (existing) existing.replaceWith(fresh);
  else body.appendChild(fresh);
}

// Quick full-size preview when the user taps a chat photo thumbnail. Just an
// overlay with the original image centered — no Gallery panel, no editor.
function _openImageLightbox(att) {
  if (!att?.id) return;
  const overlay = document.createElement('div');
  overlay.className = 'attach-lightbox';
  // Show the cached thumb immediately so the overlay doesn't sit blank
  // while a 25MB original streams in. The full image swaps in once loaded;
  // if the full load fails (404 / network), we keep the thumb + show an
  // error label rather than a blank overlay forever.
  const img = document.createElement('img');
  img.alt = att.name || '';
  img.src = `/api/upload/${att.id}?thumb=1`;
  overlay.appendChild(img);
  const full = new Image();
  full.addEventListener('load', () => { img.src = full.src; });
  full.addEventListener('error', () => {
    const err = document.createElement('div');
    err.className = 'attach-lightbox-err';
    err.textContent = 'Failed to load full-resolution image.';
    overlay.appendChild(err);
  });
  full.src = `/api/upload/${att.id}`;

  const _onKey = (e) => { if (e.key === 'Escape') _close(); };
  const _close = () => {
    document.removeEventListener('keydown', _onKey);
    if (_overlayObs) { try { _overlayObs.disconnect(); } catch {} }
    overlay.remove();
  };
  // If the overlay is removed via any path other than our close handler
  // (session switch, parent re-render, external cleanup), still drop the
  // document-level keydown listener so it doesn't leak.
  let _overlayObs = null;
  try {
    _overlayObs = new MutationObserver(() => {
      if (!document.body.contains(overlay)) {
        document.removeEventListener('keydown', _onKey);
        _overlayObs.disconnect();
      }
    });
    _overlayObs.observe(document.body, { childList: true, subtree: false });
  } catch {}
  overlay.addEventListener('click', _close);
  document.addEventListener('keydown', _onKey);
  document.body.appendChild(overlay);
}

// Vision/OCR editor modal — opened from the corner "Aa" button on a chat photo
// thumbnail. Lets the user view and correct the text the vision model fed to
// the LLM (e.g. when OCR misreads a word). Persists to the server's vision
// cache (PUT /api/upload/{id}/vision), so any subsequent message that
// references the same file picks up the corrected text.
let _visionEditorEl = null;
let _visionEditorEsc = null;
function _closeVisionEditor() {
  if (_visionEditorEsc) { document.removeEventListener('keydown', _visionEditorEsc); _visionEditorEsc = null; }
  if (_visionEditorEl) { _visionEditorEl.remove(); _visionEditorEl = null; }
}
function _openVisionEditor(att, userMsgEl) {
  if (!att?.id) return;
  _closeVisionEditor();
  const overlay = document.createElement('div');
  overlay.className = 'vision-editor-overlay';
  overlay.addEventListener('click', (e) => { if (e.target === overlay) _closeVisionEditor(); });
  const panel = document.createElement('div');
  panel.className = 'vision-editor-panel';
  const title = document.createElement('div');
  title.className = 'vision-editor-title';
  // Eye icon matches the one in Settings → Vision so users recognise where
  // this text originates.
  title.innerHTML = uiIcon("eye", 14, {"style":"opacity:0.7;flex-shrink:0"}) + "<span>Vision text</span>";
  panel.appendChild(title);
  const desc = document.createElement('div');
  desc.className = 'vision-editor-desc';
  desc.textContent = 'Edit text and save, new chats will have the new context. Regenerate or continue from there.';
  panel.appendChild(desc);
  const ta = document.createElement('textarea');
  ta.className = 'vision-editor-text';
  ta.rows = 10;
  ta.placeholder = 'Loading…';
  ta.disabled = true;
  panel.appendChild(ta);
  const actions = document.createElement('div');
  actions.className = 'vision-editor-actions';
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'vision-editor-btn';
  closeBtn.innerHTML = '<span class="vision-btn-label">Close</span>';
  closeBtn.addEventListener('click', _closeVisionEditor);
  const _saveVisionText = async () => {
    const res = await fetch(`/api/upload/${att.id}/vision`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ text: ta.value }),
    });
    if (!res.ok) throw new Error('save failed');
  };
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'vision-editor-btn vision-editor-btn-primary';
  saveBtn.innerHTML = '<span class="vision-btn-label">Save</span>';
  saveBtn.disabled = true;
  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<span class="vision-btn-label">Saving…</span>';
    try {
      await _saveVisionText();
      if (uiModule?.showToast) uiModule.showToast('Saved');
      _closeVisionEditor();
    } catch (e) {
      saveBtn.disabled = false;
      saveBtn.innerHTML = '<span class="vision-btn-label">Save</span>';
      if (uiModule?.showError) uiModule.showError('Failed to save OCR text');
    }
  });
  // Regenerate-message: save the edited text, close, then trigger a resend of
  // the user message so the new AI reply uses the edit immediately.
  const regenBtn = document.createElement('button');
  regenBtn.type = 'button';
  regenBtn.className = 'vision-editor-btn vision-editor-btn-primary';
  regenBtn.title = 'Save and regenerate the message';
  regenBtn.innerHTML = uiIcon("refresh", 12) + "<span class=\"vision-btn-label\">Regenerate message</span>";
  regenBtn.disabled = true;
  regenBtn.addEventListener('click', async () => {
    regenBtn.disabled = true;
    saveBtn.disabled = true;
    try {
      await _saveVisionText();
      _closeVisionEditor();
      if (userMsgEl && window.chatModule?.resendUserMessage) {
        window.chatModule.resendUserMessage(userMsgEl, { replaceFromHere: true });
      } else if (uiModule?.showToast) {
        uiModule.showToast('Saved');
      }
    } catch (e) {
      regenBtn.disabled = false;
      saveBtn.disabled = false;
      if (uiModule?.showError) uiModule.showError('Failed to save OCR text');
    }
  });
  actions.appendChild(closeBtn);
  actions.appendChild(saveBtn);
  actions.appendChild(regenBtn);
  panel.appendChild(actions);
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  _visionEditorEl = overlay;

  // ESC closes the popup. Registered on document so it works regardless of
  // focus (the textarea swallows the event otherwise).
  _visionEditorEsc = (e) => { if (e.key === 'Escape') _closeVisionEditor(); };
  document.addEventListener('keydown', _visionEditorEsc);

  fetch(`/api/upload/${att.id}/vision`, { credentials: 'same-origin' })
    .then(r => r.ok ? r.json() : Promise.reject(r))
    .then(data => {
      ta.value = data.text || '';
      ta.placeholder = '';
      ta.disabled = false;
      saveBtn.disabled = false;
      regenBtn.disabled = !userMsgEl;
      ta.focus();
    })
    .catch(() => {
      ta.value = '';
      ta.placeholder = 'Could not load OCR text — type your correction and save.';
      ta.disabled = false;
      saveBtn.disabled = false;
      regenBtn.disabled = !userMsgEl;
    });
}

// Tool call syntax patterns to strip from displayed text
const TOOL_CALL_RE = /\[TOOL_CALL\][\s\S]*?\[\/TOOL_CALL\]/gi;
// Strip fenced tool-call blocks that look like structured invocations, not
// regular code examples. The tool tags are NOT hard-coded here — they are the
// backend's authoritative TOOL_TAGS set, fetched once from GET /api/tools and
// built into EXEC_FENCE_RE at load. TOOL_TAGS (src/agent_tools/__init__.py) is
// thus the single source: the live-strip list can never drift from the backend
// or miss a future tool (#3993). bash/python are carved out on purpose — they
// are languages a user may legitimately have asked the model to show, not tool
// invocations.
//
// Until the fetch resolves, EXEC_FENCE_RE stays null and exec fences aren't
// stripped — normally a sub-second window before the first stream. If the fetch
// fails it stays null for the rest of the session (logged below), so live exec
// fences won't be stripped until reload. Either way the backend already strips
// persisted history (src/tool_parsing.py builds the same regex from TOOL_TAGS),
// so a reload always renders clean.
let EXEC_FENCE_RE = null;
const EXEC_FENCE_NON_TOOL = new Set(['bash', 'python']);

function escapeRegex(source) {
  return String(source).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function stripExecutedFence(match, tag, inline, body) {
  const inlineArgs = (inline || '').trim();
  if (!inlineArgs) return '';
  const bodyText = (body || '').trim();
  const content = bodyText ? `${inlineArgs}\n${bodyText}` : inlineArgs;
  try {
    JSON.parse(content);
  } catch {
    return match;
  }
  return '';
}

async function loadExecFenceRegex() {
  try {
    const res = await fetch('/api/tools', { credentials: 'same-origin' });
    const data = await res.json();
    const tags = (data.tools || [])
      .map((t) => t.id)
      .filter((id) => id && !EXEC_FENCE_NON_TOOL.has(id));
    if (tags.length) {
      EXEC_FENCE_RE = new RegExp(
        '```(' + tags.map(escapeRegex).join('|') + ')(?![\\w-])' +
        '[ \\t]*([\\[{][^\\n]*?)?[ \\t]*(?=\\r?\\n|```)' +
        '\\r?\\n?([\\s\\S]*?)```',
        'gi'
      );
    }
  } catch (err) {
    // Surface the failure rather than swallowing it: EXEC_FENCE_RE stays null,
    // so this session won't strip live exec fences until reload (persisted path
    // stays clean regardless).
    console.warn('chatRenderer: /api/tools fetch failed; live exec-fence stripping disabled until reload', err);
  }
}
loadExecFenceRegex();
// XML-style tool calls: <minimax:tool_call>, <tool_call>, <function_call>, bare <invoke>
const XML_TOOL_CALL_RE = /<(?:[\w]+:)?(?:tool_call|function_call)>[\s\S]*?<\/(?:[\w]+:)?(?:tool_call|function_call)>/gi;
const XML_INVOKE_RE = /<invoke\s+name=['"][^'"]*['"]>[\s\S]*?<\/invoke>/gi;
// DeepSeek "DSML" tool-call markup (fullwidth-pipe ｜ or ascii | delimited) that
// leaks into content when the model emits a text tool call instead of a native
// one. Strip the whole block; the second pattern catches stray/partial tags
// (e.g. mid-stream before the closing tag arrives).
const DSML_TOOL_RE = /<\s*[｜|]+\s*DSML\s*[｜|]+\s*tool_calls\s*>[\s\S]*?(?:<\s*\/\s*[｜|]+\s*DSML\s*[｜|]+\s*tool_calls\s*>|$)/gi;
const DSML_STRAY_RE = /<\s*\/?\s*[｜|]+\s*DSML\s*[｜|]+[^>]*>/gi;
const DSML_INVOKE_RE = /<\s*[｜|]+\s*DSML\s*[｜|]+\s*invoke\b[^>]*>[\s\S]*?(?:<\s*\/\s*[｜|]+\s*DSML\s*[｜|]+\s*invoke\s*>|$)/gi;
const RAW_OPENAI_TOOL_JSON_RE = /(?:\[\s*)?\{\s*"function"\s*:\s*\{[\s\S]*?\}\s*,\s*"id"\s*:\s*"[^"]*"\s*,\s*"type"\s*:\s*"function"\s*\}\s*\]?/gi;
const QWEN_ROLE_MARKER_RE = /<\/?\|(?:assistant|assistan|user|system|tool)\|>?|<\/\|end\|>?/gi;
const QWEN_BARE_MARKER_RE = /(?:^|[\t\r\n ])(?:\|?end\|?|\/?\|end\|)(?=[\t\r\n ]|$)|(?:^|[\t\r\n ])assistan(?:t)?(?=[\t\r\n ]|$)/gi;
// Self-narration about tool results (model echoing stdout/exit_code)
const TOOL_NARRATION_RE = /(?:The (?:result|output) shows?:?\s*)?-?\s*(?:stdout|stderr|exit_code):\s*.+/gi;


// Model pricing table — per million tokens
// Model info: pricing (per 1M tokens) + context window length
const MODEL_INFO = {
  // --- Anthropic ---
  'claude-sonnet-4-5':    { input: 3.00,  output: 15.00, ctx: 200000 },
  'claude-sonnet-4-6':    { input: 3.00,  output: 15.00, ctx: 200000 },
  'claude-sonnet-4':      { input: 3.00,  output: 15.00, ctx: 200000 },
  'claude-opus-4':        { input: 15.00, output: 75.00, ctx: 200000 },
  'claude-opus-4-6':      { input: 15.00, output: 75.00, ctx: 200000 },
  'claude-haiku-4':       { input: 0.80,  output: 4.00,  ctx: 200000 },
  'claude-haiku-3-5':     { input: 0.80,  output: 4.00,  ctx: 200000 },
  'claude-3-5-sonnet':    { input: 3.00,  output: 15.00, ctx: 200000 },
  'claude-3-5-haiku':     { input: 0.80,  output: 4.00,  ctx: 200000 },
  'claude-3-opus':        { input: 15.00, output: 75.00, ctx: 200000 },
  'claude-3-sonnet':      { input: 3.00,  output: 15.00, ctx: 200000 },
  'claude-3-haiku':       { input: 0.25,  output: 1.25,  ctx: 200000 },
  // --- OpenAI ---
  'gpt-5':                { input: 2.00,  output: 8.00,  ctx: 400000 },
  'gpt-4.1':              { input: 2.00,  output: 8.00,  ctx: 1047576 },
  'gpt-4.1-mini':         { input: 0.40,  output: 1.60,  ctx: 1047576 },
  'gpt-4.1-nano':         { input: 0.10,  output: 0.40,  ctx: 1047576 },
  'gpt-4o':               { input: 2.50,  output: 10.00, ctx: 128000 },
  'gpt-4o-mini':          { input: 0.15,  output: 0.60,  ctx: 128000 },
  'gpt-4-turbo':          { input: 10.00, output: 30.00, ctx: 128000 },
  'o1':                   { input: 15.00, output: 60.00, ctx: 200000 },
  'o1-mini':              { input: 3.00,  output: 12.00, ctx: 128000 },
  'o1-pro':               { input: 150.0, output: 600.0, ctx: 200000 },
  'o3':                   { input: 2.00,  output: 8.00,  ctx: 200000 },
  'o3-mini':              { input: 1.10,  output: 4.40,  ctx: 200000 },
  'o4-mini':              { input: 1.10,  output: 4.40,  ctx: 200000 },
  // --- DeepSeek ---
  'deepseek-chat':        { input: 0.27,  output: 1.10,  ctx: 64000 },
  'deepseek-coder':       { input: 0.27,  output: 1.10,  ctx: 64000 },
  'deepseek-reasoner':    { input: 0.55,  output: 2.19,  ctx: 64000 },
  'deepseek-r1':          { input: 0.55,  output: 2.19,  ctx: 64000 },
  'deepseek-v3':          { input: 0.27,  output: 1.10,  ctx: 64000 },
  'deepseek-v2':          { input: 0.14,  output: 0.28,  ctx: 64000 },
  // --- Google ---
  'gemini-2.5-pro':       { input: 1.25,  output: 10.00, ctx: 1048576 },
  'gemini-2.5-flash':     { input: 0.15,  output: 0.60,  ctx: 1048576 },
  'gemini-2.0-flash':     { input: 0.10,  output: 0.40,  ctx: 1048576 },
  'gemini-1.5-pro':       { input: 1.25,  output: 5.00,  ctx: 1048576 },
  'gemini-1.5-flash':     { input: 0.075, output: 0.30,  ctx: 1048576 },
  'gemma-3':              { input: 0.10,  output: 0.10,  ctx: 128000 },
  // --- Mistral ---
  'mistral-large':        { input: 2.00,  output: 6.00,  ctx: 128000 },
  'mistral-medium':       { input: 2.00,  output: 6.00,  ctx: 32000 },
  'mistral-small':        { input: 0.20,  output: 0.60,  ctx: 32000 },
  'mistral-nemo':         { input: 0.15,  output: 0.15,  ctx: 128000 },
  'mixtral':              { input: 0.24,  output: 0.24,  ctx: 32000 },
  'codestral':            { input: 0.30,  output: 0.90,  ctx: 32000 },
  'pixtral':              { input: 2.00,  output: 6.00,  ctx: 128000 },
  // --- xAI ---
  'grok-4':               { input: 3.00,  output: 15.00, ctx: 131072 },
  'grok-3':               { input: 3.00,  output: 15.00, ctx: 131072 },
  'grok-2':               { input: 2.00,  output: 10.00, ctx: 131072 },
  // --- Meta ---
  'llama-4':              { input: 0.20,  output: 0.20,  ctx: 1048576 },
  'llama-3.3':            { input: 0.20,  output: 0.20,  ctx: 131072 },
  'llama-3.2':            { input: 0.20,  output: 0.20,  ctx: 131072 },
  'llama-3.1':            { input: 0.20,  output: 0.20,  ctx: 131072 },
  'llama-3':              { input: 0.20,  output: 0.20,  ctx: 131072 },
  // --- Qwen ---
  'qwen3':                { input: 0.30,  output: 1.20,  ctx: 131072 },
  'qwen2.5':              { input: 0.30,  output: 1.20,  ctx: 131072 },
  'qwq':                  { input: 0.30,  output: 1.20,  ctx: 32768 },
  // --- Cohere ---
  'command-a':            { input: 2.50,  output: 10.00, ctx: 256000 },
  'command-r-plus':       { input: 2.50,  output: 10.00, ctx: 128000 },
  'command-r':            { input: 0.15,  output: 0.60,  ctx: 128000 },
  // --- Perplexity ---
  'sonar-pro':            { input: 3.00,  output: 15.00, ctx: 200000 },
  'sonar':                { input: 1.00,  output: 1.00,  ctx: 128000 },
  // --- MiniMax ---
  'minimax':              { input: 0.70,  output: 0.70,  ctx: 1000000 },
  // --- Kimi / Moonshot ---
  'moonshot':             { input: 1.00,  output: 1.00,  ctx: 128000 },
  'kimi':                 { input: 1.00,  output: 1.00,  ctx: 128000 },
  // --- Microsoft ---
  'phi-4':                { input: 0.07,  output: 0.14,  ctx: 16000 },
  'phi-3':                { input: 0.07,  output: 0.14,  ctx: 128000 },
  // --- Nvidia ---
  'nemotron':             { input: 0.30,  output: 1.20,  ctx: 131072 },
  // --- Nous ---
  'hermes':               { input: 0.20,  output: 0.20,  ctx: 131072 },
};

// Compat alias
const MODEL_PRICING = MODEL_INFO;

// Image generation cost lookup (per-image, by model × quality × size)
const IMAGE_PRICING = {
  'gpt-image-1.5': { 'low': { '1024x1024': 0.009, '1024x1536': 0.013, '1536x1024': 0.013 }, 'medium': { '1024x1024': 0.034, '1024x1536': 0.05, '1536x1024': 0.05 }, 'high': { '1024x1024': 0.133, '1024x1536': 0.2, '1536x1024': 0.2 } },
  'gpt-image-1':   { 'low': { '1024x1024': 0.011, '1024x1536': 0.016, '1536x1024': 0.016 }, 'medium': { '1024x1024': 0.042, '1024x1536': 0.063, '1536x1024': 0.063 }, 'high': { '1024x1024': 0.167, '1024x1536': 0.25, '1536x1024': 0.25 } },
  'gpt-image-1-mini': { 'low': { '1024x1024': 0.005, '1024x1536': 0.006, '1536x1024': 0.006 }, 'medium': { '1024x1024': 0.011, '1024x1536': 0.015, '1536x1024': 0.015 }, 'high': { '1024x1024': 0.036, '1024x1536': 0.052, '1536x1024': 0.052 } },
};

export function shortModel(name) {
  if (!name) return '...';
  if (typeof name !== 'string') name = String(name);
  let short = name.split('/').pop();
  // Strip .gguf extension
  short = short.replace(/\.gguf$/i, '');
  // Strip quantization suffixes (Q4_K_M, Q8_0, etc.) and shard numbers
  short = short.replace(/-0000\d-of-\d+$/, '');
  short = short.replace(/[-_](Q\d[_A-Z\d]*|F16|F32|BF16|fp16|fp32)$/i, '');
  // Truncate if still too long (keep first meaningful part)
  if (short.length > 25) {
    // Try to find a natural break point (dash after model size like -35B or -7B)
    const sizeMatch = short.match(/^(.+?-\d+[BbMm])/);
    if (sizeMatch) short = sizeMatch[1];
    else short = short.substring(0, 22) + '…';
  }
  return short;
}

function modelValue(name) {
  if (name == null) return '';
  return String(name).trim();
}

export function sameModelName(left, right) {
  const a = modelValue(left);
  const b = modelValue(right);
  if (!a || !b) return false;
  return a.toLowerCase() === b.toLowerCase()
    || shortModel(a).toLowerCase() === shortModel(b).toLowerCase();
}

export function modelRouteLabel(requestedModel, actualModel) {
  const requested = modelValue(requestedModel);
  const actual = modelValue(actualModel) || requested;
  if (!requested || sameModelName(requested, actual)) return shortModel(actual || requested);
  return shortModel(requested) + ' -> ' + shortModel(actual);
}

export function replyModelPair(modelName, metadata) {
  const meta = metadata || {};
  const actualFromMeta = modelValue(meta.model || meta.actual_model);
  const requestedFromMeta = modelValue(meta.requested_model || meta.selected_model);
  if (actualFromMeta || requestedFromMeta) {
    const actual = actualFromMeta || requestedFromMeta || modelValue(modelName);
    const requested = requestedFromMeta || actual;
    return { requestedModel: requested, actualModel: actual };
  }
  const fallback = modelValue(modelName);
  return { requestedModel: fallback, actualModel: fallback };
}

/**
 * Generate a consistent HSL color for a model name.
 * Returns an hsl() string. The hue is derived from a string hash,
 * saturation and lightness are fixed for readability on dark/light themes.
 */
export function modelColor(name) {
  if (!name) return null;
  const key = name.toLowerCase();
  let hash = 0;
  for (let i = 0; i < key.length; i++) {
    hash = ((hash << 5) - hash + key.charCodeAt(i)) | 0;
  }
  const hue = ((hash % 360) + 360) % 360;
  return `hsl(${hue}, 55%, 65%)`;
}

/** Look up model info (pricing + context) by substring match */
export function getModelInfo(modelName) {
  if (!modelName) return null;
  const key = matchModelKey(modelName, Object.keys(MODEL_INFO));
  return key ? { key, ...MODEL_INFO[key] } : null;
}

function _fmtCtx(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
  return Math.round(n / 1000) + 'K';
}

/**
 * Apply model color to a role element (sets color + dot color).
 */
export function applyModelColor(roleEl, modelName) {
  if (!modelName) return;
  const color = modelColor(modelName);
  if (color) {
    roleEl.style.color = color;
    roleEl.style.setProperty('--model-dot', color);
  }
  // Replace generic dot with provider logo if available
  const logo = providerLogo(modelName);
  const existingLogo = roleEl.querySelector('.role-provider-logo');
  if (!logo) {
    if (existingLogo) existingLogo.remove();
    roleEl.classList.remove('has-logo');
  } else if (!existingLogo) {
    const span = document.createElement('span');
    span.className = 'role-provider-logo';
    span.innerHTML = logo;
    roleEl.classList.add('has-logo');
    roleEl.prepend(span);
  }
  // Click to show model info popup
  if (!roleEl._hasInfoClick) {
    roleEl._hasInfoClick = true;
    roleEl.style.cursor = 'pointer';
    roleEl.addEventListener('click', (e) => {
      e.stopPropagation();
      document.querySelectorAll('.ctx-popup').forEach(p => { if (typeof p._dismiss === 'function') p._dismiss(); else p.remove(); });
      const info = getModelInfo(modelName);
      const short = shortModel(modelName);
      const logoHtml = providerLogo(modelName);
      const popup = document.createElement('div');
      popup.className = 'ctx-popup';
      let html = '<div style="font-weight:600;margin-bottom:6px;color:var(--fg);display:flex;align-items:center;gap:6px;">';
      if (logoHtml) html += '<span class="role-provider-logo" style="opacity:0.7">' + logoHtml + '</span>';
      html += uiModule.esc(short) + '</div>';
      html += '<div><span class="ctx-label">Model</span> ' + uiModule.esc(modelName.split('/').pop()) + '</div>';
      // Provider = the serving endpoint, distinct from the model vendor/logo
      // (e.g. the same model via OpenRouter vs Copilot vs Anthropic direct).
      const _epUrl = (window.sessionModule && window.sessionModule.getCurrentEndpointUrl)
        ? window.sessionModule.getCurrentEndpointUrl() : null;
      const _provLabel = providerLabel(_epUrl);
      if (_provLabel) html += '<div><span class="ctx-label">Provider</span> ' + uiModule.esc(_provLabel) + '</div>';
      // Show static context initially, then fetch real from server
      const _realCtx = window._realContextLengths && window._realContextLengths[modelName];
      if (_realCtx) {
        html += '<div><span class="ctx-label">Context</span> ' + _fmtCtx(_realCtx) + ' tokens';
        if (info && info.ctx && info.ctx !== _realCtx) html += ' <span style="opacity:0.35">(spec: ' + _fmtCtx(info.ctx) + ')</span>';
        html += '</div>';
      } else if (info && info.ctx) {
        html += '<div><span class="ctx-label">Context</span> <span id="_ctx-val">' + _fmtCtx(info.ctx) + ' tokens</span></div>';
      }
      // Fetch real context from server async
      if (!_realCtx && window.sessionModule) {
        const _sid = window.sessionModule.getCurrentSessionId();
        if (_sid) {
          fetch('/api/session/' + _sid + '/context_info').then(r => r.ok ? r.json() : null).then(d => {
            if (d && d.context_length) {
              if (!window._realContextLengths) window._realContextLengths = {};
              window._realContextLengths[modelName] = d.context_length;
              const el = document.getElementById('_ctx-val');
              if (el) {
                el.innerHTML = _fmtCtx(d.context_length) + ' tokens';
                if (info && info.ctx && info.ctx !== d.context_length) {
                  el.innerHTML += ' <span style="opacity:0.35">(spec: ' + _fmtCtx(info.ctx) + ')</span>';
                }
              }
            }
          }).catch(() => {});
        }
      }
      // Show configured max tokens if set
      if (window.presetsModule) {
        const _pid = window.presetsModule.getSelectedPreset();
        const _preset = _pid ? window.presetsModule.getPreset(_pid) : null;
        const _mt = _preset?.max_tokens;
        if (_mt && _mt > 0 && _mt <= 8192) {
          html += '<div><span class="ctx-label">Max tokens</span> ' + _mt.toLocaleString() + ' <span style="opacity:0.4">(configured)</span></div>';
        }
      }
      if (isCostTrackedEndpoint(_epUrl)) {
        if (info && info.input != null) html += '<div><span class="ctx-label">Input</span> $' + info.input.toFixed(2) + ' / 1M</div>';
        if (info && info.output != null) html += '<div><span class="ctx-label">Output</span> $' + info.output.toFixed(2) + ' / 1M</div>';
        if (!info) html += '<div style="opacity:0.4;font-size:0.85em;margin-top:4px;">No pricing data available</div>';
      }
      popup.innerHTML = html;
      const rect = roleEl.getBoundingClientRect();
      popup.style.top = (rect.bottom + 4) + 'px';
      popup.style.left = rect.left + 'px';
      document.body.appendChild(popup);
      const pr = popup.getBoundingClientRect();
      if (pr.bottom > window.innerHeight - 8) popup.style.top = (rect.top - pr.height - 4) + 'px';
      if (pr.right > window.innerWidth - 8) popup.style.left = (window.innerWidth - pr.width - 8) + 'px';
      bindMenuDismiss(popup, () => popup.remove());
    });
  }
}

export function getModelCost(modelName, inputTokens, outputTokens) {
  if (!modelName) return null;
  const key = matchModelKey(modelName, Object.keys(MODEL_PRICING));
  if (!key) return null;
  const price = MODEL_PRICING[key];
  return (inputTokens * price.input + outputTokens * price.output) / 1_000_000;
}

/**
 * Is this endpoint a local / self-hosted model server (vLLM, Ollama, …)?
 * Local models are free, so we must NOT bill them at cloud rates — the
 * pricing table matches on a name substring, so a local `qwen2.5-coder`
 * would otherwise be charged like cloud `qwen2.5`. When the serving host is
 * loopback, a private LAN range, Tailscale CGNAT (100.64–100.127.x), a
 * `.local` name, or the app's own host, the model is local → free.
 * Unknown / missing endpoint also counts as local (bias to not over-bill).
 */
export function isLocalEndpoint(url) {
  if (!url) return true;
  let host;
  try { host = new URL(url).hostname; } catch (_e) { return true; }
  if (!host) return true;
  if (host === 'localhost' || host === '0.0.0.0' || host === 'host.docker.internal' || host.endsWith('.local')) return true;
  if (typeof window !== 'undefined' && window.location && host === window.location.hostname) return true;
  // A single-label hostname (no dot) is an internal/Docker service name
  // (e.g. "nim-nano", "llamaswap", "nemotron-super-49b") or a LAN shortname —
  // never a public API, which always needs an FQDN. Treat as local → free.
  // (Without this, container-name endpoints get billed at cloud rates because
  // the pricing table matches on a name substring, e.g. "nemotron".)
  if (!host.includes('.')) return true;
  if (/^127\./.test(host)) return true;
  if (/^10\./.test(host)) return true;
  if (/^192\.168\./.test(host)) return true;
  if (/^172\.(1[6-9]|2\d|3[01])\./.test(host)) return true;
  const cg = host.match(/^100\.(\d+)\./);            // Tailscale CGNAT
  if (cg && +cg[1] >= 64 && +cg[1] <= 127) return true;
  return false;
}

export function isSubscriptionEndpoint(url) {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    const path = parsed.pathname.replace(/\/+$/, '');
    return parsed.hostname === 'chatgpt.com'
      && (path === '/backend-api/codex' || path.startsWith('/backend-api/codex/'));
  } catch (_e) {
    return false;
  }
}

function _currentEndpointUrl() {
  return (window.sessionModule && window.sessionModule.getCurrentEndpointUrl)
    ? window.sessionModule.getCurrentEndpointUrl() : null;
}

export function isCostTrackedEndpoint(url) {
  return !isLocalEndpoint(url) && !isSubscriptionEndpoint(url);
}

/** Cost for the current turn, returning null for non-billable endpoints. */
function _billableCost(model, inputTokens, outputTokens) {
  const url = _currentEndpointUrl();
  if (!isCostTrackedEndpoint(url)) return null;
  return getModelCost(model, inputTokens, outputTokens);
}

export function getImageCost(model, quality, size) {
  if (!model) return null;
  const m = model.toLowerCase();
  for (const [key, quals] of Object.entries(IMAGE_PRICING)) {
    if (m.includes(key)) {
      const q = quals[(quality || 'medium').toLowerCase()] || quals['medium'];
      return q ? (q[size] || q['1024x1024'] || null) : null;
    }
  }
  return null;
}

/* ── Session cost helpers ─────────────────────────────────────────── */
const _COST_KEY = 'ody-session-cost';

/** Return the accumulated cost for the current (or given) session. */
export function getSessionCost(sessionId) {
  const sid = sessionId || (window.sessionModule && window.sessionModule.getCurrentSessionId());
  if (!sid) return 0;
  try {
    const costs = JSON.parse(localStorage.getItem(_COST_KEY) || '{}');
    return costs[sid] || 0;
  } catch (_e) { return 0; }
}

/** Reset session cost for the given session (defaults to current). */
export function resetSessionCost(sessionId) {
  const sid = sessionId || (window.sessionModule && window.sessionModule.getCurrentSessionId());
  if (!sid) return;
  try {
    const costs = JSON.parse(localStorage.getItem(_COST_KEY) || '{}');
    delete costs[sid];
    localStorage.setItem(_COST_KEY, JSON.stringify(costs));
  } catch (_e) { /* ignore */ }
  updateSessionCostUI();
}

/** Update the persistent session-cost badge in the input bar. */
export function updateSessionCostUI() {
  const el = document.getElementById('session-cost-display');
  if (!el) return;
  // Non-billable endpoint? Hide the badge and clear stale cost that a previous
  // cloud-rate calculation may have left in localStorage for this session.
  const _url = _currentEndpointUrl();
  if (!isCostTrackedEndpoint(_url)) {
    const sid = window.sessionModule && window.sessionModule.getCurrentSessionId();
    if (sid && getSessionCost(sid) > 0) {
      try {
        const costs = JSON.parse(localStorage.getItem(_COST_KEY) || '{}');
        delete costs[sid];
        localStorage.setItem(_COST_KEY, JSON.stringify(costs));
      } catch (_e) { /* ignore */ }
    }
    el.style.display = 'none';
    return;
  }
  const cost = getSessionCost();
  if (cost > 0) {
    el.textContent = '$' + (cost < 0.01 ? cost.toFixed(4) : cost < 1 ? cost.toFixed(3) : cost.toFixed(2));
    el.style.display = '';
  } else {
    el.style.display = 'none';
  }
}

/** Create a timestamp span for role labels.
 * Pass an ISO string / Date / epoch-ms to render the message's own time
 * (used when replaying history). Falls back to "now" when no value is given. */
export function roleTimestamp(when) {
  const ts = document.createElement('span');
  ts.className = 'role-timestamp';
  let d;
  if (when instanceof Date) d = when;
  else if (typeof when === 'number') d = new Date(when);
  else if (typeof when === 'string' && when) d = new Date(when);
  else d = new Date();
  if (isNaN(d.getTime())) d = new Date();
  ts.textContent = d.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
  ts.title = d.toLocaleString();
  return ts;
}

/**
 * Strip tool invocation blocks from text before rendering.
 */
export function stripToolBlocks(text) {
  let cleaned = text.replace(TOOL_CALL_RE, '');
  if (EXEC_FENCE_RE) cleaned = cleaned.replace(EXEC_FENCE_RE, stripExecutedFence);
  cleaned = cleaned.replace(DSML_TOOL_RE, '');
  cleaned = cleaned.replace(DSML_INVOKE_RE, '');
  cleaned = cleaned.replace(DSML_STRAY_RE, '');
  cleaned = cleaned.replace(XML_TOOL_CALL_RE, '');
  cleaned = cleaned.replace(XML_INVOKE_RE, '');
  cleaned = cleaned.replace(RAW_OPENAI_TOOL_JSON_RE, '');
  cleaned = cleaned.replace(QWEN_ROLE_MARKER_RE, '');
  cleaned = cleaned.replace(QWEN_BARE_MARKER_RE, ' ');
  cleaned = cleaned.replace(TOOL_NARRATION_RE, '');
  cleaned = cleaned.replace(/\n{3,}/g, '\n\n');
  return cleaned.trim();
}

/**
 * Plain-text payload for the message copy buttons: the reply as the renderer
 * displays it — tool blocks and <think> reasoning stripped. dataset.raw keeps
 * the full model output (chat.js even embeds the elapsed time into the
 * <think> tag for reload persistence), so copying it verbatim leaks the
 * thinking block (#3722). Falls back to the raw text when stripping leaves
 * nothing (e.g. turns interrupted mid-thinking).
 */
export function copyMessageText(msgElement) {
  const raw = msgElement.dataset.raw || msgElement.querySelector('.body')?.textContent || '';
  const { content } = markdownModule.extractThinkingBlocks(stripToolBlocks(raw));
  return content || raw;
}

/**
 * Build a collapsible sources box (used by both research and web search).
 */
export function buildSourcesBox(sources, type, expanded) {
  var esc = uiModule.esc;
  var id = 'sources-' + Date.now() + '-' + Math.random().toString(36).substr(2, 5);
  var count = sources.length;
  var label = type === 'research' ? 'Research sources' : 'Web sources';
  var lines = '';
  for (var i = 0; i < count; i++) {
    var s = sources[i];
    var domain = '';
    try { domain = new URL(s.url).hostname.replace('www.', ''); } catch(e) { domain = s.url; }
    var title = esc(s.title || domain || '');
    var safeUrl = _safeHref(s.url);
    lines += '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer" class="source-link">'
      + '<span class="source-num">' + (i + 1) + '</span>'
      + '<span class="source-title">' + title + '</span>'
      + '<span class="source-domain">' + esc(domain) + '</span>'
      + '</a>';
  }
  var arrow = expanded ? 'down' : 'right';
  var expandedClass = expanded ? ' expanded' : '';
  return '<div class="sources-section">'
    + '<div class="sources-header" data-sources-id="' + id + '" onclick="window.toggleSources(\'' + id + '\')">'
    + '<div class="sources-header-left">' + SEARCH_ICON + '<span>' + count + ' ' + label + '</span></div>'
    + '<span class="sources-toggle" id="' + id + '-toggle" data-arrow="' + arrow + '"></span>'
    + '</div>'
    + '<div class="sources-content' + expandedClass + '" id="' + id + '">'
    + '<div class="sources-content-inner">' + lines + '</div>'
    + '</div></div>';
}

/**
 * Build the RAG "Sources (N documents)" box — mirrors the live render in
 * chat.js so persisted rag_sources survive a refresh. Items carry a
 * filename, similarity %, and snippet (not URLs, unlike web sources).
 * @param {Array<{filename, similarity, snippet}>} sources
 */
export function buildRagSourcesBox(sources) {
  if (!sources || !sources.length) return '';
  var esc = uiModule.esc;
  var items = '';
  for (var i = 0; i < sources.length; i++) {
    var s = sources[i] || {};
    var pct = (typeof s.similarity === 'number') ? (s.similarity * 100).toFixed(1) + '%' : '';
    items += '<div class="rag-source-item"><strong>' + esc(s.filename || '') + '</strong>'
      + (pct ? ' <span class="rag-similarity">' + pct + '</span>' : '')
      + '<div class="rag-snippet">' + esc(s.snippet || '') + '</div></div>';
  }
  return '<details class="rag-sources"><summary>Sources (' + sources.length + ' documents)</summary>' + items + '</details>';
}

/** Display the canonical handbook identity supplied by the Buddy tool. */
export function addBuddyHandbookLink(body, source) {
  if (!body || !/^openclank-docs-[a-z0-9-]+$/.test(source?.article_id || '')
      || typeof source.term !== 'string' || !/^[A-Za-z ]{1,80}$/.test(source.term)
      || typeof source.title !== 'string' || !source.title || source.title.length > 120) return;
  if (body.querySelector('.buddy-handbook-link')) return;
  const standalone = new RegExp('(?<![A-Za-z0-9_])' + source.term + '(?![A-Za-z0-9_])', 'i');
  const text = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  let match, node;
  while ((node = text.nextNode())) {
    if (node.parentElement.closest('a,button,code,pre,.thinking-section,.rag-sources,.sources-section')) continue;
    match = standalone.exec(node.data);
    if (match) break;
  }
  const link = document.createElement('button');
  link.type = 'button';
  link.className = 'buddy-handbook-link';
  link.textContent = match?.[0] || 'Instruction Manual · ' + source.title;
  link.title = 'Open the Instruction Manual · ' + source.title;
  link.addEventListener('click', async () => {
    const session = window.sessionModule;
    const navigation = session?.getSessionNavigationToken?.();
    const account = document.body.dataset.accountId;
    const current = () => link.isConnected && account && document.body.dataset.accountId === account
      && session?.getSessionNavigationToken?.() === navigation;
    if (!current() || link.disabled) return;
    link.disabled = true;
    try {
      if (!await openClankHandbook({articleId:source.article_id, isCurrent:current}) && current())
        uiModule.showToast('This handbook article is unavailable.');
    } catch (_) {
      if (current()) uiModule.showToast('Could not open the handbook article.');
    } finally {
      link.disabled = false;
    }
  });
  if (match) {
    const found = node.splitText(match.index);
    found.splitText(match[0].length);
    found.replaceWith(link);
  } else {
    const row = document.createElement('p');
    row.append(link);
    body.append(row);
  }
}

/**
 * Build a collapsible "Raw collected findings" section, styled like the sources box.
 * @param {Array<{url, title, summary}>} findings
 * @param {boolean} [expanded=false]
 */
export function buildFindingsBox(findings, expanded) {
  if (!findings || !findings.length) return '';
  var esc = uiModule.esc;
  var id = 'findings-' + Date.now() + '-' + Math.random().toString(36).substr(2, 5);
  var count = findings.length;
  var lines = '';
  for (var i = 0; i < count; i++) {
    var f = findings[i];
    var domain = '';
    try { domain = new URL(f.url).hostname.replace('www.', ''); } catch(e) { domain = f.url; }
    var title = esc(f.title || domain || '');
    var summary = esc(f.summary || '');
    var safeUrl = _safeHref(f.url);
    lines += '<div class="finding-item">'
      + '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer" class="source-link">'
      + '<span class="source-num">' + (i + 1) + '</span>'
      + '<span class="source-title">' + title + '</span>'
      + '<span class="source-domain">' + esc(domain) + '</span>'
      + '</a>'
      + '<div class="finding-summary">' + summary + '</div>'
      + '</div>';
  }
  var FINDINGS_ICON = uiIcon("document", 14);
  var arrow = expanded ? 'down' : 'right';
  var expandedClass = expanded ? ' expanded' : '';
  return '<div class="sources-section">'
    + '<div class="sources-header" data-sources-id="' + id + '" onclick="window.toggleSources(\'' + id + '\')">'
    + '<div class="sources-header-left">' + FINDINGS_ICON + '<span>' + count + ' Raw collected findings</span></div>'
    + '<span class="sources-toggle" id="' + id + '-toggle" data-arrow="' + arrow + '"></span>'
    + '</div>'
    + '<div class="sources-content' + expandedClass + '" id="' + id + '">'
    + '<div class="sources-content-inner">' + lines + '</div>'
    + '</div></div>';
}

/** Append report button + continue research prompt. */
export function appendReportButton(container, sessionId) {
  _appendReportButton(container, sessionId);
  _appendContinuePrompt(container);
}

function _appendContinuePrompt(container) {
  var wrap = document.createElement('div');
  wrap.className = 'continue-research-wrap';
  wrap.innerHTML =
    '<div class="continue-research-hint">'
    + uiIcon("search", 13)
    + '<span>Dig deeper? Activate Research again and type a follow-up question to continue this research.</span>'
    + '</div>';
  container.appendChild(wrap);
}
function _appendReportButton(container, sessionId) {
  var apiBase = window.API_BASE || '';

  // Wrapper holds report button + chat-about button
  var wrap = document.createElement('div');
  wrap.className = 'report-btn-wrap';

  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'view-report-btn';
  btn.innerHTML = REPORT_ICON + ' Open Visual Report';

  var reportUrl = apiBase + '/api/research/report/' + sessionId;
  btn.addEventListener('click', function() {
    window.open(reportUrl, '_blank');
  });
  wrap.appendChild(btn);

  var chatBtn = document.createElement('button');
  chatBtn.type = 'button';
  chatBtn.className = 'view-report-btn chat-about-btn';
  chatBtn.innerHTML = CHAT_ABOUT_ICON + ' Discuss';
  chatBtn.addEventListener('click', async function() {
    if (chatBtn.disabled) return;
    var origLabel = chatBtn.innerHTML;
    chatBtn.disabled = true;
    chatBtn.innerHTML = CHAT_ABOUT_ICON + ' Creating…';
    try {
      var res = await fetch(apiBase + '/api/research/spinoff/' + sessionId, { method: 'POST' });
      if (!res.ok) {
        var detail = '';
        try { detail = (await res.json()).detail || ''; } catch {}
        throw new Error(detail || ('HTTP ' + res.status));
      }
      var payload = await res.json();
      if (window.sessionModule && payload.session_id) {
        await window.sessionModule.loadSessions().catch(() => {});
        await window.sessionModule.selectSession(payload.session_id);
      }
    } catch (e) {
      chatBtn.disabled = false;
      chatBtn.innerHTML = origLabel;
      if (window.uiModule && uiModule.showError) {
        uiModule.showError('Could not start follow-up chat: ' + e.message);
      } else {
        alert('Could not start follow-up chat: ' + e.message);
      }
    }
  });
  wrap.appendChild(chatBtn);

  container.appendChild(wrap);
}

window.toggleSources = function(id) {
  // Debounce to prevent double-fire from both inline onclick and delegation
  var now = Date.now();
  if (window._lastSourcesToggle && now - window._lastSourcesToggle < 100) return;
  window._lastSourcesToggle = now;

  var content = document.getElementById(id);
  var toggle = document.getElementById(id + '-toggle');
  if (content && toggle) {
    var expanded = content.classList.contains('expanded');
    content.classList.toggle('expanded', !expanded);
    toggle.dataset.arrow = expanded ? 'right' : 'down';
  }
};

// Event delegation for sources toggle (capture phase, handles SVG targets)
document.addEventListener('click', function(e) {
  // Walk up from target manually to handle SVG elements that may not support closest()
  var el = e.target;
  while (el && el !== document) {
    if (el.classList && el.classList.contains('sources-header') && el.dataset && el.dataset.sourcesId) {
      e.stopPropagation();
      window.toggleSources(el.dataset.sourcesId);
      return;
    }
    el = el.parentElement || el.parentNode;
  }
}, true);

function resolveDocumentPlaceholderLinks(text, metadata) {
  if (!text || !metadata || !Array.isArray(metadata.tool_events)) return text;
  const docEvents = metadata.tool_events.filter(ev => ev && ev.doc_id);
  if (!docEvents.length) return text;
  return String(text).replace(/#document-(\d+)\b/g, (match, num) => {
    const idx = Number(num) - 1;
    const ev = Number.isInteger(idx) && idx >= 0 ? docEvents[idx] : null;
    return ev && ev.doc_id ? `#document-${ev.doc_id}` : match;
  });
}

// Jump-to-entity anchors — the agent emits links like
//   [New Chat](#session-89effa28)
//   [Notes](#document-abc123)
//   [Reminder](#note-42)
// and the chat-history click delegate turns them into navigation
// instead of default in-page anchor jumps. Each prefix routes to the
// matching module via a dynamic import (avoids circular deps —
// sessions.js itself imports chatRenderer.js).
document.addEventListener('click', function(e) {
  // Walk past Text nodes — clicking link text yields a Text node target
  // whose .closest is undefined, so preventDefault never fires and the
  // browser performs a default hash-navigation that resets the session.
  let _t = e.target;
  while (_t && _t.nodeType === Node.TEXT_NODE) _t = _t.parentElement;
  const a = _t && _t.closest && _t.closest('a[href]');
  if (!a) return;
  const rawHref = a.getAttribute('href') || '';
  let href = rawHref;
  try {
    const parsed = new URL(rawHref, window.location.origin);
    if (parsed.origin === window.location.origin && parsed.pathname === window.location.pathname) {
      href = parsed.hash || rawHref;
    }
  } catch (_) {}
  if (!href.startsWith('#')) return;
  let m = href.match(/^#(session|document|note|image|email|event|task|skill|research)-(.+)$/);
  if (!m) {
    const noteOpen = href.match(/^#open=notes&note=([^&]+)/);
    if (noteOpen) m = ['note', 'note', decodeURIComponent(noteOpen[1])];
  }
  if (!m) {
    const bareSession = href.match(/^#([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i);
    if (bareSession) m = ['session', 'session', bareSession[1]];
  }
  if (!m) return;
  e.preventDefault();
  e.stopPropagation();
  const [, kind, id] = m;
  if (kind === 'session') {
    try {
      a.classList.add('is-loading');
      a.setAttribute('aria-busy', 'true');
    } catch {}
    import('./sessions.js').then(mod => {
      const fn = mod.selectSession || (mod.default && mod.default.selectSession);
      if (fn) return fn(id, { showLoading: true, immediateLoading: true });
    }).finally(() => {
      try {
        a.classList.remove('is-loading');
        a.removeAttribute('aria-busy');
      } catch {}
    });
  } else if (kind === 'document') {
    import('./document.js?v=20260722emailfastindex1').then(mod => {
      const open = mod.loadDocument
        || mod.openDocument
        || (mod.default && (mod.default.loadDocument || mod.default.openDocument));
      if (open) open(id);
    }).catch(() => {});
  } else if (kind === 'note') {
    import('./notes.js').then(mod => {
      const open = mod.openNote || (mod.default && mod.default.openNote);
      if (open) open(id);
      try {
        if (/^#(?:note-|open=notes&note=)/.test(window.location.hash || '')) {
          history.replaceState(null, '', window.location.pathname + window.location.search);
        }
      } catch (_) {}
    }).catch(() => {});
  } else if (kind === 'image') {
    const scope=window.chatModule?.ratingAccountScope?.(), session=window.sessionModule;
    const navigation=session?.getSessionNavigationToken?.(), originalHref=rawHref;
    const current=()=>{ const now=window.chatModule?.ratingAccountScope?.(); return a.isConnected && a.getAttribute('href')===originalHref && scope?.sid && scope?.owner && now?.sid===scope.sid && now.owner===scope.owner && now.authEpoch===scope.authEpoch && document.body.dataset.accountId===scope.owner && session?.getCurrentSessionId?.()===scope.sid && session?.getSessionNavigationToken?.()===navigation; };
    (async()=>{
      if (!/^[A-Za-z0-9._-]{1,240}$/.test(id) || navigation===undefined || !current()) throw new Error('Gallery image authority is unavailable.');
      const resource=await filesFacadeClient.resolveResource({provider:'files',resource_id:`image:${id}`,account_id:scope.owner});
      if (!current() || resource?.kind!=='image' || !resource.ref || resource.revision?.kind!=='imageSha256' || !/^sha256:[a-f0-9]{64}$/.test(resource.revision.value || '')) throw new Error('The Gallery image is unavailable or changed.');
      await openImageResource(resource.ref,{expectedRevision:resource.revision,isCurrent:current});
    })().catch(error => uiModule.showToast?.(error.message || 'The Gallery image is unavailable.'));
  } else if (kind === 'email') {
    import('./emailLibrary.js?v=20260722emailfastindex1').then(mod => {
      const open = mod.openEmailLibrary || (mod.default && mod.default.openEmailLibrary);
      if (open) open({ uid: id });
    }).catch(() => {});
  } else if (kind === 'event') {
    import('./calendar.js').then(mod => {
      const open = mod.openCalendarTo || (mod.default && mod.default.openCalendarTo);
      if (open) open(id);
    }).catch(() => {});
  } else if (kind === 'task') {
    import('./tasks.js').then(mod => {
      const open = mod.openTasks || (mod.default && mod.default.openTasks);
      if (open) open(id);
      else { const b = document.getElementById('tasks-btn'); if (b) b.click(); }
    }).catch(() => { const b = document.getElementById('tasks-btn'); if (b) b.click(); });
  } else if (kind === 'skill') {
    import('./skills.js').then(mod => {
      const open = mod.openSkill || (mod.default && mod.default.openSkill);
      if (open) open(id);
    }).catch(() => {});
  } else if (kind === 'research') {
    import('./research/panel.js').then(mod => {
      const open = mod.openPanel || (mod.default && mod.default.openPanel);
      if (open) open(id);
    }).catch(() => {});
  }
}, true);

/**
 * Build a generated-image bubble element.
 */
export function buildImageBubble(imageUrl, prompt, model, size, quality, imageId) {
  var esc = uiModule.esc;
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-ai generated-image-wrap';
  wrap.dataset.imageUrl = imageUrl || '';
  wrap.dataset.imageKey = String(imageId || imageUrl || '');
  if (imageId) wrap.dataset.imageId = imageId;

  const role = document.createElement('div');
  role.className = 'role';
  role.textContent = (model || 'image').split('/').pop();
  wrap.appendChild(role);

  const body = document.createElement('div');
  body.className = 'body';

  const safeImageUrl = safeDisplayImageSrc(imageUrl);
  if (!safeImageUrl) {
    body.textContent = '[Image unavailable]';
    wrap.appendChild(body);
    return wrap;
  }

  const img = document.createElement('img');
  img.className = 'generated-image';
  img.alt = prompt || 'Generated image';
  img.title = prompt || 'Generated image';
  img.src = safeImageUrl;
  img.addEventListener('click', () => { window.open(safeImageUrl, '_blank', 'noopener,noreferrer'); });
  body.appendChild(img);

  if (prompt) {
    const caption = document.createElement('div');
    caption.className = 'generated-image-caption';
    caption.textContent = prompt;
    body.appendChild(caption);
  }

  wrap.appendChild(body);

  const footer = document.createElement('div');
  footer.className = 'msg-footer';

  const actions = document.createElement('span');
  actions.className = 'msg-actions';

  const copyBtn = document.createElement('button');
  copyBtn.className = 'footer-copy-btn';
  copyBtn.type = 'button';
  copyBtn.title = 'Copy prompt';
  copyBtn.innerHTML = COPY_ICON;
  copyBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    uiModule.copyToClipboard(prompt || '');
    copyBtn.innerHTML = CHECK_ICON;
    setTimeout(() => { copyBtn.innerHTML = COPY_ICON; }, 1500);
  });
  actions.appendChild(copyBtn);

  const dlBtn = document.createElement('button');
  dlBtn.className = 'footer-copy-btn';
  dlBtn.type = 'button';
  dlBtn.title = 'Download image';
  dlBtn.textContent = '\u2913';
  dlBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      const resp = await fetch(imageUrl);
      const blob = await resp.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (prompt || 'image').slice(0, 40).replace(/[^a-zA-Z0-9 ]/g, '') + '.png';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
      dlBtn.textContent = '\u2713';
      setTimeout(() => { dlBtn.textContent = '\u2913'; }, 1500);
    } catch { dlBtn.textContent = '\u2717'; setTimeout(() => { dlBtn.textContent = '\u2913'; }, 1500); }
  });
  actions.appendChild(dlBtn);

  const reuseBtn = document.createElement('button');
  reuseBtn.className = 'footer-copy-btn';
  reuseBtn.type = 'button';
  reuseBtn.title = 'Attach image to new prompt';
  reuseBtn.innerHTML = PAPERCLIP_ICON;
  reuseBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    reuseBtn.disabled = true;
    try {
      const resp = await fetch(safeImageUrl, { credentials: 'same-origin' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const blob = await resp.blob();
      const ext = (blob.type || '').includes('jpeg') ? 'jpg'
        : (blob.type || '').includes('webp') ? 'webp'
        : (blob.type || '').includes('gif') ? 'gif'
        : 'png';
      const base = (prompt || 'generated-image').slice(0, 36).replace(/[^a-zA-Z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'generated-image';
      const file = new File([blob], `${base}.${ext}`, { type: blob.type || 'image/png', lastModified: Date.now() });
      const mod = await import('./fileHandler.js');
      const addFiles = mod.addFiles || (mod.default && mod.default.addFiles);
      if (!addFiles) throw new Error('attachment handler unavailable');
      await addFiles([file], { skipCrop: true });
      const input = document.getElementById('message');
      if (input) input.focus();
      reuseBtn.innerHTML = CHECK_ICON;
      if (window.showToast) window.showToast('Image attached');
      setTimeout(() => { reuseBtn.innerHTML = PAPERCLIP_ICON; reuseBtn.disabled = false; }, 1400);
    } catch (err) {
      console.warn('Attach generated image failed', err);
      reuseBtn.textContent = '\u2717';
      if (window.showToast) window.showToast('Could not attach image');
      setTimeout(() => { reuseBtn.innerHTML = PAPERCLIP_ICON; reuseBtn.disabled = false; }, 1600);
    }
  });
  actions.appendChild(reuseBtn);

  const editBtn = document.createElement('button');
  editBtn.className = 'footer-copy-btn';
  editBtn.type = 'button';
  editBtn.title = 'Edit in image editor';
  editBtn.innerHTML = uiIcon("edit", 12);
  editBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      const parsed=new URL(imageUrl,window.location.origin), match=parsed.pathname.match(/^\/api\/generated-image\/([a-zA-Z0-9._-]{1,128})$/);
      if (parsed.origin!==window.location.origin || parsed.search || parsed.hash || !match || !window.chatModule?.openAuthorizedImageSource) throw new Error('The authorized generated image is unavailable.');
      await window.chatModule.openAuthorizedImageSource('generated',match[1],(prompt || '').trim().slice(0,60) || 'Generated image',()=>wrap.isConnected,null);
    } catch (err) {
      console.error('[chat] open in editor failed', err);
      uiModule.showToast?.(err.message || 'The authorized image is unavailable.');
    }
  });
  actions.appendChild(editBtn);

  if (imageId) {
    const impsBtn = document.createElement('button');
    impsBtn.className = 'footer-copy-btn footer-open-imps-btn';
    impsBtn.type = 'button';
    impsBtn.title = 'Open in Imps';
    impsBtn.innerHTML = uiIcon("image", 12) + "<span>Open in Imps</span>";
    impsBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        const parsed=new URL(imageUrl,window.location.origin), match=parsed.pathname.match(/^\/api\/generated-image\/([a-zA-Z0-9._-]{1,128})$/);
        if (parsed.origin!==window.location.origin || parsed.search || parsed.hash || !match || !window.chatModule?.openAuthorizedImageSource) throw new Error('The authorized generated image is unavailable.');
        await window.chatModule.openAuthorizedImageSource('generated',match[1],(prompt || '').trim().slice(0,60) || 'Generated image',()=>wrap.isConnected,null);
      } catch (err) {
        console.error('[chat] open in Imps failed', err);
        uiModule.showToast?.(err.message || 'The authorized image is unavailable.');
      }
    });
    actions.appendChild(impsBtn);
  }

  const delBtn = document.createElement('button');
  delBtn.className = 'footer-copy-btn footer-delete-btn';
  delBtn.type = 'button';
  delBtn.title = 'Delete image';
  delBtn.innerHTML = uiIcon("trash", 12);
  delBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    const ok = await uiModule.styledConfirm('Delete this image?', {
      confirmText: 'Delete',
      cancelText: 'Cancel',
      danger: true,
    });
    if (!ok) return;
    if (imageId) uiModule.showToast?.('The image remains in Files; remove it there.');
    wrap.remove();
  });
  actions.appendChild(delBtn);

  footer.appendChild(actions);

  const metrics = document.createElement('span');
  metrics.className = 'response-metrics';
  const parts = [];
  if (model) parts.push(model.split('/').pop());
  if (size) parts.push(size);
  if (quality) parts.push(quality);
  const cost = getImageCost(model, quality, size);
  if (cost !== null) parts.push('$' + (cost < 0.01 ? cost.toFixed(4) : cost.toFixed(3)));
  metrics.textContent = parts.join(' \u00B7 ');
  footer.appendChild(metrics);

  wrap.appendChild(footer);
  return wrap;
}

export function hideWelcomeScreen() {
  const ws = document.getElementById('welcome-screen');
  const cc = document.getElementById('chat-container');
  if (ws) ws.classList.add('hidden');
  if (cc) cc.classList.remove('welcome-active');
  // Update send button — switches from muted arrow to + Chat
  if (window._updateSendBtnIcon) setTimeout(window._updateSendBtnIcon, 50);
  const ib = document.getElementById('incognito-btn');
  if (ib) ib.style.display = ib.classList.contains('active') ? '' : 'none';
}

export function showWelcomeScreen() {
  const ws = document.getElementById('welcome-screen');
  const cc = document.getElementById('chat-container');
  const alreadyVisible = !!(ws && !ws.classList.contains('hidden'));
  if (ws) ws.classList.remove('hidden');
  if (cc) cc.classList.add('welcome-active');
  if (alreadyVisible) {
    return;
  }
  // Entering the New Chat / welcome state: discard any stale draft left in the
  // composer from the previous session so the input starts empty (issue #1343).
  // Switching between existing sessions loads them directly and does NOT call
  // this, so genuine drafts are not erased. Reset the autosized height and fire
  // an `input` event so the send button + autosize listeners update.
  const _msg = document.getElementById('message');
  if (_msg) {
    _msg.value = '';
    _msg.style.height = '';
    _msg.dispatchEvent(new Event('input', { bubbles: true }));
  }
  // Re-trigger the L→R clip-wipe reveal on the welcome name each time the
  // welcome screen is shown (new session, deleted last session, etc.) — without
  // this, the CSS animation only fires on initial DOM insertion.
  const wn = document.querySelector('.welcome-name');
  if (wn) {
    wn.style.animation = 'none';
    // force reflow so the next assignment registers as a new animation
    void wn.offsetHeight;
    wn.style.animation = '';
  }
  // Update send button — switches from + Chat to muted arrow on empty session
  if (window._updateSendBtnIcon) setTimeout(window._updateSendBtnIcon, 50);
  const ib = document.getElementById('incognito-btn');
  const _researchChk = document.getElementById('research-toggle');
  if (ib && !(_researchChk && _researchChk.checked)) ib.style.display = '';
  if (window.innerWidth > 768) {
    const msg = document.getElementById('message');
    if (msg) msg.focus();
  }
}

// ── Dynamic action buttons (show 3 most recent, rest under ···) ──
const _ACTION_RECENTS_KEY = 'openclank-msg-actions-recent';
const _MAX_VISIBLE = 2;

function _getRecentActions() {
  try { return JSON.parse(localStorage.getItem(_ACTION_RECENTS_KEY) || '[]'); } catch { return []; }
}
function _trackAction(id) {
  let recent = _getRecentActions().filter(x => x !== id);
  recent.unshift(id);
  if (recent.length > 10) recent.length = 10;
  localStorage.setItem(_ACTION_RECENTS_KEY, JSON.stringify(recent));
}

/**
 * Create a footer row for an AI message with timestamp and action buttons.
 */
// Ratings are explicit annotations on the committed canonical message body.
// One read-only policy/profile qualification per authenticated chat view.
let ratingCapability = null;
const pendingRecallFeedback = new Map();
function invalidateRatingCapability() {
  ratingCapability = null;
  document.querySelectorAll('.rating-action').forEach(button => { button.hidden=true; button.style.display='none'; button._qualifyRating?.(); });
}
for (const eventName of ['openclank:auth-context-changed', 'openclank:memory-policy-changed', 'odysseus:session-selected']) {
  document.addEventListener(eventName, invalidateRatingCapability);
}
function ratingCapabilityFor(scope, navigation) {
  const key = JSON.stringify([scope.owner,scope.sid,scope.authEpoch,navigation]);
  if (ratingCapability?.key === key) return ratingCapability.promise;
  const promise = fetch('/api/prefs/memory-policy/preview', {cache:'no-store'}).then(async response => {
    if (!response.ok) return false;
    const data=await response.json(), doc=data?.document;
    const accepted=['openclank.memory-policy.v2','openclank.memory-policy.v3'].includes(doc?.schema) ? doc.accepted_policy : doc;
    const ratings=data?.ratings || accepted?.policy?.ratings || data?.proposed_defaults?.ratings;
    return data?.capabilities?.consumers?.ratings === true && ratings?.visible === true;
  }).catch(() => false);
  ratingCapability={key,promise}; return promise;
}
function appendRatingAction(actions, message) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'msg-action-btn rating-action';
  button.textContent = '☆'; button.title = 'Rate this message';
  button.setAttribute('aria-label', 'Rate this message');
  button.hidden = true; button.style.display = 'none';
  let qualification = 0;
  const qualify = async () => {
    const attempt = ++qualification;
    const scope = window.chatModule?.ratingAccountScope?.();
    const navigation = window.sessionModule?.getSessionNavigationToken?.();
    if (!scope?.sid || !scope.owner || document.body.dataset.accountId !== scope.owner || navigation === undefined) return;
    const allowed=await ratingCapabilityFor(scope,navigation);
    const now = window.chatModule?.ratingAccountScope?.();
    if (attempt === qualification && message.isConnected && now?.sid === scope.sid && now.owner === scope.owner && now.authEpoch === scope.authEpoch && window.sessionModule?.getSessionNavigationToken?.() === navigation && document.body.dataset.accountId === scope.owner) {
      button.hidden = !allowed; button.style.display = allowed ? '' : 'none';
    }
  };
  requestAnimationFrame(qualify);
  button._qualifyRating = qualify;
  button.addEventListener('click', async (event) => {
    event.stopPropagation();
    const session = window.sessionModule;
    const sid = session?.getCurrentSessionId();
    const navigation = session?.getSessionNavigationToken();
    const id = message.dataset.dbId;
    const scope = window.chatModule?.ratingAccountScope?.();
    if (!sid || !session || !scope?.owner || scope.sid !== sid || document.body.dataset.accountId !== scope.owner || typeof session.getSessionNavigationToken !== 'function') return;
    const current = () => message.isConnected && message.dataset.dbId === id &&
      session.getCurrentSessionId() === sid && session.getSessionNavigationToken() === navigation &&
      document.body.dataset.accountId === scope.owner && (() => { const now = window.chatModule?.ratingAccountScope?.(); return now?.sid === sid && now.owner === scope.owner && now.authEpoch === scope.authEpoch; })();
    const existing = message.querySelector('.message-rating-editor');
    if (existing) { existing.remove(); return; }
    const panel = document.createElement('div'); panel.className = 'message-rating-editor';
    panel.setAttribute('role', 'group'); panel.setAttribute('aria-label', 'Message rating');
    const status = document.createElement('p'); status.setAttribute('role', 'status'); status.textContent = 'Loading rating…';
    panel.appendChild(status); message.appendChild(panel);
    const url = `/api/session/${encodeURIComponent(sid)}/message-rating/${encodeURIComponent(id)}`;
    let view=null;
    if (id) { try { const response = await fetch(url); if (response.ok) view = await response.json(); } catch {} }
    if (!current() || !panel.isConnected) return;
    status.textContent = !view ? 'Host message rating unavailable; choose native text if available.' : view.state === 'stale' ? 'This rating refers to an earlier message revision.' :
      view.state === 'rated' ? `Current rating: ${view.value / 2} stars.` : 'Unrated.';
    if (view?.source?.role === 'user' && view.source.source_revision === 1) {
      const group=document.createElement('details'), heading=document.createElement('summary'); heading.textContent='Recall feedback'; group.append(heading);
      const notice=document.createElement('p'); notice.setAttribute('role','status'); notice.textContent='Load the memories that reached this turn to inspect or rate them.';
      const load=document.createElement('button'); load.type='button'; load.textContent='Show memories sent to this turn';
      const choice=document.createElement('select'); choice.setAttribute('aria-label','Admitted memory'); choice.disabled=true;
      const polarity=document.createElement('select'); polarity.setAttribute('aria-label','Recall outcome');
      for (const [value,label] of [['','Choose an outcome'],['positive','Helped'],['negative','Did not help']]) {const option=document.createElement('option');option.value=value;option.textContent=label;polarity.append(option)}
      const submit=document.createElement('button'); submit.type='button'; submit.textContent='Submit feedback'; submit.disabled=true;
      const reconcile=document.createElement('button'); reconcile.type='button'; reconcile.textContent='Check submission'; reconcile.hidden=true;
      const pendingKey=JSON.stringify([scope.owner,scope.authEpoch,sid,id]);
      let pending=pendingRecallFeedback.get(pendingKey) || null, generation=0, writeAllowed=false;
      const base=`${url}/recall-feedback`;
      const source={expected_source_revision:view.source.source_revision,expected_source_hash:view.source.source_hash};
      load.addEventListener('click',async()=>{
        if (!current() || pending) return; const ticket=++generation; load.disabled=true;
        try {const response=await fetch(`${base}/preview`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(source)});
          if (!response.ok) throw new Error(); const data=await response.json();
          if (!current() || !panel.isConnected || ticket!==generation) return;
          writeAllowed=data.write_allowed===true;choice.replaceChildren(); const empty=document.createElement('option');empty.value='';empty.textContent='Choose a memory';choice.append(empty);
          const admissionLabels = { tool_admitted: 'delivered by recall tool', context_admitted: 'delivered in turn context' };
          for (const item of data.items || []) {const option=document.createElement('option');option.value=item.selector;option.textContent = [item.display, admissionLabels[item.admission], Number.isSafeInteger(item.memory?.revision) ? `revision ${item.memory.revision}` : ''].filter(Boolean).join(' · ');option.dataset.positiveKey=item.keys?.positive || '';option.dataset.negativeKey=item.keys?.negative || '';option.dataset.positiveRecorded=String(item.recorded?.positive===true);option.dataset.negativeRecorded=String(item.recorded?.negative===true);choice.append(option)}
          choice.disabled=choice.options.length<2; notice.textContent=!writeAllowed ? 'Recall feedback is read-only under current memory policy.' : data.partial ? 'Some memories are unavailable; the listed revisions can be selected.' : choice.disabled ? 'No admitted memories are available.' : 'Select the exact memory revision.';
        } catch {if(current() && panel.isConnected) notice.textContent='Admitted memories are unavailable.'} finally {if(current() && panel.isConnected) load.disabled=!!pending}
      });
      const refresh=()=>{const recorded=choice.selectedOptions[0]?.dataset?.[`${polarity.value}Recorded`]==='true';submit.disabled=!!pending || !writeAllowed || !choice.value || !polarity.value || recorded;if(recorded)notice.textContent='This assessment is already recorded.'}; choice.addEventListener('change',refresh); polarity.addEventListener('change',refresh);
      const send=async(check)=>{if (!pending || !current()) return; submit.disabled=true; reconcile.disabled=true;
        try {const response=await fetch(`${base}/${check?'reconcile':'commit'}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending)});if(!response.ok) throw new Error();
          const result=await response.json();if(!current() || !panel.isConnected)return;
          if(result.state==='applied' || result.state==='replayed'){notice.textContent='Recall feedback recorded.';pending=null;pendingRecallFeedback.delete(pendingKey);reconcile.hidden=true;load.disabled=false;choice.disabled=false;polarity.disabled=false;refresh()}
          else {notice.textContent='Submission outcome is unknown; check it without resubmitting.';reconcile.hidden=false}
        } catch {if(current() && panel.isConnected){notice.textContent='Submission outcome is unknown; check it without resubmitting.';reconcile.hidden=false}} finally {if(current() && panel.isConnected)reconcile.disabled=false}
      };
      submit.addEventListener('click',()=>{if(!choice.value || !polarity.value || pending)return;const key=choice.selectedOptions[0]?.dataset?.[`${polarity.value}Key`];if(!key)return;pending={...source,selector:choice.value,polarity:polarity.value,request_key:key};pendingRecallFeedback.set(pendingKey,pending);load.disabled=true;choice.disabled=true;polarity.disabled=true;void send(false)});
      reconcile.addEventListener('click',()=>void send(true));group.append(notice,load,choice,polarity,submit,reconcile);panel.append(group);
      if(pending){group.open=true;notice.textContent='Submission outcome is unknown; check it without resubmitting.';load.disabled=true;choice.disabled=true;polarity.disabled=true;reconcile.hidden=false}
    }
    const target = document.createElement('select'); target.setAttribute('aria-label', 'Rating target');
    for (const [v,t] of [['message','This message'],['chat','Chat text saved by Open Clank'],['complete_chat','Complete supported chat text'],['section','Select raw chat text'],['native_chat','Native session text'],['native_message','Native text part'],['native_section','Select raw native text'],['paired_section','Witnessed turn from this input']]) { const option=document.createElement('option'); option.value=v; option.textContent=t; if ((v==='message' && !view) || (v==='paired_section' && view?.source?.role!=='user')) option.disabled=true; target.appendChild(option); }
    panel.appendChild(target);
    const targetInfo=document.createElement('div'); panel.appendChild(targetInfo);
    if (!view) target.value='native_chat';
    let segments=null, targetView=null, selectedPart=null, selectionVersion=0, segmentVersion=0, activeDraftKey=view ? 'message' : null;
    const drafts=new Map();
    const rememberDraft=() => { if (activeDraftKey) drafts.set(activeDraftKey,{score:score.value,note:note.value}); };
    const loadDraft=(key, source) => {
      activeDraftKey=key;
      const draft=drafts.get(key);
      setRatingChoice(draft?.score ?? (source?.state==='rated' ? String(source.value) : ''));
      note.value=draft?.note ?? source?.note ?? '';
    };
    const targetUrl=`/api/session/${encodeURIComponent(sid)}/rating-target`;
    const nativeUrl=`/api/session/${encodeURIComponent(sid)}/native-rating`;
    const pairedUrl=`/api/session/${encodeURIComponent(sid)}/paired-rating`;
    const completeUrl=`/api/session/${encodeURIComponent(sid)}/complete-text-rating`;
    const completePreview=async()=>{const response=await fetch(completeUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({payload:null})});if(!response.ok)throw new Error();const result=await response.json();if(!['rated','unrated','stale'].includes(result?.state)||typeof result.descriptor_hash!=='string'||!Number.isSafeInteger(result.rating_revision)||result.rating_revision<0)throw new Error();return result;};
    const pairedPreview=async values=>{const response=await fetch(pairedUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message_id:id,segments:values,payload:null})});if(!response.ok)throw new Error();return await response.json();};
    const nativePreview=async (kind, values, part) => { const response=await fetch(nativeUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,segments:values,part_id:part,payload:null})}); if (!response.ok) throw new Error(); return await response.json(); };
    const preview=async (kind, values) => {
      const response=await fetch(targetUrl+'/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,segments:values})});
      if (!response.ok) throw new Error(); return await response.json();
    };
    target.onchange=async () => {
      rememberDraft(); activeDraftKey=null; score.value=''; note.value='';
      const generation=++selectionVersion; targetInfo.replaceChildren(); targetView=null; segments=null; selectedPart=null; save.disabled=true;
      if (target.value==='message') {loadDraft('message',view); save.disabled=false; status.textContent='This committed message.'; return;}
      status.textContent='Loading saved chat text…';
      try {
        if (target.value==='complete_chat') {
          const result=await completePreview();if(!current() || !panel.isConnected || generation!==selectionVersion)return;
          targetView=result;loadDraft('complete_chat',result);save.disabled=false;status.textContent=result.scope_label || 'Complete supported committed text; currentness is an optimistic observation.';return;
        }
        if (target.value==='paired_section') {
          const pair=await pairedPreview(null);if(!current() || !panel.isConnected || generation!==selectionVersion)return;
          if(pair.state!=='ready' || !pair.choices)throw new Error();
          status.textContent=pair.scope_label;segments=[];
          for(const side of ['input','output']) {
            const chooser=document.createElement('select');chooser.setAttribute('aria-label',`${side} representation`);
            for(const source of ['host','native']){const option=document.createElement('option');option.value=source;option.textContent=`${side}: ${source}`;chooser.appendChild(option)}
            const field=document.createElement('textarea');field.readOnly=true;field.setAttribute('aria-label',`${side} raw text`);field.value=pair.choices[side].host;
            chooser.onchange=()=>{field.value=pair.choices[side][chooser.value];segments=[];segmentVersion++;targetView=null;save.disabled=true;status.textContent='Representation changed; select again.'};
            const add=document.createElement('button');add.type='button';add.textContent=`Select or update ${side}`;
            add.onclick=()=>{const source=pair.choices[side][chooser.value],boundaries=[0];let shown='';
              for(let i=0;i<source.length;){if(source[i]==='\r'){i+=source[i+1]==='\n'?2:1;shown+='\n'}else{shown+=source[i];i++}boundaries.push(i)}
              const start=boundaries[field.selectionStart],end=boundaries[field.selectionEnd],split=i=>i>0 && i<source.length && /[\uD800-\uDBFF]/.test(source[i-1]) && /[\uDC00-\uDFFF]/.test(source[i]);
              if(field.value!==shown || start===undefined || end===undefined || end<=start || split(start) || split(end)){status.textContent='Select a nonempty complete text range.';return}
              const encoder=new TextEncoder();segments=segments.filter(item=>item.side!==side);segments.push({side,source:chooser.value,start:encoder.encode(source.slice(0,start)).length,end:encoder.encode(source.slice(0,end)).length});segments.sort((a,b)=>a.side==='input' ? -1 : b.side==='input' ? 1 : 0);segmentVersion++;targetView=null;save.disabled=true;status.textContent=`${segments.length} selection(s); validate before saving.`;
            };
            targetInfo.append(chooser,field,add);
          }
          const validate=document.createElement('button');validate.type='button';validate.textContent='Validate selection';targetInfo.append(validate);
          validate.onclick=async()=>{const version=segmentVersion,selectedSegments=structuredClone(segments);try{const checked=await pairedPreview(selectedSegments);if(!current() || !panel.isConnected || generation!==selectionVersion || version!==segmentVersion)return;targetView=checked;loadDraft(`paired:${checked.descriptor_hash}`,checked);save.disabled=false;status.textContent=checked.scope_label}catch{if(current())status.textContent='Selection unavailable or not consecutive; reselect.'}};
          return;
        }
        const native=target.value.startsWith('native_');
        const result=native ? await nativePreview('chat',null,null) : await preview('chat',null); if (!current() || !panel.isConnected || generation!==selectionVersion) return;
        status.textContent=result.scope_label;
        if (result.native_history?.length) {
          const prior=document.createElement('details'), title=document.createElement('summary'); title.textContent='Earlier native ratings'; prior.appendChild(title);
          for (const item of result.native_history) {const line=document.createElement('p'); line.textContent=`${item.kind} — ${item.state}: ${item.value === null ? 'unrated' : `${item.value / 2} stars`}${item.note ? ` — ${item.note}` : ''}`; prior.appendChild(line);}
          targetInfo.appendChild(prior);
        }
        if (result.section_history?.length) {
          const prior=document.createElement('details'); const title=document.createElement('summary'); title.textContent='Earlier text selections'; prior.appendChild(title);
          for (const item of result.section_history) { const line=document.createElement('p'); line.textContent=`${item.state}: ${item.value === null ? 'unrated' : `${item.value / 2} stars`}${item.note ? ` — ${item.note}` : ''}`; prior.appendChild(line); }
          targetInfo.appendChild(prior);
        }
        if (target.value==='chat' || target.value==='native_chat') {targetView=result; loadDraft(target.value,result); save.disabled=false; return;}
        if (target.value==='native_message') {
          const chooser=document.createElement('select'); chooser.setAttribute('aria-label','Native text part'); targetInfo.appendChild(chooser);
          const blank=document.createElement('option'); blank.value=''; blank.textContent='Choose a native text part'; chooser.appendChild(blank);
          for (const item of result.raw_messages) {const option=document.createElement('option'); option.value=item.part_id; option.textContent=`${item.role}: ${item.text.slice(0,80)}`; chooser.appendChild(option);}
          chooser.onchange=async () => { const part=chooser.value; selectedPart=part; targetView=null; save.disabled=true; score.value=''; note.value=''; if (!part) return; try {const checked=await nativePreview('message',null,part); if (!current() || !panel.isConnected || generation!==selectionVersion || selectedPart!==part) return; targetView=checked; loadDraft(`native:${part}`,checked); save.disabled=false; status.textContent=checked.scope_label;} catch {if (current()) status.textContent='Native text part unavailable.';} };
          return;
        }
        segments=[];
        const instructions=document.createElement('p'); instructions.textContent='Select raw text in each consecutive message, then add each selection in order. Source line endings are preserved. Separately managed history is excluded.'; targetInfo.appendChild(instructions);
        for (const item of result.raw_messages) {
          const field=document.createElement('textarea'); field.readOnly=true; field.value=item.text; field.setAttribute('aria-label',`${item.role} raw canonical text`); targetInfo.appendChild(field);
          const add=document.createElement('button'); add.type='button'; add.textContent='Add selected text'; targetInfo.appendChild(add);
          add.onclick=() => {
            // Textareas present CR/CRLF as LF. Map each displayed UTF-16
            // boundary back to the untouched canonical source before encoding.
            const source=item.text, boundaries=[0]; let shown='';
            for (let i=0;i<source.length;) {
              if (source[i]==='\r') { i += source[i+1]==='\n' ? 2 : 1; shown+='\n'; }
              else { shown+=source[i]; i++; }
              boundaries.push(i);
            }
            const start=field.selectionStart,end=field.selectionEnd;
            const rawStart=boundaries[start],rawEnd=boundaries[end];
            const split=i => i>0 && i<source.length && /[\uD800-\uDBFF]/.test(source[i-1]) && /[\uDC00-\uDFFF]/.test(source[i]);
            if (field.value!==shown || end<=start || rawStart===undefined || rawEnd===undefined || split(rawStart) || split(rawEnd)) { status.textContent='Select a nonempty complete text range.'; return; }
            rememberDraft(); activeDraftKey=null; score.value=''; note.value=''; segmentVersion++;
            const encoder=new TextEncoder(); segments.push(native ? {partID:item.part_id,start:encoder.encode(source.slice(0,rawStart)).length,end:encoder.encode(source.slice(0,rawEnd)).length} : {message_id:item.message_id,start:encoder.encode(source.slice(0,rawStart)).length,end:encoder.encode(source.slice(0,rawEnd)).length});
            targetView=null; save.disabled=true; status.textContent=`${segments.length} selection(s); validate before saving.`;
          };
        }
        const validate=document.createElement('button'); validate.type='button'; validate.textContent='Validate selection'; targetInfo.appendChild(validate);
        validate.onclick=async () => {const version=segmentVersion, selectedSegments=structuredClone(segments); try {const checked=native ? await nativePreview('section',selectedSegments,null) : await preview('section',selectedSegments); if (!current() || !panel.isConnected || generation!==selectionVersion || version!==segmentVersion) return; targetView=checked; loadDraft(`section:${checked.descriptor_hash}`,checked); save.disabled=false; status.textContent=checked.scope_label;} catch {if (current() && generation===selectionVersion && version===segmentVersion) status.textContent='Selection unavailable or not consecutive; close and reselect.';}};
      } catch {if (current() && generation===selectionVersion) status.textContent=target.value==='complete_chat' ? 'Complete supported chat text is unavailable; no complete rating can be made.' : 'Saved chat text is unavailable for rating.';}
    };
    const label = document.createElement('label'); label.textContent = 'Rating (1 to 5 stars, in half-star steps) ';
    const score = document.createElement('select'); score.setAttribute('aria-label', 'Rating');
    const blank = document.createElement('option'); blank.value = ''; blank.textContent = 'Choose a rating'; score.appendChild(blank);
    for (let value = 2; value <= 10; value++) {
      const option=document.createElement('option');
      option.value=String(value); option.textContent=value === 2 ? '1 star' : `${value / 2} stars`;
      score.appendChild(option);
    }
    const clear=document.createElement('option'); clear.value='clear'; clear.textContent='Clear rating'; score.appendChild(clear);
    let earlierScore=null;
    function setRatingChoice(value) {
      earlierScore?.remove(); earlierScore=null;
      if (value === '0' || value === '1') {
        earlierScore=document.createElement('option');
        earlierScore.value=value; earlierScore.textContent=`Previously rated ${Number(value) / 2} stars`; earlierScore.disabled=true;
        score.insertBefore(earlierScore,blank.nextSibling);
      }
      score.value=value;
    }
    if (view?.state === 'rated') setRatingChoice(String(view.value));
    label.appendChild(score); panel.appendChild(label);
    const noteLabel = document.createElement('label'); noteLabel.textContent = 'Why? (optional) ';
    const note = document.createElement('textarea'); note.maxLength = 4096; note.value = view?.note || '';
    noteLabel.appendChild(note); panel.appendChild(noteLabel);
    const save = document.createElement('button'); save.type = 'button'; save.textContent = 'Save rating'; panel.appendChild(save);
    const reconcilePair=document.createElement('button');reconcilePair.type='button';reconcilePair.textContent='Check paired rating';reconcilePair.hidden=true;panel.appendChild(reconcilePair);
    const close = document.createElement('button'); close.type = 'button'; close.textContent = 'Close'; close.onclick = () => panel.remove(); panel.appendChild(close);
    let previousPayload = null, requestKey = null, pendingPaired=null;
    reconcilePair.onclick=async()=>{
      if(!pendingPaired || !current() || !panel.isConnected)return;
      reconcilePair.disabled=true;
      try {const response=await fetch(pairedUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...pendingPaired,mode:'reconcile'})});
        if(!response.ok)throw new Error();const result=await response.json();if(!current() || !panel.isConnected)return;
        status.textContent=result.state==='recorded' ? `Rating recorded; current source state is ${result.current_state || 'unknown'}.` : 'Rating outcome remains unknown.';
      } catch {if(current() && panel.isConnected)status.textContent='Rating outcome remains unknown.'}
      finally {if(panel.isConnected)reconcilePair.disabled=false;}
    };
    if (!view) target.onchange();
    save.addEventListener('click', async () => {
      if (!current() || !panel.isConnected) return;
      const frozenTarget=target.value, frozenVersion=selectionVersion;
      const choice=score.value, value=choice === 'clear' ? null : Number(choice);
      const selected=frozenTarget==='message' ? view : targetView;
      if (!selected) return;
      if (value !== null && (!Number.isInteger(value) || value < 2 || value > 10)) {
        status.textContent='Choose 1 to 5 stars or Clear rating.';
        return;
      }
      const reason=value === null ? null : note.value || null;
      const mutation={expected_descriptor_hash:selected.descriptor_hash,expected_rating_revision:selected.rating_revision,action:value === null ? 'clear':'set',value,note:reason};
      const native=frozenTarget.startsWith('native_');
      const paired=frozenTarget==='paired_section';
      const complete=frozenTarget==='complete_chat';
      const payload = frozenTarget==='message' ? {expected_source_revision: view.source.source_revision, expected_source_hash: view.source.source_hash,
        expected_rating_revision: view.rating_revision, action: value === null ? 'clear' : 'set', value, note:reason} :
        complete ? {payload:mutation} : paired ? {message_id:id,segments:structuredClone(segments),payload:mutation} : native ? {kind:frozenTarget.slice(7),segments:frozenTarget==='native_section' ? structuredClone(segments) : null,part_id:frozenTarget==='native_message' ? selectedPart : null,payload:mutation} :
        {kind:frozenTarget,segments:frozenTarget==='chat' ? null : structuredClone(segments),...mutation};
      const signature = JSON.stringify(payload);
      if (signature !== previousPayload) { previousPayload = signature; requestKey = crypto.randomUUID(); }
      if(paired && pendingPaired)return;
      if(paired)pendingPaired={...payload,payload:{...payload.payload,request_key:requestKey}};
      save.disabled=true; target.disabled=true; score.disabled=true; note.disabled=true; targetInfo.inert=true; close.disabled=true; status.textContent = 'Saving…';
      try {
        const response = await fetch(frozenTarget==='message' ? url : complete ? completeUrl : paired ? pairedUrl : native ? nativeUrl : targetUrl, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(native || paired || complete ? {...payload,payload:{...payload.payload,request_key:requestKey}} : {...payload,request_key:requestKey})});
        if (!current() || !panel.isConnected || target.value!==frozenTarget || selectionVersion!==frozenVersion) return;
        if (response.status === 409) { if(paired)reconcilePair.hidden=false;status.textContent = paired ? 'Source or rating changed; check the original receipt.' : 'Message or rating changed. Your input is preserved; close and reopen to review the current revision.'; return; }
        if (!response.ok) throw new Error();
        const result = await response.json(); if (!current() || !panel.isConnected || target.value!==frozenTarget || selectionVersion!==frozenVersion) return;
        if(paired && (!['applied','replayed'].includes(result?.state) || !Number.isSafeInteger(result.rating_revision) || !['current','stale','unknown','superseded'].includes(result.current_state)))throw new Error();
        if(complete && (!['applied','replayed'].includes(result?.state) || !Number.isSafeInteger(result.rating_revision) || !['current','stale','unknown'].includes(result.current_state)))throw new Error();
        if ((result.state === 'replayed' && result.current_state !== 'current') || ((paired || complete) && result.current_state && result.current_state!=='current')) { if(paired)reconcilePair.hidden=false;status.textContent = 'The save was recorded, but current source status is stale or unknown. Your input is preserved; check the original receipt.'; return; }
        selected.rating_revision = result.rating_revision; selected.state = value === null ? 'unrated' : 'rated'; selected.value = value; selected.note=reason;
        setRatingChoice(value === null ? '' : String(value));
        if (value === null) note.value='';
        previousPayload = null; requestKey = null;pendingPaired=null; status.textContent = value === null ? 'Rating cleared.' : `Saved ${value / 2} stars.`;
      } catch { if (current() && panel.isConnected) {if(paired)reconcilePair.hidden=false;status.textContent = paired ? 'Outcome unknown; check the original receipt without resending.' : 'Could not save. Your input is preserved; retry to check the same request.';} }
      finally { if (panel.isConnected) { target.disabled=!!pendingPaired; score.disabled=!!pendingPaired; note.disabled=!!pendingPaired; targetInfo.inert=!!pendingPaired; close.disabled=false; save.disabled=!!pendingPaired || target.value!==frozenTarget || selectionVersion!==frozenVersion; } }
    });
  });
  actions.appendChild(button);
}

export function createMsgFooter(msgElement) {
  const footer = document.createElement('div');
  footer.className = 'msg-footer';

  const actions = document.createElement('span');
  actions.className = 'msg-actions';
  appendRatingAction(actions, msgElement);

  // Define all available actions: { id, icon, title, className, handler }
  const allActions = [
    { id: 'copy', icon: COPY_ICON, title: 'Copy message', cls: 'footer-copy-btn', html: true, handler(e) {
      e.stopPropagation();
      const btn = e.currentTarget;
      uiModule.copyToClipboard(copyMessageText(msgElement));
      btn.innerHTML = CHECK_ICON;
      setTimeout(() => { btn.innerHTML = COPY_ICON; }, 1500);
    }},
    { id: 'edit', icon: '\u270E', title: 'Edit', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      if (window.chatModule?.editAIMessage) window.chatModule.editAIMessage(msgElement);
    }},
    { id: 'regen', icon: '\u21BB', title: 'Regenerate from here', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      if (window.chatModule?.regenerateFrom) window.chatModule.regenerateFrom(msgElement);
    }},
    { id: 'shorten', icon: '\u2702', title: 'Rewrite shorter', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      if (window.chatModule?.rewriteWith) window.chatModule.rewriteWith(msgElement, 'Rewrite your last response to be shorter and more concise. Keep the key information but cut the fluff.');
    }},
    { id: 'explain', icon: '?', title: 'Explain simpler', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      if (window.chatModule?.rewriteWith) window.chatModule.rewriteWith(msgElement, 'Explain your last response in simpler terms. Use plain language and short sentences.');
    }},
    { id: 'fork', icon: '\u2ADD', title: 'Fork conversation', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      if (window.chatModule?.forkFrom) window.chatModule.forkFrom(msgElement);
    }},
    { id: 'delete', icon: '\u2715', title: 'Delete message', cls: 'msg-action-btn msg-delete-btn', handler(e) {
      e.stopPropagation();
      if (window.chatModule?.deleteMessage) window.chatModule.deleteMessage(msgElement);
    }},
  ];

  // Filter out unavailable actions (e.g. TTS when not enabled)
  const availableActions = allActions.filter(a => !a.available || a.available());

  // Determine which 3 to show: use recent order, fallback to defaults
  const recent = _getRecentActions();
  const defaults = ['copy', 'delete', 'fork'];
  const order = recent.length > 0 ? recent : defaults;
  const sorted = [...availableActions].sort((a, b) => {
    const ai = order.indexOf(a.id), bi = order.indexOf(b.id);
    if (ai >= 0 && bi >= 0) return ai - bi;
    if (ai >= 0) return -1;
    if (bi >= 0) return 1;
    return 0;
  });
  const visible = sorted.slice(0, _MAX_VISIBLE);
  const overflow = sorted.slice(_MAX_VISIBLE);

  // Render visible buttons
  function _addBtn(action, container) {
    const btn = _makeActionBtn(action.cls, action.title, action.html ? '' : action.icon, (e) => {
      _trackAction(action.id);
      action.handler(e);
    });
    if (action.html) btn.innerHTML = action.icon;
    btn.dataset.action = action.id;
    container.appendChild(btn);
  }

  visible.forEach(a => _addBtn(a, actions));

  // Overflow "···" button
  if (overflow.length > 0) {
    const moreBtn = document.createElement('button');
    moreBtn.className = 'msg-action-btn msg-more-btn';
    moreBtn.type = 'button';
    moreBtn.title = 'More actions';
    moreBtn.textContent = '\u00B7\u00B7\u00B7';
    moreBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      // Toggle overflow menu — close any existing one first (through its own
      // dismiss so the Escape registry entry goes with it).
      const existing = document.querySelector('.msg-overflow-menu');
      if (existing) {
        if (typeof existing._dismiss === 'function') existing._dismiss(); else existing.remove();
        if (existing._trigger === moreBtn) return;
      }

      const menu = document.createElement('div');
      menu.className = 'msg-overflow-menu';
      let closeMenu = () => menu.remove();
      overflow.forEach(a => {
        const item = document.createElement('button');
        item.className = 'msg-overflow-item';
        item.type = 'button';
        item.title = a.title;
        item.innerHTML = `<span class="overflow-icon">${a.icon}</span> ${a.title}`;
        item.addEventListener('click', (ev) => {
          ev.stopPropagation();
          _trackAction(a.id);
          closeMenu();
          a.handler(ev);
        });
        menu.appendChild(item);
      });
      menu._trigger = moreBtn;
      document.body.appendChild(menu);
      // Position fixed relative to the ··· button
      const btnRect = moreBtn.getBoundingClientRect();
      menu.style.top = (btnRect.top - menu.offsetHeight - 4) + 'px';
      menu.style.left = btnRect.left + 'px';
      // Flip down if above viewport
      if (parseFloat(menu.style.top) < 8) menu.style.top = (btnRect.bottom + 4) + 'px';
      // Keep within right edge
      const mr = menu.getBoundingClientRect();
      if (mr.right > window.innerWidth - 8) menu.style.left = (window.innerWidth - mr.width - 8) + 'px';
      // Close on outside click or Escape. The trigger button is treated as
      // "inside" so its own click toggles rather than double-fires.
      closeMenu = bindMenuDismiss(menu, () => menu.remove(), (ev) => !menu.contains(ev.target) && ev.target !== moreBtn);    });
    actions.appendChild(moreBtn);
  }

  // Memory-used indicator pill
  const mems = msgElement._memoriesUsed;
  if (mems && mems.length > 0) {
    const pill = document.createElement('button');
    pill.className = 'memory-used-pill';
    pill.type = 'button';
    const pinnedCount = mems.filter(m => m.type === 'pinned').length;
    const recalledCount = mems.filter(m => m.type === 'recalled').length;
    const parts = [];
    if (pinnedCount) parts.push(`${pinnedCount} pinned`);
    if (recalledCount) parts.push(`${recalledCount} recalled`);
    pill.innerHTML = `${uiIcon("memory", 12, {"style":"vertical-align:-2px;margin-right:3px"})}<span class="memory-used-pill-text">${parts.join(', ')}</span>`;
    pill.title = mems.map(m => `[${m.type}] ${m.text}`).join('\n');

    pill.addEventListener('click', (e) => {
      e.stopPropagation();
      let detail = pill._openDetail || document.querySelector('.memory-used-detail');
      if (detail) {
        if (typeof detail._dismiss === 'function') detail._dismiss();
        else { detail.remove(); pill._openDetail = null; }
        return;
      }
      detail = document.createElement('div');
      detail.className = 'memory-used-detail';
      let closeDetail = () => { detail.remove(); pill._openDetail = null; };
      mems.forEach(m => {
        const row = document.createElement('div');
        row.className = 'memory-used-row';
        row.style.cursor = 'pointer';
        row.title = 'Click to open memory manager';
        const badge = document.createElement('span');
        badge.className = 'memory-used-badge ' + (m.type === 'pinned' ? 'pinned' : 'recalled');
        badge.textContent = m.type === 'pinned' ? '\u25CF' : '\u21BB';
        const text = document.createElement('span');
        text.className = 'memory-used-text';
        text.textContent = m.text;
        row.appendChild(badge);
        row.appendChild(text);
        row.addEventListener('click', (ev) => {
          ev.stopPropagation();
          closeDetail();
          const memModal = document.getElementById('memory-modal');
          if (memModal) memModal.classList.remove('hidden');
        });
        detail.appendChild(row);
      });
      detail.style.visibility = 'hidden';
      document.body.appendChild(detail);
      const pillRect = pill.getBoundingClientRect();
      const detailRect = detail.getBoundingClientRect();
      const spaceAbove = pillRect.top;
      const spaceBelow = window.innerHeight - pillRect.bottom;
      if (spaceAbove >= detailRect.height + 8 || spaceAbove > spaceBelow) {
        detail.style.top = (pillRect.top - detailRect.height - 8) + 'px';
      } else {
        detail.style.top = (pillRect.bottom + 8) + 'px';
      }
      detail.style.left = pillRect.left + 'px';
      if (pillRect.left + detailRect.width > window.innerWidth - 8) {
        detail.style.left = (window.innerWidth - detailRect.width - 8) + 'px';
      }
      if (parseFloat(detail.style.left) < 8) detail.style.left = '8px';
      detail.style.visibility = '';
      pill._openDetail = detail;
      // Close on outside click or Escape (pill click toggles, so it's inside).
      closeDetail = bindMenuDismiss(detail, () => { detail.remove(); pill._openDetail = null; }, (ev) => !detail.contains(ev.target) && ev.target !== pill);    });

    footer.appendChild(pill);
  }

  footer.appendChild(actions);
  return footer;
}

/**
 * Create a footer row for a user message with action buttons (same system as AI footer).
 */
const _USER_ACTION_RECENTS_KEY = 'openclank-user-actions-recent';

function _getUserRecentActions() {
  try { return JSON.parse(localStorage.getItem(_USER_ACTION_RECENTS_KEY) || '[]'); } catch { return []; }
}
function _trackUserAction(id) {
  let recent = _getUserRecentActions().filter(x => x !== id);
  recent.unshift(id);
  if (recent.length > 10) recent.length = 10;
  localStorage.setItem(_USER_ACTION_RECENTS_KEY, JSON.stringify(recent));
}

export function createUserMsgFooter(msgElement) {
  const footer = document.createElement('div');
  footer.className = 'msg-footer';

  const actions = document.createElement('span');
  actions.className = 'msg-actions';
  appendRatingAction(actions, msgElement);

  const allActions = [
    { id: 'edit', icon: '\u270E', title: 'Edit message', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      if (window.chatModule?.editUserMessage) window.chatModule.editUserMessage(msgElement);
    }},
    { id: 'delete', icon: '\u2715', title: 'Delete message', cls: 'msg-action-btn msg-delete-btn', handler(e) {
      e.stopPropagation();
      if (window.chatModule?.deleteMessage) window.chatModule.deleteMessage(msgElement);
    }},
    { id: 'copy', icon: COPY_ICON, title: 'Copy message', cls: 'footer-copy-btn', html: true, handler(e) {
      e.stopPropagation();
      const btn = e.currentTarget;
      uiModule.copyToClipboard(msgElement.querySelector('.body')?.textContent || '');
      btn.innerHTML = CHECK_ICON;
      setTimeout(() => { btn.innerHTML = COPY_ICON; }, 1500);
    }},
    { id: 'resend', icon: '\u21BB', title: 'Resend message', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      if (window.chatModule?.resendUserMessage) window.chatModule.resendUserMessage(msgElement);
    }},
    { id: 'fork', icon: '\u2ADD', title: 'Fork before this message', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      if (window.chatModule?.forkFrom) window.chatModule.forkFrom(msgElement);
    }},
  ];

  const recent = _getUserRecentActions();
  const defaults = ['edit', 'delete', 'copy'];
  const order = recent.length > 0 ? recent : defaults;
  const sorted = [...allActions].sort((a, b) => {
    const ai = order.indexOf(a.id), bi = order.indexOf(b.id);
    if (ai >= 0 && bi >= 0) return ai - bi;
    if (ai >= 0) return -1;
    if (bi >= 0) return 1;
    return 0;
  });
  const visible = sorted.slice(0, _MAX_VISIBLE);
  const overflow = sorted.slice(_MAX_VISIBLE);

  visible.forEach(a => {
    const btn = _makeActionBtn(a.cls, a.title, a.html ? '' : a.icon, (ev) => {
      _trackUserAction(a.id);
      a.handler(ev);
    });
    if (a.html) btn.innerHTML = a.icon;
    btn.dataset.action = a.id;
    actions.appendChild(btn);
  });

  if (overflow.length > 0) {
    const moreBtn = document.createElement('button');
    moreBtn.className = 'msg-action-btn msg-more-btn';
    moreBtn.type = 'button';
    moreBtn.title = 'More actions';
    moreBtn.textContent = '\u00B7\u00B7\u00B7';
    moreBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const existing = document.querySelector('.msg-overflow-menu');
      if (existing) {
        if (typeof existing._dismiss === 'function') existing._dismiss(); else existing.remove();
        if (existing._trigger === moreBtn) return;
      }

      const menu = document.createElement('div');
      menu.className = 'msg-overflow-menu';
      let closeMenu = () => menu.remove();
      overflow.forEach(a => {
        const item = document.createElement('button');
        item.className = 'msg-overflow-item';
        item.type = 'button';
        item.title = a.title;
        item.innerHTML = `<span class="overflow-icon">${a.icon}</span> ${a.title}`;
        item.addEventListener('click', (ev) => {
          ev.stopPropagation();
          _trackUserAction(a.id);
          closeMenu();
          a.handler(ev);
        });
        menu.appendChild(item);
      });
      menu._trigger = moreBtn;
      document.body.appendChild(menu);
      const btnRect = moreBtn.getBoundingClientRect();
      menu.style.top = (btnRect.top - menu.offsetHeight - 4) + 'px';
      menu.style.left = btnRect.left + 'px';
      if (parseFloat(menu.style.top) < 8) menu.style.top = (btnRect.bottom + 4) + 'px';
      const mr = menu.getBoundingClientRect();
      if (mr.right > window.innerWidth - 8) menu.style.left = (window.innerWidth - mr.width - 8) + 'px';
      closeMenu = bindMenuDismiss(menu, () => menu.remove(), (ev) => !menu.contains(ev.target) && ev.target !== moreBtn);    });
    actions.appendChild(moreBtn);
  }

  footer.appendChild(actions);
  return footer;
}

/**
 * Display performance metrics for a message.
 */
export function displayMetrics(messageElement, metrics) {
  messageElement
    .querySelectorAll('.response-metrics, .metrics-divider, .ctx-divider, .ctx-ring')
    .forEach((el) => el.remove());

  const metricsContainer = document.createElement('span');
  metricsContainer.className = 'response-metrics';

  const responseTime = metrics.response_time;
  const inputTokens = metrics.input_tokens || 0;
  const outputTokens = metrics.output_tokens || 0;
  const tps = metrics.tokens_per_second;
  const isReal = metrics.usage_source === 'real';
  const ctxPct = metrics.context_percent;
  const model = metrics.model || 'Unknown';
  const cost = _billableCost(model, inputTokens, outputTokens);

  // Nothing useful to show — bail out (only if ALL metrics are missing)
  if (!responseTime && !inputTokens && !outputTokens && tps == null && !ctxPct) return;

  // Accumulate session cost (only on fresh metrics, not history reload)
  if (!metrics._fromHistory) {
    const _sid = window.sessionModule && window.sessionModule.getCurrentSessionId();
    if (_sid && cost !== null) {
      try {
        const _costs = JSON.parse(localStorage.getItem(_COST_KEY) || '{}');
        _costs[_sid] = (_costs[_sid] || 0) + cost;
        localStorage.setItem(_COST_KEY, JSON.stringify(_costs));
      } catch (_e) { /* ignore */ }
      updateSessionCostUI();
    }
  }

  // Keep token counts in the Message Stats popup; the footer should stay slim.
  const costStr0 = cost !== null ? `$${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(3)}` : null;
  const hasTps = tps != null && tps !== 'undefined';
  const metricsLabel = hasTps
    ? `${tps} tok/s`
    : costStr0
      ? costStr0
      : responseTime != null
        ? `${responseTime}s`
        : '';
  if (!metricsLabel) return;
  metricsContainer.textContent = metricsLabel;
  metricsContainer.style.cursor = 'pointer';
  metricsContainer.title = 'Click for details';
  const metricsDivider = document.createElement('span');
  metricsDivider.className = 'metrics-divider';
  metricsDivider.textContent = ' | ';
  metricsDivider.style.color = 'var(--color-muted-alt)';
  metricsDivider.style.pointerEvents = 'none';
  metricsContainer.addEventListener('click', (e) => {
    e.stopPropagation();
    document.querySelectorAll('.ctx-popup').forEach(p => { if (typeof p._dismiss === 'function') p._dismiss(); else p.remove(); });

    const costStr = cost !== null ? `$${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(3)}` : '';
    const costRows = costStr ? `<div><span class="ctx-label">Cost</span> ${costStr}</div>` : '';
    const speedStr = tps != null && tps !== 'undefined' ? `${tps} tok/s` : 'n/a';
    const totalTok = inputTokens + outputTokens;
    const ctxColor = ctxPct >= 85 ? 'var(--red, #e06c75)' : ctxPct >= 70 ? '#ff9900' : 'var(--color-muted-alt, #6b7280)';
    const prepTime = metrics.agent_prep_time;
    const modelWaitTime = metrics.agent_model_wait_time;
    const prepBreakdown = metrics.agent_prep_breakdown || null;
    const prepDetails = prepBreakdown
      ? Object.entries(prepBreakdown).map(([k, v]) => `${k}: ${v}s`).join('<br>')
      : '';

    // Session total cost
    let sessionCostStr = '';
    const sc = getSessionCost();
    if (costStr && sc > 0) {
      sessionCostStr = `<div><span class="ctx-label">Session</span> $${sc < 0.01 ? sc.toFixed(4) : sc.toFixed(3)}</div>`;
    }

    const popup = document.createElement('div');
    popup.className = 'ctx-popup';
    popup.innerHTML = `
      <div style="font-weight:600;margin-bottom:6px;color:var(--fg);">Message Stats</div>
      <div><span class="ctx-label">Model</span> ${model.split('/').pop()}</div>
      <div><span class="ctx-label">Input</span> ${inputTokens.toLocaleString()} tokens${isReal ? '' : '~'}</div>
      <div><span class="ctx-label">Output</span> ${outputTokens.toLocaleString()} tokens${isReal ? '' : '~'}</div>
      <div><span class="ctx-label">Total</span> ${totalTok.toLocaleString()} tokens</div>
      <div><span class="ctx-label">Speed</span> ${speedStr}</div>
      <div><span class="ctx-label">Time</span> ${responseTime}s</div>
      ${prepTime != null ? `<div><span class="ctx-label">Prep</span> ${prepTime}s</div>` : ''}
      ${modelWaitTime != null ? `<div><span class="ctx-label">Model wait</span> ${modelWaitTime}s</div>` : ''}
      ${costRows}
      ${sessionCostStr}
      ${prepDetails ? `<div style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border);font-size:0.85em;opacity:0.8;">
        <div style="font-weight:600;margin-bottom:4px;color:var(--fg);">Agent prep</div>
        ${prepDetails}
      </div>` : ''}
      ${ctxPct !== undefined && ctxPct > 0 ? `<div style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border);">
        <span class="ctx-label">Context</span> <span style="color:${ctxColor};font-weight:600;">${ctxPct}%</span> used
      </div>` : ''}
      ${isReal ? '' : '<div style="margin-top:4px;font-size:0.8em;opacity:0.4;">~ estimated token count</div>'}
    `;

    const rect = metricsContainer.getBoundingClientRect();
    popup.style.left = rect.left + 'px';
    popup.style.visibility = 'hidden';
    document.body.appendChild(popup);
    const pr = popup.getBoundingClientRect();
    const spaceAbove = rect.top;
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceAbove >= pr.height + 8 || spaceAbove > spaceBelow) {
      popup.style.top = (rect.top - pr.height - 8) + 'px';
    } else {
      popup.style.top = (rect.bottom + 8) + 'px';
    }
    if (pr.right > window.innerWidth - 8) popup.style.left = (window.innerWidth - pr.width - 8) + 'px';
    if (parseFloat(popup.style.left) < 8) popup.style.left = '8px';
    popup.style.visibility = '';

    bindMenuDismiss(popup, () => popup.remove());
  });

  // Store real context length for model info popup
  if (metrics.context_length && metrics.model) {
    if (!window._realContextLengths) window._realContextLengths = {};
    window._realContextLengths[metrics.model] = metrics.context_length;
  }

  // Context usage ring
  let ctxRing = null;
  const ctxLen = metrics.context_length || 0;
  if (ctxPct !== undefined && ctxPct > 0) {
    const r = 6, stroke = 1.5;
    const circ = 2 * Math.PI * r;
    const fill = circ * (ctxPct / 100);
    const ctxColor = ctxPct >= 85 ? 'var(--red, #e06c75)' : ctxPct >= 70 ? '#ff9900' : 'var(--green, #98c379)';
    ctxRing = document.createElement('span');
    ctxRing.className = 'ctx-ring';
    ctxRing.title = `${ctxPct}% context used — click for details`;
    ctxRing.style.cursor = 'pointer';
    ctxRing.style.setProperty('--ctx-color', ctxColor);
    ctxRing.innerHTML = `<svg width="14" height="14" viewBox="0 0 14 14">
      <circle cx="7" cy="7" r="${r}" fill="none" stroke="var(--border, #333)" stroke-width="${stroke}" opacity="0.3"/>
      <circle cx="7" cy="7" r="${r}" fill="none" stroke="var(--ctx-stroke)" stroke-width="${stroke}"
        stroke-dasharray="${fill} ${circ - fill}" stroke-dashoffset="${circ * 0.25}"
        stroke-linecap="round" transform="rotate(-90 7 7)"/>
    </svg><span class="ctx-ring-pct">${Math.round(ctxPct)}%</span>`;

    ctxRing.addEventListener('click', (e) => {
      e.stopPropagation();
      document.querySelectorAll('.ctx-detail-popup').forEach(p => { if (typeof p._dismiss === 'function') p._dismiss(); else p.remove(); });

      const usedTokens = inputTokens || 0;
      const totalCtx = ctxLen || 0;
      const modelShort = model.split('/').pop();
      const fmtNum = n => n ? n.toLocaleString() : '?';

      const popup = document.createElement('div');
      popup.className = 'ctx-detail-popup';
      popup.innerHTML = `
        <div style="font-weight:600;margin-bottom:8px;color:var(--fg);">Context Window</div>
        <div class="ctx-bar-wrap">
          <div class="ctx-bar-fill" style="width:${Math.min(ctxPct, 100)}%;background:${ctxColor};"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:0.75rem;margin-top:4px;opacity:0.6;">
          <span>${fmtNum(usedTokens)} used</span>
          <span>${fmtNum(totalCtx)} total</span>
        </div>
        <div style="margin-top:8px;font-size:0.8rem;">
          <div><span class="ctx-label">Model</span> ${modelShort}</div>
          <div><span class="ctx-label">Usage</span> <span style="color:${ctxColor};font-weight:600;">${ctxPct}%</span></div>
          <div><span class="ctx-label">Window</span> ${fmtNum(totalCtx)} tokens</div>
        </div>
        ${ctxPct >= 70 ? `<button class="ctx-compact-btn" title="Summarize older messages to free up context">Compact context</button>` : ''}
      `;

      const compactBtn = popup.querySelector('.ctx-compact-btn');
      if (compactBtn) {
        compactBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const sid = window.sessionModule && window.sessionModule.getCurrentSessionId();
          if (!sid) return;
          popup.remove();

          // Add a spinner bubble at the bottom of chat
          const chatBox = document.getElementById('chat-history');
          if (!chatBox) return;
          const compactMsg = document.createElement('div');
          compactMsg.className = 'msg msg-ai';
          const compactRole = document.createElement('div');
          compactRole.className = 'role';
          compactRole.textContent = window.__agentName || 'Open Clank';
          const compactBody = document.createElement('div');
          compactBody.className = 'body';
          compactBody.innerHTML = 'Compacting context <span class="compact-wave">▁▂▃▅▂▁</span>';
          compactMsg.appendChild(compactRole);
          compactMsg.appendChild(compactBody);
          chatBox.appendChild(compactMsg);
          chatBox.scrollTop = chatBox.scrollHeight;

          // Animate the wave
          const waveFrames = ['▁▂▃▅▂▁', '▂▃▅▃▂▁', '▃▅▃▂▁▂', '▅▃▂▁▂▃', '▃▂▁▂▃▅', '▂▁▂▃▅▃'];
          let frame = 0;
          const waveEl = compactBody.querySelector('.compact-wave');
          const waveInterval = setInterval(() => {
            frame = (frame + 1) % waveFrames.length;
            if (waveEl) waveEl.textContent = waveFrames[frame];
          }, 150);

          try {
            const res = await fetch(window.location.origin + '/api/session/' + sid + '/compact', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
            });
            clearInterval(waveInterval);
            if (res.ok) {
              const data = await res.json();
              // Reload session — the compacted history will show
              if (window.sessionModule) await window.sessionModule.selectSession(sid);
              // Scroll to the compacted message (first msg with compacted metadata)
              setTimeout(() => {
                const msgs = document.querySelectorAll('#chat-history .msg');
                for (const m of msgs) {
                  if (m.querySelector('.body')?.textContent.includes('Conversation compacted')) {
                    m.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    break;
                  }
                }
              }, 200);
            } else {
              let detail = 'Compaction failed. Try again later.';
              try {
                const err = await res.json();
                if (err.detail) detail = err.detail;
              } catch {}
              compactBody.textContent = detail;
              compactBody.style.color = 'var(--red)';
            }
          } catch (err) {
            clearInterval(waveInterval);
            console.warn('compact failed:', err);
            compactBody.innerHTML = '<span style="color:var(--red);">Compaction failed: ' + err.message + '</span>';
          }
        });
      }

      const rect = ctxRing.getBoundingClientRect();
      popup.style.visibility = 'hidden';
      document.body.appendChild(popup);
      const pr = popup.getBoundingClientRect();
      // Position above the ring, right-aligned
      popup.style.left = Math.max(8, rect.right - pr.width) + 'px';
      const spaceAbove = rect.top;
      if (spaceAbove >= pr.height + 8) {
        popup.style.top = (rect.top - pr.height - 8) + 'px';
      } else {
        popup.style.top = (rect.bottom + 8) + 'px';
      }
      popup.style.visibility = '';

      bindMenuDismiss(popup, () => popup.remove(), (ev) => !popup.contains(ev.target) && ev.target !== ctxRing && !ctxRing.contains(ev.target));
    });
  }

  let footer = messageElement.querySelector('.msg-footer');
  if (!footer) {
    footer = createMsgFooter(messageElement);
    if (messageElement.classList?.contains('agent-thread')) {
      footer.classList.add('agent-thread-footer');
    }
    messageElement.appendChild(footer);
  }
  if (footer) {
    const actions = footer.querySelector('.msg-actions');
    if (actions) {
      footer.insertBefore(metricsDivider, actions);
      footer.insertBefore(metricsContainer, metricsDivider);
    } else {
      footer.appendChild(metricsContainer);
      footer.appendChild(metricsDivider);
    }
    if (ctxRing) {
      const ctxDiv = document.createElement('span');
      ctxDiv.textContent = ' | ';
      ctxDiv.style.color = 'var(--color-muted-alt)';
      ctxDiv.style.pointerEvents = 'none';
      ctxDiv.className = 'ctx-divider';
      footer.appendChild(ctxDiv);
      footer.appendChild(ctxRing);
    }
  } else {
    messageElement.appendChild(metricsContainer);
    if (ctxRing) messageElement.appendChild(ctxRing);
  }

  if (uiModule) uiModule.scrollHistory();
}

/** Remove any unanswered multiple-choice cards currently in the chat. */
export function removeAskUserCards(root) {
  const scope = root || document.getElementById('chat-history') || document;
  scope.querySelectorAll('.ask-user-card').forEach((node) => node.remove());
}

function _renderMimoQuestionCard(aq, renderOptions) {
  const chatBox = document.getElementById('chat-history');
  const questions = Array.isArray(aq.questions) ? aq.questions : [];
  if (!chatBox || !aq.request_id || !aq.session_id || !questions.length) return null;
  if (aq.blocking === false) {
    chatBox.querySelectorAll('.mimo-question-card').forEach(card => {
      if (card.dataset.requestId === aq.native_request_id) card.remove();
    });
  } else {
    chatBox.querySelectorAll('.mimo-question-card').forEach(card => {
      if (card.dataset.blocking !== 'false') card.remove();
    });
  }

  const card = document.createElement('form');
  card.className = 'ask-user-card mimo-question-card';
  card.dataset.requestId = aq.native_request_id || aq.request_id;
  card.dataset.hostRequestId = aq.request_id;
  card.dataset.sessionId = aq.session_id;
  if (typeof aq.actor_session_id === 'string' && typeof aq.actor_id === 'string' && typeof aq.question_ref === 'string' && Number.isSafeInteger(aq.question_revision)) {
    card.dataset.actorSessionId = aq.actor_session_id; card.dataset.actorId = aq.actor_id;
    card.dataset.questionRef = aq.question_ref; card.dataset.questionRevision = String(aq.question_revision);
  }
  card.dataset.blocking = aq.blocking === false ? 'false' : 'true';
  card.dataset.interactive = renderOptions.interactive === false ? 'false' : 'true';
  let sending = false;
  card.setAttribute('aria-label', 'Open Clank agent question');
  const status = document.createElement('div');
  status.className = 'settings-save-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');

  const send = async (payload) => {
    if (aq.blocking === false) {
      const submitAnswer = window.chatModule?.submitOptionalQuestionAnswer;
      if (typeof submitAnswer !== 'function') throw new Error('Question delivery is not available yet. Your answer was not sent.');
      const result = await submitAnswer(aq, payload.answers || [], payload.rejected ? 'dismissed' : 'answered', payload.retry === true);
      const delivery = result?.deliveryState || result?.delivery || '';
      const state = delivery === 'unavailable' ? 'unavailable' : result?.receipt?.state || delivery || 'pending';
      if (state === 'consumed') {
        card.remove();
        document.dispatchEvent(new CustomEvent('odysseus:interactive-resolved', {
          detail: { sessionId: aq.session_id, kind: 'question', requestId: aq.native_request_id },
        }));
      } else {
        status.textContent = state === 'unavailable'
          ? 'Answer saved; delivery is unavailable. Retry the saved answer later.'
          : 'Answer saved; waiting for the actor to reach a safe delivery point.';
        submit.disabled = true;
        close.disabled = true;
        if (aq.saved_answer) retrySaved.hidden = false;
      }
      return result;
    }
    const response = renderOptions.submitTaskWait
      ? await renderOptions.submitTaskWait(payload)
      : await fetch(
          `${window.API_BASE || ''}/api/session/${encodeURIComponent(aq.session_id)}/question`,
          { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: aq.request_id, ...payload }) },
        );
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) {
      const detail = data.detail || data.error;
      const code = detail && typeof detail === 'object' ? detail.code : '';
      if (code === 'question_stale') {
        card.remove();
        document.dispatchEvent(new CustomEvent('odysseus:interactive-resolved', {
          detail: { sessionId: aq.session_id, kind: 'question', requestId: aq.request_id },
        }));
      }
      throw new Error((typeof detail === 'string' ? detail : detail?.message) || `Question reply failed (${response.status})`);
    }
    card.remove();
    document.dispatchEvent(new CustomEvent('odysseus:interactive-resolved', {
      detail: { sessionId: aq.session_id, kind: 'question', requestId: aq.request_id },
    }));
  };

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'modal-close ask-user-close';
  close.setAttribute('aria-label', 'Reject question');
  close.innerHTML = uiIcon('close', 16); close.setAttribute('aria-label', close.getAttribute('aria-label') || close.title || 'Close');
  close.addEventListener('click', async () => {
    if (sending || renderOptions.interactive === false) return;
    sending = true; close.disabled = true; submit.disabled = true;
    try {
      if (aq.blocking === false) aq.saved_answer = { disposition: 'dismissed', answers: [] };
      await send({ rejected: true });
    }
    catch (error) {
      status.textContent = error.message;
      if (aq.blocking === false) { close.disabled = true; submit.disabled = true; retrySaved.hidden = false; }
      else { close.disabled = false; submit.disabled = false; }
    }
    finally { sending = false; }
  });
  card.appendChild(close);

  questions.forEach((question, questionIndex) => {
    const group = document.createElement('fieldset');
    group.className = 'ask-user-question-group';
    const legend = document.createElement('legend');
    legend.className = 'ask-user-question';
    legend.textContent = question.question || question.header || `Question ${questionIndex + 1}`;
    group.appendChild(legend);
    const options = Array.isArray(question.options) ? question.options : [];
    options.forEach((option, optionIndex) => {
      const row = document.createElement('label');
      row.className = 'ask-user-option';
      const input = document.createElement('input');
      input.type = question.multiple ? 'checkbox' : 'radio';
      input.name = `mimo-question-${questionIndex}`;
      input.value = String(option.label || option);
      input.dataset.questionIndex = String(questionIndex);
      input.dataset.optionIndex = String(optionIndex);
      const savedValues = aq.saved_answer?.answers?.[questionIndex] || [];
      if (savedValues.includes(input.value)) input.checked = true;
      if (!question.multiple) input.addEventListener('change', () => {
        if (input.checked) {
          const custom = group.querySelector('[data-custom-question-index]');
          if (custom) custom.value = '';
        }
      });
      row.appendChild(input);
      const label = document.createElement('span');
      label.className = 'ask-user-option-label';
      label.textContent = input.value;
      row.appendChild(label);
      if (option.description) {
        const description = document.createElement('span');
        description.className = 'ask-user-option-desc';
        description.textContent = option.description;
        row.appendChild(description);
      }
      group.appendChild(row);
    });
    if (question.custom !== false) {
      const custom = document.createElement('input');
      custom.type = 'text';
      custom.className = 'styled-prompt-input ask-user-other-input';
      custom.placeholder = 'Custom answer…';
      custom.dataset.customQuestionIndex = String(questionIndex);
      const savedValues = aq.saved_answer?.answers?.[questionIndex] || [];
      const optionLabels = options.map(option => String(option.label || option));
      const savedCustom = savedValues.filter(value => !optionLabels.includes(value));
      if (savedCustom.length) custom.value = savedCustom.join(', ');
      if (!question.multiple) custom.addEventListener('input', () => {
        if (custom.value.trim()) group.querySelectorAll('input[type=radio]').forEach(input => { input.checked = false; });
      });
      group.appendChild(custom);
    }
    card.appendChild(group);
  });

  const submit = document.createElement('button');
  submit.type = 'submit';
  submit.className = 'confirm-btn confirm-btn-primary';
  submit.textContent = 'Answer';
  card.appendChild(submit);
  const retrySaved = document.createElement('button');
  retrySaved.type = 'button';
  retrySaved.className = 'confirm-btn';
  retrySaved.textContent = 'Retry saved answer';
  retrySaved.hidden = true;
  card.appendChild(retrySaved);
  card.appendChild(status);
  retrySaved.addEventListener('click', async () => {
    if (sending || !aq.saved_answer) return;
    sending = true; retrySaved.disabled = true;
    status.textContent = 'Checking saved answer delivery…';
    try { await send({ answers: aq.saved_answer.answers || [], rejected: aq.saved_answer.disposition === 'dismissed', retry: true }); }
    catch (error) { status.textContent = `Saved answer is still unresolved: ${error.message}`; retrySaved.disabled = false; }
    finally { sending = false; }
  });
  card.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (sending || renderOptions.interactive === false) return;
    const answers = questions.map((question, index) => {
      const selected = Array.from(card.querySelectorAll(`input[name="mimo-question-${index}"]:checked`)).map((input) => input.value);
      const custom = card.querySelector(`[data-custom-question-index="${index}"]`);
      const customValue = custom ? custom.value.trim() : '';
      if (customValue && !question.multiple) return [customValue];
      if (customValue) selected.push(customValue);
      return selected;
    });
    if (answers.some((answer) => !answer.length)) {
      status.textContent = 'Answer every question before continuing.';
      return;
    }
    sending = true; submit.disabled = true; close.disabled = true;
    status.textContent = 'Sending…';
    try {
      aq.saved_answer = { disposition: 'answered', answers };
      await send({ answers });
    }
    catch (error) {
      status.textContent = error.message;
      if (aq.blocking === false) {
        card.querySelectorAll('input').forEach(input => { input.disabled = true; });
        submit.disabled = true; close.disabled = true; retrySaved.hidden = false;
      } else { submit.disabled = false; close.disabled = false; }
    }
    finally { sending = false; }
  });

  if (renderOptions.interactive === false) {
    card.querySelectorAll('input,button').forEach(input => { input.disabled = true; });
    close.hidden = true; submit.hidden = true;
    status.textContent = 'Question from this chat’s history.';
  } else if (aq.blocking === false && aq.answer_state === 'answer-queued') {
    card.querySelectorAll('input').forEach(input => { input.disabled = true; });
    close.hidden = true; submit.hidden = true;
    status.textContent = 'Your answer is saved and waiting for the actor to reach a safe delivery point.';
    if (aq.saved_answer) retrySaved.hidden = false;
  } else if (aq.blocking === false && aq.answer_state === 'stale') {
    card.querySelectorAll('input,button').forEach(input => { input.disabled = true; });
    close.hidden = true; submit.hidden = true;
    status.textContent = 'This question is stale and cannot accept an answer.';
  } else if (aq.blocking === false && aq.local_delivery_state === 'unavailable') {
    card.querySelectorAll('input').forEach(input => { input.disabled = true; });
    close.hidden = true; submit.hidden = true;
    status.textContent = 'The saved answer could not be delivered. Retry the same answer when the actor is available.';
    if (aq.saved_answer) retrySaved.hidden = false;
  } else if (aq.blocking === false && aq.local_delivery_state === 'unknown') {
    status.textContent = 'A saved answer has an unknown delivery outcome. Retry the same answer to check it.';
  }
  chatBox.appendChild(card);
  if (renderOptions.scroll !== false) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  if (renderOptions.focus !== false) card.focus();
  return card;
}

/**
 * Render an ask_user payload as a durable choice card.
 *
 * This lives in the history renderer rather than the streaming loop so the
 * same UI can be used both for a live SSE event and for a persisted tool event
 * after a session reload.
 */
export function renderAskUserCard(payload, options) {
  const aq = payload || {};
  const renderOptions = options || {};
  if (aq.request_id && Array.isArray(aq.questions)) {
    return _renderMimoQuestionCard(aq, renderOptions);
  }
  const opts = Array.isArray(aq.options) ? aq.options : [];
  const chatBox = document.getElementById('chat-history');
  if (!chatBox || !aq.question || opts.length < 2) return null;

  removeAskUserCards(chatBox);

  const card = document.createElement('div');
  card.className = 'ask-user-card';
  card.setAttribute('role', 'group');
  card.tabIndex = -1;
  const multi = !!aq.multi;
  const emojiText = (value) => svgifyEmoji(uiModule.esc(String(value)));

  const head = document.createElement('div');
  head.className = 'ask-user-head';
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'modal-close ask-user-close';
  closeBtn.setAttribute('aria-label', 'Dismiss question');
  closeBtn.innerHTML = uiIcon('close', 16); closeBtn.setAttribute('aria-label', closeBtn.getAttribute('aria-label') || closeBtn.title || 'Close');
  closeBtn.addEventListener('click', () => {
    card.remove();
    const input = uiModule.el('message');
    if (input) input.focus();
  });
  head.appendChild(closeBtn);
  card.appendChild(head);

  const question = document.createElement('div');
  question.className = 'ask-user-question';
  question.id = `ask-user-q-${Date.now()}-${Math.floor(Math.random() * 1e4)}`;
  question.innerHTML = emojiText(aq.question);
  card.appendChild(question);
  card.setAttribute('aria-labelledby', question.id);

  const list = document.createElement('div');
  list.className = 'ask-user-options';
  card.appendChild(list);

  const send = (text) => {
    if (!text) return;
    card.remove();
    const input = uiModule.el('message');
    if (input) input.value = text;
    const sendButton = document.querySelector('.send-btn');
    if (sendButton) sendButton.click();
  };

  opts.forEach((opt) => {
    const label = (opt && opt.label) ? String(opt.label) : String(opt || '');
    if (!label) return;
    const description = (opt && opt.description) ? String(opt.description) : '';
    const row = document.createElement(multi ? 'label' : 'button');
    row.className = 'ask-user-option';
    if (multi) {
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = label;
      row.appendChild(checkbox);
    }
    const labelText = document.createElement('span');
    labelText.className = 'ask-user-option-label';
    labelText.innerHTML = emojiText(label);
    row.appendChild(labelText);
    if (description) {
      const descriptionText = document.createElement('span');
      descriptionText.className = 'ask-user-option-desc';
      descriptionText.innerHTML = emojiText(description);
      row.appendChild(descriptionText);
    }
    if (!multi) {
      row.type = 'button';
      row.addEventListener('click', () => send(label));
    }
    list.appendChild(row);
  });

  const other = document.createElement('div');
  other.className = 'ask-user-other';
  const otherInput = document.createElement('input');
  otherInput.type = 'text';
  otherInput.className = 'styled-prompt-input ask-user-other-input';
  otherInput.placeholder = multi ? 'Other (added to selection)…' : 'Other… (type your own answer)';
  otherInput.setAttribute('aria-label', multi ? 'Add a custom option' : 'Type a custom answer');
  const otherSend = document.createElement('button');
  otherSend.type = 'button';
  otherSend.className = 'confirm-btn confirm-btn-primary ask-user-other-send';
  otherSend.setAttribute('aria-label', 'Send answer');
  otherSend.textContent = multi ? 'Send selection' : 'Send';
  const submit = () => {
    const freeText = otherInput.value.trim();
    if (multi) {
      const picked = Array.from(card.querySelectorAll('.ask-user-option input:checked')).map((input) => input.value);
      if (freeText) picked.push(freeText);
      if (picked.length) send(picked.join(', '));
    } else if (freeText) {
      send(freeText);
    }
  };
  otherSend.addEventListener('click', submit);
  otherInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });
  other.appendChild(otherInput);
  other.appendChild(otherSend);
  card.appendChild(other);

  chatBox.appendChild(card);
  if (renderOptions.scroll !== false) {
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  if (renderOptions.focus !== false) {
    try { card.focus(); } catch (_) {}
  }
  return card;
}

/** Pure view model for the real-desktop control approval; null for every other permission type. */
export function controlPermissionView(pq) {
  const kind = /^lifetools_.+_(control|inspect)_desktop$/.exec(String(pq?.permission_type || ''));
  if (!kind) return null;
  const reading = kind[1] === 'inspect';
  const summary = typeof pq.detail?.summary === 'string' && pq.detail.summary ? pq.detail.summary : null;
  return {
    title: reading ? 'Read an application window' : 'Desktop control',
    summary: summary ?? 'Details unavailable',
    caution: reading ? 'This reads what that window shows. Secret-looking values are hidden, but the filter is not a guarantee.' : 'This acts on your real desktop. Effects are not verified automatically.',
    // No remembered-choice options, and no Allow when the exact action cannot be shown.
    options: summary === null
      ? [{ id: 'reject', label: 'Reject' }]
      : [{ id: 'once', label: 'Allow once' }, { id: 'reject', label: 'Reject' }],
  };
}

/** Pure: is anything of desktop control running that a person could stop? */
export function controlStopVisible(status) {
  return !!status && (status.session_active === true || status.accessibility_active === true || status.busy === true);
}

let controlStopCard = null;
let controlStopTimer = null;
let controlStopQuiet = 0;
let controlStopLimit = 3;

function controlStopChat() {
  return window.sessionModule?.getCurrentSessionId?.() || '';
}

async function controlStopStatus(fetchFn, sessionId) {
  if (!sessionId) return null;
  const response = await fetchFn(`/api/desktop-control/status?include_recording=false&session_id=${encodeURIComponent(sessionId)}`, {
    credentials: 'same-origin', cache: 'no-store',
  });
  return response.ok ? response.json() : null;
}

/**
 * While a desktop-control session may exist, keep one real "Stop control" button at the end of the chat. It calls the Host
 * stop route directly (no model, no approval), is visible only while a session is active, and removes itself afterwards.
 * The watch is bounded: it ends after `quietPolls` consecutive polls (2 s apart) with nothing running. Starting it again while
 * it runs resets the quiet count (and may lengthen the window, e.g. after an Allow, to cover the time until the helper is active).
 */
export function watchControlStop(fetchFn = (...a) => window.fetch(...a), quietPolls = 3) {
  controlStopQuiet = 0;
  controlStopLimit = Math.max(controlStopLimit, quietPolls);
  if (controlStopTimer) return;
  const hide = () => { if (controlStopCard) { controlStopCard.remove(); controlStopCard = null; } };
  const updateDetail = status => {
    const activity = Array.isArray(status.activity) ? status.activity : [];
    const lines = activity.map(item => {
      const display = item.target?.kind === 'shared_display'
        ? `Shared display ${item.target.width} × ${item.target.height}`
        : 'Target details unavailable';
      const label = item.kind === 'a11y' ? 'Accessibility' : 'Desktop input';
      const shortcut = item.takeoverShortcut ? ` · Takeover key ${item.takeoverShortcut}` : '';
      const overlay = item.cursorOverlayAtBegin === true ? ' · Cursor overlay available at start' : '';
      return `${label} · ${item.platform} · ${item.actorId} · ${display}${shortcut}${overlay}`;
    });
    if (status.pending_stop_reconciliation) lines.push('Waiting for helper stop reconciliation.');
    lines.push('Stop ends desktop control on this host.');
    controlStopCard._controlStatus.textContent = lines.join(' ');
  };
  const show = status => {
    const chatBox = document.getElementById('chat-history');
    if (!chatBox) return;
    if (controlStopCard?.isConnected) { updateDetail(status); return; }
    controlStopCard = document.createElement('div');
    controlStopCard.className = 'ask-user-card control-stop-card';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'ask-user-option';
    const label = document.createElement('span');
    label.className = 'ask-user-option-label';
    label.textContent = 'Stop desktop control on this host';
    button.appendChild(label);
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const response = await fetchFn('/api/desktop-control/stop', { method: 'POST', credentials: 'same-origin' });
        const result = response.ok ? await response.json() : null;
        if (result?.taken_over !== true || result.takeoverScope !== 'host') throw new Error('Stop unavailable');
        hide();
      } catch (_) {
        button.disabled = false;
        if (button.isConnected) controlStopCard._controlStatus.textContent = 'Stop could not be confirmed. Check desktop control status.';
      }
    });
    controlStopCard.appendChild(button);
    const detail = document.createElement('div');
    detail.className = 'control-stop-status';
    controlStopCard._controlStatus = detail;
    controlStopCard.appendChild(detail);
    chatBox.appendChild(controlStopCard);
    updateDetail(status);
  };
  const poll = async () => {
    const sessionId = controlStopChat();
    let status = null;
    try { status = await controlStopStatus(fetchFn, sessionId); } catch (_) {}
    if (sessionId !== controlStopChat()) return;
    if (controlStopVisible(status)) { controlStopQuiet = 0; controlStopLimit = 3; show(status); return; }
    hide();
    if (++controlStopQuiet >= controlStopLimit) { clearInterval(controlStopTimer); controlStopTimer = null; controlStopLimit = 3; }
  };
  controlStopTimer = setInterval(poll, 2000);
  void poll();
}

/** One status read at page load: a session that is already running gets its Stop button (and the bounded watch). */
export async function checkControlStopOnLoad(fetchFn = (...a) => window.fetch(...a)) {
  const sessionId = controlStopChat();
  try {
    const status = await controlStopStatus(fetchFn, sessionId);
    if (sessionId === controlStopChat() && controlStopVisible(status)) watchControlStop(fetchFn);
  } catch (_) {}
}
if (typeof window !== 'undefined' && typeof document !== 'undefined' && typeof window.fetch === 'function') {
  void checkControlStopOnLoad();
  document.addEventListener('odysseus:session-selected', () => {
    if (controlStopCard) { controlStopCard.remove(); controlStopCard = null; }
    void checkControlStopOnLoad();
  });
}

/**
 * Inline permission prompt (C1). The agent's turn is blocked server-side
 * until one of the three buttons answers the request — there is no timeout.
 * Chat/workspace/always choices persist a grant at the selected lifetime;
 * once is consumed only by this request.
 */
export function renderPermissionCard(payload, sessionId, renderOptions = {}) {
  const pq = payload || {};
  // renderOptions.container lets the Clankshop show the same card; onceOrReject limits it to Allow once and Reject.
  const chatBox = renderOptions.container || document.getElementById('chat-history');
  if (!chatBox || !pq.request_id) return null;

  const card = document.createElement('div');
  card.className = 'ask-user-card permission-card';
  card.setAttribute('role', 'group');
  card.dataset.requestId = pq.request_id;
  card.dataset.sessionId = sessionId;

  const title = document.createElement('div');
  title.className = 'ask-user-question';
  const control = pq.sudo_password ? null : controlPermissionView(pq);
  if (control && typeof watchControlStop === 'function') watchControlStop();
  title.textContent = pq.sudo_password
    ? 'Approved command needs your sudo password'
    : control ? control.title : `Agent requests permission: ${pq.permission_type || 'unknown'}`;
  card.appendChild(title);

  const d = control ? {} : (pq.detail || {});
  const exactDetails = exactOperationDetails(d);
  const detailText = control ? control.summary : exactDetails.length ? exactDetails.join('\n') : (Object.keys(d).length ? JSON.stringify(d) : '');
  if (detailText) {
    const detail = document.createElement('div');
    detail.className = 'permission-detail';
    detail.style.cssText = 'font-size:12px;opacity:0.75;word-break:break-all;white-space:pre-wrap;margin:2px 0 8px;font-family:inherit;';
    detail.textContent = detailText;
    card.appendChild(detail);
  }
  if (control) {
    const caution = document.createElement('div');
    caution.style.cssText = 'font-size:12px;opacity:0.75;margin:0 0 8px;';
    caution.textContent = control.caution;
    card.appendChild(caution);
  }

  if (exactDetails.length && hasAdditionalOperationDetails(d)) {
    const extra = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'Additional request fields';
    const description = document.createElement('p');
    description.textContent = 'Additional fields are present; their effects are unknown.';
    extra.append(summary, description); card.appendChild(extra);
  }

  let answered = false;
  if (!pq.sudo_password && !control) {
    const effects = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'Effect details unavailable';
    const explanation = document.createElement('p');
    explanation.textContent = 'Effects and recovery coverage are unknown. Approval scope depends on the choice below.';
    effects.append(summary, explanation); card.appendChild(effects);
    void verifiedOperationExplanation(pq, sessionId).then(value => {
      const getSession = window.sessionModule?.getCurrentSessionId;
      if (!value || answered || !card.isConnected || typeof getSession !== 'function' || getSession() !== sessionId || card.dataset.requestId !== pq.request_id) return;
      summary.textContent = value.intent === 'requested_file_edit' ? 'Requested effect: edit the file' : 'Effects not determined';
    });
  }

  const list = document.createElement('div');
  list.className = 'ask-user-options';
  card.appendChild(list);

  const finish = (label, ok) => {
    list.remove();
    const done = document.createElement('div');
    done.style.cssText = 'font-size:12px;opacity:0.7;font-style:italic;';
    done.textContent = ok ? `→ ${label}` : `Failed to send answer (${label}) — request may have expired.`;
    card.appendChild(done);
  };

  const answer = async (optionId, label, secret) => {
    answered = true;
    // An Allow on a control/read card starts (or restarts) the Stop watch with a long quiet window: the helper needs a moment to report active.
    if (control && optionId === 'once' && typeof watchControlStop === 'function') watchControlStop(undefined, 15);
    list.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    const body = new FormData();
    body.append('request_id', pq.request_id);
    body.append('option_id', optionId);
    if (secret) body.append('secret', secret);
    try {
      const res = renderOptions.submitTaskWait
        ? await renderOptions.submitTaskWait({ option_id: optionId })
        : await fetch(`/api/session/${encodeURIComponent(sessionId)}/permission`, {
            method: 'POST', body, credentials: 'same-origin',
          });
      if (renderOptions.submitTaskWait && res.status === 503) {
        let detail = {};
        try { detail = await res.json(); } catch (_) {}
        if (detail.detail === 'Answer accepted; task wait bookkeeping is unavailable') {
          finish('Answer accepted; task wait bookkeeping is unavailable', true);
          return;
        }
      }
      finish(label, res.ok);
      if (res.ok) {
        document.dispatchEvent(new CustomEvent('odysseus:interactive-resolved', {
          detail: { sessionId, kind: 'permission', requestId: pq.request_id },
        }));
      }
    } catch (e) {
      console.warn('permission resolve failed:', e);
      finish(label, false);
    }
  };

  if (pq.sudo_password) {
    // The exact command was already approved; this card only collects the
    // password sudo needs. The value goes straight to the single-use server
    // stash — it is never rendered back or persisted client-side.
    const input = document.createElement('input');
    input.type = 'password';
    input.className = 'styled-prompt-input ask-user-other-input';
    input.placeholder = 'sudo password (never stored)';
    input.autocomplete = 'off';
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); answer('once', 'Submit password', input.value); }
    });
    list.appendChild(input);
    [
      { id: 'once', label: 'Submit password', withSecret: true },
      { id: 'reject', label: 'Run without password', withSecret: false },
    ].forEach((b) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'ask-user-option';
      const labelText = document.createElement('span');
      labelText.className = 'ask-user-option-label';
      labelText.textContent = b.label;
      row.appendChild(labelText);
      row.addEventListener('click', () => answer(b.id, b.label, b.withSecret ? input.value : ''));
      list.appendChild(row);
    });
    chatBox.appendChild(card);
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    try { input.focus(); } catch (_) {}
    return card;
  }

  const alwaysDesc = pq.always_pattern && pq.always_pattern !== '*'
    ? `${pq.always_pattern}/**`
    : `all “${pq.permission_type || 'unknown'}” requests`;
  if (control) {
    control.options.forEach((b) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'ask-user-option';
      row.dataset.optionId = b.id;
      const labelText = document.createElement('span');
      labelText.className = 'ask-user-option-label';
      labelText.textContent = b.label;
      row.appendChild(labelText);
      row.addEventListener('click', () => answer(b.id, b.label));
      list.appendChild(row);
    });
    chatBox.appendChild(card);
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    // Reject takes focus so a stray Enter or Space can never allow.
    try { list.querySelector('[data-option-id="reject"]').focus(); } catch (_) {}
    return card;
  }

  [
    { id: 'once', label: 'Allow once', desc: '' },
    { id: 'chat', label: 'Allow in this chat', desc: 'Until this chat is reset' },
    { id: 'workspace', label: 'Allow in this workspace', desc: 'Shared by chats using this workspace' },
    { id: 'always', label: 'Always allow', desc: alwaysDesc },
    { id: 'reject', label: 'Reject', desc: '' },
  ].filter((b) => !renderOptions.onceOrReject || b.id === 'once' || b.id === 'reject').forEach((b) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'ask-user-option';
    const labelText = document.createElement('span');
    labelText.className = 'ask-user-option-label';
    labelText.textContent = b.label;
    row.appendChild(labelText);
    if (b.desc) {
      const descEl = document.createElement('span');
      descEl.className = 'ask-user-option-desc';
      descEl.textContent = b.desc;
      row.appendChild(descEl);
    }
    row.addEventListener('click', () => answer(b.id, b.label));
    list.appendChild(row);
  });

  chatBox.appendChild(card);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  return card;
}

export function renderActorAccounting(wrap, accounting) {
  if (!wrap || !accounting || typeof accounting !== 'object') return null;
  wrap.querySelector(':scope > .actor-accounting')?.remove();
  const state = String(accounting.state || 'not_recorded');
  const total = Number.isInteger(accounting.total) ? accounting.total : null;
  const label = state === 'partial'
    ? (total ? `Observed workers: ${total} (partial)` : 'Worker accounting: partial coverage')
    : (total === null ? `Sub-agents: ${state === 'unavailable' ? 'unavailable' : 'not recorded'}` : `Sub-agents: ${total}`);
  let node;
  if (total > 0) {
    node = document.createElement('details');
    node.className = 'actor-accounting';
    const summary = document.createElement('summary');
    summary.textContent = label;
    node.appendChild(summary);
    const list = document.createElement('ol');
    list.className = 'actor-accounting-list';
    for (const actor of accounting.actors || []) {
      const item = document.createElement('li');
      item.dataset.actorId = actor.id || '';
      if (actor.session_id) item.dataset.actorSessionId = actor.session_id;
      if (actor.parent_id) item.dataset.parentActorId = actor.parent_id;
      if (actor.parent_session_id) item.dataset.parentActorSessionId = actor.parent_session_id;
      const revision = Number.isSafeInteger(actor.execution_revision) && actor.execution_revision >= 0
        ? `revision ${actor.execution_revision}` : '';
      if (revision) item.dataset.executionRevision = String(actor.execution_revision);
      const identity = actor.session_id ? `actor ${actor.id} · session ${actor.session_id}` : '';
      const parent = actor.parent_id && actor.parent_session_id
        ? `parent ${actor.parent_id} (session ${actor.parent_session_id})` : '';
      const requested = actor.requested_model ? `requested ${actor.requested_model}` : '';
      const effective = actor.effective_model && typeof actor.effective_model === 'object'
        ? `effective ${[actor.effective_model.provider_id, actor.effective_model.model_id].filter(Boolean).join('/')}`
        : '';
      item.textContent = [
        actor.description || actor.agent || actor.id || 'Sub-agent', actor.status || 'unknown',
        identity, revision, parent, requested, effective,
      ].filter(Boolean).join(' · ');
      if (actor.parent_id && !actor.session_id) item.title = `Child of ${actor.parent_id}`;
      list.appendChild(item);
    }
    node.appendChild(list);
  } else {
    node = document.createElement('div');
    node.className = 'actor-accounting';
    node.textContent = label;
  }
  const footer = wrap.querySelector(':scope > .msg-footer');
  wrap.insertBefore(node, footer || null);
  return node;
}

/**
 * Add a message to the chat history.
 */
export function addMessage(role, content, modelName, metadata) {
  try {
    hideWelcomeScreen();
    const box = document.getElementById('chat-history');
    if (!box) { console.error('Chat history element not found'); return; }

    // Loading a later user message means any earlier ask_user card was
    // answered.  This also removes the live card as soon as a manual reply is
    // appended, even when the user did not click one of its buttons.
    if (role === 'user') removeAskUserCards(box);
    if (role === 'user' && typeof metadata?.buddy_help === 'string' && metadata.buddy_help) {
      const entry = document.createElement('div');
      entry.className = 'chat-buddy-system';
      const label = document.createElement('strong');
      label.textContent = 'System prompt · Buddy';
      const action = document.createElement('span');
      action.textContent = 'Explain ' + metadata.buddy_help;
      entry.append(label, action);
      box.appendChild(entry);
      return entry;
    }

    var esc = uiModule.esc;
    const textRaw = Array.isArray(content) ? markdownModule.renderContent(content) : content;

    // --- Agent multi-bubble reconstruction from saved metadata ---
    if (role === 'assistant' && metadata && metadata.tool_events && metadata.tool_events.length > 0) {
      const toolEvents = metadata.tool_events;
      const transcript = Array.isArray(metadata?._history_round_texts)
        ? {
            roundTexts: metadata._history_round_texts,
            source: metadata._history_transcript_source || 'history-normalized',
          }
        : normalizeAssistantTranscript(
            typeof textRaw === 'string' ? textRaw : String(textRaw || ''),
            metadata,
          );
      const roundTexts = transcript.roundTexts;
      let pendingAskUser = null;
      let lastWrap = null;
      let firstMsgAi = null;
      let lastMsgAi = null;
      let thinkingRendered = false;

      const toolsByRound = {};
      for (const ev of toolEvents) {
        const r = ev.round || 1;
        if (!toolsByRound[r]) toolsByRound[r] = [];
        toolsByRound[r].push(ev);
      }

      const maxRound = Math.max(...Object.keys(toolsByRound).map(Number), roundTexts.length);

      for (let r = 0; r < maxRound; r++) {
        const roundNum = r + 1;
        const txt = resolveDocumentPlaceholderLinks((roundTexts[r] || '').trim(), metadata);

        if (txt) {
          const wrap = document.createElement('div');
          const isFirstText = !firstMsgAi;
          wrap.className = 'msg msg-ai' + (isFirstText ? '' : ' msg-continuation');
          wrap.dataset.transcriptSource = transcript.source;
          const roleEl = document.createElement('div');
          roleEl.className = 'role';
          const pair = replyModelPair(modelName, metadata);
          const contModel = pair.actualModel || pair.requestedModel;
          roleEl.textContent = modelRouteLabel(pair.requestedModel, contModel);
          if (pair.requestedModel && contModel && !sameModelName(pair.requestedModel, contModel)) {
            roleEl.title = pair.requestedModel + ' -> ' + contModel;
          }
          applyModelColor(roleEl, contModel);
          if (isFirstText) roleEl.appendChild(roleTimestamp(metadata?.timestamp));
          wrap.appendChild(roleEl);
          const body = document.createElement('div');
          body.className = 'body';
          // Check if this is the last text round — sources go on top of final response
          var agentSourcesPrefix = '';
          var isLastTextRound = true;
          for (let rr = r + 1; rr < maxRound; rr++) {
            if ((roundTexts[rr] || '').trim()) { isLastTextRound = false; break; }
          }
          var agentFindingsSuffix = '';
          if (isLastTextRound && metadata?.web_sources?.length) {
            agentSourcesPrefix = buildSourcesBox(metadata.web_sources, 'web');
          } else if (isLastTextRound && metadata?.research_sources?.length) {
            agentSourcesPrefix = buildSourcesBox(metadata.research_sources, 'research');
          }
          if (isLastTextRound && metadata?.research_findings?.length) {
            agentFindingsSuffix = buildFindingsBox(metadata.research_findings);
          }
          // RAG document sources — restored on the final text round.
          if (isLastTextRound && metadata?.rag_sources?.length) {
            agentFindingsSuffix += buildRagSourcesBox(metadata.rag_sources);
          }
          let displayText = txt;
          if (!thinkingRendered && metadata?.thinking && !/<think(?:\s|>)/i.test(txt)) {
            const thinkTime = metadata.thinking_time || null;
            displayText = '<think' + (thinkTime ? ' time="' + thinkTime + '"' : '') + '>' +
              metadata.thinking + '</think>\n\n' + txt;
            thinkingRendered = true;
          } else if (/<think(?:\s|>)/i.test(txt)) {
            thinkingRendered = true;
          }
          body.innerHTML = agentSourcesPrefix + markdownModule.processWithThinking(markdownModule.squashOutsideCode(displayText)) + agentFindingsSuffix;
          if (isLastTextRound) addBuddyHandbookLink(body, metadata?.buddy_help_link);
          wrap.appendChild(body);
          wrap.dataset.raw = txt;
          if (metadata?._db_id) wrap.dataset.dbId = metadata._db_id;
          box.appendChild(wrap);
          lastWrap = wrap;
          if (!firstMsgAi) firstMsgAi = wrap;
          lastMsgAi = wrap;
        }

        const roundTools = toolsByRound[roundNum] || [];
        if (roundTools.length > 0) {
          // Reuse previous thread if no text separated us (merge consecutive tool rounds)
          let threadWrap = null;
          if (!txt && lastWrap && lastWrap.classList.contains('agent-thread')) {
            threadWrap = lastWrap;
          } else {
            threadWrap = document.createElement('div');
            threadWrap.className = 'agent-thread';
            // Extend line up if there's a chat bubble above
            if (txt) threadWrap.classList.add('has-top');
            box.appendChild(threadWrap);
          }
          for (const ev of roundTools) {
            if (ev.ask_user) pendingAskUser = ev.ask_user;
            const ok = (ev.exit_code === 0 || ev.exit_code == null);
            let outHtml = '';
            if (ev.output && ev.output.trim()) {
              outHtml = `<details class="agent-tool-output"><summary>Output</summary><pre>${esc(ev.output)}</pre></details>`;
            }
            const screenshotSrc = safeToolScreenshotSrc(ev.screenshot);
            if (screenshotSrc) {
              outHtml += `<details class="agent-tool-output"><summary>Screenshot</summary><img src="${esc(screenshotSrc)}" style="max-width:100%;border-radius:6px;margin-top:6px;border:1px solid var(--border)" /></details>`;
            }
            outHtml += renderSkillUseReceipt(ev, esc);
            // File-write/edit diff (persisted in the tool event) \u2014 re-render it
            // so it survives reload, matching the live stream.
            let evDiffHtml = '';
            if (ev.diff && ev.diff.text) {
              const d = ev.diff;
              const stat = [
                d.new_file ? '<span class="diff-stat-new">new</span>' : '',
                d.added ? `<span class="diff-stat-add">+${d.added}</span>` : '',
                d.removed ? `<span class="diff-stat-del">\u2212${d.removed}</span>` : '',
              ].filter(Boolean).join(' ');
              const rows = d.text.split('\n').map(line => {
                let cls = 'diff-ctx', text = line;
                if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-meta';
                else if (line.startsWith('@@')) cls = 'diff-hunk';
                // Drop the leading diff marker (+/-/space) — colour encodes add/del.
                else if (line.startsWith('+')) { cls = 'diff-add'; text = line.slice(1); }
                else if (line.startsWith('-')) { cls = 'diff-del'; text = line.slice(1); }
                else if (line.startsWith(' ')) { text = line.slice(1); }
                return `<span class="${cls}">${esc(text) || '&nbsp;'}</span>`;
              }).join('');  // spans are display:block \u2014 a literal \n would double-space
              evDiffHtml = `<details class="agent-tool-output agent-tool-diff"><summary><span class="diff-file">${esc(d.file || 'diff')}</span> <span class="diff-summary-stats">${stat}</span></summary><pre class="diff-pre">${rows}</pre></details>`;
            }
            const node = document.createElement('div');
            node.className = 'agent-thread-node' + (ok ? '' : ' error');
            // Hide the raw JSON command when a diff says it better (same as live).
            const evCmdHtml = (ev.command && !(ev.diff && ev.diff.text)) ? `<pre class="agent-thread-cmd">${esc(ev.command)}</pre>` : '';
            node.innerHTML = `<div class="agent-thread-dot"></div><div class="agent-thread-header"><span class="agent-thread-icon">${ok ? '\u2713' : '\u2717'}</span><span class="agent-thread-tool">${esc(ev.tool)}</span><span class="agent-thread-status">${ok ? 'done' : 'failed'}</span><span class="agent-thread-chevron">\u25B6</span></div><div class="agent-thread-content">${evCmdHtml}${outHtml}${evDiffHtml}</div>`;
            const resultButton=mediaResultButton(ev.structured);
            if(resultButton)node.querySelector('.agent-thread-content')?.appendChild(resultButton);
            const capturedResource=capturedResourceButton(ev.structured);
            if(capturedResource)node.querySelector('.agent-thread-content')?.appendChild(capturedResource);
            const htmlPreview=createCapturedHtmlPreview(ev.structured);
            if(htmlPreview)node.querySelector('.agent-thread-content')?.appendChild(htmlPreview);
            const savedFile=resourceResultButton(ev.resourceResult);
            if(savedFile)node.querySelector('.agent-thread-content')?.appendChild(savedFile);
            const capturedButton=capturedChangeButton(ev.structured?.rawOutput?.metadata?.history?.managed_action_ref);
            if(capturedButton)node.querySelector('.agent-thread-content')?.appendChild(capturedButton);
            const turnButton=capturedTurnButton(ev.structured?.rawOutput?.metadata?.history?.managed_action_ref);
            if(turnButton)node.querySelector('.agent-thread-content')?.appendChild(turnButton);
            const skillSourceButton=currentSkillSourceButton(ev);
            if(skillSourceButton)node.querySelector('.agent-thread-content')?.appendChild(skillSourceButton);
            const searchHits = searchHitButtons(ev);
            if (searchHits) node.querySelector('.agent-thread-content')?.appendChild(searchHits);
            // Click handling is delegated globally \u2014 see chat.js init.
            threadWrap.appendChild(node);
          }
          // Check if next round has text — extend line down to connect
          const nextTxt = (roundTexts[r + 1] || '').trim();
          if (nextTxt) threadWrap.classList.add('has-bottom');
          lastWrap = threadWrap;

          for (const ev of roundTools) {
            if (ev.image_url) {
              box.appendChild(buildImageBubble(ev.image_url, ev.image_prompt, ev.image_model, ev.image_size, ev.image_quality, ev.image_id));
            }
          }
        }
      }

      const firstWrap = lastMsgAi || lastWrap;
      if (firstWrap && firstWrap.classList.contains('msg-ai')) {
        if (metadata?.memories_used?.length) firstWrap._memoriesUsed = metadata.memories_used;
        firstWrap.appendChild(createMsgFooter(firstWrap));
        if (metadata?.actor_accounting) renderActorAccounting(firstWrap, metadata.actor_accounting);
        if (metadata) displayMetrics(firstWrap, metadata);
      }

      if (window.odysseusHighlight) window.odysseusHighlight.highlightAll(box);
      if (markdownModule.renderMermaid) markdownModule.renderMermaid(box);
      if (pendingAskUser) {
        // Session history is rendered oldest-to-newest.  A later user message
        // removes this card; if there is none, the pending choice survives a
        // refresh.  Avoid stealing focus while the history is loading.
        renderAskUserCard(pendingAskUser, { focus: false, scroll: false, interactive: !pendingAskUser.request_id });
      }
      return lastWrap;
    }

    // --- Wake-task / supervisor system check-in ---
    // The self-wake mechanism injects "Did you finish?" as a user message
    // (or persisted history shows a "[Task] Self-check: <id>" envelope)
    // so the agent loop re-enters and re-checks status. Render as a
    // normal user-style bubble — same chrome as a real user message,
    // just with role "Supervisor" and a short summary body — instead of
    // a slim system chip. Matches chat style and integrates cleanly
    // into the conversation flow.
    let _isWakeCheck = !!(metadata?.wake_check_in || metadata?.hidden_from_user_view);
    if (!_isWakeCheck && typeof textRaw === 'string') {
      // Also catch historical messages persisted as "[Task] Self-check: <sid>"
      // (older wake tasks that didn't set wake_check_in metadata).
      if (/^\s*\[Task\]\s+Self-check:/i.test(textRaw)) {
        _isWakeCheck = true;
      }
    }
    if (_isWakeCheck) {
      // Supervisor self-check messages are an internal control signal —
      // skip rendering entirely so they don't show up in the conversation.
      return null;
    }

    // --- Standard single-bubble message ---
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + (role === 'user' ? 'msg-user' : 'msg-ai');

    const r = document.createElement('div');
    r.className = 'role';
    const isSlash = metadata?.source === 'slash';
    const isCompacted = metadata?.compacted;
    const replyModels = replyModelPair(modelName, metadata);
    const resolvedModel = replyModels.actualModel || replyModels.requestedModel;
    var _roleText = role === 'user' ? 'You' : (isSlash || isCompacted) ? (window.__agentName || 'Open Clank') : modelRouteLabel(replyModels.requestedModel, resolvedModel);
    var _agentLabelKind = (role !== 'user' && (isSlash || isCompacted)) ? 'default' : '';
    if (role === 'assistant' && (metadata?.research || metadata?.research_clarification)) {
      _roleText += ' (Research)';
    }
    if (metadata?.group_model && role !== 'user') {
      _roleText = metadata.group_model;
      _agentLabelKind = '';
    } else if (metadata?.character_name && role !== 'user' && !isSlash && !isCompacted) {
      // Identity presentation is LIVE (ruling R17): a chat with its own
      // persona shows that persona; everything else shows the CURRENT
      // default persona name — renaming the default rebrands old messages.
      var _sessName = window.__sessionPersonaName || '';
      if (_sessName) {
        _roleText = _sessName;
        _agentLabelKind = 'session';
      } else {
        _roleText = window.__agentName || metadata.character_name;
        _agentLabelKind = 'default';
      }
    }
    r.textContent = _roleText;
    if (_agentLabelKind) r.dataset.agentName = _agentLabelKind;
    if (role !== 'user') {
      if (!isSlash && !isCompacted && replyModels.requestedModel && resolvedModel && !sameModelName(replyModels.requestedModel, resolvedModel)) {
        r.title = replyModels.requestedModel + ' -> ' + resolvedModel;
      }
      if (!isSlash && !isCompacted) applyModelColor(r, resolvedModel);
      r.appendChild(roleTimestamp(metadata?.timestamp));
    }

    const b = document.createElement('div');
    b.className = 'body';

    let text = markdownModule.squashOutsideCode(stripToolBlocks(textRaw || ''));
    if (role === 'assistant') {
      text = resolveDocumentPlaceholderLinks(text, metadata);
    }

    // For user messages, pull out vision-model image descriptions ([Image: name]\n
    // <multi-line desc>) into a collapsible "image description" section. Done for
    // ALL user messages (not just ones with attachment metadata) so it rebuilds
    // from the stored text even after a browser restart drops the cached attachments.
    const attachments = metadata?.attachments;
    const _visionBlocks = [];
    if (role === 'user') {
      text = text.replace(
        /\n*\[Image: ([^\]]+)\]\n([\s\S]*?)(?=\n*\[Image: |\n*\[Image attached: |\n*=== File: |\n*\[PDF content\]:|$)/g,
        (_m, name, desc) => { const d = desc.trim(); if (d) _visionBlocks.push({ name: name, desc: d }); return ''; }
      );
    }
    // With attachments present, also strip the embedded file/PDF/image-marker text.
    if (role === 'user' && attachments?.length) {
      // Strip === File: ... === blocks, [PDF content]: blocks, and [Image attached: ...] lines
      text = text
        .replace(/\n*=== File: .+? ===\n\[Type: .+?\]\n+```[\s\S]*?```/g, '')
        .replace(/\n*=== File: .+? ===\n\[Type: .+?\]\n+[\s\S]*?(?=\n*=== File:|$)/g, '')
        .replace(/\n*\[PDF content\]:[\s\S]*?(?=\n*\[PDF content\]|\n*=== File:|$)/g, '')
        .replace(/\n*\[Image attached: [^\]]+\]/g, '')
        .replace(/\n*\[Attached (?:document|non-text) file\]/g, '')
        .trim();
    }

	    wrap.dataset.raw = text;
	    if (role === 'user' && metadata?.model_request_options) {
	      wrap.dataset.modelRequestOptions = JSON.stringify(metadata.model_request_options);
	    }
	    if (metadata?._db_id) wrap.dataset.dbId = metadata._db_id;
    // Prepend sources box if saved in metadata
    var sourcesPrefix = '';
    var findingsSuffix = '';
    if (role === 'assistant' && metadata?.research_sources?.length) {
      sourcesPrefix = buildSourcesBox(metadata.research_sources, 'research');
    } else if (role === 'assistant' && metadata?.web_sources?.length) {
      sourcesPrefix = buildSourcesBox(metadata.web_sources, 'web');
    }
    if (role === 'assistant' && metadata?.research_findings?.length) {
      findingsSuffix = buildFindingsBox(metadata.research_findings);
    }
    // RAG document sources — restored from metadata so they survive refresh.
    if (role === 'assistant' && metadata?.rag_sources?.length) {
      findingsSuffix += buildRagSourcesBox(metadata.rag_sources);
    }
    // If thinking is stored in metadata (not in text), reconstruct the full display
    if (role === 'assistant' && metadata?.thinking) {
      const thinkTime = metadata.thinking_time || null;
      const thinkHtml = markdownModule.processWithThinking(
        '<think' + (thinkTime ? ` time="${thinkTime}"` : '') + '>' + metadata.thinking + '</think>\n\n' + text
      );
      b.innerHTML = sourcesPrefix + thinkHtml + findingsSuffix;
	    } else {
	      b.innerHTML = sourcesPrefix + markdownModule.processWithThinking(text) + findingsSuffix;
	    }
	    b.dataset.raw = text;

    // The vision/OCR caption is stripped from the displayed text above (so the
    // bubble doesn't show the raw model output) but no longer rendered as an
    // inline collapsible — the user can still view/edit it via the "Caption"
    // button on the photo thumbnail. _visionBlocks is intentionally left unused
    // so the parsing-and-strip side-effect on `text` still happens.
    void _visionBlocks;

    // Add "Open Visual Report" button for persisted research messages
    if (role === 'assistant' && metadata?.research) {
      var _sid = window.sessionModule?.getCurrentSessionId?.();
      if (_sid) _appendReportButton(b, _sid);
    }

    // Style [Doc edit: ...] prefix in user messages
    if (role === 'user') {
      // Match compact format: [Doc edit: line X] instruction
      b.innerHTML = b.innerHTML.replace(
        /\[Doc edit: (lines? [\d–\-]+)\]\s*/,
        '<span class="doc-edit-tag">Doc edit: $1</span> '
      );
      // Match raw format: "In the document, edit this specific text (line X):\n```\n...\n```\n\nInstruction: ..."
      // After markdown processing this becomes a <p> + <pre><code> block + <p>Instruction: text</p>
      const rawDocMatch = b.innerHTML.match(/In the document, edit this specific text \((lines? [\d–\-]+)\)/);
      if (rawDocMatch) {
        const lineRef = rawDocMatch[1];
        // Extract instruction text (after "Instruction: ")
        const instrMatch = b.textContent.match(/Instruction:\s*([\s\S]*)$/);
        const instrText = instrMatch ? instrMatch[1].trim() : '';
        b.innerHTML = '<span class="doc-edit-tag">Doc edit: ' + lineRef + '</span> ' + markdownModule.processWithThinking(instrText);
      }

      // Render attachment cards
      if (attachments?.length) {
        b.appendChild(buildAttachCards(attachments));
      }
    }

    if (role === 'assistant') addBuddyHandbookLink(b, metadata?.buddy_help_link);
    wrap.appendChild(r);
    wrap.appendChild(b);

    // Add stopped indicator + continue button for messages that were stopped by user
    if (role === 'assistant' && metadata?.stopped) {
      const stoppedIndicator = document.createElement('div');
      stoppedIndicator.className = 'stopped-indicator';
      const stoppedLabel = document.createElement('span');
      // Differentiate between "stopped mid-stream" (had content, can continue)
      // and "cancelled before any content" — the latter has no Continue affordance.
      stoppedLabel.textContent = metadata.cancelled
        ? '[Cancelled by user]'
        : '[Message interrupted]';
      stoppedIndicator.appendChild(stoppedLabel);
      // Continue button only makes sense when there's partial content to
      // resume from \u2014 skip it for fully-cancelled (empty) turns.
      if (!metadata.cancelled) {
        const continueBtn = document.createElement('button');
        continueBtn.className = 'continue-btn';
        continueBtn.title = 'Continue';
        continueBtn.textContent = '\u25B8';
        continueBtn.addEventListener('click', () => {
          stoppedIndicator.remove();
          if (window.chatModule) {
            window.chatModule.setHideUserBubble();
            window.chatModule.setPendingContinue(wrap);
            const rawText = wrap.dataset.raw || wrap.querySelector('.body')?.textContent || '';
            const cutoff = rawText;
            const msgInput = document.getElementById('message');
            if (msgInput) {
              msgInput.value = 'Your previous response was interrupted. It ended with:\n\n' + cutoff.slice(-500) + '\n\nDo NOT repeat what you already said. Continue exactly from where you were cut off.';
              const sb = document.querySelector('.send-btn');
              if (sb) sb.click();
            }
          }
        });
        stoppedIndicator.appendChild(continueBtn);
      }
      b.appendChild(stoppedIndicator);
    }

    if (metadata?.edited) {
      const editedIndicator = document.createElement('div');
      editedIndicator.className = 'edited-indicator';
      editedIndicator.textContent = '[Message edited]';
      b.appendChild(editedIndicator);
    }

    // Restore variant navigation from saved metadata
    if (role === 'assistant' && metadata?.variants && metadata.variants.length > 1) {
      wrap.dataset.variants = JSON.stringify(metadata.variants);
      const idx = metadata.variantIndex ?? metadata.variants.length - 1;
      wrap.dataset.variantIndex = String(idx);

      // Re-render from `raw` markdown rather than trusting cached `v.html`.
      // Variants ride through localStorage / chat export-import; cached HTML
      // would let an attacker-controlled session JSON inject markup.
      const _renderVariant = (v) => (v && v.raw)
        ? markdownModule.processWithThinking(markdownModule.squashOutsideCode(v.raw))
        : (v && v.html) || '';

      // Show the selected variant's content
      const v = metadata.variants[idx];
      if (v) {
        b.innerHTML = _renderVariant(v);
        wrap.dataset.raw = v.raw;
      }

      // Render nav
      const nav = document.createElement('span');
      nav.className = 'variant-nav';
      nav.addEventListener('click', (e) => e.stopPropagation());

      const divider = document.createElement('span');
      divider.className = 'variant-divider';
      divider.textContent = '|';
      nav.appendChild(divider);

      const tagLabel = document.createElement('span');
      const _icons = { regen: '\u21BB', shorter: '\u2702', simpler: '?', original: '\u25CB' };
      const _tl0 = metadata.variants[idx]?.label;
      tagLabel.className = 'variant-tag' + (_tl0 === 'shorter' ? ' variant-tag-scissors' : '');
      tagLabel.textContent = _icons[_tl0] || '';
      nav.appendChild(tagLabel);

      const prevBtn = document.createElement('button');
      prevBtn.className = 'variant-btn';
      prevBtn.textContent = '<';
      prevBtn.disabled = idx === 0;
      nav.appendChild(prevBtn);

      const numLeft = document.createElement('button');
      numLeft.className = 'variant-num';
      numLeft.textContent = String(idx + 1);
      numLeft.disabled = idx === 0;
      nav.appendChild(numLeft);

      const slash = document.createElement('span');
      slash.className = 'variant-slash';
      slash.textContent = '/';
      nav.appendChild(slash);

      const numRight = document.createElement('button');
      numRight.className = 'variant-num';
      numRight.textContent = String(metadata.variants.length);
      numRight.disabled = idx === metadata.variants.length - 1;
      nav.appendChild(numRight);

      const nextBtn = document.createElement('button');
      nextBtn.className = 'variant-btn';
      nextBtn.textContent = '>';
      nextBtn.disabled = idx === metadata.variants.length - 1;
      nav.appendChild(nextBtn);

      const switchFn = (newIdx) => {
        const vars = metadata.variants;
        if (newIdx < 0 || newIdx >= vars.length) return;
        const sv = vars[newIdx];
        b.innerHTML = _renderVariant(sv);
        wrap.dataset.raw = sv.raw;
        wrap.dataset.variantIndex = String(newIdx);
        if (window.odysseusHighlight) window.odysseusHighlight.highlightAll(wrap);
        tagLabel.textContent = _icons[sv.label] || '';
        tagLabel.className = 'variant-tag' + (sv.label === 'shorter' ? ' variant-tag-scissors' : '');
        numLeft.textContent = String(newIdx + 1);
        numLeft.disabled = newIdx === 0;
        numRight.disabled = newIdx === vars.length - 1;
        prevBtn.disabled = newIdx === 0;
        nextBtn.disabled = newIdx === vars.length - 1;
      };
      prevBtn.addEventListener('click', (e) => { e.stopPropagation(); switchFn(parseInt(wrap.dataset.variantIndex) - 1); });
      numLeft.addEventListener('click', (e) => { e.stopPropagation(); switchFn(parseInt(wrap.dataset.variantIndex) - 1); });
      numRight.addEventListener('click', (e) => { e.stopPropagation(); switchFn(parseInt(wrap.dataset.variantIndex) + 1); });
      nextBtn.addEventListener('click', (e) => { e.stopPropagation(); switchFn(parseInt(wrap.dataset.variantIndex) + 1); });

      r.appendChild(nav);
    }

    if (role === 'assistant') {
      // The "N pinned" / "N recalled" pill in the footer reads from
      // wrap._memoriesUsed — propagate it from saved metadata so the pill
      // survives a page refresh (live-stream path sets it via SSE, but
      // history reloads need this assignment).
      if (metadata?.memories_used?.length) wrap._memoriesUsed = metadata.memories_used;
      wrap.appendChild(createMsgFooter(wrap));
      if (metadata?.actor_accounting) renderActorAccounting(wrap, metadata.actor_accounting);
      if (metadata) displayMetrics(wrap, metadata);
    } else {
      // Add timestamp to user header (like AI messages)
      r.appendChild(roleTimestamp(metadata?.timestamp));

      wrap.appendChild(createUserMsgFooter(wrap));
    }

    box.appendChild(wrap);

    // TTS is now part of the msg-actions system
    if (role === 'assistant' && markdownModule.renderMermaid) {
      markdownModule.renderMermaid(wrap);
    }
    return wrap;
  } catch (error) {
    console.error('Error in addMessage:', error);
    if (uiModule) uiModule.showError('Failed to add message: ' + error.message);
  }
}

export function renderInputReceipt(receipt, actions = {}) {
  const box = document.getElementById('chat-history');
  if (!box || !receipt || typeof receipt.requestID !== 'string') return null;
  const id = `chat-input-receipt-${encodeURIComponent(receipt.requestID)}`;
  let card = document.getElementById(id);
  if (!card) {
    card = document.createElement('section');
    card.className = 'chat-input-receipt';
    card.id = id;
    card.setAttribute('aria-live', 'polite');
  } else {
    card.replaceChildren();
  }
  card.dataset.state = String(receipt.state || 'persisted');
  card.dataset.deliveryState = String(receipt.deliveryState || '');
  const heading = document.createElement('div');
  heading.className = 'chat-input-receipt-heading';
  const title = document.createElement('strong');
  title.textContent = 'Follow-up input';
  const status = document.createElement('span');
  status.className = 'chat-input-receipt-status';
  const statusKey = String(receipt.deliveryState || receipt.state || 'unknown');
  status.textContent = ({
    consumed: 'Saved to queue; delivery unverified', queued: 'Saved to queue; delivery unverified',
    'queue-delivered': 'Delivered to the queue (model use unverified)', persisted: 'Saved for a safe boundary',
    'pending-boundary': 'Waiting for a safe boundary', unavailable: 'Delivery needs attention',
    unknown: 'Outcome unknown', cancelled: 'Cancelled', failed: 'Unavailable',
  })[statusKey] || statusKey;
  heading.append(title, status);
  const body = document.createElement('div');
  body.className = 'chat-input-receipt-text';
  body.textContent = String(receipt.text || '').slice(0, 200_000);
  const details = document.createElement('div');
  details.className = 'chat-input-receipt-details';
  const uploadCount = Array.isArray(receipt.uploadIDs) ? receipt.uploadIDs.length
    : Array.isArray(receipt.attachmentUploadIDs) ? receipt.attachmentUploadIDs.length
      : Number(receipt.attachmentCount || 0);
  const detailItems = [];
  if (uploadCount) detailItems.push(`${uploadCount} attachment${uploadCount === 1 ? '' : 's'}`);
  if (['unavailable', 'unknown'].includes(statusKey) && typeof receipt.deliveryReason === 'string' && receipt.deliveryReason) detailItems.push(receipt.deliveryReason.slice(0, 240));
  details.textContent = detailItems.join(' · ');
  const controls = document.createElement('div');
  controls.className = 'chat-input-receipt-actions';
  const addAction = (label, handler, action) => {
    if (typeof handler !== 'function') return;
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.dataset.action = action;
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await handler(receipt); }
      catch (error) { uiModule?.showError?.(error?.message || 'The follow-up action failed.'); }
      finally { button.disabled = false; }
    });
    controls.appendChild(button);
  };
  const terminal = receipt.state === 'consumed' || receipt.state === 'cancelled';
  if (!terminal) {
    addAction('Retry', actions.onRetry, 'retry');
    if (receipt.deliveryState === 'pending-boundary' || receipt.deliveryState === 'unavailable' || receipt.state === 'failed') {
      addAction('Send now', actions.onSendNow, 'send-now');
    }
    addAction('Cancel', actions.onCancel, 'cancel');
  }
  card.append(heading, body, details, controls);
  if (!card.isConnected) box.appendChild(card);
  return card;
}

const chatRenderer = {
  shortModel,
  sameModelName,
  modelRouteLabel,
  replyModelPair,
  modelColor,
  applyModelColor,
  getModelCost,
  isCostTrackedEndpoint,
  isSubscriptionEndpoint,
  getImageCost,
  getSessionCost,
  resetSessionCost,
  updateSessionCostUI,
  roleTimestamp,
  stripToolBlocks,
  copyMessageText,
  safeToolScreenshotSrc,
  safeDisplayImageSrc,
  removeAskUserCards,
  renderAskUserCard,
  renderPermissionCard,
  renderActorAccounting,
  buildSourcesBox,
  buildFindingsBox,
  appendReportButton,
  buildImageBubble,
  renderInputReceipt,
  hideWelcomeScreen,
  showWelcomeScreen,
  createMsgFooter,
  displayMetrics,
  addMessage,
  buildAttachCards,
  updateMessageAttachments,
};

export default chatRenderer;

export { createCapturedHtmlPreview };

export { capturedResourceButton };
