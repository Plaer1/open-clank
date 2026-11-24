import { hostRasterMime } from './editor/hostRasterOriginal.js';
import { registerAdapter } from './custom-context-menu.js';
import { recordPresentation, achievementOwner } from './achievementProducer.js';
import { openArtProjectDialog } from './editor/artProjectDialog.js';
import { cloneArtProject, artProjectEqual } from './editor/artProjectMetadata.js';
import { selectArtProjectReference, checkArtProjectReference } from './editor/artProjectReferences.js';
import { uiIcon } from './uiIcons.js';
import uiModule from './ui.js';
import dragSortModule from './dragSort.js';
import spinnerModule from './spinner.js';
import { createColorPicker } from './colorPicker.js';
import { namespaceEditorIds, editorIdSelector } from './editor/dom-identity.js';
import legacyModalManager from './modalManager.js';
import { createOpenClankWindow } from './copal/windows.js';
import { canvasCoords as _canvasCoords } from './editor/canvas-coords.js';
import { drawCheckerboard as _drawCheckerboard } from './editor/checkerboard.js';
import { dilateMask as _dilateMask, applyInpaintFeather as _applyInpaintFeather } from './editor/mask-utils.js';
import {
  lassoOffsetPoints as _lassoOffsetPointsImpl,
  getLassoPath as _getLassoPathImpl,
  buildLassoMask as _buildLassoMaskImpl,
} from './editor/tools/lasso-mask.js';
import { floodFillMask as _floodFillMask } from './editor/tools/flood-fill.js';
import { drawHistogram as _drawHistogram } from './editor/fx/histogram.js';
import {
  applyAdjustment as _applyAdjToCanvas,
  renderLayerPixelAdjustments as _renderLayerPixelAdjustmentsImpl,
  renderLayerWithAdjLayers as _renderLayerWithAdjLayers,
} from './editor/fx/pixel-pass.js';
import {
  layerFilterString as _layerFilterString,
  fxFilterToSlider as _fxFilterToSlider,
} from './editor/fx/filter-string.js';
import {
  layerHasAdjustments as _layerHasAdjustments,
  layerNeedsPixelPass as _layerNeedsPixelPass,
  adjustmentsKey as _adjustmentsKey,
  defaultAdjParams as _defaultAdjParams,
  adjLayerLabel as _adjLayerLabel,
  ADJ_ICONS as _ADJ_ICONS,
  HISTORY_ICON as _HISTORY_ICON,
  isMaskCanvasEmpty as _isMaskCanvasEmpty,
  isLayerEmpty as _isLayerEmpty,
  relTime as _relTime,
} from './editor/layer-helpers.js';
import { computeSnap as _computeSnapImpl, cursorForHandle as _cursorForHandle } from './editor/snap.js';
import {
  layerUnionAlpha as _layerUnionAlphaImpl,
  seamMask as _seamMaskImpl,
  layerBodyMask as _layerBodyMaskImpl,
} from './editor/harmonize-masks.js';
import {
  gaussianBlur as _gaussianBlur,
  zoomBlur as _zoomBlur,
  motionBlur as _motionBlur,
} from './editor/filters/blur.js';
import { edgeFeather as _edgeFeather } from './editor/filters/edge-feather.js';
import {
  buildThumbnail as _buildThumbnailImpl,
  buildMergedMaskCanvas as _buildMergedMaskCanvasImpl,
} from './editor/composite-helpers.js';
import { buildToolbar as _buildToolbar } from './editor/build/toolbar.js?v=20260708sam3';
import { buildTopbar as _buildTopbar } from './editor/build/topbar.js';
import {
  controlsHTML as _controlsHTML,
  layerPanelHTML as _layerPanelHTML,
} from './editor/build/controls.js?v=20260708match1';
import {
  transformPopupHTML as _transformPopupHTML,
  attachSpinRepeat as _attachSpinRepeat,
} from './editor/build/transform-popup.js';
import {
  shortcutsPopupHTML as _shortcutsPopupHTML,
  historyPanelHTML as _historyPanelHTMLImpl,
  canvasSizePromptHTML as _canvasSizePromptHTML,
} from './editor/build/popups.js';
import { state as legacyState, createImageStateParts, createEditorLifetime } from './editor/state.js';
import { createImageScene } from './editor/imageScene.js';
import { createImageSessionPersistence, editableImageMetadata, serializeEditableImageScene, decodeInpaintSidecar, flattenImageScene } from './editor/imageSessionPersistence.js';
import { createMoveTool } from './editor/tools/move.js';
import { createCropTool } from './editor/tools/crop.js';
import { createLassoTool } from './editor/tools/lasso.js';
import { createWandTool } from './editor/tools/wand.js';
import { createCloneTool } from './editor/tools/clone.js';
import { createTransformDragTool } from './editor/tools/transform-drag.js';
import { createStrokeTool } from './editor/tools/stroke.js';
import { createLayerPanelRenderer } from './editor/layer-panel.js';
import { createTransformHandles } from './editor/tools/transform-handles.js';
import { createCanvasTransforms } from './editor/canvas-transforms.js';
import { createApplyImageTool } from './editor/ai-tool-runner.js';
import { createStrokePipeline } from './editor/stroke-pipeline.js';
import { createAdjPopupSystem } from './editor/fx/adj-popup.js';
import { createHistoryPanel } from './editor/history-panel.js';
import { createTransformSession } from './editor/tools/transform-session.js';
import { wireCanvasEvents } from './editor/canvas-events.js';
import { buildRightPanel } from './editor/build/right-panel.js';
import { wireSliderUx } from './editor/slider-ux.js';
import { createShortcutsPopover } from './editor/shortcuts-popover.js';
import { wireKeyboardShortcuts } from './editor/keyboard-shortcuts.js';
import { wireClipboardAndDrop, CANVAS_MAX_IMPORT_BYTES, CANVAS_MAX_IMPORT_DIMENSION, CANVAS_MAX_IMPORT_PIXELS } from './editor/clipboard-and-drop.js';
import { filesFacadeClient } from './filesFacadeClient.js';
import { wireAIModelSelectors } from './editor/ai-models.js';
import { wireInpaintButtons } from './editor/ai-inpaint.js?v=20260708match1';
import { wireAIToolsMisc } from './editor/ai-tools-misc.js';
import { wireRembgAndSharpen } from './editor/ai-rembg.js';
import { wireStrokeToolSliders } from './editor/stroke-tool-sliders.js';
import { wireImport } from './editor/wire-import.js';
import { wireMergeButtons } from './editor/wire-merge-buttons.js';
import { wireSelectionControls } from './editor/wire-selection-controls.js';
import { wireInpaintControls } from './editor/wire-inpaint-controls.js?v=20260708match1';
import { wireTopbar, closeOtherTopbarMenus as _closeOtherTopbarMenus } from './editor/wire-topbar.js';
import { wireTopbarOverflow } from './editor/wire-topbar-overflow.js';
import { wireTopbarMenus } from './editor/wire-topbar-menus.js';

const activeImageStates = new WeakSet();
const readOnlyImageScenes = new WeakSet();
let imageRuntimeIdentity = 0;
export function createImageRuntime({ session = null, root = null, lifetime = null, authority = null, persistence = null, modalManager = null, viewId = null, onContextFeedback = null, hostOriginal = null, requestHostSave = null, state: injectedState = null, legacy = false } = {}) {
  if (!legacy && (!session?.captureTarget || typeof session?.attachRuntime !== 'function' || typeof session?.attachSceneAuthority !== 'function' || !root?.ownerDocument || typeof lifetime?.add !== 'function' || typeof authority?.validate !== 'function' || typeof authority?.validateScope !== 'function' || !persistence || !modalManager || lifetime.disposed)) throw new TypeError('image runtime ownership dependencies required');
  if (!legacy && (authority.validate(session.captureTarget()) !== true || session.captureTarget()?.scope?.accountId !== document.body.dataset.accountId)) throw new Error('image authority unavailable');
  const retainedScene = !legacy ? session.getSceneAuthority?.() : null;
  const retainedPersistence = !legacy ? session.getPersistenceController?.() : null;
  if (!legacy && Boolean(retainedScene) !== Boolean(retainedPersistence)) throw new Error('retained image ownership is incomplete');
  if (retainedPersistence) persistence=retainedPersistence.adapter;
  if (retainedScene && injectedState) throw new Error('retained image requires its own scene state');
  const stateParts = !legacy && !injectedState ? createImageStateParts({scene:retainedScene?.state || null}) : null;
  const state = legacy ? legacyState : (injectedState || stateParts.state);
  const runtimePrefix = `ge-runtime-${++imageRuntimeIdentity}`;
  let openIdentity = 0;
  const scopeIds = node => legacy ? node : namespaceEditorIds(node, `${runtimePrefix}-${openIdentity}`);
  const isolatedSourceToken = Symbol('isolated-source');
  let runtimeAttachment = null, sessionPersistence = null, sceneReady = false, initialOpenPending = false;
  let isolatedVisible = true, isolatedFocused = true;
  if (!legacy && (state === legacyState || activeImageStates.has(state))) throw new TypeError('isolated runtime requires independent state');
  modalManager ||= legacyModalManager;
  if (!legacy) { activeImageStates.add(state); lifetime.add(() => activeImageStates.delete(state)); }
  if (!legacy && retainedPersistence) { sessionPersistence=retainedPersistence; sessionPersistence.setHostSaveHandler(requestHostSave); }
  else if (!legacy) { sessionPersistence = createImageSessionPersistence({session,scene:stateParts?.scene || state,adapter:persistence,authority,hostOriginal,requestHostSave}); const owner = session.attachPersistenceController(sessionPersistence); sessionPersistence.setReceiptOwner(owner); lifetime.add(() => { if (!state.layers.length && sessionPersistence.canDispose()) owner.detach(); }); }
/**
 * Imps — canvas-based image editor with layers, brush, eraser, text, crop, and inpaint mask.
 */

let _editorWindow = null;
let _openGeneration = 0;
let _impsAccountEpoch = 0;
let _saveSequence = 0;
const _draftFlights = new Map();
const _managedSaveFlights = new Map();
const _managedSaveReceipts = new Map();
const _captureOrigin = () => Object.freeze({ generation: _openGeneration, accountEpoch: _impsAccountEpoch, owner: document.body.dataset.accountId || null, target: legacy ? null : JSON.stringify([session.captureTarget().sessionId, session.captureTarget().scope, session.captureTarget().sourceKey]) });
const _accountCurrent = origin => {
  if (!origin?.owner || origin.accountEpoch !== _impsAccountEpoch || origin.owner !== (document.body.dataset.accountId || null)) return false;
  if (legacy) return true;
  try { const target = session.captureTarget(); return !lifetime.disposed && target?.scope?.accountId === origin.owner && JSON.stringify([target.sessionId, target.scope, target.sourceKey]) === origin.target && authority.validateScope(target) === true; } catch (_) { return false; }
};
const _originCurrent = origin => _accountCurrent(origin) && origin.generation === _openGeneration;
const _onAuthContextChanged = () => { _impsAccountEpoch += 1; if (legacy) { _managedSaveReceipts.clear(); _draftFlights.clear(); _managedSaveFlights.clear(); } else if (state.editorOpen) _closeEditorContent(); };
document.addEventListener('openclank:auth-context-changed', _onAuthContextChanged);
if (!legacy) lifetime.add(() => document.removeEventListener('openclank:auth-context-changed', _onAuthContextChanged));
const API_BASE = window.location.origin;
let _coreLifetime = null, _flushSceneProjection = () => {}, _clipboardWiring = null;
let _editorPortal = null;
const _ownedPortals = new Set();
function _registerEditorPortal(node) { if (node) _ownedPortals.add(node); return node; }
function _editorFind(id) {
  const selector = editorIdSelector(id);
  return state.container?.querySelector(selector) || [..._ownedPortals].map(node => node.matches?.(selector) ? node : node.querySelector?.(selector)).find(Boolean) || null;
}
function _editorQuery(selector) { return state.container?.querySelector(selector) || [..._ownedPortals].map(node => node.querySelector?.(selector)).find(Boolean) || null; }
function _editorQueryAll(selector) { return [...(state.container?.querySelectorAll(selector) || []), ...[..._ownedPortals].flatMap(node => [...(node.querySelectorAll?.(selector) || [])])]; }
const _coreFocused = () => {
  if (!legacy) { try { if (!isolatedVisible || !isolatedFocused || authority.isActive?.() === false || lifetime.disposed || authority.validateScope(session.captureTarget()) !== true) return false; } catch (_) { return false; } }
  const root = state.container;
  const active = root?.ownerDocument?.activeElement;
  return Boolean(root && ((legacy && active === root.ownerDocument.body) || root.contains(active) || state.historyPanelEl?.contains(active)));
};

async function _confirmedImpsOwner() {
  try {
    const response = await fetch('/api/auth/status', { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) return null;
    const data = await response.json();
    const owner = String(data?.username || '').trim().toLowerCase();
    return owner || null;
  } catch (_) {
    return null;
  }
}

function _clearPendingSaveCopy() {
  state.pendingSaveCopy = null;
  try { localStorage.removeItem('imps.pendingSaveCopy'); } catch (_) {}
  const retryButton = _editorFind('ge-retry-save-copy');
  if (retryButton) retryButton.hidden = true;
}

async function _hydratePendingSaveCopy() {
  const button = _editorFind('ge-retry-save-copy');
  if (!button) return;
  let pending;
  try { pending = JSON.parse(localStorage.getItem('imps.pendingSaveCopy') || 'null'); } catch (_) { _clearPendingSaveCopy(); return; }
  if (!pending || typeof pending !== 'object'
    || !(typeof pending.source_project_id === 'string' || (pending.unbound === true && pending.source_project_id == null))
    || typeof pending.operation_id !== 'string' || typeof pending.resource_ref !== 'string'
    || JSON.stringify(pending).length > 512 * 1024) { _clearPendingSaveCopy(); return; }
  const owner = await _confirmedImpsOwner();
  if (!owner || String(pending.owner || '').toLowerCase() !== owner) { _clearPendingSaveCopy(); return; }
  state.pendingSaveCopy = pending;
  button.hidden = false;
}

if (legacy && !window.__impsPendingOwnerGuard) {
  window.__impsPendingOwnerGuard = true;
  document.addEventListener('openclank:auth-user-ready', (event) => {
    const announced = String(event?.detail?.username || '').trim().toLowerCase();
    const pending = state.pendingSaveCopy;
    if (pending?.owner && announced && pending.owner !== announced) _clearPendingSaveCopy();
  });
  document.addEventListener('openclank:auth-user-cleared', _clearPendingSaveCopy);
}
// ── State ──
// Transform-overlay canvas — sits over the main canvas with extra margin
// so resize / rotation handles render OUTSIDE the image edges. Pointer
// events disabled; the main canvas still handles all input.
const _TRANSFORM_OVERLAY_MARGIN = 60; // image-space px of slack on each side
// Thin wrappers around the transform-handles impls — the refactor
// imported them under *Impl aliases but several call sites still use
// the bare names. Without these, _startTransform threw a ReferenceError
// before opening the popup, so Transform showed no handles / no popup.
let _transformHandles = null;
function _drawTransformHandles() { _transformHandles?.drawHandles(_TRANSFORM_OVERLAY_MARGIN); }
function _getTransformHandle(x, y) { return _transformHandles?.getHandleAt(x, y); }
function _syncTransformOverlay() { _transformHandles?.syncOverlay(_TRANSFORM_OVERLAY_MARGIN); }
// Inpaint uses a much bigger default brush — when the user enters
// the inpaint tool for the first time in this editor session we bump
// the slider to this value (without touching other tools).
const _INPAINT_DEFAULT_BRUSH = 100;
let _samAbortController = null;
let _syncInpaintControls = () => {};

function _cancelSamQuery(showToast = true) {
  if (!_samAbortController) return false;
  try { _samAbortController.abort(); } catch {}
  if (showToast && uiModule) uiModule.showToast('SAM query cancelled');
  return true;
}

function _impsEditMounted() {
  return !!state.container?.matches('#imps-editor-container.imps-editor');
}

if (legacy && !window.__impsEditEscHardGuardInstalled) {
  window.__impsEditEscHardGuardInstalled = true;
  window.addEventListener('keydown', (e) => {
    if (!_coreLifetime || _coreLifetime.disposed || !state.editorOpen || !_coreFocused()) return;
    const isSamCancel = !!_samAbortController
      && (e.key === 'Escape' || ((e.ctrlKey || e.metaKey) && String(e.key || '').toLowerCase() === 'c'));
    if (isSamCancel) {
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      _cancelSamQuery();
      return;
    }
    if (e.key !== 'Escape') return;
    if (_editorWindow?.visible && (window.__impsEditLive || _impsEditMounted())) {
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
    }
  }, true);
}

// Document-level click-away handlers for topbar dropdowns. Each
// openEditor invocation adds 6 of these (save / edge / image / filter /
// resize / more), and without removal they accumulated across reopens.
// Tracked here and removed wholesale in closeEditor.
function _registerDocClickAway(handler) {
  document.addEventListener('click', handler);
  state.editorDocClickHandlers.push(handler);
}

// Drawing state

// Move tool state
// Crop state
// Persistent mode toggle for wand clicks. 'replace' = a new click
// replaces the selection (default); 'add' = always union; 'subtract' =
// always remove from the existing selection. Shift / Alt held during a
// click still override this transiently.
// Last seed click so the tolerance slider can re-run the wand live.
// Stored in canvas coords (same units `_runMagicWand` accepts).
// Lasso state

// Transform state
// Snapshot of the layer's pixels at the moment the transform started.
// Lets the popup live-preview by re-applying from the original on every
// input change instead of stacking destructive edits.
// Current popup-driven values (re-applied on every change).

// Inpaint mask (separate canvas, same dimensions as image)
// Cached canvas reused each composite() to merge every visible mask
// sub-layer into a single tinted overlay (avoids re-allocating on each
// frame). Recreated lazily if dimensions change.
// Softer default than the original full-saturation red — the user
// found the previous tint distracting. Tweakable via the color picker
// under the Paint/Erase row.
// Persistent paint/erase toggle for the Inpaint brush. False = paint
// (default), true = erase. Ctrl+Alt held during a stroke flips this
// transiently for the duration of that one stroke.
// Resolved per-stroke at pointerdown: state.inpaintEraseMode XOR (Ctrl+Alt).
// Most-recent inpaint result layer id — the post-generation Feather
// slider edits this layer's alpha edge live.

// _dilateMask + _applyInpaintFeather live in editor/mask-utils.js
// — see import at top of file.

// Eraser settings
// Edge softness, 0..100. 0 = hard pixel edge; higher values blur the
// stroke's alpha so the eraser fades out at the brush perimeter.
// Brush settings (same shape as eraser).
// Clone Stamp brush modifiers — independent from the Brush tool's
// settings so users can dial in cloning without losing their brush
// preset (and vice-versa).
// `state.cloneSourceX/Y` is the sample anchor set by Alt-click. While
// painting, the source point moves in lockstep with the brush so the
// sampled offset stays constant (Photoshop "aligned" mode).
// First brush coord of the current stroke — used to compute the
// running offset (`sample = source + (current - strokeStart)`).
// Snapshot of the source layer's pixels at stroke-start so we can keep
// sampling clean pixels even after the brush has painted over them
// (avoids feedback / smearing).
// Double-tap detection for the Clone tool on touch devices — sets the
// sample anchor without a keyboard Alt modifier.

// Undo/Redo
const MAX_HISTORY = 20;

/** Get the selected normalized image model route.
 * Provider URLs and provider model aliases are not browser execution
 * authority; dropdown values are stable `/api/v1/providers/models` IDs. */
function _getSelectedAIEndpoint(type) {
  let raw = '';
  if (type === 'inpaint') {
    raw = _editorFind('ge-ai-inpaint')?.value || '';
  } else if (type) {
    // Per-tool dropdowns (harmonize/upscale/style). Each lives in its
    // own section's panel and is marked with data-ge-tool-model="<name>".
    const sel = _editorQuery(`select[data-ge-tool-model="${type}"]`);
    raw = sel?.value || '';
  }
  if (!raw && !type) raw = _editorFind('ge-ai-model')?.value || '';
  return { modelRouteId: raw };
}

/** Shared helper: flatten layers → POST to API → add result as new layer. */
// Maps a layer-name (the past-participle returned from each AI tool —
// "BG Removed", "Sharpened", etc.) into a present-progressive label for
// the busy button state ("Removing…", "Sharpening…"). Falls back to a
// neutral "Processing…" when the layer name doesn't match a known verb.
const _BUSY_LABELS = {
  'bg removed': 'Removing…',
  'sharpened': 'Sharpening…',
  'enhanced': 'Enhancing…',
  'harmonized': 'Harmonizing…',
  'upscaled': 'Upscaling…',
  'styled': 'Styling…',
};
function _deriveBusyLabel(layerName) {
  if (!layerName) return 'Processing…';
  return _BUSY_LABELS[String(layerName).toLowerCase()] || 'Processing…';
}

// AI-tool runner — sharpen / harmonize / upscale / style / bg-remove
// all flatten the doc, POST a PNG to a server endpoint, and drop the
// result back as a new layer. Full implementation in editor/ai-tool-
// runner.js; instantiated lazily (so it can reference function decls
// that haven't hoisted at module load time? — actually all named
// function decls hoist, so we instantiate at module top).
let _aiLifetime = null;
let _ownedApplyImageTool;
const _applyImageTool = (...args) => _ownedApplyImageTool?.(...args);
function _initApplyImageTool() { _ownedApplyImageTool = createApplyImageTool({
  markSceneChanged: _markSceneChanged,
  resultHooks:legacy ? null : {
    reserve:() => sessionPersistence.reserveResult({kind:'ai',epoch:String(_impsAccountEpoch),generation:_sceneAuthority.revision}),
    submit:id => sessionPersistence.submitResult(id), receive:(id,result) => sessionPersistence.receiveResult(id,result),
    reject:id => sessionPersistence.rejectResult(id), unknown:id => sessionPersistence.markResultUnknown(id), resolve:id => sessionPersistence.resolveResult(id),
  },
  beginMutation:() => _sceneAuthority.beginGesture(_gestureView,'ai-result'), endMutation:token => _sceneAuthority.endGesture(token),
  state, root: state.container, lifetime: _aiLifetime, captureOrigin: () => { const check=_captureAIOperation(), revision=_sceneAuthority.revision; return () => { const status=check(); return status==='current' && !_sceneAuthority.canApplyAt(revision) ? 'changed' : status; }; },
  flatten: () => flatten(),
  saveState: _saveState,
  createLayer,
  composite,
  renderLayerPanel: () => _renderLayerPanel(),
  deriveBusyLabel: (name) => _deriveBusyLabel(name),
  getSelectedAIEndpoint: (type) => _getSelectedAIEndpoint(type),
  openCookbookForDependency: (pkg) => _openCookbookForDependency(pkg),
  openCookbookForImg2img: () => _openCookbookForImg2img(),
  spinnerModule,
  uiModule,
}); }

function _captureAIOperation() {
  const lifetime = _aiLifetime, generation = _openGeneration, edit = state.editEpoch, origin = _captureOrigin();
  const owner = document.body.dataset.accountId, resource = state.managedResourceId, provider = state.managedProvider;
  const project = state.projectId, revision = state.projectRevision, imageRevision = state.expectedImageRevision, layer = state.activeLayerId;
  return () => !lifetime || lifetime.disposed || generation !== _openGeneration || owner !== document.body.dataset.accountId || !_originCurrent(origin) ? 'retired' : edit !== state.editEpoch || resource !== state.managedResourceId || provider !== state.managedProvider || project !== state.projectId || revision !== state.projectRevision || imageRevision !== state.expectedImageRevision || layer !== state.activeLayerId ? 'changed' : 'current';
}

function _setAiCommandStatus(text, kind = '') {
  const el = _editorFind('ge-ai-command-status');
  if (!el) return;
  el.textContent = text || '';
  el.dataset.kind = kind || '';
}

function _escapeAiCommandText(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function _clickToolButton(toolId) {
  const btn = state.container?.querySelector(`.ge-tool-btn[data-tool="${toolId}"]`);
  if (btn) btn.click();
}

function _runExistingButton(id, status) {
  const btn = _editorFind(id);
  if (!btn) {
    _setAiCommandStatus('That edit is not available in this editor state.', 'error');
    return false;
  }
  if (status) _setAiCommandStatus(status, 'running');
  btn.click();
  return true;
}

function _openSamPrompt() {
  if (state.tool !== 'sam') {
    _clickToolButton('sam');
  } else {
    const controls = _editorFind('ge-controls') || _editorQuery('.ge-controls');
    controls?.classList.remove('dismissed');
    _editorFind('ge-sam-section')?.style.removeProperty('display');
  }
  requestAnimationFrame(() => {
    const input = _editorFind('ge-sam-query');
    input?.focus();
    input?.select?.();
  });
}

function _buildAiCommandBox() {
  const wrap = document.createElement('div');
  wrap.className = 'ge-ai-command ge-ai-command-collapsed';
  wrap.id = 'ge-ai-command';
  wrap.innerHTML = `
    <button type="button" class="ge-ai-command-toggle" id="ge-ai-command-toggle" aria-expanded="false">
      <span class="ge-btn-ai-mark" aria-hidden="true">✦</span>
      <span>Quick Edit</span>
      ${uiIcon("chevron-up", 12, {"className":"ge-ai-command-toggle-caret"})}
    </button>
    <form class="ge-ai-command-form" id="ge-ai-command-form">
      <input type="text" class="ge-ai-command-input" id="ge-ai-command-input" autocomplete="off" />
      <button type="submit" class="ge-ai-command-run" id="ge-ai-command-run" title="Run AI edit" aria-label="Run AI edit">
        ${uiIcon("up", 15)}
      </button>
      <button type="button" class="ge-ai-command-close" id="ge-ai-command-close" title="Collapse AI edit" aria-label="Collapse AI edit">
        ${uiIcon("chevron-down", 14)}
      </button>
    </form>
    <div class="ge-ai-command-suggestions" id="ge-ai-command-suggestions" hidden></div>
    <div class="ge-ai-command-status" id="ge-ai-command-status" aria-live="polite"></div>
  `;
  return wrap;
}

const _AI_COMMAND_SUGGESTIONS = [
  { label: 'Rotate 90', insert: 'rotate 90', hint: 'Turn the image clockwise', aliases: ['ro', 'rotate', 'right', 'clockwise', 'turn'] },
  { label: 'Rotate left', insert: 'rotate left', hint: 'Turn the image counter-clockwise', aliases: ['rotate left', 'left', 'counter clockwise', 'ccw'] },
  { label: 'Rotate 180', insert: 'rotate 180', hint: 'Flip the canvas upside down', aliases: ['rotate 180', 'upside down'] },
  { label: 'Flip horizontal', insert: 'flip horizontal', hint: 'Mirror left to right', aliases: ['flip', 'mirror', 'horizontal'] },
  { label: 'Flip vertical', insert: 'flip vertical', hint: 'Mirror top to bottom', aliases: ['flip vertical', 'vertical'] },
  { label: 'Remove background', insert: 'remove background', hint: 'Make the background transparent', aliases: ['remove bg', 'background', 'transparent', 'cut out'] },
  { label: 'Upscale', insert: 'upscale 2x', hint: 'Increase image resolution', aliases: ['upscale', 'bigger', 'larger', '2x', '4x'] },
  { label: 'Denoise', insert: 'denoise', hint: 'Reduce grain and noise', aliases: ['denoise', 'noise', 'grain', 'clean up'] },
  { label: 'Sharpen', insert: 'sharpen', hint: 'Make details crisper', aliases: ['sharpen', 'sharp', 'clearer', 'crisp', 'enhance'] },
  { label: 'Enhance face', insert: 'enhance face', hint: 'Restore portrait and skin detail', aliases: ['face', 'portrait', 'skin', 'selfie', 'restore'] },
  { label: 'Style edit', insert: 'style: ', hint: 'Run a full-image prompt edit', aliases: ['style', 'paint', 'anime', 'photo', 'prompt'] },
];

function _matchAiCommandSuggestions(query) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return [];
  return _AI_COMMAND_SUGGESTIONS
    .map((item) => {
      const hay = [item.label, item.insert, ...(item.aliases || [])].map(v => String(v || '').toLowerCase());
      const starts = hay.some(v => v.startsWith(q));
      const contains = hay.some(v => v.includes(q));
      if (!starts && !contains) return null;
      return { item, score: starts ? 0 : 1 };
    })
    .filter(Boolean)
    .sort((a, b) => a.score - b.score || a.item.label.localeCompare(b.item.label))
    .slice(0, 6)
    .map(hit => hit.item);
}

function _wireAiCommandBox() {
  const wrap = _editorFind('ge-ai-command');
  const toggle = _editorFind('ge-ai-command-toggle');
  const closeBtn = _editorFind('ge-ai-command-close');
  const form = _editorFind('ge-ai-command-form');
  const input = _editorFind('ge-ai-command-input');
  const runBtn = _editorFind('ge-ai-command-run');
  const suggestions = _editorFind('ge-ai-command-suggestions');
  if (!wrap || !form || !input || !runBtn) return;
  let suggestionItems = [];
  let suggestionIndex = 0;
  const hideSuggestions = () => {
    suggestionItems = [];
    suggestionIndex = 0;
    if (suggestions) {
      suggestions.hidden = true;
      suggestions.innerHTML = '';
    }
  };
  const renderSuggestions = () => {
    if (!suggestions || wrap.classList.contains('ge-ai-command-collapsed')) return;
    suggestionItems = _matchAiCommandSuggestions(input.value);
    suggestionIndex = Math.min(suggestionIndex, Math.max(0, suggestionItems.length - 1));
    if (!suggestionItems.length) {
      hideSuggestions();
      return;
    }
    suggestions.hidden = false;
    suggestions.innerHTML = suggestionItems.map((item, idx) => `
      <button type="button" class="ge-ai-command-suggestion${idx === suggestionIndex ? ' active' : ''}" data-ai-command-suggestion="${idx}">
        <span class="ge-ai-command-suggestion-main">${_escapeAiCommandText(item.label)}</span>
        <span class="ge-ai-command-suggestion-hint">${_escapeAiCommandText(item.hint || item.insert)}</span>
      </button>
    `).join('');
  };
  const pickSuggestion = (idx, run = false) => {
    const item = suggestionItems[idx];
    if (!item) return false;
    input.value = item.insert;
    hideSuggestions();
    input.focus();
    if (run) form.requestSubmit();
    return true;
  };
  wrap.addEventListener('pointerdown', (e) => e.stopPropagation());
  wrap.addEventListener('click', (e) => e.stopPropagation());
  suggestions?.addEventListener('pointerdown', (e) => e.preventDefault());
  suggestions?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-ai-command-suggestion]');
    if (!btn) return;
    pickSuggestion(Number(btn.dataset.aiCommandSuggestion), true);
  });
  const setOpen = (open) => {
    wrap.classList.toggle('ge-ai-command-collapsed', !open);
    toggle?.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) requestAnimationFrame(() => {
      input.focus();
      renderSuggestions();
    });
    else hideSuggestions();
  };
  toggle?.addEventListener('click', () => setOpen(wrap.classList.contains('ge-ai-command-collapsed')));
  closeBtn?.addEventListener('click', () => setOpen(false));
  input.addEventListener('input', renderSuggestions);
  input.addEventListener('keydown', (e) => {
    const open = suggestions && !suggestions.hidden && suggestionItems.length;
    if (open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault();
      suggestionIndex = e.key === 'ArrowDown'
        ? (suggestionIndex + 1) % suggestionItems.length
        : (suggestionIndex - 1 + suggestionItems.length) % suggestionItems.length;
      renderSuggestions();
      return;
    }
    if (open && e.key === 'Enter') {
      e.preventDefault();
      pickSuggestion(suggestionIndex, true);
      return;
    }
    if (open && e.key === 'Tab') {
      e.preventDefault();
      pickSuggestion(suggestionIndex, false);
      return;
    }
    if (open && e.key === 'Escape') {
      e.preventDefault();
      hideSuggestions();
    }
  });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    hideSuggestions();
    const prompt = input.value.trim();
    if (!prompt) {
      _setAiCommandStatus('Type what you want changed.', 'error');
      input.focus();
      return;
    }
    const p = prompt.toLowerCase();
    try {
      if (/\brotate\b.*\b180\b|\bupside\s*down\b/.test(p)) {
        _setAiCommandStatus(_rotateAllLayers(180) ? 'Rotation scheduled.' : 'Rotation was not scheduled.', 'done');
        return;
      }
      if (/\brotate\b.*\b(left|ccw|counter)\b|\bturn\s+left\b/.test(p)) {
        _setAiCommandStatus(_rotateAllLayers(270) ? 'Rotation scheduled.' : 'Rotation was not scheduled.', 'done');
        return;
      }
      if (/\brotate\b|\bturn\s+right\b|\bclockwise\b/.test(p)) {
        _setAiCommandStatus(_rotateAllLayers(90) ? 'Rotation scheduled.' : 'Rotation was not scheduled.', 'done');
        return;
      }
      if (/\bflip\b.*\b(vertical|v)\b|\bmirror\b.*\b(vertical|v)\b/.test(p)) {
        _setAiCommandStatus(_flipAllLayers('v') ? 'Flipped vertical.' : 'Flip was not applied.', 'done');
        return;
      }
      if (/\bflip\b|\bmirror\b/.test(p)) {
        _setAiCommandStatus(_flipAllLayers('h') ? 'Flipped horizontal.' : 'Flip was not applied.', 'done');
        return;
      }
      if (/\b(remove|erase|cut\s*out|transparent)\b.*\b(bg|background)\b|\b(bg|background)\b.*\b(remove|erase|transparent)\b/.test(p)) {
        _clickToolButton('rembg');
        _runExistingButton('ge-rembg-run', 'Removing background...');
        return;
      }
      if (/\b(upscale|higher\s*res|increase\s*resolution|bigger|2x|4x)\b/.test(p)) {
        _clickToolButton('upscale');
        _runExistingButton('ge-upscale-ai', 'Upscaling image...');
        return;
      }
      if (/\b(denoise|noise|grain|grainy|clean\s*up)\b/.test(p)) {
        _setAiCommandStatus('Denoising image...', 'running');
        await _applyImageTool('/api/image/denoise', { strength: 0.55 }, 'Denoised', runBtn, { busyLabel: 'Denoising...' });
        _setAiCommandStatus('Added denoised layer.', 'done');
        return;
      }
      if (/\b(face|portrait|skin|selfie|restore)\b/.test(p)) {
        _setAiCommandStatus('Enhancing face/portrait...', 'running');
        await _applyImageTool('/api/image/enhance-face', {}, 'Enhanced Face', runBtn, { busyLabel: 'Enhancing...' });
        _setAiCommandStatus('Added enhanced layer.', 'done');
        return;
      }
      if (/\b(sharpen|sharp|crisp|clearer|make it look better|enhance|improve|better)\b/.test(p)) {
        const amount = _editorFind('ge-sharpen-amount');
        if (amount) {
          amount.value = '65';
          amount.dispatchEvent(new Event('input', { bubbles: true }));
        }
        _clickToolButton('sharpen');
        _runExistingButton('ge-sharpen-run', 'Sharpening image...');
        return;
      }

      const stylePrompt = _editorFind('ge-style-prompt');
      const styleStrength = _editorFind('ge-style-strength');
      if (stylePrompt) stylePrompt.value = prompt;
      if (styleStrength) {
        styleStrength.value = /\b(subtle|slight|small)\b/.test(p) ? '35' : '55';
        styleStrength.dispatchEvent(new Event('input', { bubbles: true }));
      }
      _clickToolButton('style');
      _runExistingButton('ge-style-run', 'Running full-image AI edit...');
    } catch (err) {
      console.error('[ge-ai-command] failed', err);
      _setAiCommandStatus(err?.message || 'AI edit failed', 'error');
    }
  });
}

// Layer offsets for move tool

// ── Layer class ──


function createLayer(name, width, height) {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const layer = {
    id: 'layer-' + (state.nextLayerId++),
    name,
    canvas,
    ctx: canvas.getContext('2d'),
    visible: true,
    opacity: 1,
    locked: false,
    // Mask sub-layers — same shape as adjLayers, parallel concept.
    // Each entry: {id, name, canvas, visible}. The "active" mask is the
    // one that paint / lasso / inpaint operations target; rendered as a
    // red overlay in composite().
    masks: [],
    activeMaskId: null,
    // Non-destructive adjustments. B/C/S/H are applied at composite()
    // via ctx.filter (fast, CSS). Levels + Color Balance need per-pixel
    // math and are baked into a cached canvas (layer._adjCache) that
    // gets re-rendered only when those values change.
    adjustments: {
      brightness: 1, // 0..2 (1 = neutral)
      contrast: 1,   // 0..2 (1 = neutral)
      saturation: 1, // 0..2 (0 = grayscale, 1 = neutral)
      hue: 0,        // degrees, -180..180
      // Levels — Photoshop-style three-stop adjust applied per channel.
      // input 0..255, gamma 0.1..9.9. Default is identity.
      levels: { inBlack: 0, inWhite: 255, gamma: 1.0, outBlack: 0, outWhite: 255 },
      // Color Balance — additive per-channel shifts weighted by tone.
      // Each value is -100..+100 mapping to roughly ±60 in 0..255 space.
      colorBalance: {
        shadows:    { r: 0, g: 0, b: 0 },
        midtones:   { r: 0, g: 0, b: 0 },
        highlights: { r: 0, g: 0, b: 0 },
      },
    },
  };
  state.layerOffsets.set(layer.id, { x: 0, y: 0 });
  _markSceneChanged();
  return layer;
}

// _layerFilterString + _fxFilterToSlider live in editor/fx/filter-string.js
// — see import at top.

// _layerHasAdjustments lives in editor/layer-helpers.js — see import at top.

// ── Mask sub-layers ──
// Resolves to the parent layer that should own masks for the current
// edit. The "active" parent is whichever layer the user has selected,
// excluding mask-sublayer entries themselves.
function _activeParentLayer() {
  return state.layers.find(l => l.id === state.activeLayerId) || state.layers[state.layers.length - 1] || null;
}

// Find the active mask sub-layer (the one paint/lasso/inpaint ops
// target). Returns null if the parent has no masks OR if no mask is
// currently activated (i.e. the user explicitly selected the parent
// pixels as the paint target). Earlier code fell back to "last mask
// in the list" which meant clicking the parent row couldn't escape
// mask-paint mode — that surprised the user, so the fallback is
// gone.
function _getActiveMaskLayer() {
  const parent = _activeParentLayer();
  if (!parent || !parent.masks || !parent.masks.length) return null;
  if (!parent.activeMaskId) return null;
  return parent.masks.find(m => m.id === parent.activeMaskId) || null;
}

// Get-or-create a mask sub-layer on the active parent. Used by tools
// that need a mask to write into (Brush on mask, Inpaint stroke,
// lasso→mask, wand→mask).
function _ensureActiveMaskLayer() {
  const parent = _activeParentLayer();
  if (!parent) return null;
  if (!parent.masks) parent.masks = [];
  let mask = _getActiveMaskLayer();
  if (mask) return mask;
  const c = document.createElement('canvas');
  c.width = state.imgWidth;
  c.height = state.imgHeight;
  mask = {
    id: 'mask-' + (state.nextLayerId++),
    name: 'Mask ' + (parent.masks.length + 1),
    canvas: c,
    ctx: c.getContext('2d'),
    visible: true,
  };
  parent.masks.push(mask);
  parent.activeMaskId = mask.id;
  _markSceneChanged();
  return mask;
}

// True if any visible layer in the doc carries a mask sub-layer; drives
// the "red overlay" pass in composite().
function _hasAnyMasks() {
  for (const l of state.layers) {
    if (l.masks && l.masks.length) return true;
  }
  return false;
}

// Union of every VISIBLE mask sub-layer across the whole document,
// returned as a fresh image-sized canvas with white = masked area.
// Used by inpaint Generate/Remove so the AI sees the combined region
// instead of just the active mask. Returns null when no masks exist
// (caller should fall back to the active mask plumbing in that case).
function _buildMergedMaskCanvas() {
  return _buildMergedMaskCanvasImpl(state.layers, state.imgWidth, state.imgHeight);
}

// True if the layer needs the (slower) per-pixel LUT pass — i.e. Levels
// or Color Balance are non-identity. Brightness/Contrast/Saturation/Hue
// alone can stay on the fast CSS-filter path.
// _layerNeedsPixelPass + _adjustmentsKey live in editor/layer-helpers.js.

// Per-pixel Levels + Color Balance. Renders the layer.canvas into a
// cached canvas (layer._adjCache) with the LUT-style transforms applied.
// CSS-filter adjustments (B/C/S/H) are still applied at composite() on
// top of this cache.
// Pixel-pass adjustment math lives in editor/fx/pixel-pass.js. This
// wrapper forwards the layer + a fresh adjustments-cache key so
// existing callers stay unchanged.
function _renderLayerPixelAdjustments(layer) {
  return _renderLayerPixelAdjustmentsImpl(layer, _adjustmentsKey(layer.adjustments));
}
// Layer FX popup — floating window bound to a specific layer. Sliders
// edit that layer's adjustments and live-update composite(). The popup
// stays open across clicks elsewhere unless dismissed via its × button.

// FX / adjustment-popup machinery — full implementation in
// editor/fx/adj-popup.js. Wrappers preserve the legacy names that
// every layer-row FX button, panel-row click, and undo/redo path
// already references.
let _adjPopupSystem;
function _initAdjPopupSystem() { _adjPopupSystem = createAdjPopupSystem({
  markSceneChanged: _markSceneChanged,
  withSceneMutation: _withSceneMutation, captureOrigin: () => ({view:_previewOrigin(), source:legacy ? null : JSON.stringify(session.captureTarget())}),
  originCurrent: value => !!value && _previewCurrent(value.view) && (legacy || value.source === JSON.stringify(session.captureTarget())), onRefused: () => uiModule?.showToast('The image changed; reopen the adjustment.'),
  state, root: state.container, lifetime: _coreLifetime, modalManager,
  composite,
  saveState: _saveState,
  renderLayerPanel: () => _renderLayerPanel(),
}); }
const _closeFxPopup = (...args) => _adjPopupSystem?.closeFxPopup(...args);
const _ensureAdjustments = (...args) => _adjPopupSystem?.ensureAdjustments(...args);
const _ensureFxDock = (...args) => _adjPopupSystem?.ensureFxDock(...args);
const _closeFxMenu = (...args) => _adjPopupSystem?.closeFxMenu(...args);
const _openFxPopup = (...args) => _adjPopupSystem?.openFxPopup(...args);
const _openAdjPopup = (...args) => _adjPopupSystem?.openAdjPopup(...args);
const _editAdjLayer = (...args) => _adjPopupSystem?.editAdjLayer(...args);
const _closeAdjPopup = (...args) => _adjPopupSystem?.closeAdjPopup(...args);
const _minimiseAdjPopup = (...args) => _adjPopupSystem?.minimiseAdjPopup(...args);
const _syncFxPanelToActiveLayerIfPresent = (...args) => _adjPopupSystem?.syncFxPanelToActiveLayerIfPresent(...args);

function activeLayer() {
  return state.layers.find(l => l.id === state.activeLayerId) || null;
}

// Flood-fill enclosed regions of the inpaint mask. After the user
// draws a closed shape (circle, lasso, whatever), the interior is
// alpha=0 surrounded by white mask. We mark all alpha=0 pixels
// reachable from the canvas edges as "outside"; anything still alpha=0
// after that pass is enclosed and gets filled with white.
function _fillEnclosedMaskRegions() {
  if (!state.maskCanvas || !state.maskCtx) return;
  const w = state.maskCanvas.width, h = state.maskCanvas.height;
  if (w * h > 4096 * 4096) return; // safety cap
  const img = state.maskCtx.getImageData(0, 0, w, h);
  const d = img.data;
  // visited bitmap — 0 = unvisited, 1 = reached from edge (outside),
  // 2 = mask (alpha>0). After BFS, alpha=0 pixels with visited[i]=0
  // are enclosed.
  const visited = new Uint8Array(w * h);
  const stack = [];
  // Pre-mark all mask pixels as visited=2 so we don't cross them.
  for (let i = 0, j = 0; i < d.length; i += 4, j++) {
    if (d[i + 3] > 0) visited[j] = 2;
  }
  // Seed flood from every edge pixel that's empty.
  const seed = (x, y) => {
    const k = y * w + x;
    if (visited[k] === 0) { visited[k] = 1; stack.push(k); }
  };
  for (let x = 0; x < w; x++) { seed(x, 0); seed(x, h - 1); }
  for (let y = 0; y < h; y++) { seed(0, y); seed(w - 1, y); }
  // BFS — 4-connected.
  while (stack.length) {
    const k = stack.pop();
    const x = k % w, y = (k - x) / w;
    if (x > 0)     { const n = k - 1; if (visited[n] === 0) { visited[n] = 1; stack.push(n); } }
    if (x < w - 1) { const n = k + 1; if (visited[n] === 0) { visited[n] = 1; stack.push(n); } }
    if (y > 0)     { const n = k - w; if (visited[n] === 0) { visited[n] = 1; stack.push(n); } }
    if (y < h - 1) { const n = k + w; if (visited[n] === 0) { visited[n] = 1; stack.push(n); } }
  }
  // Anything still visited=0 → enclosed empty region. Fill white.
  let filled = false;
  for (let j = 0, i = 0; j < visited.length; j++, i += 4) {
    if (visited[j] === 0) {
      d[i] = 255; d[i + 1] = 255; d[i + 2] = 255; d[i + 3] = 255;
      filled = true;
    }
  }
  if (filled) state.maskCtx.putImageData(img, 0, 0);
}

// True if a layer has no opaque pixels — used to tag the row in the
// layer panel as "(empty)" so the user can tell at a glance which
// layers carry actual content.
// Lightweight loading overlay anchored to the canvas area. Used for
// blocking operations (rotation on big images, etc.) so the user gets
// feedback while the main thread is busy. The actual heavy call should
// be deferred with rAF so the overlay paints before the block.
function _showCanvasLoading(message) {
  if (!state.container) return;
  let overlay = state.container.querySelector('.ge-canvas-loading');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.className = 'ge-canvas-loading ge-frosted';
    overlay.innerHTML = `
      <div class="ge-canvas-loading-spinner"></div>
      <div class="ge-canvas-loading-msg"></div>
    `;
    state.container.appendChild(overlay);
  }
  overlay.querySelector('.ge-canvas-loading-msg').textContent = message || 'Working…';
  overlay._loadingOwner = Symbol('canvas loading');
  overlay.style.display = '';
  return overlay._loadingOwner;
}
function _hideCanvasLoading(owner) {
  const overlay = state.container && state.container.querySelector('.ge-canvas-loading');
  if (overlay && (!owner || overlay._loadingOwner === owner)) overlay.style.display = 'none';
}

// Cheap "is this mask canvas blank?" check. Used to suffix "(empty)" on
// mask sub-layer rows in the panel so the user can tell at a glance
// which masks have actually been painted on.
// _isMaskCanvasEmpty lives in editor/layer-helpers.js.

// Document-wide rotate / flip — implementations in
// editor/canvas-transforms.js. Wrappers preserve the legacy names that
// the topbar Image menu and shortcuts already wire to.
const _sceneAuthority = retainedScene || createImageScene({state:stateParts?.scene || state, document, limit:MAX_HISTORY, sceneKey:stateParts?.scene || state,
  ...(legacy ? {onChange:() => _schedulePersist()} : {persistence:sessionPersistence})});
if (!legacy && !retainedScene) { const releaseScene = session.attachSceneAuthority(_sceneAuthority); lifetime.add(() => { if (!state.layers.length && _sceneAuthority.canDispose()) releaseScene(); }); }
if (!legacy && !retainedScene) sessionPersistence.setResultSceneAuthority(_sceneAuthority);
let _feedbackSourceWitness = null;
const _markSceneChanged = () => { _feedbackSourceWitness = null; return _sceneAuthority.markChanged(); };
// Each mounted view owns separate gesture tokens; only the scene can grant them.
const _gestureView = {};
function _makeGesture(operation) {
  let token = null;
  const gesture = {
    begin() { if (token) return false; token = _sceneAuthority.beginGesture(_gestureView, operation); return !!token; },
    owns() { return _sceneAuthority.ownsGesture(token); },
    end() { if (!token) return false; const old = token; token = null; return _sceneAuthority.endGesture(old); },
  };
  lifetime?.add(() => gesture.end());
  return gesture;
}
const _strokeGesture = _makeGesture('stroke'), _transformGesture = _makeGesture('transform'), _moveGesture = _makeGesture('move');
function _withSceneMutation(operation, action) {const token=_sceneAuthority.beginGesture(_gestureView,operation); if (!token) {uiModule?.showToast('Finish the current image edit first.'); return false;} try {return action();} finally {_sceneAuthority.endGesture(token);}}
function _canvasContextReady() {
  return state.editorOpen && (legacy ? state.layers.length > 0 : sceneReady && isolatedVisible && authority.isActive?.() !== false)
    && state.container?.isConnected && !state.container.inert && _originCurrent(_captureOrigin());
}

function _canvasContextWritable() {
  return _canvasContextReady() && _sceneAuthority.canApplyAt(_sceneAuthority.revision)
    && !readOnlyImageScenes.has(_sceneAuthority)
    && (legacy || sessionPersistence.recoveryStatus().state === 'ready');
}

function _captureFeedbackRegion() {
  if (legacy) return null;
  const target = session.captureTarget(), rect = state.cropRect, witness = _feedbackSourceWitness;
  if (!sceneReady || !state.editorOpen || !isolatedVisible || !witness
    || witness.revision !== _sceneAuthority.revision || witness.target !== JSON.stringify(target)
    || authority.validate(target) !== true || target.draftId || !_previewCurrent(_cropPreview)
    || !rect || !state.imgWidth || !state.imgHeight) return null;
  const {x,y,w,h} = rect;
  if (![x,y,w,h].every(Number.isFinite) || x < 0 || y < 0 || w <= 0 || h <= 0
    || x + w > state.imgWidth || y + h > state.imgHeight) return null;
  return Object.freeze({
    resourceRef:target.opaqueRef, expectedRevision:target.sourceRevision,
    anchor:{kind:'image',x:x/state.imgWidth,y:y/state.imgHeight,width:w/state.imgWidth,height:h/state.imgHeight},
    witness:JSON.stringify([witness,rect]),
  });
}

function _wireCanvasContext(canvas, area) {
  const canvasCurrent = ({ adapterContext:target } = {}) => Boolean(target && canvas === state.mainCanvas
    && _canvasContextReady() && _previewCurrent(target.origin)
    && (!target.feedback || _captureFeedbackRegion()?.witness === target.feedback.witness
      && String(window.sessionModule?.getCurrentSessionId?.() || '') === target.chat)
    && (!target.selection || (target.kind === 'wand'
      ? target.selection === state.wandMask && target.layer.id === state.wandLayerId
      : target.selection === state.lassoPoints && !state.lassoActive && _previewCurrent(_lassoPreview))));
  registerAdapter(area, {
    hitTest:(node, event) => event.pointerType !== 'touch' && (node === canvas || node === area) ? true : 'native',
    capture:(node, event) => {
      if (!_canvasContextReady()) return null;
      const target = { origin:_previewOrigin() };
      if (node !== canvas) return target;
      const keyboard = event.detail === 0 && event.button !== 2 && !event.pointerType;
      const {x,y} = keyboard ? {} : _canvasCoords(event, canvas);
      if (onContextFeedback) {
        const feedback = _captureFeedbackRegion();
        const chat = String(window.sessionModule?.getCurrentSessionId?.() || '');
        const box = state.cropRect;
        if (feedback && chat && box && (keyboard || x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h)) {
          Object.assign(target, { feedback, chat });
        }
      }
      if (!_canvasContextWritable()) return target;
      const points = state.lassoPoints;
      const insideLasso = () => {
        if (keyboard) return true;
        let inside = false;
        for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
          const a = points[i], b = points[j];
          if ((a.y > y) !== (b.y > y) && x < (b.x - a.x) * (y - a.y) / (b.y - a.y) + a.x) inside = !inside;
        }
        return inside;
      };
      const wandLayer = state.layers.find(layer => layer.id === state.wandLayerId);
      if (state.wandMask) {
        if (wandLayer && !wandLayer.locked && (keyboard || _wandHits(x,y))) {
          Object.assign(target, { kind:'wand', selection:state.wandMask, layer:wandLayer });
        }
      } else if (points.length >= 3 && !state.lassoActive && _previewCurrent(_lassoPreview)
        && target.origin.layer && !target.origin.layer.locked && insideLasso()) {
        Object.assign(target, { kind:'lasso', selection:points, layer:target.origin.layer });
      }
      return target;
    },
    isCurrent:canvasCurrent,
    commands:request => {
      if (!canvasCurrent(request)) return [];
      const selection = request.adapterContext.selection;
      return [
        ...(request.adapterContext.feedback ? [{ id:'image-region-feedback', label:'Feedback on image region…', dialog:true }] : []),
        ...(selection && _canvasContextWritable() ? [
          { id:'image-selection-copy', label:'Copy selection to layer' },
          { id:'image-selection-invert', label:'Invert selection' },
          { id:'image-selection-delete', label:'Delete selected pixels', destructive:true },
        ] : []),
        { id:'image-zoom-fit', label:'Fit image' },
        { id:'image-zoom-actual', label:'Actual size' },
      ];
    },
    execute:(command, request) => {
      if (!canvasCurrent(request)) return false;
      if (command === 'image-zoom-fit') { _fitZoom(); return true; }
      if (command === 'image-zoom-actual') { state.zoom = 1; _applyZoom(); return true; }
      const { kind, layer, feedback, chat } = request.adapterContext;
      if (command === 'image-region-feedback' && feedback) return onContextFeedback(feedback, chat, () => canvasCurrent(request));
      if (!kind || !state.layers.includes(layer) || layer.locked || !_canvasContextWritable()) return false;
      if (command === 'image-selection-invert') _invertSelection();
      else if (command === 'image-selection-copy') (kind === 'wand' ? _wandCopyToNewLayer : _lassoCopyToLayer)();
      else if (command === 'image-selection-delete') (kind === 'wand' ? _wandDeleteSelection : _lassoDeleteSelection)();
      else return false;
      return true;
    },
  });
}

function _previewOrigin() {return {revision:_sceneAuthority.revision,width:state.imgWidth,height:state.imgHeight,layer:activeLayer(),view:_coreLifetime,origin:_captureOrigin()};}
function _previewCurrent(value) {return !!value && !value.view?.disposed && value.view===_coreLifetime && _originCurrent(value.origin) && value.revision===_sceneAuthority.revision && value.width===state.imgWidth && value.height===state.imgHeight && value.layer===activeLayer() && (!value.layer || state.layers.includes(value.layer));}
const _writerOrigin = () => ({view:_previewOrigin(), source:legacy ? null : JSON.stringify(session.captureTarget())});
const _writerCurrent = value => !!value && _previewCurrent(value.view) && (legacy || value.source === JSON.stringify(session.captureTarget()));
let _cropPreview=null, _lassoPreview=null;

let _canvasTransforms;
function _initCanvasTransforms(container) { _canvasTransforms = createCanvasTransforms({
  root: container,
  state, lifetime: () => _coreLifetime, markSceneChanged: _markSceneChanged,
  withSceneMutation: _withSceneMutation, captureOrigin: _writerOrigin, originCurrent: _writerCurrent,
  onResize: () => _syncInpaintControls?.(),
  clearPreview: () => { state.lassoPoints = []; state.lassoActive = false; _lassoPreview = null; _cropPreview = null; },
  saveState: _saveState,
  composite,
  fitZoom: () => _fitZoom(),
  showCanvasLoading: (label) => _showCanvasLoading(label),
  hideCanvasLoading: (owner) => _hideCanvasLoading(owner),
}); }
function _rotateAllLayers(deg) { return _canvasTransforms.rotateAll(deg); }
function _flipAllLayers(axis)  { return _canvasTransforms.flipAll(axis); }

// _isLayerEmpty lives in editor/layer-helpers.js.

// ── Composite ──

function composite() {
  if (!legacy && (!isolatedVisible || authority.isActive?.() === false)) return;
  if (!state.mainCtx) return;
  state.mainCtx.clearRect(0, 0, state.mainCanvas.width, state.mainCanvas.height);
  // Checkerboard background
  _drawCheckerboard(state.mainCtx, state.mainCanvas.width, state.mainCanvas.height);
  for (const layer of state.layers) {
    if (!layer.visible) continue;
    state.mainCtx.globalAlpha = layer.opacity;
    const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
    // Source = layer.canvas walked through all its adjustment
    // sub-layers (plus any staged-in-progress edit). Falls back to
    // raw layer.canvas when no adjustments are present.
    const source = _renderLayerWithAdjLayers(layer, _adjPopupSystem?.previewFor(layer));
    state.mainCtx.drawImage(source, off.x, off.y);
    state.mainCtx.globalAlpha = 1;
  }
  // Show mask overlay as red tint whenever a mask sub-layer is present
  // on the active parent (was previously gated on inpaint-tool only; now
  // masks are first-class layer entities, so users see them in any tool).
  // Mask canvas has white pixels — we tint them red for visibility.
  if (state.maskVisible) {
    // Build a SINGLE merged mask canvas from every visible mask
    // sub-layer (union of alpha — `lighter` keeps max alpha per pixel,
    // so overlapping strokes don't visually stack). Then tint it red
    // once and composite at the configured opacity. Result: the user
    // sees a flat, consistent translucent red over the masked area
    // regardless of how many strokes / masks contributed to it. Mask
    // visibility is INDEPENDENT of parent visibility — hiding the
    // parent layer doesn't hide its masks.
    let _haveAny = false;
    if (!state.compositeMaskUnion) state.compositeMaskUnion = document.createElement('canvas');
    const union = state.compositeMaskUnion;
    union.width = state.mainCanvas.width;
    union.height = state.mainCanvas.height;
    const uctx = union.getContext('2d');
    uctx.clearRect(0, 0, union.width, union.height);
    uctx.globalCompositeOperation = 'lighter';
    for (const ly of state.layers) {
      if (!ly.masks || !ly.masks.length) continue;
      for (const mk of ly.masks) {
        if (!mk.visible) continue;
        if (!mk.canvas || !mk.canvas.width || !mk.canvas.height) continue;
        uctx.drawImage(mk.canvas, 0, 0);
        _haveAny = true;
      }
    }
    uctx.globalCompositeOperation = 'source-over';
    if (_haveAny) {
      // Tint the merged mask in-place: anywhere alpha exists becomes
      // red; everywhere else stays transparent.
      uctx.globalCompositeOperation = 'source-in';
      uctx.fillStyle = state.maskTintColor || 'rgba(255, 50, 50, 1)';
      uctx.fillRect(0, 0, union.width, union.height);
      uctx.globalCompositeOperation = 'source-over';
      state.mainCtx.globalAlpha = state.maskTintOpacity;
      state.mainCtx.drawImage(union, 0, 0);
      state.mainCtx.globalAlpha = 1;
    }
  }
  // Draw transform handles if active
  if (state.transformActive) _drawTransformHandles();
  else if (state.transformOverlay) state.transformOverlay.style.display = 'none';
  // Persist the lasso selection overlay across composite redraws — without
  // this, leaving/re-entering the canvas (which triggers _endDraw →
  // composite) would visually wipe a completed selection even though
  // state.lassoPoints is still populated.
  if (!state.lassoActive && state.lassoPoints.length >= 3) _drawLassoOverlay();
  // Same idea for the crop overlay — once the user releases the drag,
  // the crop rect should remain visible until they Apply or cancel.
  // Hovering over the floating Apply button counts as a mouseleave on
  // the canvas, which used to wipe the overlay.
  if (state.cropRect && !state.cropping) _drawCropOverlay();
  // Snap guides — drawn while the user is moving a layer with Ctrl held.
  if (state.activeSnapGuides && state.activeSnapGuides.length) _drawSnapGuides();
  // Magic-wand selection overlay (translucent red tint of the mask).
  if (state.wandMask && state.wandLayerId && state.wandMaskVisible) _drawWandOverlay();
  // Keep the per-tool clear-X badges in sync. Cheap: two classList
  // toggles. Composite runs on every visible state change, so this
  // catches every lasso/wand mutation site without each one having to
  // remember to call the sync helper.
  _syncToolClearIndicators();
}

function _drawSnapGuides() {
  const ctx = state.mainCtx;
  ctx.save();
  ctx.strokeStyle = 'rgba(224, 108, 117, 0.85)';
  ctx.lineWidth = 1 / state.zoom;
  ctx.setLineDash([4 / state.zoom, 3 / state.zoom]);
  for (const g of state.activeSnapGuides) {
    ctx.beginPath();
    if (g.vertical) {
      ctx.moveTo(g.x, 0);
      ctx.lineTo(g.x, state.imgHeight);
    } else {
      ctx.moveTo(0, g.y);
      ctx.lineTo(state.imgWidth, g.y);
    }
    ctx.stroke();
  }
  ctx.restore();
}

// Draw the dim-everything-else + cleared-crop-window overlay for the
// current `state.cropRect`. Shared by _continueCrop (live preview during drag)
// and composite() (re-draw after canvas redraws while the crop is held).
function _drawCropOverlay() {
  if (!state.cropRect || !state.mainCtx || !state.mainCanvas) return;
  const { x, y, w, h } = state.cropRect;
  state.mainCtx.fillStyle = 'rgba(0,0,0,0.4)';
  state.mainCtx.fillRect(0, 0, state.mainCanvas.width, state.mainCanvas.height);
  state.mainCtx.clearRect(x, y, w, h);
  state.mainCtx.save();
  state.mainCtx.beginPath();
  state.mainCtx.rect(x, y, w, h);
  state.mainCtx.clip();
  _drawCheckerboard(state.mainCtx, state.mainCanvas.width, state.mainCanvas.height);
  for (const layer of state.layers) {
    if (!layer.visible) continue;
    state.mainCtx.globalAlpha = layer.opacity;
    const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
    state.mainCtx.drawImage(layer.canvas, off.x, off.y);
  }
  state.mainCtx.globalAlpha = 1;
  state.mainCtx.restore();
  state.mainCtx.strokeStyle = '#fff';
  state.mainCtx.lineWidth = 1;
  state.mainCtx.setLineDash([4, 4]);
  state.mainCtx.strokeRect(x, y, w, h);
  state.mainCtx.setLineDash([]);
}

// _drawCheckerboard lives in editor/checkerboard.js — see import at top.

// ── History ──

const _coreHistory = _sceneAuthority.history;

function _saveState(label) {
  state.editEpoch += 1;
  // saveState() runs FIRST in every mutating op (import, paste, copy,
  // merge, mask, delete, brush, …). If anything here throws, the whole
  // operation aborts before its real work runs — which silently breaks
  // import ("no layer created") and every layer button. So each step is
  // isolated: a history-snapshot failure must degrade (lose one undo
  // step) rather than kill the user's action.
  try {
    _coreHistory.save(label);
  } catch (e) {
    console.error('[imps] saveState snapshot failed (continuing without this undo step):', e);
  }
  try { _invalidateWandCache(); } catch (e) { console.error('[imps] invalidateWandCache:', e); }
  try { _schedulePersist(); } catch (e) { console.error('[imps] schedulePersist:', e); }
  try { _refreshHistoryPanelIfOpen(); } catch (e) { console.error('[imps] refreshHistoryPanel:', e); }
}

async function _openProjectMetadata() {
  if (!state.editorOpen || !state.layers.length) throw new Error('Open an image first.');
  const origin=_writerOrigin(), identity=JSON.stringify([state.managedProvider,state.managedResourceId,state.projectId,state.projectRevision,state.expectedImageRevision]);
  const accountId=document.body.dataset.accountId || null;
  const recoveryClear=()=>legacy ? !state._legacyProjectPending && !state._legacyCreatePending && !state._legacyImportPending && !state.pendingSaveCopy && ![..._managedSaveFlights.values()].some(flight=>flight.uncertain) : sessionPersistence.recoveryStatus().state==='ready';
  const current=()=>Boolean(accountId && _writerCurrent(origin) && document.body.dataset.accountId===accountId && identity===JSON.stringify([state.managedProvider,state.managedResourceId,state.projectId,state.projectRevision,state.expectedImageRevision]) && recoveryClear());
  if (!current()) throw new Error('The image or its recovery operation is unavailable.');
  const extras=state._editableExtras, present=extras!=null && Object.prototype.hasOwnProperty.call(extras,'artProject');
  const original=present ? cloneArtProject(extras.artProject) : undefined;
  const abort=new AbortController(); lifetime?.add(()=>abort.abort());
  let next; try { next=await openArtProjectDialog({value:original,present,current,accountId,signal:abort.signal,selectReference:selectArtProjectReference,checkReference:checkArtProjectReference}); } finally { abort.abort(); }
  if (next===null) return;
  if (!current()) throw new Error('The image changed; reopen metadata.');
  if (present && artProjectEqual(original,next)) return;
  const prepared=cloneArtProject(next), token=_sceneAuthority.beginGesture(_gestureView,'project-metadata');
  if (!token) throw new Error('Finish the current image edit first.');
  try {
    if (!current()) throw new Error('The image changed; reopen metadata.');
    _coreHistory.save('Project metadata');
    state._editableExtras={...(state._editableExtras || {}),artProject:prepared};
    state.editEpoch+=1; _markSceneChanged(); _schedulePersist();
  } finally { _sceneAuthority.endGesture(token); }
}

// ────────── Persistent edit drafts (server-backed) ──────────
// The previous implementation keyed drafts by Files image id in
// localStorage; that meant blank-canvas sessions silently lost work and
// drafts couldn't roam between devices. We now hold a server-side draft
// row identified by a uuid (`state.draftId`) and PUT updates to it on a
// debounced timer. `state.imageId` (Files image id) is still tracked separately
// for "save back to the original photo" behaviour.
const PERSIST_DEBOUNCE_MS = 800;
const THUMB_MAX = 160;

function _schedulePersist() {
  if (!legacy) return;
  if (!state.editorOpen || !state.layers.length) return;
  if (state.persistTimer) clearTimeout(state.persistTimer);
  state.persistTimer = setTimeout(() => { state.persistTimer = null; _persistDraft(); }, PERSIST_DEBOUNCE_MS);
}

function _editableMetadata(node) {
  return editableImageMetadata(node);
}

function _buildDraftPayload() {
  return { ..._serializeEditableState(), imageId: state.imageId, ...(state._legacyProjectPending ? {_imageProjectOperation:state._legacyProjectPending} : {}) };
}

function _buildThumbnail() {
  return _buildThumbnailImpl(state.layers, state.imgWidth, state.imgHeight, state.layerOffsets, THUMB_MAX, 0.6);
}

async function _persistDraft(pendingCopy = null) {
  if (!state.editorOpen || !state.layers.length) return;
  if (legacy && _activeFilterPreview) { _activeFilterPreview.deferredDraft = true; return; }
  if (!legacy) return sessionPersistence.saveDraft(_captureManagedSaveSnapshot(),pendingCopy,_buildThumbnail());
  const origin = _captureOrigin();
  const content = { name: state.draftName || 'Untitled', source_image_id: state.imageId || null, width: state.imgWidth, height: state.imgHeight, payload: _buildDraftPayload() };
  if (!legacy) content.payload._imageSessionRecovery = {...session.captureTarget(), draftId:null};
  if (!legacy && pendingCopy) content.payload._imagePendingCopy = pendingCopy;
  const snapshot = Object.freeze({ origin, draftId: state.draftId, editEpoch: state.editEpoch, sceneRevision: state.sceneRevision, name: content.name, imageId: content.source_image_id,
    body: JSON.stringify({ ...content, thumbnail: _buildThumbnail() }) });
  let flight = _draftFlights.get(origin.generation);
  if (!flight) { flight = { tail: Promise.resolve(), draftId: snapshot.draftId }; _draftFlights.set(origin.generation, flight); }
  const run = async () => {
    if (!_accountCurrent(origin)) return { status: 'superseded' };
    if (flight.uncertain) throw new Error('draft creation outcome requires reconciliation');
    let id = flight.draftId;
    let res;
    try { res = await fetch(id ? `/api/editor-drafts/${encodeURIComponent(id)}` : '/api/editor-drafts', {
      method: id ? 'PUT' : 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: snapshot.body,
    }); } catch (error) { if (!id) flight.uncertain = true; throw error; }
    if (id && res.status === 404 && _accountCurrent(origin)) {
      id = null;
      try { res = await fetch('/api/editor-drafts', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: snapshot.body }); }
      catch (error) { flight.uncertain = true; throw error; }
    }
    if (!res.ok) { if (!id && res.status >= 500) flight.uncertain = true; throw new Error(`draft save failed (${res.status})`); }
    let receipt;
    try { receipt = await res.json(); } catch (error) { if (!id) flight.uncertain = true; throw error; }
    if (id == null) {
      if (typeof receipt?.id !== 'string' || !receipt.id.trim()) { flight.uncertain = true; throw new Error('draft creation receipt is missing an ID; reconciliation required'); }
      flight.draftId = receipt.id;
    }
    if (_originCurrent(origin)) {
      if (state.draftId === snapshot.draftId || state.draftId === id || state.draftId === flight.draftId) state.draftId = flight.draftId;
      state.persistDirty = state.sceneRevision !== snapshot.sceneRevision || (state.draftName || 'Untitled') !== snapshot.name || (state.imageId || null) !== snapshot.imageId;
      if (state.persistDirty && state.editorOpen) _schedulePersist();
    }
    return {status:'saved', receipt, origin, editEpoch:snapshot.editEpoch, sceneRevision:snapshot.sceneRevision};
  };
  const result = flight.tail.catch(() => {}).then(run);
  flight.tail = result;
  state.persistInFlight = result;
  result.catch(error => console.warn('[ge] draft save failed', error)).finally(() => {
    if (_originCurrent(origin) && state.persistInFlight === result) state.persistInFlight = null;
    if (flight.tail === result && !flight.uncertain) _draftFlights.delete(origin.generation);
  });
  return result;
}

async function _loadDraftById(draftId) {
  if (!draftId) return null;
  try {
    const res = await fetch(`/api/editor-drafts/${encodeURIComponent(draftId)}`, {
      credentials: 'same-origin',
    });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`draft lookup failed (${res.status})`);
    const out = await res.json();
    if (!out || !out.payload || !Array.isArray(out.payload.layers)) return null;
    return out;
  } catch (_) {
    return null;
  }
}

async function _findDraftForImage(imageId) {
  if (!imageId) return null;
  try {
    const res = await fetch('/api/editor-drafts', { credentials: 'same-origin' });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`draft lookup failed (${res.status})`);
    const out = await res.json();
    const match = (out.drafts || []).find(d => d.source_image_id === imageId);
    if (!match) return null;
    return _loadDraftById(match.id);
  } catch (error) {
    throw error;
  }
}

async function _clearDraftServer(draftId) {
  if (!draftId) return;
  try {
    await fetch(`/api/editor-drafts/${encodeURIComponent(draftId)}`, {
      method: 'DELETE', credentials: 'same-origin',
    });
  } catch (_) { /* best-effort */ }
}

// Hydrate state.layers from a previously-persisted draft. Accepts either the
// raw payload (v1 localStorage shape) or a server response with
// {payload: {...}}. Returns a promise that resolves once every layer's
// dataURL has decoded into its canvas.
async function _restoreDraft(draft, expectedGeneration = _openGeneration, expectedOrigin = _captureOrigin(), validateTarget = () => true, allowRetainedResults = false, importRevision = null) {
  _feedbackSourceWitness = null;
  expectedGeneration ??= _openGeneration;
  const data = draft.payload && draft.payload.layers ? draft.payload : draft;
  if (!allowRetainedResults && data._imageRetainedResults?.length) throw new Error('Retained image results require their original session recovery');
  if (!Array.isArray(data.layers) || !data.layers.length || !Number.isInteger(data.imgWidth)
    || !Number.isInteger(data.imgHeight) || data.imgWidth <= 0 || data.imgHeight <= 0) {
    throw new Error('Invalid editable project dimensions or layers');
  }
  const identities = new Set();
  let importPixels=0, importEncoded=0;
  const checkImport = () => { if (importRevision !== null && (!state.editorOpen || expectedGeneration !== _openGeneration || !_originCurrent(expectedOrigin) || !validateTarget() || !_sceneAuthority.canApplyAt(importRevision))) throw new Error('Image import origin changed'); };
  if (importRevision !== null && (data.imgWidth>CANVAS_MAX_IMPORT_DIMENSION || data.imgHeight>CANVAS_MAX_IMPORT_DIMENSION || data.imgWidth*data.imgHeight>CANVAS_MAX_IMPORT_PIXELS)) throw new Error('Image project dimensions exceed import limits');
  if (importRevision !== null && (data._imageHostOriginal || data._imageSessionRecovery || data._imageProjectOperation || data._imagePendingCopy || data._imageAttachmentApply || data._imageSourceProvenance || (data._imageRetainedResults != null && (!Array.isArray(data._imageRetainedResults) || data._imageRetainedResults.length)))) throw new Error('This draft has recovery operations that require its original session');
  async function decode(node, fallbackW, fallbackH) {
    checkImport();
    const id = String(node.id ?? '');
    if (!id || identities.has(id)) throw new Error('Missing or duplicate layer/mask identity');
    identities.add(id);
    const width = node.canvasW || fallbackW, height = node.canvasH || fallbackH;
    if (importRevision !== null) {
      const payloads=[node.dataUrl,...(node.inpaintSource?.v===1 ? ['ai','base','mask'].map(key=>node.inpaintSource[key]?.dataUrl) : [])];
      const dimensions=[[width,height],...(node.inpaintSource?.v===1 ? ['ai','base','mask'].map(key=>[node.inpaintSource[key]?.width,node.inpaintSource[key]?.height]) : [])];
      for (const [w,h] of dimensions) { if (!Number.isSafeInteger(w) || !Number.isSafeInteger(h) || w<1 || h<1 || w>CANVAS_MAX_IMPORT_DIMENSION || h>CANVAS_MAX_IMPORT_DIMENSION) throw new Error('Image project dimensions exceed import limits'); importPixels+=w*h; }
      for (const payload of payloads) { if (typeof payload!=='string') throw new Error('Image project pixels are unavailable'); importEncoded+=payload.length; }
      if (importPixels>CANVAS_MAX_IMPORT_PIXELS || importEncoded>Math.ceil(CANVAS_MAX_IMPORT_BYTES/3)*4+payloads.length*64) throw new Error('Image project exceeds import limits');
    }
    const image = await new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('Project pixels could not be decoded'));
      if (!String(node.dataUrl || '').startsWith('data:image/')) return reject(new Error('Project pixels are unavailable'));
      img.src = node.dataUrl;
    });
    checkImport();
    if (image.naturalWidth !== width || image.naturalHeight !== height) throw new Error('Project pixel dimensions disagree');
    const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
    const inpaintSource=await decodeInpaintSidecar(node.inpaintSource,document);
    checkImport();
    const masks = await Promise.all((node.masks || []).map(mask => decode(mask, data.imgWidth, data.imgHeight)));
    if (node.activeMaskId != null && !masks.some(mask => mask.id === node.activeMaskId)) throw new Error('Project active mask is unavailable');
    return { ..._editableMetadata(node), _editableExtras: { ...node }, id: node.id, canvas, ctx, masks, inpaintSource,
      visible: node.visible !== false, opacity: typeof node.opacity === 'number' ? node.opacity : 1,
      locked: !!node.locked, activeMaskId: node.activeMaskId ?? null };
  }
  // Prepare every canvas before changing the open document. One bad layer
  // rejects the restore and cannot become a successful reopen receipt.
  const layers = await Promise.all(data.layers.map(layer => decode(layer, data.imgWidth, data.imgHeight)));
  checkImport();
  if (!state.editorOpen || expectedGeneration !== _openGeneration || !_originCurrent(expectedOrigin) || !validateTarget()) throw new Error('Project restore superseded');
  if (legacy && data._imageProjectOperation) {
    const pending=data._imageProjectOperation;
    if (Object.keys(pending).sort().join(',')!=='kind,operationId,owner,phase,projectId,provider,resourceId' || !/^imps:[0-9a-f-]{36}$/.test(pending.operationId || '') || !['create','save','import'].includes(pending.kind) || pending.phase!=='issued' || pending.owner!==expectedOrigin.owner || pending.provider!==state.managedProvider || pending.resourceId!==state.managedResourceId || pending.projectId!==state.projectId) throw new Error('Project operation recovery source changed');
    state._legacyProjectPending=Object.freeze(pending);
  }
  if (data.activeLayerId != null && !layers.some(layer => layer.id === data.activeLayerId)) throw new Error('Project active layer is unavailable');
  const offsets=new Map(layers.map((layer,index)=>[layer.id,{...(data.layers[index].offset || {x:0,y:0})}]));
  const nextLayerId=data.nextLayerId || (Math.max(0,...[...identities].map(id=>Number(id.match(/(\d+)$/)?.[1]) || 0))+1);
  const inpaintLayer=layers.find(layer=>layer.id===data.lastInpaintLayerId && layer.inpaintSource && ['ai','base','mask'].every(key=>layer.inpaintSource[key].width===data.imgWidth && layer.inpaintSource[key].height===data.imgHeight) && layer.canvas.width===data.imgWidth && layer.canvas.height===data.imgHeight);
  const extras={...data}; for (const key of ['layers','_imageHostPixelsPending','_imageHostPixelsSaved','_imageHostOriginal','_imageSessionRecovery','_imageProjectOperation','_imagePendingCopy','_imageSceneRevision','_imageRetainedResults','_imageAttachmentApply','_imageSourceProvenance']) delete extras[key];
  const maskCanvas=document.createElement('canvas'); maskCanvas.width=data.imgWidth; maskCanvas.height=data.imgHeight; const maskCtx=maskCanvas.getContext('2d');
  if (!maskCtx) throw new Error('Image project canvas is unavailable');
  const activeId=data.activeLayerId ?? layers[layers.length-1].id, active=layers.find(layer=>layer.id===activeId), activeMask=active?.masks?.find(mask=>mask.id===active.activeMaskId);
  checkImport();
  const importToken=importRevision === null ? null : _sceneAuthority.beginGesture(_gestureView,'import-project');
  if (importRevision !== null && !importToken) throw new Error('Another image edit is active');
  try {
  state.imgWidth=data.imgWidth; state.imgHeight=data.imgHeight;
  state.maskCanvas=activeMask?.canvas || maskCanvas; state.maskCtx=activeMask?.ctx || maskCtx;
  state.layers = layers;
  state.layerOffsets=offsets;
  state.nextLayerId = nextLayerId;
  state.activeLayerId = activeId;
  state.lastInpaintLayerId=inpaintLayer?.id || null;
  state._editableExtras = extras;
  if (importRevision !== null) { state.undoStack=[]; state.redoStack=[]; }
  _markSceneChanged();
  } finally { if (importToken) _sceneAuthority.endGesture(importToken); }
  try { if (state.mainCanvas) { state.mainCanvas.width=data.imgWidth; state.mainCanvas.height=data.imgHeight; } _syncInpaintControls(); }
  catch (error) { console.error('[imps] project view refresh failed',error); uiModule?.showToast?.('Image imported, but its view could not be refreshed.'); }
  return { decoded: true, layerIds: layers.map(layer => String(layer.id)), payloadCount: identities.size };
}

// Used both by the fresh openEditor path and by _restoreDraft. The full
// _initCanvas in openEditor is closure-scoped, so factored out here.
function _initCanvasFromDims(w, h) {
  state.imgWidth = w;
  state.imgHeight = h;
  if (state.mainCanvas) {
    state.mainCanvas.width = w;
    state.mainCanvas.height = h;
  }
  state.maskCanvas = document.createElement('canvas');
  state.maskCanvas.width = w;
  state.maskCanvas.height = h;
  state.maskCtx = state.maskCanvas.getContext('2d');
}

function _restoreViewAfterHistory(snap, dimsChanged) {
  if (snap.imgWidth && snap.imgHeight) {
    if (state.mainCanvas) { state.mainCanvas.width = snap.imgWidth; state.mainCanvas.height = snap.imgHeight; }
  }
  // Repoint the global mask plumbing at the active parent's active
  // mask sub-layer (if any) — undo can swap the actual canvas object.
  {
    const m = _getActiveMaskLayer();
    if (m) { state.maskCanvas = m.canvas; state.maskCtx = m.ctx; }
    else { state.maskCanvas = null; state.maskCtx = null; }
  }
  composite();
  _renderLayerPanel();
  _syncToolClearIndicators();
  _syncInpaintControls();
  // Refit the viewport when canvas size changed (crop undo/redo) so the
  // user sees the full restored image, not the zoomed-in upper-left
  // corner left over from the previous fit.
  if (dimsChanged) _fitZoom();
  // Update the topbar canvas-size badge directly (the helper is scoped
  // inside _buildEditor, so we touch the DOM here).
  const sizeLabel = state.container?.querySelector(':is(#ge-canvas-size,[data-ge-id="ge-canvas-size"])');
  if (sizeLabel) sizeLabel.textContent = `${state.imgWidth}×${state.imgHeight}`;
}

function undo() { return _sceneAuthority.undo(); }
function redo() { return _sceneAuthority.redo(); }

// Jump to any state in the labeled history. Negative offsets go back
// (into state.undoStack), positive go forward (into state.redoStack). 0 = current.
// Used by the history panel.
// History panel — full implementation in editor/history-panel.js.
// Wrappers preserve the legacy names that the topbar History button
// + undo/redo paths already reference.
let _historyPanel;
function _initHistoryPanel(container) { _historyPanel = createHistoryPanel({ scopeIds, undo, redo, state, root: container, lifetime: () => _coreLifetime, isFocused: _coreFocused, modalManager }); }
const _jumpToHistory = (...args) => _historyPanel?.jumpToHistory(...args);
const _toggleHistoryPanel = (...args) => _historyPanel?.toggleHistoryPanel(...args);
const _refreshHistoryPanelIfOpen = (...args) => _historyPanel?.refreshHistoryPanelIfOpen(...args);

// _relTime lives in editor/layer-helpers.js.

// ── Canvas event helpers ──

// _canvasCoords lives in editor/canvas-coords.js — see import at top.

// ── Drawing ──

function _beginDraw(e) {
  // Fall back to the parent resolver so a stale activeLayerId doesn't
  // block strokes when there ARE layers present.
  const layer = activeLayer() || _activeParentLayer();
  // Transform-tool drag (handle grab or move-fallback) — handler in
  // editor/tools/transform-drag.js.
  if (_transformDragTool.tryBegin(e)) return;
  // Magic wand is selection-only — works even on locked layers because
  // it doesn't mutate the layer until an action (Erase/Copy) is taken.
  // Full implementation in editor/tools/wand.js.
  if (state.tool === 'wand') return _wandTool.click(e);
  if (state.tool === 'sam') return _runSamSelection(e);
  // Inpaint can create its own layer + mask on the fly, so skip the
  // "no active layer → bail" gate for it specifically.
  if (state.tool !== 'inpaint' && (!layer || layer.locked)) return;
  // Keep activeLayerId in sync so downstream lookups resolve.
  if (layer && state.activeLayerId !== layer.id && !_withSceneMutation('active-layer',() => {state.activeLayerId = layer.id; _markSceneChanged(); return true;})) return;
  if (state.tool === 'move') return _beginMove(e);
  if (state.tool === 'crop') return _beginCrop(e);
  if (state.tool === 'lasso') return _beginLasso(e);
  // Clone-stamp — source pick + stroke start. Full implementation in
  // editor/tools/clone.js; per-sample stamping continues through the
  // shared `_strokeTo` pipeline below.
  if (state.tool === 'clone') return _cloneTool.begin(e);
  // Brush / Eraser / Inpaint share a stroke pipeline — handler in
  // editor/tools/stroke.js. Returns true for those tools, false
  // otherwise (any other tool that reached here is a no-op).
  _strokeTool.tryBegin(e);
}

function _continueDraw(e) {
  // _continueDraw is now bound to the window so drags can extend past
  // the canvas. The brush-cursor overlay should only follow the cursor
  // when it's actually over the canvas, otherwise hide it.
  const overCanvas = state.mainCanvas && e.target === state.mainCanvas;
  if (['eraser', 'inpaint', 'lasso', 'brush'].includes(state.tool) && state.mainCanvas) {
    if (overCanvas) _updateBrushCursor(e);
    else if (state.cursorEl) state.cursorEl.style.display = 'none';
  }
  // Transform-tool hover-cursor + handle drag — handler in
  // editor/tools/transform-drag.js. Returns true when the drag is
  // consuming the event (rotation / resize); the hover-cursor pass
  // returns false so the dispatcher can still fall through to other
  // tools that share the canvas hover (none currently, but kept for
  // future-proofing).
  if (_transformDragTool.tryContinue(e)) return;
  if (state.lassoActive) return _continueLasso(e);
  if (!state.drawing) {
    if (state.moving) return _continueMove(e);
    if (state.cropping || state.cropMoving) return _continueCrop(e);
    return;
  }
  // In-progress stroke (brush / eraser / inpaint / clone) — handler in
  // editor/tools/stroke.js.
  _strokeTool.tryContinue(e);
}

function _endDraw() {
  // Transform-tool drag end — handler in editor/tools/transform-drag.js.
  if (_transformDragTool.tryEnd()) return;
  if (state.lassoActive) return _endLasso();
  if (state.moving) return _endMove();
  if (state.cropping || state.cropMoving) return _endCrop();
  // Stroke end (brush / eraser / inpaint / clone) — handler in
  // editor/tools/stroke.js.
  _strokeTool.tryEnd();
}

// Floating popup that appears after an inpaint stroke so the user can
// type a prompt and Generate without diverting to the side panel. The
// popup re-uses the existing #ge-inpaint-prompt and #ge-inpaint-run
// elements by reparenting them into a positioned wrapper, so all the
// existing handlers (Enter to submit, generate-button click) still fire.
// Inpaint-stroke prompt popup feature was removed — the user types in
// the side panel and hits Generate there. Helpers _showInpaintPrompt /
// _dismissInpaintPrompt and their dismiss-handlers were dead code and
// have been deleted.

// Clone Stamp painter — stamps circular samples from the source
// snapshot at every interpolated point between the last brush position
// and the current one, so a drag produces a continuous clone. The
// sample offset is fixed at stroke-start (Photoshop "aligned" mode):
// `sample = source + (cursor − strokeStart)`.
// Stroke pipeline — paints one segment last→current onto the active
// layer (or active mask sub-layer). Full implementation in
// editor/stroke-pipeline.js.
let _strokePipeline;
function init_strokePipeline() { _strokePipeline = createStrokePipeline({ state, root: state.container, lifetime: _coreLifetime,
  // Use the fallback-capable parent resolver so the stroke pipeline
  // and _getActiveMaskLayer() agree on which layer is active. Plain
  // activeLayer() returns null when activeLayerId is stale, which made
  // strokeTo bail even though a mask had been created on the fallback
  // parent — the "inpaint draws nothing" bug.
  activeLayer: () => activeLayer() || _activeParentLayer(),
  getActiveMaskLayer: () => _getActiveMaskLayer(),
  composite,
}); }
const _strokeTo = (...args) => _strokePipeline?.strokeTo(...args);
const _cloneStrokeTo = (...args) => _strokePipeline?.cloneStrokeTo(...args);

// ── Brush cursor overlay ──


function _updateBrushCursor(e) {
  if (!state.mainCanvas) return;
  const rect = state.mainCanvas.getBoundingClientRect();
  const clientX = e.touches ? e.touches[0].clientX : e.clientX;
  const clientY = e.touches ? e.touches[0].clientY : e.clientY;

  if (!state.cursorEl) {
    state.cursorEl = document.createElement('div');
    state.cursorEl.className = 'ge-brush-cursor';
    _editorPortal?.appendChild(state.cursorEl);
  }

  // Lasso uses the feather radius (or a sensible default) so the circle
  // shows the area that will be selected. Other tools use the brush size.
  let basePx;
  if (state.tool === 'lasso') {
    const f = parseInt(_editorFind('ge-lasso-feather')?.value || '0');
    basePx = Math.max(10, f * 2);
  } else {
    basePx = state.brushSize;
  }
  const diameter = basePx * state.zoom;
  state.cursorEl.style.width = diameter + 'px';
  state.cursorEl.style.height = diameter + 'px';
  state.cursorEl.style.left = (clientX - diameter / 2) + 'px';
  state.cursorEl.style.top = (clientY - diameter / 2) + 'px';
  state.cursorEl.style.display = '';
  if (state.tool === 'inpaint') {
    // Visual cue for paint vs erase mode. Ctrl+Alt held mid-hover also
    // flips the cursor so the user sees the effective mode before they
    // click. Red = paint mask, white-dashed = erase mask.
    const flip = e && e.ctrlKey && e.altKey;
    const eraseEffective = flip ? !state.inpaintEraseMode : state.inpaintEraseMode;
    if (eraseEffective) {
      state.cursorEl.style.borderColor = 'rgba(255,255,255,0.9)';
      state.cursorEl.style.background = 'rgba(255,255,255,0.10)';
      state.cursorEl.style.borderStyle = 'dashed';
    } else {
      state.cursorEl.style.borderColor = 'rgba(255,80,80,0.8)';
      state.cursorEl.style.background = 'rgba(255,50,50,0.25)';
      state.cursorEl.style.borderStyle = 'solid';
    }
  } else if (state.tool === 'lasso') {
    state.cursorEl.style.borderColor = 'rgba(255,255,255,0.85)';
    state.cursorEl.style.background = 'rgba(0,0,0,0.15)';
    state.cursorEl.style.borderStyle = 'solid';
  } else {
    state.cursorEl.style.borderColor = state.tool === 'eraser' ? 'rgba(255,255,255,0.6)' : state.color;
    state.cursorEl.style.background = 'transparent';
    state.cursorEl.style.borderStyle = 'solid';
  }
}

// ── Move tool ──

// Move tool — full implementation lives in editor/tools/move.js. Wrap
// `_beginMove` / `_continueMove` / `_endMove` to the factory output so
// the existing dispatcher (_beginDraw / _continueDraw / _endDraw) keeps
// working without changes.
let _moveTool;
function init_moveTool() { _moveTool = createMoveTool({ state, root: state.container, lifetime: _coreLifetime, activeLayer, saveState: _saveState, composite, markSceneChanged: _markSceneChanged, gesture:_moveGesture, sceneRevision:()=>_sceneAuthority.revision, borrowTransform:layer=>_transformSession?.borrowMove(layer) }); }
const _beginMove = (...args) => _moveTool?.begin(...args);
const _continueMove = (...args) => _moveTool?.drag(...args);
const _endMove = (...args) => _moveTool?.end(...args);

// ── Crop tool ──

// Crop tool — full implementation in editor/tools/crop.js. Wire
// `_beginCrop` / `_continueCrop` / `_endCrop` to the factory output so
// the existing dispatcher keeps working without changes.
let _cropTool;
function init_cropTool() { _cropTool = createCropTool({ state, root: state.container, lifetime: _coreLifetime, composite, showCropApply: () => _showCropApply(), capturePreview:() => {_cropPreview=_previewOrigin();} }); }
const _beginCrop = (...args) => _cropTool?.begin(...args);
const _continueCrop = (...args) => _cropTool?.drag(...args);
const _endCrop = (...args) => _cropTool?.end(...args);

function _showCropApply() {
  let pop = state.container.querySelector('.ge-crop-apply');
  if (pop) pop.remove();
  // A small floating panel: W × H inputs and the Apply button.
  pop = document.createElement('div');
  pop.className = 'ge-crop-apply';
  pop.innerHTML = `
    <input type="number" class="ge-crop-w" min="1" max="20000" value="${Math.round(state.cropRect.w)}" title="Width">
    <span class="ge-crop-x">×</span>
    <input type="number" class="ge-crop-h" min="1" max="20000" value="${Math.round(state.cropRect.h)}" title="Height">
    <button class="ge-crop-apply-btn">Apply</button>
  `;
  const area = state.container.querySelector('.ge-canvas-area');
  if (!area || !state.cropRect || !state.mainCanvas) return;
  area.appendChild(pop);

  pop.querySelector('.ge-crop-apply-btn').addEventListener('click', () => _applyCrop());
  // Editing W/H updates the crop rect anchored at its top-left so the
  // user sees the dimensions live in the overlay.
  const wInput = pop.querySelector('.ge-crop-w');
  const hInput = pop.querySelector('.ge-crop-h');
  const onSize = () => {
    if (!state.cropRect) return;
    const w = Math.max(1, parseInt(wInput.value, 10) || state.cropRect.w);
    const h = Math.max(1, parseInt(hInput.value, 10) || state.cropRect.h);
    state.cropRect = { ...state.cropRect, w, h };
    composite();
  };
  wInput.addEventListener('input', onSize);
  hInput.addEventListener('input', onSize);
  // Enter in either field triggers apply.
  [wInput, hInput].forEach(inp => {
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') _applyCrop(); });
  });

  // Position the panel just outside the bottom-right corner of the
  // crop rectangle (where the user finished dragging), in area coords.
  const canvasRect = state.mainCanvas.getBoundingClientRect();
  const areaRect = area.getBoundingClientRect();
  const scaleX = canvasRect.width / state.mainCanvas.width;
  const scaleY = canvasRect.height / state.mainCanvas.height;
  const localX = (canvasRect.left - areaRect.left) + (state.cropRect.x + state.cropRect.w) * scaleX;
  const localY = (canvasRect.top - areaRect.top) + (state.cropRect.y + state.cropRect.h) * scaleY;
  pop.style.position = 'absolute';
  pop.style.left = (localX + 6) + 'px';
  pop.style.top = (localY + 6) + 'px';
  // Clamp inside the CANVAS image bounds (not just the canvas-area) so
  // the panel doesn't sit on the dark padding around the canvas — it
  // stays anchored over the actual image.
  requestAnimationFrame(() => {
    const bRect = pop.getBoundingClientRect();
    const canvasLeft = canvasRect.left - areaRect.left;
    const canvasTop = canvasRect.top - areaRect.top;
    const canvasRight = canvasLeft + canvasRect.width;
    const canvasBottom = canvasTop + canvasRect.height;
    let nx = parseFloat(pop.style.left) || 0;
    let ny = parseFloat(pop.style.top) || 0;
    if (nx + bRect.width > canvasRight - 4) nx = canvasRight - bRect.width - 4;
    if (ny + bRect.height > canvasBottom - 4) ny = canvasBottom - bRect.height - 4;
    nx = Math.max(canvasLeft + 4, nx);
    ny = Math.max(canvasTop + 4, ny);
    pop.style.left = nx + 'px';
    pop.style.top = ny + 'px';
  });
}

function _applyCrop() {
  if (!state.cropRect) return;
  if (!_previewCurrent(_cropPreview)) {uiModule?.showToast('The image changed; draw the crop again.'); return;}
  return _withSceneMutation('crop',() => {
  const { x, y, w, h } = state.cropRect;
  const cw = Math.round(w);
  const ch = Math.round(h);
  const pixels=state.layers.map(layer => {
    const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
    return {layer,data:layer.ctx.getImageData(x - off.x, y - off.y, cw, ch)};
  });
  _saveState('Crop');
  for (const {layer,data} of pixels) {
    layer.canvas.width = cw;
    layer.canvas.height = ch;
    layer.ctx.putImageData(data, 0, 0);
    state.layerOffsets.set(layer.id, { x: 0, y: 0 });
  }
  state.mainCanvas.width = cw;
  state.mainCanvas.height = ch;
  state.imgWidth = cw;
  state.imgHeight = ch;
  if (state.maskCanvas) { state.maskCanvas.width = cw; state.maskCanvas.height = ch; }
  state.cropRect = null;
  _markSceneChanged();
  const btn = state.container.querySelector('.ge-crop-apply');
  if (btn) btn.remove();
  composite();
  _fitZoom();
  _cropPreview=null;
  });
}

// Text tool was removed from the toolbar; the _placeText implementation
// (and the `state.tool === 'text'` dispatcher branch) used to live here
// but had no remaining call sites and was dead code.

// ── Free Transform (Ctrl+Alt+T) ──

// Transform session — full implementation in
// editor/tools/transform-session.js. Wrappers preserve the legacy
// names that the dispatcher / toolbar / shortcuts already reference.
let _transformSession;
function init_transformSession() { _transformSession = createTransformSession({ scopeIds, state, root: state.container, lifetime: _coreLifetime, markSceneChanged: _markSceneChanged, sceneRevision: () => _sceneAuthority.revision, gesture: _transformGesture,
  activeLayer,
  saveState: _saveState,
  composite,
  fitZoom: () => _fitZoom(),
  drawTransformHandles: () => _drawTransformHandles(),
  showCanvasLoading: (label) => _showCanvasLoading(label),
  hideCanvasLoading: () => _hideCanvasLoading(),
  undo,
  uiModule,
}); }
const _startTransform = (...args) => _transformSession?.startTransform(...args);
const _openTransformPopup = (...args) => _transformSession?.openTransformPopup(...args);
const _closeTransformPopup = (...args) => _transformSession?.closeTransformPopup(...args);
const _reapplyTransform = (...args) => _transformSession?.reapplyTransform(...args);
const _confirmTransform = (...args) => _transformSession?.confirmTransform(...args);
const _cancelTransform = (...args) => _transformSession?.cancelTransform(...args);

// ── Lasso tool ──

// Lasso tool — full implementation in editor/tools/lasso.js.
let _lassoTool;
function init_lassoTool() { _lassoTool = createLassoTool({ state, root: state.container, lifetime: _coreLifetime,
  capturePreview:() => {_lassoPreview=_previewOrigin();},
  composite,
  drawLassoOverlay: () => _drawLassoOverlay(),
  syncToolClearIndicators: () => _syncToolClearIndicators(),
}); }
const _beginLasso = (...args) => _lassoTool?.begin(...args);
const _continueLasso = (...args) => _lassoTool?.drag(...args);
const _endLasso = (...args) => _lassoTool?.end(...args);

// Magic wand — selection-only click handler in editor/tools/wand.js.
let _wandTool;
function init_wandTool() { _wandTool = createWandTool({ state, root: state.container, lifetime: _coreLifetime,
  clearWand:options => _wandClear(options),
  activeLayer,
  saveState: _saveState,
  composite,
  wandHits: (x, y) => _wandHits(x, y),
  runMagicWand: (x, y, mode) => _runMagicWand(x, y, mode),
}); }

// Clone-stamp tool — source-pick + stroke-start handler in
// editor/tools/clone.js. Per-sample stamping still runs through the
// shared stroke pipeline (`_strokeTo`) since clone-mode is detected
// there from state.cloneSourceSnapshot.
let _cloneTool;
function init_cloneTool() { _cloneTool = createCloneTool({ state, root: state.container, lifetime: _coreLifetime,
  gesture: _strokeGesture,
  activeLayer,
  saveState: _saveState,
  strokeTo: (x, y) => _strokeTo(x, y),
  showToast: (msg) => { if (uiModule) uiModule.showToast(msg); },
}); }

// Transform-tool drag interactions (handle picking, rotation, resize)
// in editor/tools/transform-drag.js. The dispatcher calls
// `tryBegin/tryContinue/tryEnd` and short-circuits when they return true.
let _transformDragTool;
function init_transformDragTool() { _transformDragTool = createTransformDragTool({ state, root: state.container, lifetime: _coreLifetime,
  beginMove: (e,options) => _beginMove(e,options),
  composite,
  drawTransformHandles: () => _drawTransformHandles(),
  reapplyTransform: () => _reapplyTransform(),
  getTransformHandle: (x, y) => _getTransformHandle(x, y),
  cursorForHandle: _cursorForHandle,
}); }

// Shared stroke pipeline (brush / eraser / inpaint) in
// editor/tools/stroke.js. Clone reuses tryContinue / tryEnd via the
// shared drawing flag; clone's own begin is in editor/tools/clone.js.
let _strokeTool;
function init_strokeTool() { _strokeTool = createStrokeTool({ state, root: state.container, lifetime: _coreLifetime, markSceneChanged: _markSceneChanged,
  gesture: _strokeGesture,
  saveState: _saveState,
  strokeTo: (x, y) => _strokeTo(x, y),
  composite,
  getActiveMaskLayer: () => _getActiveMaskLayer(),
  activeParentLayer: () => _activeParentLayer(),
  ensureActiveMaskLayer: () => _ensureActiveMaskLayer(),
  createLayer,
  renderLayerPanel: () => _renderLayerPanel(),
  syncToolClearIndicators: () => _syncToolClearIndicators(),
}); }

// Compute the outward-normal offset of the lasso polygon by `grow`
// pixels at each vertex. Lets the Edge stroke slider visually move
// the dashed outline in/out without re-running the mask raster.
// Thin wrapper around the pure helper in editor/tools/lasso-mask.js
// so existing callers using module state stay unchanged.
function _lassoOffsetPoints(grow) {
  return _lassoOffsetPointsImpl(state.lassoPoints, grow);
}

function _drawLassoOverlay() {
  if (state.lassoPoints.length < 3) return;
  // Read live slider values so the overlay shows the actual edge that
  // will be committed: Edge stroke shifts the polygon outline in/out;
  // Feather draws a soft red halo to suggest the alpha fade.
  const featherEl = _editorFind('ge-lasso-feather');
  const growEl = _editorFind('ge-lasso-grow');
  const feather = featherEl ? parseInt(featherEl.value || '0', 10) : 0;
  const grow = growEl ? parseInt(growEl.value || '0', 10) : 0;
  const ringPts = grow ? _lassoOffsetPoints(grow) : state.lassoPoints;
  const tracePath = (pts) => {
    state.mainCtx.beginPath();
    state.mainCtx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) state.mainCtx.lineTo(pts[i].x, pts[i].y);
    state.mainCtx.closePath();
  };
  if (feather > 0) {
    // Concentric outer outlines that fade out, suggesting the feather
    // fade band that will be applied to the mask alpha at commit.
    const rings = 4;
    for (let r = 1; r <= rings; r++) {
      const offset = (feather * r) / rings;
      tracePath(_lassoOffsetPoints(grow + offset));
      state.mainCtx.strokeStyle = `rgba(255, 80, 80, ${0.4 * (1 - r / rings)})`;
      state.mainCtx.lineWidth = 1 / state.zoom;
      state.mainCtx.setLineDash([]);
      state.mainCtx.stroke();
    }
  }
  tracePath(ringPts);
  state.mainCtx.strokeStyle = '#fff';
  state.mainCtx.lineWidth = 1 / state.zoom;
  state.mainCtx.setLineDash([4 / state.zoom, 4 / state.zoom]);
  state.mainCtx.stroke();
  state.mainCtx.setLineDash([]);
  state.mainCtx.fillStyle = 'rgba(255, 80, 80, 0.1)';
  state.mainCtx.fill();
}

function _getLassoPath(ctx) {
  _getLassoPathImpl(ctx, state.lassoPoints);
}

/**
 * Build a feathered selection mask from the current lasso polygon.
 * Implementation lives in editor/tools/lasso-mask.js — this wrapper
 * forwards the current `state.lassoPoints` so existing callers keep
 * working unchanged.
 */
function _buildLassoMask(w, h, offX, offY, feather, grow) {
  return _buildLassoMaskImpl(state.lassoPoints, w, h, offX, offY, feather, grow);
}

// ── Magic Wand ──

// Click-fill from (cx, cy) on the active layer. Builds a binary mask of
// all pixels reachable from the seed whose RGB distance is within
// state.wandTolerance × 4.42 (4.42 ≈ scale factor so tolerance=100 ≈ max).
//
// `mode`:
//   'replace'  (default) — replaces any previous selection
//   'add'      — unions the new region with the existing selection
//   'subtract' — removes the new region from the existing selection
// Cached layer pixel data + dimensions for the wand. `getImageData` is
// the dominant cost when live-retuning tolerance (millions of pixels →
// 50–200 ms per call on a 4K canvas). Invalidated by _invalidateWandCache
// whenever the active layer changes or the editor closes.
// Pristine snapshot of the last Bg-Removed cutout so the Edge cleanup
// sliders can live-rebuild the alpha without re-running the model.
function _invalidateWandCache() { state.wandSrcCache = null; }
function _getWandSource(layer) {
  if (state.wandSrcCache && state.wandSrcCache.layerId === layer.id
      && state.wandSrcCache.w === layer.canvas.width
      && state.wandSrcCache.h === layer.canvas.height) {
    return state.wandSrcCache;
  }
  const w = layer.canvas.width, h = layer.canvas.height;
  state.wandSrcCache = {
    layerId: layer.id, w, h,
    data: layer.ctx.getImageData(0, 0, w, h).data,
  };
  return state.wandSrcCache;
}

// Click-deselect helper: returns true if (cx, cy) lands inside the
// existing wand selection on the same layer. Used by the mousedown
// handler to make a second click "in the selection" toggle it off.
function _wandHits(cx, cy) {
  if (!state.wandMask || !state.wandLayerId) return false;
  const layer = state.layers.find(l => l.id === state.wandLayerId);
  if (!layer) return false;
  const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
  const lx = Math.floor(cx - off.x);
  const ly = Math.floor(cy - off.y);
  if (lx < 0 || ly < 0 || lx >= state.wandMask.width || ly >= state.wandMask.height) return false;
  try {
    const px = state.wandMask.getContext('2d').getImageData(lx, ly, 1, 1).data;
    return px[3] > 128;
  } catch { return false; }
}

function _captureWandOrigin() {const layer=activeLayer(), activeMask=_getActiveMaskLayer(), sourceCanvas=activeMask?.canvas || layer?.canvas; return {..._previewOrigin(),activeMask,sourceCanvas,sourceWidth:sourceCanvas?.width,sourceHeight:sourceCanvas?.height,selection:state.wandMask,seed:state.wandLastSeed};}
function _wandOriginCurrent(value) {return _previewCurrent(value) && value.activeMask===_getActiveMaskLayer() && value.sourceCanvas===(value.activeMask?.canvas || value.layer?.canvas) && value.sourceWidth===value.sourceCanvas?.width && value.sourceHeight===value.sourceCanvas?.height && value.selection===state.wandMask && value.seed===state.wandLastSeed;}
function _runMagicWand(cx, cy, mode = 'replace', opts = {}) {
  const expected=opts.expected || _captureWandOrigin();
  if (!_wandOriginCurrent(expected)) return;
  if (!opts.retune && !opts.deferred) {
    const cleanup = _showWandLoading();
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        try {
          _runMagicWand(cx, cy, mode, { ...opts, expected, deferred: true });
        } finally {
          cleanup();
        }
      });
    });
    return;
  }
  return _withSceneMutation('wand-selection',() => {
  if (!_wandOriginCurrent(expected)) return;
  const layer = activeLayer();
  if (!layer || layer.locked) return;
  // If an active mask sub-layer is selected, the wand operates on the
  // MASK pixels rather than the parent layer's pixels — lets the user
  // click inside / outside an existing mask to select that region for
  // further editing. Mask canvases are doc-sized with no per-layer
  // offset, so the seed coords are used as-is.
  const activeMask = _getActiveMaskLayer();
  const sourceCanvas = activeMask ? activeMask.canvas : layer.canvas;
  const sourceCtx = activeMask ? activeMask.ctx : layer.ctx;
  // Snapshot current state to undo BEFORE mutating the selection, but
  // skip when called via the tolerance slider (`opts.retune`) so dragging
  // the slider doesn't fill the undo stack with intermediate states.
  const off = activeMask ? { x: 0, y: 0 } : (state.layerOffsets.get(layer.id) || { x: 0, y: 0 });
  const lx = Math.floor(cx - off.x);
  const ly = Math.floor(cy - off.y);
  const w = sourceCanvas.width, h = sourceCanvas.height;
  if (lx < 0 || ly < 0 || lx >= w || ly >= h) return;
  // Read pixels from the chosen source. Bypass the cache when sourcing
  // from a mask — masks change frequently and the cache is keyed by
  // parent layer id, not by mask id.
  const src = activeMask
    ? sourceCtx.getImageData(0, 0, w, h).data
    : _getWandSource(layer).data;
  // Pixel-level flood fill lives in editor/tools/flood-fill.js.
  // Returns a mask canvas at (w × h) with white where the fill landed.
  const mask = _floodFillMask(src, w, h, lx, ly, state.wandTolerance);
  if (!mask) return;
  if (!opts.retune) _saveState();
  state.wandLastSeed = { x: cx, y: cy, mode };
  // Merge with existing selection per `mode`. If the existing mask is
  // for a different layer or has different dimensions, treat as replace
  // since merging doesn't make sense across canvases.
  const compatible = state.wandMask && state.wandLayerId === layer.id &&
    state.wandMask.width === mask.width && state.wandMask.height === mask.height;
  if (compatible && mode === 'add') {
    // Union: paint new selection on top of the existing one.
    state.wandMask.getContext('2d').drawImage(mask, 0, 0);
  } else if (compatible && mode === 'subtract') {
    // Difference: erase new selection from the existing one.
    const ec = state.wandMask.getContext('2d');
    ec.save();
    ec.globalCompositeOperation = 'destination-out';
    ec.drawImage(mask, 0, 0);
    ec.restore();
  } else {
    state.wandMask = mask;
    state.wandLayerId = layer.id;
  }
  _markSceneChanged();
  composite();
  _syncToolClearIndicators();
  });
}

async function _runSamSelection(e) {
  const layer = activeLayer();
  if (!layer || layer.locked) {
    if (uiModule) uiModule.showToast('Select an unlocked layer');
    return;
  }
  const coords = _canvasCoords(e, state.mainCanvas);
  const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
  const lx = Math.floor(coords.x - off.x);
  const ly = Math.floor(coords.y - off.y);
  if (lx < 0 || ly < 0 || lx >= layer.canvas.width || ly >= layer.canvas.height) return;

  let mode = state.wandMode || 'replace';
  if (e.shiftKey) mode = 'add';
  else if (e.altKey) mode = 'subtract';

  _cancelSamQuery(false);
  const controller = new AbortController();
  _samAbortController = controller;
  const view=_coreLifetime, origin=_captureOrigin(), unregister=view?.add(() => controller.abort()) || (() => {});
  const cleanup = _showWandLoading();
  try {
    await _requestAndApplySamMask(layer, {
      points: [{ x: lx, y: ly, label: 1 }],
    }, mode, { x: coords.x, y: coords.y }, { signal: controller.signal });
  } catch (err) {
    if (!view?.disposed && _originCurrent(origin) && err?.name !== 'AbortError' && uiModule) {
      uiModule.showToast(err.message || String(err), 7000);
    }
  } finally {
    cleanup();
    unregister();
    if (_samAbortController === controller) _samAbortController = null;
  }
}

async function _runSamTextSelection() {
  const layer = activeLayer();
  if (!layer || layer.locked) {
    if (uiModule) uiModule.showToast('Select an unlocked layer');
    return;
  }
  const input = _editorFind('ge-sam-query');
  const text = (input?.value || '').trim();
  if (!text) {
    if (uiModule) uiModule.showToast('Type an object to find');
    input?.focus();
    return;
  }
  const btn = _editorFind('ge-sam-find');
  const old = btn?.innerHTML;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="ge-btn-ai-mark" aria-hidden="true">✦</span>Finding…';
  }
  _cancelSamQuery(false);
  const controller = new AbortController();
  _samAbortController = controller;
  const view=_coreLifetime, origin=_captureOrigin(), unregister=view?.add(() => controller.abort()) || (() => {});
  const cleanup = _showWandLoading();
  try {
    await _requestAndApplySamMask(layer, { text }, state.wandMode || 'replace', null, { signal: controller.signal });
  } catch (err) {
    if (!view?.disposed && _originCurrent(origin) && err?.name !== 'AbortError' && uiModule) {
      uiModule.showToast(err.message || String(err), 7000);
    }
  } finally {
    cleanup();
    unregister();
    if (_samAbortController === controller) _samAbortController = null;
    if (btn && !view?.disposed && _originCurrent(origin)) {
      btn.disabled = false;
      btn.innerHTML = old || '<span class="ge-btn-ai-mark" aria-hidden="true">✦</span>Find';
    }
  }
}

async function _requestAndApplySamMask(layer, payload, mode, seedPoint, opts = {}) {
  const view=_coreLifetime, origin=_captureOrigin(), revision=_sceneAuthority.revision, width=layer.canvas.width, height=layer.canvas.height, layerId=String(layer.id);
  if (!view || view.disposed || !_originCurrent(origin) || !_sceneAuthority.canApplyAt(revision) || activeLayer()!==layer || !state.layers.includes(layer) || !Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width<1 || height<1 || width>16384 || height>16384 || width*height>67108864 || !layerId || layerId.length>256 || !['replace','add','subtract'].includes(mode)) throw new Error('Finish the current image edit before selecting.');
  const current = () => !view.disposed && !opts.signal?.aborted && _originCurrent(origin) && state.editorOpen && _sceneAuthority.canApplyAt(revision) && state.layers.includes(layer) && activeLayer()===layer && String(layer.id)===layerId && layer.canvas.width===width && layer.canvas.height===height;
  let resultId=null, submitted=false, received=false, applied=false;
  try {
  if (!legacy) resultId=sessionPersistence.reserveResult({kind:'ai',epoch:String(_impsAccountEpoch),generation:revision});
  const image=layer.canvas.toDataURL('image/png').split(',')[1];
  if (resultId) {sessionPersistence.submitResult(resultId); submitted=true;}
  const res = await fetch('/api/image/mask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: opts.signal,
      body: JSON.stringify({
        image,
        ...payload,
      }),
    });
  let data;
  try { data=await res.json(); } catch(error) {if (!res.ok && resultId) {sessionPersistence.rejectResult(resultId); submitted=false;} throw error;}
  if (!res.ok || !data.mask) {
    if (resultId) {sessionPersistence.rejectResult(resultId); submitted=false;}
    throw new Error(data.detail || data.error || `Mask failed (${res.status})`);
  }
  if (resultId) {sessionPersistence.receiveResult(resultId,{bytes:data.mask,mime:'image/png',metadata:{label:'Selection mask',mask:{layerId,width,height,mode}}}); received=true;}
  if (!current()) {if (!view.disposed && _originCurrent(origin)) uiModule?.showToast(resultId ? 'Selection mask retained for this image.' : 'Selection could not be applied; run it again.'); return;}
  if (!data.bbox) {
    throw new Error(data.grounding ? `Found ${data.grounding.label || 'object'}, but SAM returned an empty mask` : 'SAM returned an empty mask');
  }

  const img = new Image();
  await new Promise((resolve, reject) => {
    const finish=error => {opts.signal?.removeEventListener('abort',abort); img.onload=null; img.onerror=null; if (error) reject(error); else resolve();};
    const abort=() => {img.src=''; finish(Object.assign(new Error('Selection retired'),{name:'AbortError'}));};
    if (opts.signal?.aborted) return abort();
    opts.signal?.addEventListener('abort',abort,{once:true});
    img.onload = () => finish();
    img.onerror = () => finish(new Error('Failed to decode mask'));
    img.src = 'data:image/png;base64,' + data.mask;
  });
  if (!current()) {if (!view.disposed && _originCurrent(origin)) uiModule?.showToast(resultId ? 'Selection mask retained for this image.' : 'Selection could not be applied; run it again.'); return;}
  const mask = document.createElement('canvas');
  mask.width = width;
  mask.height = height;
  const mctx = mask.getContext('2d');
  mctx.drawImage(img, 0, 0, mask.width, mask.height);
  const maskData = mctx.getImageData(0, 0, mask.width, mask.height);
  const md = maskData.data;
  for (let i = 0; i < md.length; i += 4) {
    const alpha = md[i]; // server mask is white selected / black unselected
    md[i] = 255;
    md[i + 1] = 255;
    md[i + 2] = 255;
    md[i + 3] = alpha;
  }
  mctx.putImageData(maskData, 0, 0);

  const token=_sceneAuthority.beginGesture(_gestureView,'sam-selection');
  if (!token) {uiModule?.showToast(resultId ? 'Selection mask retained for this image.' : 'Selection could not be applied; run it again.'); return;}
  try {
  _saveState();
  const compatible = state.wandMask && state.wandLayerId === layer.id &&
    state.wandMask.width === mask.width && state.wandMask.height === mask.height;
  if (compatible && mode === 'add') {
    state.wandMask.getContext('2d').drawImage(mask, 0, 0);
  } else if (compatible && mode === 'subtract') {
    const ec = state.wandMask.getContext('2d');
    ec.save();
    ec.globalCompositeOperation = 'destination-out';
    ec.drawImage(mask, 0, 0);
    ec.restore();
  } else {
    state.wandMask = mask;
    state.wandLayerId = layer.id;
  }
  state.wandLastSeed = seedPoint
    ? { x: seedPoint.x, y: seedPoint.y, mode, source: 'sam' }
    : { x: 0, y: 0, mode, source: 'sam-text' };
  state.wandMaskVisible = true;
  _markSceneChanged();
  if (resultId) sessionPersistence.resolveResult(resultId);
  applied=true;
  composite();
  _syncToolClearIndicators();
  if (data.grounding && uiModule) {
    const pct = Math.round((data.grounding.score || 0) * 100);
    uiModule.showToast(`Selected ${data.grounding.label || 'object'}${pct ? ` (${pct}%)` : ''}`, 2500);
  }
  } finally {_sceneAuthority.endGesture(token);}
  } catch(error) {
    if (applied) throw error;
    if (resultId && !received) {try {if (submitted) sessionPersistence.markResultUnknown(resultId); else sessionPersistence.resolveResult(resultId);} catch (_) {}}
    if (received) {if (!view.disposed && _originCurrent(origin)) uiModule?.showToast('Selection mask retained for this image.'); return;}
    throw error;
  }
}

function _showWandLoading() {
  const area = state.container?.querySelector('.ge-canvas-area');
  if (!area) return () => {};
  const overlay = document.createElement('div');
  overlay.className = 'ge-wand-loading';
  let spinner = null;
  try {
    spinner = spinnerModule.createWhirlpool(30);
    spinner.element.style.cssText = 'width:30px;height:30px;margin:0;';
    overlay.appendChild(spinner.element);
  } catch (_) {
    overlay.textContent = 'Selecting...';
  }
  area.appendChild(overlay);
  return () => {
    try { spinner?.destroy?.(); } catch {}
    overlay.remove();
  };
}

// Draw the wand selection as a translucent red overlay, mirroring the
// inpaint-mask visual so users know what's selected.
function _drawWandOverlay() {
  if (!state.wandMask || !state.mainCtx) return;
  const layer = state.layers.find(l => l.id === state.wandLayerId);
  if (!layer) return;
  const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
  // Tint the white mask red, draw at the layer's offset on the main canvas.
  const tint = document.createElement('canvas');
  tint.width = state.wandMask.width;
  tint.height = state.wandMask.height;
  const tc = tint.getContext('2d');
  tc.drawImage(state.wandMask, 0, 0);
  tc.globalCompositeOperation = 'source-in';
  tc.fillStyle = 'rgba(255, 60, 60, 1)';
  tc.fillRect(0, 0, tint.width, tint.height);
  state.mainCtx.save();
  state.mainCtx.globalAlpha = 0.4;
  state.mainCtx.drawImage(tint, off.x, off.y);
  state.mainCtx.globalAlpha = 1;
  state.mainCtx.restore();
}

function _wandClearOwned() {
  state.wandMask = null;
  state.wandLayerId = null;
  state.wandLastSeed = null;
  _invalidateWandCache();
  composite();
  _syncToolClearIndicators();
}
function _wandClear({history=true}={}) {if (!state.wandMask) return false; return _withSceneMutation('wand-clear',() => {if (history) _saveState('Clear selection'); _wandClearOwned(); _markSceneChanged(); return true;});}

// Hover thumbnail — generated by downscaling the layer's canvas into a
// small floating panel. Lives in document.body so panel `overflow:
// hidden` can't clip it. One singleton element, repositioned per hover.
function _showLayerThumb(rowEl, layer) {
  if (!layer || !layer.canvas) return;
  if (!state.layerThumbEl) {
    state.layerThumbEl = document.createElement('div');
    state.layerThumbEl.className = 'ge-layer-thumb';
    _editorPortal?.appendChild(state.layerThumbEl);
  }
  const SIZE = 120;
  // Downscale layer onto a small canvas, preserving aspect.
  const lw = layer.canvas.width, lh = layer.canvas.height;
  const scale = Math.min(SIZE / lw, SIZE / lh);
  const tw = Math.max(1, Math.round(lw * scale));
  const th = Math.max(1, Math.round(lh * scale));
  const c = document.createElement('canvas');
  c.width = tw; c.height = th;
  // Checker bg so transparency reads
  const ctx = c.getContext('2d');
  const tile = 8;
  for (let y = 0; y < th; y += tile) for (let x = 0; x < tw; x += tile) {
    ctx.fillStyle = ((x / tile + y / tile) & 1) ? '#444' : '#333';
    ctx.fillRect(x, y, tile, tile);
  }
  ctx.drawImage(layer.canvas, 0, 0, tw, th);
  state.layerThumbEl.innerHTML = '';
  state.layerThumbEl.appendChild(c);
  // Position to the LEFT of the row so it doesn't cover other layers.
  const r = rowEl.getBoundingClientRect();
  state.layerThumbEl.style.top = Math.max(8, r.top - 4) + 'px';
  state.layerThumbEl.style.right = (window.innerWidth - r.left + 8) + 'px';
  state.layerThumbEl.style.left = '';
  state.layerThumbEl.style.display = 'block';
}
function _hideLayerThumb() {
  if (state.layerThumbEl) state.layerThumbEl.style.display = 'none';
}

// Shift+click on a layer row → use that layer's opaque pixels as a
// wand-style selection. Lifts pixel alpha > 0 into the wand mask so the
// user can immediately Bg-Remove / Erase / Copy through the layer.
function _loadLayerAlphaAsSelection(layer) {return _withSceneMutation('layer-alpha-selection',()=>_loadLayerAlphaAsSelectionOwned(layer));}
function _loadLayerAlphaAsSelectionOwned(layer) {
  if (!layer || !layer.canvas || !state.layers.includes(layer)) return;
  const w = layer.canvas.width, h = layer.canvas.height;
  const src = layer.ctx.getImageData(0, 0, w, h).data;
  const mask = document.createElement('canvas');
  mask.width = w; mask.height = h;
  const mctx = mask.getContext('2d');
  const mdata = mctx.createImageData(w, h);
  for (let i = 0; i < w * h; i++) {
    if (src[i * 4 + 3] > 0) {
      mdata.data[i * 4]     = 255;
      mdata.data[i * 4 + 1] = 255;
      mdata.data[i * 4 + 2] = 255;
      mdata.data[i * 4 + 3] = 255;
    }
  }
  mctx.putImageData(mdata, 0, 0);
  _saveState();
  state.wandMask = mask;
  state.wandLayerId = layer.id;
  state.wandLastSeed = null;
  _markSceneChanged();
  composite();
  if (uiModule) uiModule.showToast('Layer pixels selected');
}

// Invert the active selection: lasso (point list — turn into a polygon
// covering the canvas with the lasso polygon as a hole) or wand (flip
// the mask alpha). Wired to Ctrl+Alt+I.
function _invertSelection() {if (!state.wandMask && state.lassoPoints.length>=3 && !_previewCurrent(_lassoPreview)) {uiModule?.showToast('The image changed; draw the selection again.'); return false;} return _withSceneMutation('invert-selection',_invertSelectionOwned);}
function _invertSelectionOwned() {
  if (state.wandMask && state.wandLayerId) {
    _saveState();
    const w = state.wandMask.width, h = state.wandMask.height;
    const ctx = state.wandMask.getContext('2d');
    const data = ctx.getImageData(0, 0, w, h);
    const d = data.data;
    for (let i = 0; i < d.length; i += 4) {
      const a = d[i + 3] > 128 ? 0 : 255;
      d[i] = 255; d[i + 1] = 255; d[i + 2] = 255; d[i + 3] = a;
    }
    ctx.putImageData(data, 0, 0);
    _markSceneChanged();
    composite();
    if (uiModule) uiModule.showToast('Selection inverted');
    return true;
  }
  if (state.lassoPoints.length >= 3 && !state.lassoActive) {
    // Build polygon covering the whole canvas, with the lasso as a hole.
    // Easiest: convert lasso to wand mask, then invert.
    _saveState();
    const w = state.imgWidth, h = state.imgHeight;
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const cctx = c.getContext('2d');
    cctx.fillStyle = '#fff';
    cctx.fillRect(0, 0, w, h);
    cctx.globalCompositeOperation = 'destination-out';
    cctx.beginPath();
    cctx.moveTo(state.lassoPoints[0].x, state.lassoPoints[0].y);
    for (let i = 1; i < state.lassoPoints.length; i++) cctx.lineTo(state.lassoPoints[i].x, state.lassoPoints[i].y);
    cctx.closePath();
    cctx.fill();
    state.wandMask = c;
    state.wandLayerId = state.activeLayerId;
    state.wandLastSeed = null;
    state.lassoPoints = [];
    state.lassoActive = false;
    _markSceneChanged();
    composite();
    if (uiModule) uiModule.showToast('Selection inverted (converted to wand)');
    return true;
  }
  return false;
}

// Convert the wand selection into the inpaint mask, mirroring _lassoToMask.
// Switches to the inpaint tool so the user sees the result right away.
function _wandToMask() {return _withSceneMutation('wand-to-mask',_wandToMaskOwned);}
function _wandToMaskOwned({skipHistory=false}={}) {
  if (!state.wandMask || !state.wandLayerId) return;
  const layer = state.layers.find(l => l.id === state.wandLayerId);
  if (!layer) return;
  if (!skipHistory) _saveState('Selection to mask');
  const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
  // Make the wand's parent active so the mask is attached to it, then
  // get-or-create a mask sub-layer on it. Repoint the global mask
  // plumbing at the new sub-layer's canvas/ctx.
  state.activeLayerId = layer.id;
  const mask = _ensureActiveMaskLayer();
  if (!mask) return;
  state.maskCanvas = mask.canvas;
  state.maskCtx = mask.ctx;
  // Refine the wand mask with the panel's Feather + Edge stroke values
  // before merging into the inpaint mask. Grow/shrink uses the same
  // blur+threshold dilate/erode as the lasso path; feather blurs the
  // result's alpha for a soft edge.
  const wFeather = parseInt(_editorFind('ge-wand-feather')?.value || '0', 10);
  const wGrow = parseInt(_editorFind('ge-wand-grow')?.value || '0', 10);
  let refinedWand = state.wandMask;
  if (wGrow !== 0) {
    const c = document.createElement('canvas');
    c.width = state.wandMask.width; c.height = state.wandMask.height;
    const bctx = c.getContext('2d');
    bctx.filter = `blur(${Math.abs(wGrow)}px)`;
    bctx.drawImage(state.wandMask, 0, 0);
    bctx.filter = 'none';
    const blurred = bctx.getImageData(0, 0, c.width, c.height).data;
    const out = bctx.createImageData(c.width, c.height);
    const od = out.data;
    const thr = wGrow > 0 ? 32 : 200;
    for (let i = 0; i < od.length; i += 4) {
      const a = blurred[i + 3] >= thr ? 255 : 0;
      od[i] = a; od[i + 1] = a; od[i + 2] = a; od[i + 3] = a;
    }
    bctx.putImageData(out, 0, 0);
    refinedWand = c;
  }
  if (wFeather > 0) {
    const c = document.createElement('canvas');
    c.width = refinedWand.width; c.height = refinedWand.height;
    const fctx = c.getContext('2d');
    fctx.filter = `blur(${wFeather}px)`;
    fctx.drawImage(refinedWand, 0, 0);
    fctx.filter = 'none';
    refinedWand = c;
  }
  // Draw the refined wand mask into the inpaint mask canvas at the
  // layer's offset. OR-like merge: any painted pixel in the wand mask
  // is added to the inpaint mask (max alpha wins). Matches the lasso
  // path's semantics.
  const tmp = document.createElement('canvas');
  tmp.width = state.maskCanvas.width;
  tmp.height = state.maskCanvas.height;
  const tctx = tmp.getContext('2d');
  tctx.drawImage(refinedWand, off.x, off.y);
  const incoming = tctx.getImageData(0, 0, tmp.width, tmp.height);
  const cur = state.maskCtx.getImageData(0, 0, state.maskCanvas.width, state.maskCanvas.height);
  for (let i = 0; i < incoming.data.length; i += 4) {
    if (incoming.data[i + 3] > cur.data[i + 3]) {
      cur.data[i]     = 255;
      cur.data[i + 1] = 255;
      cur.data[i + 2] = 255;
      cur.data[i + 3] = incoming.data[i + 3];
    }
  }
  state.maskCtx.putImageData(cur, 0, 0);
  // Stay on the Wand tool — just bake the selection into the mask.
  // Clear the wand selection so a re-click starts fresh (and the red
  // overlay doesn't double up over the inpaint-mask red tint).
  state.wandMask = null;
  state.wandLayerId = null;
  state.wandLastSeed = null;
  mask.visible = true;
  layer.activeMaskId = mask.id;
  state.maskVisible = true;
  _markSceneChanged();
  composite();
  _renderLayerPanel();
  if (uiModule) uiModule.showToast('Selection added to mask');
}

function _autoMatchLastInpaintLayer() {
  const origin=_writerOrigin();
  const layer = state.layers.find(l => l.id === state.lastInpaintLayerId);
  const src = layer?.inpaintSource;
  if (layer?.locked) { uiModule?.showToast('Select an unlocked layer'); return; }
  if (!layer || !src?.base || !src?.mask) {
    if (uiModule) uiModule.showToast('Run inpaint first');
    return;
  }
  const w = state.imgWidth;
  const h = state.imgHeight;
  let baseData, resultData, maskData;
  try {
    baseData = src.base.getContext('2d').getImageData(0, 0, w, h).data;
    resultData = layer.canvas.getContext('2d').getImageData(0, 0, w, h).data;
    maskData = src.mask.getContext('2d').getImageData(0, 0, w, h).data;
  } catch (err) {
    if (uiModule) uiModule.showToast('Auto match failed: cannot read pixels');
    return;
  }

  const inside = { r: 0, g: 0, b: 0, y: 0, n: 0 };
  const outside = { r: 0, g: 0, b: 0, y: 0, n: 0 };
  const step = Math.max(1, Math.round(Math.max(w, h) / 900));
  const radius = Math.max(2, Math.round(Math.min(w, h) * 0.006));
  const sample = (bucket, data, idx) => {
    const r = data[idx], g = data[idx + 1], b = data[idx + 2];
    bucket.r += r; bucket.g += g; bucket.b += b;
    bucket.y += 0.2126 * r + 0.7152 * g + 0.0722 * b;
    bucket.n++;
  };
  const isMasked = (x, y) => {
    if (x < 0 || y < 0 || x >= w || y >= h) return false;
    return maskData[(y * w + x) * 4 + 3] > 24;
  };
  for (let y = radius; y < h - radius; y += step) {
    for (let x = radius; x < w - radius; x += step) {
      const idx = (y * w + x) * 4;
      const m = maskData[idx + 3] > 24;
      let touchesOther = false;
      for (let dy = -radius; dy <= radius && !touchesOther; dy += radius) {
        for (let dx = -radius; dx <= radius; dx += radius) {
          if (!dx && !dy) continue;
          if (isMasked(x + dx, y + dy) !== m) {
            touchesOther = true;
            break;
          }
        }
      }
      if (!touchesOther) continue;
      if (m && resultData[idx + 3] > 24) sample(inside, resultData, idx);
      else if (!m && baseData[idx + 3] > 24) sample(outside, baseData, idx);
    }
  }
  if (inside.n < 20 || outside.n < 20) {
    if (uiModule) uiModule.showToast('Auto match needs a larger mask edge');
    return;
  }
  for (const b of [inside, outside]) {
    b.r /= b.n; b.g /= b.n; b.b /= b.n; b.y /= b.n;
  }
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const dr = clamp((outside.r - inside.r) * 0.55, -55, 55);
  const dg = clamp((outside.g - inside.g) * 0.55, -55, 55);
  const db = clamp((outside.b - inside.b) * 0.55, -55, 55);
  const dy = clamp((outside.y - inside.y) * 0.35, -35, 35);
  const staged = (layer.adjLayers || []).filter(a => a.id !== 'auto-match-color' && a.id !== 'auto-match-light');
  staged.push({
    id: 'auto-match-light',
    type: 'brightness-contrast',
    params: {
      brightness: clamp(1 + (dy / 255), 0.75, 1.25),
      contrast: 1,
    },
    opacity: 0.7,
    visible: true,
  });
  staged.push({
    id: 'auto-match-color',
    type: 'color-balance',
    params: {
      shadows: { r: dr * 0.45, g: dg * 0.45, b: db * 0.45 },
      midtones: { r: dr, g: dg, b: db },
      highlights: { r: dr * 0.35, g: dg * 0.35, b: db * 0.35 },
    },
    opacity: 0.75,
    visible: true,
  });
  return _withSceneMutation('inpaint-auto-match', () => {
    if (!_writerCurrent(origin) || !state.layers.includes(layer) || layer.locked || layer.inpaintSource !== src) return false;
    _saveState('Auto match inpaint color'); layer.adjLayers=staged; layer._adjFinalKey=null;
    _markSceneChanged(); composite(); _renderLayerPanel(); uiModule?.showToast('Auto matched color'); return true;
  });
}

// Reveal/hide the small "X" badge on the Lasso, Wand, and SAM tool buttons
// based on whether each tool currently holds a selection. Called from
// anywhere selection state mutates (wand click, lasso close, undo, etc.).
function _syncToolClearIndicators() {
  // Selection state drives:
  //   (1) the "from-selection" highlight on each layer's Add-mask btn
  //   (2) the visibility of the post-selection refine rows (Feather +
  //       Edge stroke) on the lasso / wand panels.
  //   (3) the topbar Fill button — visible whenever lasso/wand/active
  //       mask gives us a region to fill.
  const lassoHasSel = state.lassoPoints.length >= 3 && !state.lassoActive;
  const wandHasSel = !!state.wandMask;
  const hasMaskTarget = !!_getActiveMaskLayer();
  const hasSel = lassoHasSel || wandHasSel;
  _editorQueryAll('.ge-layer-mask-btn').forEach(b => {
    b.classList.toggle('from-selection', hasSel);
  });
  // Fill action now lives in the Image menu — enable when there's
  // something fillable (selection or active mask).
  const fillItem = _editorFind('ge-image-action-fill');
  if (fillItem) {
    fillItem.disabled = !(hasSel || hasMaskTarget);
    fillItem.title = fillItem.disabled
      ? 'Make a selection or pick a mask first'
      : 'Fill the active selection / mask with the current color';
  }
  // Topbar Selection button only makes sense with an active selection.
  const edgeWrap = _editorFind('ge-edge-wrap');
  if (edgeWrap) edgeWrap.hidden = !hasSel;
  const lFeather = _editorFind('ge-lasso-refine-feather');
  const lGrow = _editorFind('ge-lasso-refine-grow');
  if (lFeather) lFeather.style.display = lassoHasSel ? '' : 'none';
  if (lGrow) lGrow.style.display = lassoHasSel ? '' : 'none';
  const wFeather = _editorFind('ge-wand-refine-feather');
  const wGrow = _editorFind('ge-wand-refine-grow');
  if (wFeather) wFeather.style.display = wandHasSel ? '' : 'none';
  if (wGrow) wGrow.style.display = wandHasSel ? '' : 'none';
  if (!state.container) return;
  const lassoBtn = state.container.querySelector('.ge-tool-btn[data-tool="lasso"]');
  const wandBtn  = state.container.querySelector('.ge-tool-btn[data-tool="wand"]');
  const samBtn   = state.container.querySelector('.ge-tool-btn[data-tool="sam"]');
  const inpaintBtn = state.container.querySelector('.ge-tool-btn[data-tool="inpaint"]');
  if (lassoBtn) lassoBtn.classList.toggle('has-selection', state.lassoPoints.length >= 3 && !state.lassoActive);
  if (wandBtn)  wandBtn.classList.toggle('has-selection', !!state.wandMask);
  if (samBtn)   samBtn.classList.toggle('has-selection', !!state.wandMask);
  // Inpaint no longer carries a clear-X badge; masks live as sub-layers
  // in the layer panel and are deleted from there.
  if (inpaintBtn) inpaintBtn.classList.remove('has-selection');
}

function _hasMaskPixels() {
  if (!state.maskCanvas || !state.maskCtx) return false;
  try {
    const d = state.maskCtx.getImageData(0, 0, state.maskCanvas.width, state.maskCanvas.height).data;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 0) return true;
  } catch (_) {}
  return false;
}

function _wandDeleteSelection() {return _withSceneMutation('wand-delete',_wandDeleteSelectionOwned);}
function _wandDeleteSelectionOwned() {
  if (!state.wandMask) return;
  const layer = state.layers.find(l => l.id === state.wandLayerId);
  if (!layer || layer.locked) return;
  _saveState();
  // Use destination-out with the mask to erase the selected pixels.
  layer.ctx.save();
  layer.ctx.globalCompositeOperation = 'destination-out';
  layer.ctx.drawImage(state.wandMask, 0, 0);
  layer.ctx.restore();
  _wandClearOwned();
  _markSceneChanged();
}

function _wandCopyToNewLayer() {return _withSceneMutation('wand-copy',_wandCopyToNewLayerOwned);}
function _wandCopyToNewLayerOwned() {
  if (!state.wandMask) return;
  const src = state.layers.find(l => l.id === state.wandLayerId);
  if (!src) return;
  _saveState();
  // Clip the source by the mask, put it on a new layer.
  const tmp = document.createElement('canvas');
  tmp.width = src.canvas.width;
  tmp.height = src.canvas.height;
  const tCtx = tmp.getContext('2d');
  tCtx.drawImage(src.canvas, 0, 0);
  tCtx.globalCompositeOperation = 'destination-in';
  tCtx.drawImage(state.wandMask, 0, 0);
  const newLayer = createLayer('Wand copy', src.canvas.width, src.canvas.height);
  newLayer.ctx.drawImage(tmp, 0, 0);
  const srcOff = state.layerOffsets.get(src.id) || { x: 0, y: 0 };
  state.layerOffsets.set(newLayer.id, { ...srcOff });
  const idx = state.layers.findIndex(l => l.id === src.id);
  state.layers.splice(idx + 1, 0, newLayer);
  state.activeLayerId = newLayer.id;
  _markSceneChanged();
  composite();
  _renderLayerPanel();
  _revealLayerPanel();
  if (uiModule) uiModule.showToast('Copied to new layer');
}

function _cutSelectionAtomic(kind, layer, clipped, origin) {
  return _withSceneMutation('cut-selection', () => {
    if (!_writerCurrent(origin) || !state.layers.includes(layer) || layer.locked) return false;
    if (kind === 'wand' && (!state.wandMask || state.wandLayerId !== layer.id)) return false;
    if (kind === 'lasso' && (!_previewCurrent(_lassoPreview) || state.lassoPoints.length < 3)) return false;
    const w=layer.canvas.width,h=layer.canvas.height, staged=document.createElement('canvas'); staged.width=w; staged.height=h;
    const ctx=staged.getContext('2d'); ctx.drawImage(layer.canvas,0,0);
    if (kind === 'wand') { ctx.globalCompositeOperation='destination-out'; ctx.drawImage(state.wandMask,0,0); ctx.globalCompositeOperation='source-over'; }
    else { const off=state.layerOffsets.get(layer.id) || {x:0,y:0}; const feather=parseInt(_editorFind('ge-lasso-feather')?.value || '0'), grow=parseInt(_editorFind('ge-lasso-grow')?.value || '0'); const mask=_buildLassoMask(w,h,off.x,off.y,feather,grow), pixels=ctx.getImageData(0,0,w,h), alpha=mask.getContext('2d').getImageData(0,0,w,h).data; for(let i=0;i<w*h;i++) pixels.data[i*4+3]=Math.round(pixels.data[i*4+3]*(1-alpha[i*4]/255)); ctx.putImageData(pixels,0,0); }
    _saveState('Cut selection');
    if (kind === 'wand') { const copy=createLayer('Wand copy',w,h); copy.ctx.drawImage(clipped,0,0); state.layerOffsets.set(copy.id,{...(state.layerOffsets.get(layer.id)||{x:0,y:0})}); state.layers.splice(state.layers.indexOf(layer)+1,0,copy); state.activeLayerId=copy.id; _wandClearOwned(); }
    else state.lassoPoints=[];
    layer.ctx.clearRect(0,0,w,h); layer.ctx.drawImage(staged,0,0); layer._adjCacheKey=null; layer._adjFinalKey=null;
    _markSceneChanged(); composite(); _renderLayerPanel(); return true;
  });
}
function _clearSelectionAtomic() { return _withSceneMutation('clear-selection',()=>{ if (state.wandMask) { _saveState('Clear selection'); _wandClearOwned(); _markSceneChanged(); } state.lassoPoints=[]; state.lassoActive=false; composite(); return true; }); }

function _lassoDeleteSelection() {if (!_previewCurrent(_lassoPreview)) {uiModule?.showToast('The image changed; draw the selection again.'); return;} return _withSceneMutation('lasso-delete',_lassoDeleteSelectionOwned);}
function _lassoDeleteSelectionOwned() {
  const layer = activeLayer();
  if (!layer || state.lassoPoints.length < 3) return;
  const feather = parseInt(_editorFind('ge-lasso-feather')?.value || '0');
  const grow = parseInt(_editorFind('ge-lasso-grow')?.value || '0');
  _saveState();
  const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
  const w = layer.canvas.width, h = layer.canvas.height;

  const mask = _buildLassoMask(w, h, off.x, off.y, feather, grow);
  const maskData = mask.getContext('2d').getImageData(0, 0, w, h);
  const imgData = layer.ctx.getImageData(0, 0, w, h);

  for (let i = 0; i < w * h; i++) {
    const maskVal = maskData.data[i * 4]; // red channel
    if (maskVal > 0) {
      const fade = maskVal / 255;
      imgData.data[i * 4 + 3] = Math.round(imgData.data[i * 4 + 3] * (1 - fade));
    }
  }
  layer.ctx.putImageData(imgData, 0, 0);
  _markSceneChanged();

  state.lassoPoints = [];
  composite();
  uiModule.showToast('Selection deleted');
}

function _lassoCopyToLayer() {if (!_previewCurrent(_lassoPreview)) {uiModule?.showToast('The image changed; draw the selection again.'); return;} return _withSceneMutation('lasso-copy',_lassoCopyToLayerOwned);}
function _lassoCopyToLayerOwned() {
  const layer = activeLayer();
  if (!layer || state.lassoPoints.length < 3) return;
  const feather = parseInt(_editorFind('ge-lasso-feather')?.value || '0');
  const grow = parseInt(_editorFind('ge-lasso-grow')?.value || '0');
  _saveState();
  const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
  const w = layer.canvas.width, h = layer.canvas.height;

  const mask = _buildLassoMask(w, h, off.x, off.y, feather, grow);
  const newLayer = createLayer('Selection', state.imgWidth, state.imgHeight);

  // Copy layer pixels masked by the selection
  const srcData = layer.ctx.getImageData(0, 0, w, h);
  const maskData = mask.getContext('2d').getImageData(0, 0, w, h);
  const outData = newLayer.ctx.createImageData(w, h);

  for (let i = 0; i < w * h; i++) {
    const maskVal = maskData.data[i * 4];
    if (maskVal > 0) {
      const fade = maskVal / 255;
      outData.data[i * 4] = srcData.data[i * 4];
      outData.data[i * 4 + 1] = srcData.data[i * 4 + 1];
      outData.data[i * 4 + 2] = srcData.data[i * 4 + 2];
      outData.data[i * 4 + 3] = Math.round(srcData.data[i * 4 + 3] * fade);
    }
  }
  newLayer.ctx.putImageData(outData, 0, 0);

  state.layers.push(newLayer);
  _markSceneChanged();
  state.activeLayerId = newLayer.id;
  state.lassoPoints = [];
  _renderLayerPanel();
  _revealLayerPanel();
  composite();
  uiModule.showToast('Selection copied to new layer');
}

function _lassoToMask() {if (!_previewCurrent(_lassoPreview)) {uiModule?.showToast('The image changed; draw the selection again.'); return;} return _withSceneMutation('lasso-to-mask',_lassoToMaskOwned);}
function _lassoToMaskOwned({skipHistory=false}={}) {
  if (state.lassoPoints.length < 3) return;
  if (!skipHistory) _saveState('Selection to mask');
  // Get-or-create a mask sub-layer on the active parent layer and
  // repoint the global mask plumbing at it.
  const mask = _ensureActiveMaskLayer();
  if (!mask) return;
  state.maskCanvas = mask.canvas;
  state.maskCtx = mask.ctx;

  // Fill selection into the mask with feather + grow/shrink applied.
  const feather = parseInt(_editorFind('ge-lasso-feather')?.value || '0');
  const grow = parseInt(_editorFind('ge-lasso-grow')?.value || '0');
  const lassoFill = _buildLassoMask(state.maskCanvas.width, state.maskCanvas.height, 0, 0, feather, grow);
  const maskData = lassoFill.getContext('2d').getImageData(0, 0, state.maskCanvas.width, state.maskCanvas.height);
  const curData = state.maskCtx.getImageData(0, 0, state.maskCanvas.width, state.maskCanvas.height);
  // Merge: add the new selection to existing mask
  for (let i = 0; i < maskData.data.length; i += 4) {
    const val = maskData.data[i];
    if (val > curData.data[i]) {
      curData.data[i] = val;
      curData.data[i + 1] = val;
      curData.data[i + 2] = val;
      curData.data[i + 3] = val;
    }
  }
  state.maskCtx.putImageData(curData, 0, 0);
  _markSceneChanged();

  // Stay on the Lasso tool — just bake the selection into the mask.
  // Keep the lasso points so the user can keep tweaking; clear the
  // active-shape state so the next click starts fresh if they want.
  state.lassoPoints = [];
  composite();
  _renderLayerPanel();
  uiModule.showToast('Selection added to mask');
}

// ── Edge feather ──

// Themed slider modal for filter parameters. Builds a single in-line
// overlay anchored to the canvas-area's centre. `params` is an array
// of `{ key, label, min, max, step, value, suffix }`. As the user
// drags any slider the `onPreview(values)` callback fires for live
// rendering; clicking Apply commits and resolves the returned Promise
// with the final values; Cancel / Esc resolves with null. The caller
// is responsible for snapshotting state BEFORE opening (so Cancel can
// restore the layer's pixels).
function _filterSliderPrompt(title, params, onPreview) {
  return new Promise((resolve) => {
    if (!state.container) { resolve(null); return; }
    const overlay = document.createElement('div');
    overlay.className = 'ge-filter-overlay';
    let rows = '';
    for (const p of params) {
      // Reuse the editor's eraser-row class so the slider picks up the
      // standard slim red-thumb styling instead of the bare browser
      // default. Value chip on the same line as the label.
      rows += `
        <div class="ge-filter-row ge-eraser-row">
          <label>${p.label} <span class="ge-filter-row-value" data-val-for="${p.key}">${p.value}${p.suffix || ''}</span></label>
          <input type="range" data-key="${p.key}" min="${p.min}" max="${p.max}" step="${p.step || 1}" value="${p.value}" />
        </div>
      `;
    }
    overlay.innerHTML = `
      <div class="ge-filter-modal">
        <div class="ge-filter-modal-head">${title}</div>
        ${rows}
        <div class="ge-filter-modal-actions">
          <button type="button" class="ge-btn ge-btn-sm" data-action="cancel">Cancel</button>
          <button type="button" class="ge-btn ge-btn-sm ge-btn-primary" data-action="apply">Apply</button>
        </div>
      </div>
    `;
    state.container.appendChild(overlay);
    const values = {};
    for (const p of params) values[p.key] = p.value;
    // Initial preview render.
    try { onPreview(values); } catch {}
    overlay.querySelectorAll('input[type="range"]').forEach(inp => {
      inp.addEventListener('input', (e) => {
        const k = e.target.dataset.key;
        const v = parseFloat(e.target.value);
        values[k] = v;
        const lbl = overlay.querySelector(`[data-val-for="${k}"]`);
        const param = params.find(p => p.key === k);
        if (lbl) lbl.textContent = v + (param && param.suffix ? param.suffix : '');
        try { onPreview(values); } catch {}
      });
    });
    let settled = false;
    const cleanup = (result) => {
      if (settled) return; settled = true;
      try { overlay.remove(); } catch {}
      document.removeEventListener('keydown', onKey, true);
      resolve(result);
    };
    const onKey = (e) => {
      if (!_coreFocused() && !overlay.contains(document.activeElement)) return;
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cleanup(null); }
      else if (e.key === 'Enter') { e.preventDefault(); cleanup(values); }
    };
    document.addEventListener('keydown', onKey, true);
    _coreLifetime?.add(() => cleanup(null));
    overlay.querySelector('[data-action="apply"]').addEventListener('click', () => cleanup(values));
    overlay.querySelector('[data-action="cancel"]').addEventListener('click', () => cleanup(null));
    // Click outside the modal (on the dim backdrop) = cancel.
    overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(null); });
  });
}

// A preview owns the scene until it either restores its baseline or commits.
let _activeFilterPreview = null;
async function _applyLiveBlur({ title, params, label, renderer }) {
  const origin = _previewOrigin(), layer = activeLayer(), view = _coreLifetime;
  if (!layer || layer.locked) { uiModule?.showToast('Select an unlocked layer'); return; }
  const token = _sceneAuthority.beginGesture(_gestureView, 'filter-preview');
  if (!token) { uiModule?.showToast('Finish the current image edit first.'); return; }
  const w = layer.canvas.width, h = layer.canvas.height, originalCanvas = layer.canvas;
  let snap, release, settled = false, previewed = false;
  const preview = {deferredDraft:false, finish:null}; _activeFilterPreview = preview;
  const ownsBaseline = () => _sceneAuthority.ownsGesture(token) && _sceneAuthority.revision === origin.revision && state.layers.includes(layer) && layer.canvas === originalCanvas && layer.canvas.width === w && layer.canvas.height === h;
  const invalidatePixels = () => { layer._adjCacheKey = null; layer._adjFinalKey = null; _adjPopupSystem?.invalidatePreview(layer); };
  const restore = () => { if (previewed && snap && ownsBaseline()) { layer.ctx.clearRect(0, 0, w, h); layer.ctx.drawImage(snap, 0, 0); invalidatePixels(); previewed = false; if (!view?.disposed) composite(); } };
  const finish = () => { if (settled) return; settled = true; restore(); _sceneAuthority.endGesture(token); if (_activeFilterPreview === preview) _activeFilterPreview = null; if (preview.deferredDraft) _schedulePersist(); };
  preview.finish = finish;
  try {
    snap = document.createElement('canvas'); snap.width = w; snap.height = h;
    snap.getContext('2d').drawImage(layer.canvas, 0, 0);
    release = view?.add(finish);
    const stage = values => { const output = document.createElement('canvas'); output.width = w; output.height = h; renderer(snap, values, output.getContext('2d')); return output; };
    const draw = values => {
      if (settled || !_previewCurrent(origin) || !ownsBaseline()) return;
      const output = stage(values);
      layer.ctx.clearRect(0, 0, w, h); layer.ctx.drawImage(output, 0, 0); invalidatePixels(); previewed = true; composite();
    };
    const result = await _filterSliderPrompt(title, params, draw);
    if (settled || result === null || !_previewCurrent(origin) || !ownsBaseline()) return;
    // Stage first, then snapshot the original pixels exactly once for undo.
    const output = stage(result);
    restore();
    if (!_previewCurrent(origin) || !ownsBaseline()) return;
    _saveState(label);
    layer.ctx.clearRect(0, 0, w, h); layer.ctx.drawImage(output, 0, 0); invalidatePixels();
    _markSceneChanged(); composite(); uiModule?.showToast(label + ' applied');
  } finally {
    finish(); release?.();
  }
}

function _applyGaussianBlur() {
  _applyLiveBlur({
    title: 'Gaussian Blur',
    label: 'Gaussian Blur',
    params: [{ key: 'radius', label: 'Radius', min: 0, max: 100, step: 1, value: 6, suffix: 'px' }],
    renderer: _gaussianBlur,
  });
}

function _applyZoomBlur() {
  _applyLiveBlur({
    title: 'Zoom Blur',
    label: 'Zoom Blur',
    params: [{ key: 'strength', label: 'Strength', min: 1, max: 50, step: 1, value: 15 }],
    renderer: _zoomBlur,
  });
}

function _applyMotionBlur() {
  _applyLiveBlur({
    title: 'Motion Blur',
    label: 'Motion Blur',
    params: [
      { key: 'length', label: 'Length', min: 1, max: 200, step: 1, value: 20, suffix: 'px' },
      { key: 'angle', label: 'Angle', min: -180, max: 180, step: 1, value: 0, suffix: '°' },
    ],
    renderer: _motionBlur,
  });
}

function _applyEdgeFeather(layer, width, hardDelete) {
  return _withSceneMutation('edge-feather', () => {
    if (!state.layers.includes(layer) || layer !== activeLayer() || layer.locked) return false;
    const w = layer.canvas.width, h = layer.canvas.height;
    const imgData = layer.ctx.getImageData(0, 0, w, h);
    _edgeFeather(imgData, width, hardDelete);
    _saveState(hardDelete ? 'Delete edges' : 'Feather edges');
    layer.ctx.putImageData(imgData, 0, 0);
    layer._adjCacheKey = null; layer._adjFinalKey = null;
    _markSceneChanged(); return true;
  });
}

// ── Zoom ──

function _fitZoom() {
  const fit = _getFitZoom();
  if (!fit) return;
  state.zoom = fit;
  _applyZoom();
}

function _getFitZoom() {
  const area = state.container.querySelector('.ge-canvas-area');
  if (!area || !state.imgWidth) return null;
  const pad = 20;
  const maxW = area.clientWidth - pad * 2;
  const maxH = area.clientHeight - pad * 2;
  return Math.min(1, maxW / state.imgWidth, maxH / state.imgHeight);
}

function _applyZoom() {
  if (!state.mainCanvas) return;
  state.mainCanvas.style.width = (state.imgWidth * state.zoom) + 'px';
  state.mainCanvas.style.height = (state.imgHeight * state.zoom) + 'px';
  const label = state.container.querySelector('.ge-zoom-label');
  if (label) label.textContent = Math.round(state.zoom * 100) + '%';
  _syncZoomControls();
  const area = state.container && state.container.querySelector('.ge-canvas-area');
  if (area && area._resetPan) area._resetPan();
}

function _syncZoomControls() {
  const fitBtn = _editorFind('ge-zoom-fit');
  const actualBtn = _editorFind('ge-zoom-100');
  const fit = _getFitZoom();
  const isFit = fit !== null && Math.abs(state.zoom - fit) < 0.001;
  const isActual = !isFit && Math.abs(state.zoom - 1) < 0.001;
  if (fitBtn) {
    fitBtn.classList.toggle('active', isFit);
    fitBtn.setAttribute('aria-pressed', isFit ? 'true' : 'false');
  }
  if (actualBtn) {
    actualBtn.classList.toggle('active', isActual);
    actualBtn.setAttribute('aria-pressed', isActual ? 'true' : 'false');
  }
}

function _positionInpaintPanel(anchorBtn) {
  const panel = _editorFind('ge-inpaint-section');
  if (!panel || window.innerWidth <= 820) return;
  if (panel.dataset.userMoved === '1') {
    panel.classList.add('ge-inpaint-popover');
    return;
  }
  panel.classList.add('ge-inpaint-popover');
  // Anchor to the Layers header on the right panel so the popover
  // appears to slide out from there. The toolbar button on the left
  // shifts around as controls reflow, which was causing the popover
  // to land in different spots on each open and look "jumpy" when the
  // user grabbed it to move. Anchoring to the right panel — which has
  // a stable position — keeps the docked appearance steady.
  const layersHeader = _editorQuery('.ge-layers-header');
  const rightPanel = _editorQuery('.ge-right-panel');
  const ref = layersHeader || rightPanel;
  if (!ref) {
    // Fallback to the old toolbar-button anchor if the layers panel
    // isn't on screen yet.
    const r = anchorBtn?.getBoundingClientRect?.();
    if (!r) return;
    requestAnimationFrame(() => {
      const panelW = panel.offsetWidth || 320;
      const panelH = panel.offsetHeight || 520;
      const left = Math.min(window.innerWidth - panelW - 12, Math.max(12, r.right + 10));
      const top = Math.min(window.innerHeight - panelH - 12, Math.max(12, r.top));
      panel.style.left = `${left}px`;
      panel.style.top = `${top}px`;
    });
    return;
  }
  requestAnimationFrame(() => {
    const refRect = ref.getBoundingClientRect();
    const panelW = panel.offsetWidth || 320;
    const panelH = panel.offsetHeight || 520;
    // Sit immediately to the left of the right panel, top-aligned with
    // the Layers header. 10px gap so it's clearly a separate window
    // and not visually fused with the panel.
    let left = refRect.left - panelW - 10;
    let top = refRect.top;
    // Clamp into the viewport so the popover never leaves the screen.
    left = Math.max(12, Math.min(window.innerWidth - panelW - 12, left));
    top = Math.max(12, Math.min(window.innerHeight - panelH - 12, top));
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
  });
}

function _wireInpaintPopoverWindow() {
  const panel = _editorFind('ge-inpaint-section');
  if (!panel || panel.dataset.windowWired === '1') return;
  panel.dataset.windowWired = '1';
  const closeBtn = _editorFind('ge-inpaint-popover-close');
  closeBtn?.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    panel.classList.add('dismissed');
    panel.style.display = 'none';
    _editorFind('ge-controls')?.classList.remove('ge-inpaint-popover-host');
  });
  const head = panel.querySelector('[data-inpaint-drag]');
  if (!head) return;
  head.addEventListener('pointerdown', (e) => {
    if (window.innerWidth <= 820 || e.target.closest('button')) return;
    e.preventDefault();
    e.stopPropagation();
    panel.classList.add('ge-inpaint-popover');
    const startX = e.clientX;
    const startY = e.clientY;
    const r0 = panel.getBoundingClientRect();
    head.setPointerCapture(e.pointerId);
    head.style.cursor = 'grabbing';
    const onMove = (ev) => {
      const w = panel.offsetWidth || r0.width;
      const h = panel.offsetHeight || r0.height;
      const nx = Math.max(8, Math.min(window.innerWidth - w - 8, r0.left + ev.clientX - startX));
      const ny = Math.max(8, Math.min(window.innerHeight - h - 8, r0.top + ev.clientY - startY));
      panel.dataset.userMoved = '1';
      panel.style.left = `${nx}px`;
      panel.style.top = `${ny}px`;
    };
    const onUp = () => {
      try { head.releasePointerCapture(e.pointerId); } catch {}
      head.style.cursor = '';
      head.removeEventListener('pointermove', onMove);
      head.removeEventListener('pointerup', onUp);
    };
    head.addEventListener('pointermove', onMove);
    head.addEventListener('pointerup', onUp);
  });
}

// ── Build DOM ──

function _buildEditor(container) {
  _closeTransformPopup();
  _coreLifetime?.dispose();
  _coreLifetime = createEditorLifetime();
  const renderLifetime = _coreLifetime;
  let sceneFrame = null, sceneDirty = false, structureDirty = false, historyRestore = null;
  const win=container.ownerDocument.defaultView || window;
  const flush = () => {
    sceneFrame=null; if (renderLifetime.disposed || !state.editorOpen || (!legacy && !isolatedVisible)) return;
    if (historyRestore) { const event=historyRestore; historyRestore=null; _restoreViewAfterHistory(event.snapshot,event.dimsChanged); structureDirty=false; sceneDirty=false; return; }
    if (sceneDirty) {
      if (state.mainCanvas && (state.mainCanvas.width !== state.imgWidth || state.mainCanvas.height !== state.imgHeight)) { state.mainCanvas.width=state.imgWidth; state.mainCanvas.height=state.imgHeight; structureDirty=true; }
      for (const layer of state.layers) _adjPopupSystem?.invalidatePreview(layer);
      composite(); sceneDirty=false;
    }
    if (structureDirty) { const mask=_getActiveMaskLayer(); state.maskCanvas=mask?.canvas || null; state.maskCtx=mask?.ctx || null; _renderLayerPanel(); _refreshHistoryPanelIfOpen(); _syncToolClearIndicators(); _syncInpaintControls?.(); structureDirty=false; }
  };
  const schedule = () => { if (sceneFrame != null || renderLifetime.disposed || (!legacy && !isolatedVisible)) return; sceneFrame=win.requestAnimationFrame(flush); };
  _flushSceneProjection = () => { if (sceneDirty || structureDirty || historyRestore) schedule(); };
  renderLifetime.add(() => { if (sceneFrame != null) win.cancelAnimationFrame(sceneFrame); sceneFrame=null; _flushSceneProjection=()=>{}; });
  renderLifetime.add(_sceneAuthority.subscribeView((_revision,event) => {
    if (event?.kind === 'historyRestore') { historyRestore=event; structureDirty=true; sceneDirty=true; }
    else if (event?.kind === 'historyChanged') structureDirty=true;
    else sceneDirty=true;
    schedule();
  }));
  renderLifetime.add(_sceneAuthority.subscribeGestureEnd(() => { structureDirty=true; sceneDirty=true; schedule(); }));
  openIdentity += 1;
  if (!legacy) {
    const rejectUnauthorized = event => { let ok = false; try { ok = !lifetime.disposed && authority.validateScope(session.captureTarget()) === true; } catch (_) {} if (!ok) { event.preventDefault(); event.stopImmediatePropagation(); } };
    for (const event of ['pointerdown','mousedown','touchstart','click','input','change','keydown']) _coreLifetime.listen(container, event, rejectUnauthorized, true);
  }
  _initCanvasTransforms(container);
  _initHistoryPanel(container);
  _initLayerPanel(container);
  state.container = container;
  _aiLifetime = createEditorLifetime();
  const aiLifetime = _aiLifetime;
  _coreLifetime.add(() => aiLifetime.dispose());
  _coreLifetime.listen(document, 'openclank:auth-context-changed', () => aiLifetime.dispose());
  _initApplyImageTool();
  _initAdjPopupSystem();
  _transformHandles = createTransformHandles({state, root: container, lifetime: _coreLifetime});
  init_strokePipeline();
  init_moveTool();
  init_cropTool();
  init_transformSession();
  init_lassoTool();
  init_wandTool();
  init_cloneTool();
  init_transformDragTool();
  init_strokeTool();
  _ownedPortals.clear();
  _editorPortal = document.createElement('div');
  _editorPortal.className = 'ge-owned-portals';
  _editorPortal.style.display = legacy || isolatedVisible ? 'contents' : 'none';
  if (legacy) document.body.appendChild(_editorPortal);
  _registerEditorPortal(_editorPortal);
  const ownedPortal = _editorPortal;
  _coreLifetime.add(() => { ownedPortal.remove(); _ownedPortals.clear(); if (_editorPortal === ownedPortal) _editorPortal = null; });
  container.innerHTML = '';
  container.className = 'imps-editor';

  // Toolbar (left) — DOM construction lives in editor/build/toolbar.js;
  // the big tool-switch handler stays here so it can touch module state.
  const { toolbar, toolKeyMap: _toolKeyMap } = _buildToolbar({
    currentTool: state.tool,
    onClearSelection: (which) => {
      if (which === 'lasso') {
        state.lassoPoints = [];
        state.lassoActive = false;
        composite();
      } else if (which === 'wand') {
        _wandClear();
      } else if (which === 'sam') {
        _openSamPrompt();
        return;
      }
      _syncToolClearIndicators();
    },
    onSelectTool: (toolId, _btn, toolbarEl) => {
      // Leaving transform mode without confirm? Treat tool change as confirm.
      if (state.transformActive && toolId !== 'transform') _confirmTransform();
      // Re-clicking the active tool toggles the mobile control sheet —
      // lets the user swipe-down to dismiss, then tap the tool again to
      // bring it back. On desktop this is a no-op visually since the
      // controls live in the right panel.
      const reactivated = state.tool === toolId;
      state.tool = toolId;
      const controls = _editorFind('ge-controls') || _editorQuery('.ge-controls');
      if (controls) {
        if (reactivated) controls.classList.toggle('dismissed');
        else controls.classList.remove('dismissed');
      }
      // On mobile, picking a tool that's about to SHOW its controls
      // panel auto-minimises the layers sheet so the controls aren't
      // covered. Swiping the layers handle back up restores it.
      const isMobile = window.innerWidth <= 820;
      const hasToolControls = ['brush', 'eraser', 'clone', 'inpaint', 'sam'].includes(toolId);
      const controlsVisible = controls && !controls.classList.contains('dismissed');
      if (isMobile && hasToolControls && controlsVisible) {
        const rp = _editorQuery('.ge-right-panel');
        if (rp) {
          rp.classList.remove('expanded');
          rp.classList.add('minimized');
        }
      }
      toolbarEl.querySelectorAll('.ge-tool-btn').forEach(b => b.classList.toggle('active', b.dataset.tool === state.tool));
      // Activate drag-resize handles when picking the Resize tool
      if (toolId === 'transform' && !state.transformActive) _startTransform();
      // Show/hide brush controls. Brush, Eraser AND Clone use the
      // shared size+color row; Inpaint has its OWN size slider.
      const brushControls = _editorFind('ge-brush-controls');
      const needsBrush = ['brush', 'eraser', 'clone'].includes(toolId);
      if (brushControls) brushControls.style.display = needsBrush ? '' : 'none';
      // Eraser and Clone don't care about color — hide the color row.
      const colorRow = _editorFind('ge-color-row');
      if (colorRow) colorRow.style.display = (toolId === 'eraser' || toolId === 'clone') ? 'none' : '';
      const colorLabel = colorRow?.querySelector('label');
      if (colorLabel) colorLabel.textContent = 'Color';
      const sizeLabelEl = brushControls?.querySelector('.ge-size-slider')?.parentElement?.querySelector('label');
      if (sizeLabelEl && sizeLabelEl.firstChild && sizeLabelEl.firstChild.nodeType === Node.TEXT_NODE) {
        sizeLabelEl.firstChild.nodeValue = (toolId === 'eraser') ? 'Brush Size ' : 'Size ';
      }
      // Per-tool stroke-modifier sections (opacity / flow / softness).
      const brushSection = _editorFind('ge-brush-section');
      if (brushSection) brushSection.style.display = toolId === 'brush' ? '' : 'none';
      const cloneSection = _editorFind('ge-clone-section');
      if (cloneSection) cloneSection.style.display = toolId === 'clone' ? '' : 'none';
      const lassoSection = _editorFind('ge-lasso-section');
      if (lassoSection) lassoSection.style.display = state.tool === 'lasso' ? '' : 'none';
      const wandSection = _editorFind('ge-wand-section');
      if (wandSection) wandSection.style.display = state.tool === 'wand' ? '' : 'none';
      const samSection = _editorFind('ge-sam-section');
      if (samSection) samSection.style.display = state.tool === 'sam' ? '' : 'none';
      const inpaintSection = _editorFind('ge-inpaint-section');
      if (inpaintSection) {
        if (state.tool === 'inpaint') {
          if (reactivated) inpaintSection.classList.toggle('dismissed');
          else inpaintSection.classList.remove('dismissed');
          inpaintSection.style.display = inpaintSection.classList.contains('dismissed') ? 'none' : '';
          const inpaintOpen = !inpaintSection.classList.contains('dismissed');
          controls?.classList.toggle('ge-inpaint-popover-host', inpaintOpen && window.innerWidth > 820);
          if (inpaintOpen) _positionInpaintPanel(_btn);
        } else {
          controls?.classList.remove('ge-inpaint-popover-host');
          inpaintSection.classList.remove('dismissed');
          inpaintSection.style.display = 'none';
        }
      }
      // Entering inpaint mode: make sure the active parent layer has a
      // mask sub-layer, and point the global mask plumbing at it. Also
      // force the global mask-visibility flag back on — a previous
      // Generate cleared it, but on re-entry the user expects to see
      // their mask again.
      if (state.tool === 'inpaint') {
        // If the user just made a SAM/Wand selection and then moves to
        // Inpaint, do the obvious thing: bake that selection into the
        // inpaint mask. Otherwise Generate says "draw the area first"
        // even though a red selection is visible on screen.
        if (state.wandMask && state.wandLayerId) {
          _wandToMask();
        }
        // First inpaint entry per session: bump the brush size to the
        // mask-friendly default (other tools keep their own size).
        if (!state.inpaintBrushInitialised) {
          state.brushSize = _INPAINT_DEFAULT_BRUSH;
          state.inpaintBrushInitialised = true;
          const inp = _editorFind('ge-inpaint-brush-slider');
          if (inp) {
            const pos = Math.round(Math.log(Math.max(1, state.brushSize)) / Math.log(800) * 1000);
            inp.value = String(pos);
            const lbl = _editorFind('ge-inpaint-brush-label');
            if (lbl) lbl.textContent = `${state.brushSize}px`;
          }
        }
        // If the active parent already carries one or more masks, reuse
        // the most-recent one instead of creating a new "Mask 2" /
        // "Mask 3" every time the user re-enters inpaint.
        const parent = _activeParentLayer();
        if (parent && parent.masks && parent.masks.length) {
          if (!parent.activeMaskId) {
            parent.activeMaskId = parent.masks[parent.masks.length - 1].id;
            _markSceneChanged();
          }
          const m = _getActiveMaskLayer();
          if (m) { state.maskCanvas = m.canvas; state.maskCtx = m.ctx; }
        } else {
          const mask = _ensureActiveMaskLayer();
          if (mask) {
            state.maskCanvas = mask.canvas;
            state.maskCtx = mask.ctx;
            // Reflect the freshly-created mask sub-row in the panel.
            _renderLayerPanel();
          }
        }
        if (!state.maskVisible) {
          state.maskVisible = true;
          const maskBtn = _editorFind('ge-mask-vis');
          if (maskBtn) {
            maskBtn.innerHTML = uiIcon("eye", 14);
            maskBtn.title = 'Hide mask';
            maskBtn.classList.add('visible');
          }
        }
      }
      const eraserSection = _editorFind('ge-eraser-section');
      if (eraserSection) eraserSection.style.display = state.tool === 'eraser' ? '' : 'none';
      const sharpenSection = _editorFind('ge-sharpen-section');
      if (sharpenSection) sharpenSection.style.display = state.tool === 'sharpen' ? '' : 'none';
      const rembgSection = _editorFind('ge-rembg-section');
      if (rembgSection) {
        const show = state.tool === 'rembg';
        rembgSection.style.display = show ? '' : 'none';
        if (show) _checkRembgInstalled();
      }
      const importSection = _editorFind('ge-import-section');
      if (importSection) importSection.style.display = state.tool === 'import' ? '' : 'none';
      const harmonizeSection = _editorFind('ge-harmonize-section');
      if (harmonizeSection) harmonizeSection.style.display = state.tool === 'harmonize' ? '' : 'none';
      const upscaleSection = _editorFind('ge-upscale-section');
      if (upscaleSection) upscaleSection.style.display = state.tool === 'upscale' ? '' : 'none';
      const styleSection = _editorFind('ge-style-section');
      if (styleSection) styleSection.style.display = state.tool === 'style' ? '' : 'none';
      // Toggle cursor — hide native cursor for tools that draw via our
      // own circle overlay (brush/eraser/inpaint/lasso); for other tools
      // pick a cursor that matches the tool's affordance.
      const useCircle = state.tool === 'brush' || state.tool === 'eraser' || state.tool === 'inpaint' || state.tool === 'lasso' || state.tool === 'clone';
      if (state.mainCanvas) {
        // Custom SVG cursor for the Move tool — white fill with black
        // stroke so it reads on both light and dark canvases.
        const moveCursorSvg = `data:image/svg+xml;utf8,${encodeURIComponent(
          '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="white" stroke="black" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 L9 5 H11 V11 H5 V9 L2 12 L5 15 V13 H11 V19 H9 L12 22 L15 19 H13 V13 H19 V15 L22 12 L19 9 V11 H13 V5 H15 Z"/></svg>'
        )}`;
        let cursor = 'crosshair';
        if (state.tool === 'move') cursor = `url("${moveCursorSvg}") 12 12, move`;
        else if (state.tool === 'transform') cursor = 'default';
        else if (useCircle) cursor = 'crosshair';
        state.mainCanvas.style.cursor = cursor;
      }
      if (state.cursorEl) state.cursorEl.style.display = useCircle ? '' : 'none';
      composite();
    },
  });
  // Top bar — static DOM lives in editor/build/topbar.js; all click
  // handlers below wire to the IDs baked into the markup.
  const topBar = _buildTopbar();
  container.appendChild(topBar);

  // Editor body (toolbar + canvas + panel)
  const editorBody = document.createElement('div');
  editorBody.className = 'ge-editor-body';
  editorBody.appendChild(toolbar);

  // Canvas area (center)
  const canvasArea = document.createElement('div');
  canvasArea.className = 'ge-canvas-area';
  state.mainCanvas = document.createElement('canvas');
  state.mainCanvas.className = 'ge-main-canvas';
  state.mainCanvas.tabIndex = 0;
  state.mainCanvas.setAttribute('aria-label', 'Image canvas');
  _wireCanvasContext(state.mainCanvas, canvasArea);
  state.mainCtx = state.mainCanvas.getContext('2d');
  // Initial cursor matches the default tool (move) so the user sees the
  // four-arrow icon as soon as the editor opens. Uses a filled white
  // arrow with black stroke for readability on light AND dark canvases.
  if (state.tool === 'move') {
    const svg = `data:image/svg+xml;utf8,${encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="white" stroke="black" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 L9 5 H11 V11 H5 V9 L2 12 L5 15 V13 H11 V19 H9 L12 22 L15 19 H13 V13 H19 V15 L22 12 L19 9 V11 H13 V5 H15 Z"/></svg>'
    )}`;
    state.mainCanvas.style.cursor = `url("${svg}") 12 12, move`;
  } else {
    state.mainCanvas.style.cursor = 'crosshair';
  }
  canvasArea.appendChild(state.mainCanvas);

  // Transform overlay — separate canvas positioned over the main canvas
  // with extra margin so the resize/rotation handles can render OUTSIDE
  // the image bounds. Sized + zoomed in sync with the main canvas via
  // _syncTransformOverlay(). The overlay is pointer-events:none so it
  // doesn't intercept clicks; hit-testing still happens in image coords.
  state.transformOverlay = document.createElement('canvas');
  state.transformOverlay.className = 'ge-transform-overlay';
  state.transformOverlayCtx = state.transformOverlay.getContext('2d');
  canvasArea.appendChild(state.transformOverlay);
  canvasArea.appendChild(_buildAiCommandBox());
  // Keep the transform handles glued to the photo while the canvas-area
  // scrolls (the overlay is anchored to the canvas's live rect, so a
  // re-draw on scroll re-reads its position).
  canvasArea.addEventListener('scroll', () => {
    if (state.transformActive) _drawTransformHandles();
  }, { passive: true });

  // Canvas events (mouse + touch + pinch-zoom + pan) — full
  // implementation in editor/canvas-events.js.
  wireCanvasEvents({
    state, root:container, lifetime:_coreLifetime,
    canvasArea,
    beginDraw: _beginDraw,
    continueDraw: _continueDraw,
    endDraw: _endDraw,
    updateBrushCursor: (e) => _updateBrushCursor(e),
    syncZoomControls: () => _syncZoomControls(),
  });

  editorBody.appendChild(canvasArea);

  // Right panel (controls + layers + resize handle) — full
  // implementation in editor/build/right-panel.js.
  const { rightPanel, controls, layerPanel } = buildRightPanel({
    state, root: container, lifetime: _coreLifetime,
    controlsHTML: _controlsHTML,
    layerPanelHTML: _layerPanelHTML,
  });
  editorBody.appendChild(rightPanel);
  container.appendChild(editorBody);
  _wireInpaintPopoverWindow();

  // Slider UX (expand-while-using, floating bubble, click-to-type) —
  // full implementation in editor/slider-ux.js.
  wireSliderUx({ state, root: container, lifetime: _coreLifetime });

  // Shortcuts cheatsheet popover — full implementation in
  // editor/shortcuts-popover.js. (Dead `_makeShortcutsDraggable`
  // helper for the old centered-modal version was dropped.)
  const _shortcutsPopover = createShortcutsPopover({root: container, portal: _editorPortal, lifetime: _coreLifetime, scopeIds});
  const _toggleShortcuts = _shortcutsPopover.toggleShortcuts;
  _editorFind('ge-shortcuts-btn')?.addEventListener('click', () => _toggleShortcuts());

  // Dismiss-listeners for the inpaint popup are attached lazily by
  // _showInpaintPrompt() and removed by _dismissInpaintPrompt(), so the
  // active-edit path doesn't pay for them on every event. (Listening on
  // mousedown/wheel/touchstart/input/change at capture phase, even with
  // a fast `closest()` check, added up to noticeable lag during heavy
  // brush use.)

  // Wire up controls
  controls.querySelector('.ge-color-picker').addEventListener('input', (e) => { state.color = e.target.value; });
  // Swap the editor's native color inputs for the in-house HSV picker
  // we built in the theme system — eyedropper, suggestions, recents,
  // no native OS dialog. Each picker keeps its existing `input` event
  // wiring so callers just keep reading `e.target.value`.
  const editorColorPicker = createColorPicker({root: container, lifetime: _coreLifetime});
  const attachColorPicker = editorColorPicker.attach;
  controls.querySelectorAll('.ge-color-picker').forEach(attachColorPicker);
  controls.querySelectorAll('.ge-color-picker').forEach(el => {
    // Set the initial swatch background so it reflects the starting value.
    el.value = el.value;
  });
  // Hide brush controls initially (default tool is Move)
  const initBrushCtrl = _editorFind('ge-brush-controls');
  if (initBrushCtrl) initBrushCtrl.style.display = 'none';
  // Brush-size slider is exponential — slider position 0..1000 maps to
  // brush size 1..800 via Math.pow(800, pos/1000). This gives fine
  // control at small sizes (where precision matters most) and bigger
  // jumps at the high end (where +/-50 px is barely visible anyway).
  // We expose two sliders (global brush-controls + inpaint section) and
  // keep them in sync via _brushSizeSync.
  function _brushSizeSync(source) {
    const globalLabel = controls.querySelector('.ge-size-label');
    const globalInput = controls.querySelector('.ge-size-slider');
    const inpaintLabel = _editorFind('ge-inpaint-brush-label');
    const inpaintInput = _editorFind('ge-inpaint-brush-slider');
    const pos = Math.round(Math.log(Math.max(1, state.brushSize)) / Math.log(800) * 1000);
    if (globalLabel) globalLabel.textContent = state.brushSize + 'px';
    if (inpaintLabel) inpaintLabel.textContent = state.brushSize + 'px';
    if (globalInput && source !== globalInput) globalInput.value = String(pos);
    if (inpaintInput && source !== inpaintInput) inpaintInput.value = String(pos);
  }
  function _wireBrushSlider(el) {
    if (!el) return;
    el.addEventListener('input', (e) => {
      const pos = parseInt(e.target.value, 10);
      state.brushSize = Math.max(1, Math.round(Math.pow(800, pos / 1000)));
      _brushSizeSync(e.target);
    });
  }
  _wireBrushSlider(controls.querySelector('.ge-size-slider'));
  _wireBrushSlider(_editorFind('ge-inpaint-brush-slider'));
  // Topbar wiring (undo/redo/history, Save dropdown, zoom buttons,
  // Export/Download/Project, Edge popup, cross-dropdown coordination) —
  // full implementation in editor/wire-topbar.js.
  wireTopbar({
    registerPortal: _registerEditorPortal, portal: _editorPortal,
    state, root: container, lifetime: _coreLifetime,
    undo, redo,
    toggleHistoryPanel: _toggleHistoryPanel,
    fitZoom: () => _fitZoom(),
    applyZoom: () => _applyZoom(),
    exportToGallery, retryPendingSaveCopy, downloadPNG,
    saveProject: () => _saveProject(),
    loadProjectPrompt: () => _loadProjectPrompt(),
    activeLayer,
    saveState: _saveState,
    applyEdgeFeather: _applyEdgeFeather,
    composite,
    registerDocClickAway: _registerDocClickAway,
    uiModule,
  });
  if (legacy) void _hydratePendingSaveCopy();
  // Fill — visible only when a selection or active mask exists. Pours
  // the current colour into whichever target is live:
  //   - active mask sub-layer → fills the layer's pixels clipped by
  //     the mask (uses mask alpha as a stencil).
  //   - lasso closed → fills the polygon area on the active layer.
  //   - wand selection → fills the wand mask area on the active layer.
  // Fill — invoked from the Image menu's "Fill selection / mask" item.
  // Pours the current colour into whichever target is live:
  //   - active mask sub-layer → fills the layer's pixels clipped by
  //     the mask (uses mask alpha as a stencil).
  //   - lasso closed → fills the polygon area on the active layer.
  //   - wand selection → fills the wand mask area on the active layer.
  function _doFillSelection() { return _withSceneMutation('fill-selection', _doFillSelectionOwned); }
  function _doFillSelectionOwned() {
    const layer = activeLayer();
    if (!layer || layer.locked) return;
    const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
    const w = layer.canvas.width;
    const h = layer.canvas.height;
    const mask = _getActiveMaskLayer();
    const hasLasso = state.lassoPoints.length >= 3 && !state.lassoActive;
    if (!mask && hasLasso && !_previewCurrent(_lassoPreview)) return false;
    if (!mask && !hasLasso && state.wandMask && state.wandLayerId !== layer.id) return false;
    const stencil = document.createElement('canvas');
    stencil.width = w; stencil.height = h;
    const sctx = stencil.getContext('2d');
    if (mask) {
      sctx.drawImage(mask.canvas, -off.x, -off.y);
    } else if (hasLasso) {
      const feather = parseInt(_editorFind('ge-lasso-feather')?.value || '0');
      const grow = parseInt(_editorFind('ge-lasso-grow')?.value || '0');
      sctx.drawImage(_buildLassoMask(w, h, off.x, off.y, feather, grow), 0, 0);
    } else if (state.wandMask) {
      sctx.drawImage(state.wandMask, 0, 0);
    } else {
      return;
    }
    sctx.globalCompositeOperation = 'source-in';
    sctx.fillStyle = state.color;
    sctx.fillRect(0, 0, w, h);
    sctx.globalCompositeOperation = 'source-over';
    _saveState('Fill selection');
    layer.ctx.drawImage(stencil, 0, 0);
    layer._adjCacheKey=null; layer._adjFinalKey=null;
    _markSceneChanged();
    composite();
    _renderLayerPanel();
    if (uiModule) uiModule.showToast('Filled');
  }

  // AI model selectors (Gen, Inpaint, per-tool) — full
  // implementation in editor/ai-models.js.
  wireAIModelSelectors({
    container, lifetime: _coreLifetime,
    apiBase: API_BASE,
    openCookbookForImg2img: () => _openCookbookForImg2img(),
  });

  if (legacy) {
    const menu=_editorFind('ge-save-menu'), trigger=_editorFind('ge-save-menu-btn');
    const checkItem=document.createElement('button');
    checkItem.type='button'; checkItem.className='dropdown-item-compact'; checkItem.textContent='Check original save'; checkItem.hidden=true;
    menu?.appendChild(checkItem);
    const metadataItem=document.createElement('button'); metadataItem.type='button'; metadataItem.className='dropdown-item-compact'; metadataItem.textContent='Project metadata…'; menu?.appendChild(metadataItem);
    metadataItem.addEventListener('click',()=>{_openProjectMetadata().catch(error=>uiModule?.showToast(error.message || 'Project metadata unavailable.',6000));});
    trigger?.addEventListener('click',()=>{checkItem.hidden=!state._legacyProjectPending;});
    checkItem.addEventListener('click',async()=>{
      const pending=state._legacyProjectPending, origin=_captureOrigin();
      const current=()=>pending && _originCurrent(origin) && state._legacyProjectPending===pending && pending.owner===origin.owner && pending.provider===state.managedProvider && pending.resourceId===state.managedResourceId && pending.projectId===state.projectId;
      if (!current()) return;
      let message='Original save outcome is unknown; the draft remains available.';
      try {
        const result=await _impsRequest('/api/imps/project-operations/status',{method:'POST',body:JSON.stringify({operation_id:pending.operationId})});
        if (!current()) return;
        if (result?.state==='available' && result.action_id===pending.operationId && result.outcome==='not_committed') message='Original save was not committed; the draft remains available.';
        else if (result?.state==='available' && result.action_id===pending.operationId && result.outcome==='committed' && ['complete','skipped','unavailable'].includes(result.history_status)) message=result.history_status==='unavailable' ? 'Original save committed; History recovery is unavailable. The draft remains available.' : 'Original save committed; the draft remains available.';
      } catch (_) {}
      if (current()) uiModule?.showToast(message,7000);
    });
  }
  _editorFind('ge-save').addEventListener('click', async () => {
    if (!legacy) { try { const receipt=await session.save(); if (state.editorOpen && receipt?.status!=='cancelled') uiModule?.showToast(receipt?.destinationLabel ? 'Saved to '+receipt.destinationLabel : receipt?.status==='unavailable' ? receipt.reason : receipt?.status==='saved_copy' ? 'Image copy saved in Files.' : receipt?.lore_receipt?.history_status==='unavailable' ? 'Saved, but History recovery is unavailable for this change.' : 'Image saved.'); } catch (error) { if (state.editorOpen) uiModule?.showToast(error?.message || 'Image save failed.',6000); } return; }
    if (!state.managedResourceId && !state.imageId) {
      await exportToGallery();
      return;
    }
    // Normalize legacy image opens onto the managed identity so Save
    // goes through /api/imps/projects/* rather than the retired replace path.
    if (!state.managedResourceId && state.imageId) {
      _bindManagedIdentity('files', `image:${state.imageId}`);
    }
    // Capture the save target before any asynchronous canvas encoding. A
    // different resource may be opened while toBlob() is pending.
    const saveOperation = _captureManagedSaveSnapshot();
    const endBusy = _saveButtonBusy('Saving…');
    let blob = null;
    let savedOk = false;
    const t0 = performance.now();
    try {
      // Encode directly from the flattened canvas via toBlob() to avoid
      // the dataURL round-trip (which doubles peak memory). Pick JPEG for
      // photo sources so 24MP uploads don't balloon to 200MB+ PNG —
      // critical when the editor is accessed over Tailscale Funnel etc.
      const flat = flatten();
      const ext = (state.originalExt || 'png').toLowerCase();
      const isJpeg = ext === 'jpg' || ext === 'jpeg';
      const mime = state._legacyRasterMime;
      if (!mime) throw Error('Original encoding or write access is unavailable. Use Save as copy.');
      const quality = isJpeg ? 0.92 : undefined;
      blob = await new Promise((resolve, reject) => {
        flat.toBlob(b => b ? resolve(b) : reject(new Error('Canvas encode failed')), mime, quality);
      });
      const body = await _managedSave({ blob, operation: saveOperation });
      const totalMs = Math.round(performance.now() - t0);
      if (uiModule) uiModule.showToast(body?.lore_receipt?.history_status==='unavailable' ? 'Saved, but History recovery is unavailable for this change.' : `Saved over original (${(blob.size / 1024 / 1024).toFixed(1)}MB · ${(totalMs / 1000).toFixed(1)}s)`, body?.lore_receipt?.history_status==='unavailable' ? 6000 : 4000);
      window.dispatchEvent(new CustomEvent('imps-refresh'));
      savedOk = true;
    } catch (e) {
      console.error('[save] error:', e);
      const sizeMB = blob ? ` (${(blob.size / 1024 / 1024).toFixed(1)}MB)` : '';
      let msg = e?.message || 'unknown';
      if (e?.name === 'TypeError' || /fetch|network|load failed/i.test(msg)) {
        msg = `network dropped${sizeMB} — try "Save as copy" or check connection`;
      } else {
        msg += sizeMB;
      }
      if (uiModule) uiModule.showToast('Failed to save: ' + msg, 6000);
    } finally {
      endBusy();
      if (savedOk) _flashSaveButtonOk();
      window.dispatchEvent(new CustomEvent('imps-managed-save-settled'));
    }
  });

  // Topbar overflow + canvas-size badge — full implementation in
  // editor/wire-topbar-overflow.js.
  wireTopbarOverflow({ container, state, lifetime: _coreLifetime });

  // Topbar dropdown menus (Image, Filter, Resize) + the resize-canvas
  // helpers — full implementation in editor/wire-topbar-menus.js. The
  // returned `_resizeCustomPrompt` is consumed by the keyboard
  // shortcuts module (Ctrl+Shift+T).
  const { resizeCustomPrompt: _resizeCustomPrompt } = wireTopbarMenus({
    markSceneChanged: _markSceneChanged,
    state, root: container, lifetime: _coreLifetime,
    closeOtherTopbarMenus: (id) => _closeOtherTopbarMenus(id, container),
    registerDocClickAway: _registerDocClickAway,
    saveState: _saveState,
    composite,
    fitZoom: () => _fitZoom(),
    promptCanvasSize: (opts) => _promptCanvasSize(opts),
    captureResizeOrigin: () => _canvasTransforms.captureBounds(), resizeBounds: (w,h,origin) => _canvasTransforms.resizeBounds(w,h,origin),
    doFillSelection: () => _doFillSelection(),
    rotateAllLayers: (deg) => _rotateAllLayers(deg),
    flipAllLayers: (axis) => _flipAllLayers(axis),
    applyGaussianBlur: () => _applyGaussianBlur(),
    applyZoomBlur: () => _applyZoomBlur(),
    uiModule,
  });

  // Inpaint side-panel controls (Feather/Strength previews, post-gen
  // edge tuner, mask vis/invert/clear, paint-erase toggle, mask tint
  // pickers) — full implementation in editor/wire-inpaint-controls.js.
  ({syncInpaintAvailability:_syncInpaintControls} = wireInpaintControls({
    scopeIds,
    markSceneChanged: _markSceneChanged,
    state, root: container, lifetime: _coreLifetime,
    captureOrigin:_writerOrigin, originCurrent:_writerCurrent, withSceneMutation:_withSceneMutation, saveState:_saveState,
    composite,
    applyInpaintFeather: _applyInpaintFeather,
    autoMatchInpaint: _autoMatchLastInpaintLayer,
    syncToolClearIndicators: () => _syncToolClearIndicators(),
    attachColorPicker,
    uiModule,
  }));

  // AI inpaint (Generate / Remove / Outpaint) — full implementation
  // in editor/ai-inpaint.js.
  wireInpaintButtons({
    markSceneChanged: _markSceneChanged,
    state, root: container, lifetime: _aiLifetime, captureOrigin: () => {const check=_captureAIOperation(), revision=_sceneAuthority.revision; return () => {const status=check(); return status==='current' && !_sceneAuthority.canApplyAt(revision) ? 'changed' : status;};},
    sceneRevision:() => _sceneAuthority.revision,
    canCapture:() => _sceneAuthority.canApplyAt(_sceneAuthority.revision),
    beginMutation:() => _sceneAuthority.beginGesture(_gestureView,'inpaint-result'), endMutation:token => _sceneAuthority.endGesture(token),
    resultHooks:legacy ? null : {
      reserve:() => sessionPersistence.reserveResult({kind:'ai',epoch:String(_impsAccountEpoch),generation:_sceneAuthority.revision}),
      submit:id => sessionPersistence.submitResult(id), receive:(id,result) => sessionPersistence.receiveResult(id,result),
      reject:id => sessionPersistence.rejectResult(id), unknown:id => sessionPersistence.markResultUnknown(id), resolve:id => sessionPersistence.resolveResult(id),
    },
    buildMergedMaskCanvas: () => _buildMergedMaskCanvas(),
    dilateMask: _dilateMask,
    applyInpaintFeather: _applyInpaintFeather,
    getSelectedAIEndpoint: (type) => _getSelectedAIEndpoint(type),
    ensureActiveMaskLayer: () => _ensureActiveMaskLayer(),
    saveState: _saveState,
    createLayer,
    composite,
    renderLayerPanel: () => _renderLayerPanel(),
    spinnerModule,
    uiModule,
  });

  // Per-tool Opacity / Flow / Softness sliders (Eraser / Brush /
  // Clone) — full implementation in editor/stroke-tool-sliders.js.
  wireStrokeToolSliders({state, root: container, lifetime: _coreLifetime});

  // Sharpen + Bg Remove + edge cleanup — full implementation in
  // editor/ai-rembg.js. Returns the selection-hint-mask builder so
  // the wand-rembg button (in the wand controls section) can reuse it.
  const { buildSelectionHintMask: _buildSelectionHintMask } = wireRembgAndSharpen({
    markSceneChanged: _markSceneChanged,
    canCapture:() => _sceneAuthority.canApplyAt(_sceneAuthority.revision),
    beginMutation:() => _sceneAuthority.beginGesture(_gestureView,'rembg-edge'), endMutation:token => _sceneAuthority.endGesture(token),
    state, root: container, lifetime: _aiLifetime, captureOrigin: _captureAIOperation,
    applyImageTool: _applyImageTool,
    openCookbookForDependency: (pkg) => _openCookbookForDependency(pkg),
    composite,
    renderLayerPanel: () => _renderLayerPanel(),
    uiModule,
  });

  // Image import (topbar / panel File / Clipboard / Files picker) —
  // full implementation in editor/wire-import.js. Returns the shared
  // handleImportedImage sink so drag-drop wires through the same path.
  const captureImport = () => {
    const generation = _openGeneration, edit = state.editEpoch, revision = _sceneAuthority.revision, owner = document.body.dataset.accountId, origin=_captureOrigin();
    const sourceIdentity=target => JSON.stringify([target.sessionId,target.scope,target.sourceKey,target.sourceRevision,target.sourceDigest,target.projectId,target.projectRevision]);
    const source=legacy ? null : sourceIdentity(session.captureTarget());
    const lifetime = _coreLifetime;
    const check=() => !lifetime || lifetime.disposed || generation !== _openGeneration || owner !== document.body.dataset.accountId || !_originCurrent(origin) ? 'retired' : edit !== state.editEpoch || !_sceneAuthority.canApplyAt(revision) || (!legacy && source!==sourceIdentity(session.captureTarget())) ? 'edited' : 'current';
    check.revision=revision; return check;
  };
  const startImport = legacy ? null : check => {
    const id=sessionPersistence.reserveResult({kind:'import',epoch:String(_impsAccountEpoch),generation:check.revision}); let received=false, receiving=false;
    return {
      get received() { return received; },
      async receive(blob,metadata={}) { receiving=true; try {const data=new Uint8Array(await blob.arrayBuffer()); let binary=''; for (let i=0;i<data.length;i+=32768) binary+=String.fromCharCode(...data.subarray(i,i+32768)); sessionPersistence.receiveResult(id,{bytes:btoa(binary),mime:blob.type || 'image/png',metadata}); received=true;} catch(error) {try {sessionPersistence.cancelLocalReservation(id);} catch (_) {} throw error;} finally {receiving=false;} },
      apply(fn) { if (!received || check()!=='current') return false; const token=_sceneAuthority.beginGesture(_gestureView,'import'); if (!token) return false; try {fn(); sessionPersistence.resolveResult(id); return true;} finally {_sceneAuthority.endGesture(token);} },
      cancel(knownFailure=false) { try {if (!received && !receiving) sessionPersistence.cancelLocalReservation(id); else if (received && knownFailure) sessionPersistence.resolveResult(id);} catch (_) {} },
    };
  };
  const { handleImportedImage: _handleImportedImage } = wireImport({
    markSceneChanged: _markSceneChanged,
    state, container, lifetime: _coreLifetime, captureImport, startImport,
    saveState: _saveState,
    createLayer,
    composite,
    renderLayerPanel: () => _renderLayerPanel(),
    uiModule,
  });

  // Harmonize / Canvas Upscale / AI Upscale / Style Transfer +
  // Add-Empty-Layer — full implementation in editor/ai-tools-misc.js.
  const { addEmptyLayer: _addEmptyLayer } = wireAIToolsMisc({
    markSceneChanged: _markSceneChanged, scaleCanvas: factor => _canvasTransforms.scaleAll(factor), withSceneMutation:_withSceneMutation,
    state, root: container, lifetime: _aiLifetime, captureOrigin: () => {const check=_captureAIOperation(), revision=_sceneAuthority.revision; return () => {const status=check(); return status==='current' && !_sceneAuthority.canApplyAt(revision) ? 'changed' : status;};},
    canCapture:() => _sceneAuthority.canApplyAt(_sceneAuthority.revision),
    beginMutation:() => _sceneAuthority.beginGesture(_gestureView,'ai-result'), endMutation:token => _sceneAuthority.endGesture(token),
    resultHooks:legacy ? null : {
      reserve:() => sessionPersistence.reserveResult({kind:'ai',epoch:String(_impsAccountEpoch),generation:_sceneAuthority.revision}),
      submit:id => sessionPersistence.submitResult(id), receive:(id,result) => sessionPersistence.receiveResult(id,result),
      reject:id => sessionPersistence.rejectResult(id), unknown:id => sessionPersistence.markResultUnknown(id), resolve:id => sessionPersistence.resolveResult(id),
    },
    apiBase: API_BASE,
    buildLayerBodyMask: _buildLayerBodyMask,
    buildSeamMask: _buildSeamMask,
    applyImageTool: _applyImageTool,
    flatten,
    saveState: _saveState,
    fitZoom: () => _fitZoom(),
    composite,
    createLayer,
    renderLayerPanel: () => _renderLayerPanel(),
    spinnerModule,
    uiModule,
  });
  // (Merge dropdown removed — Merge Down / Merge All / Flatten Copy
  // are now three inline icon buttons in the layers header next to
  // + Add. Their individual click handlers below already bind by id.)

  // Lasso + Magic Wand panel controls — full implementation in
  // editor/wire-selection-controls.js.
  wireSelectionControls({
    state, root: container, lifetime: _coreLifetime,
    composite,
    invertSelection: _invertSelection,
    lassoDeleteSelection: _lassoDeleteSelection,
    lassoCopyToLayer: _lassoCopyToLayer,
    lassoToMask: _lassoToMask,
    runMagicWand: (x, y, mode, opts) => _runMagicWand(x, y, mode, opts),
    captureWandOrigin:() => _captureWandOrigin(),
    wandClear: _wandClear,
    wandDeleteSelection: _wandDeleteSelection,
    wandCopyToNewLayer: _wandCopyToNewLayer,
    wandToMask: _wandToMask,
    buildSelectionHintMask: _buildSelectionHintMask,
    applyImageTool: _applyImageTool,
    uiModule,
  });
  _editorFind('ge-sam-find')?.addEventListener('click', () => _runSamTextSelection());
  _editorFind('ge-sam-query')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      _runSamTextSelection();
    }
  });
  _editorFind('ge-sam-clear')?.addEventListener('click', () => _wandClear());
  _editorFind('ge-sam-mask')?.addEventListener('click', () => _wandToMask());
  _editorFind('ge-sam-vis')?.addEventListener('click', () => {
    state.wandMaskVisible = !state.wandMaskVisible;
    const btn = _editorFind('ge-sam-vis');
    if (btn) {
      btn.innerHTML = state.wandMaskVisible
        ? uiIcon("eye", 14)
        : uiIcon("eye-off", 14);
      btn.title = state.wandMaskVisible ? 'Hide selection overlay' : 'Show selection overlay';
      btn.classList.toggle('visible', state.wandMaskVisible);
    }
    composite();
  });
  _wireAiCommandBox();

  // Merge / Flatten buttons (layer-panel footer) — full
  // implementation in editor/wire-merge-buttons.js.
  wireMergeButtons({
    markSceneChanged: _markSceneChanged,
    withSceneMutation:_withSceneMutation,
    state, root: state.container, lifetime: _coreLifetime,
    saveState: _saveState,
    createLayer,
    renderLayerPanel: () => _renderLayerPanel(),
    composite,
    renderLayer: (layer) => _renderLayerWithAdjLayers(layer),
    uiModule,
  });

  // Capture-phase Escape interceptor — runs BEFORE any bubble-phase
  // handler (keyboard shortcuts, etc.) so cancelling a
  // crop / lasso / transform inside the editor can't ever bubble up and
  // close the editor host.
  _coreLifetime.listen(document, 'keydown', (e) => {
    if (!state.editorOpen || !_coreFocused() || !state.container?.getClientRects().length) return;
    // Esc on the shortcuts overlay closes it; takes priority over the
    // other modal cancels so the cheatsheet feels responsive AND so the
    // a stale handler does not close the editor.
    if (e.key === 'Escape' && _shortcutsPopover.isOpen()) {
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      _toggleShortcuts(false);
      return;
    }
    // Enter accepts an active crop (same as the Apply button). Skip when
    // typing in a field — the crop W/H inputs handle their own Enter, and
    // we don't want to hijack Enter elsewhere.
    if (e.key === 'Enter' && state.cropRect && !state.cropping && !state.cropMoving) {
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      e.preventDefault();
      e.stopPropagation();
      _applyCrop();
      return;
    }
    if (e.key !== 'Escape') return;
    // Escape stays inside Imps and must not close the editor or cancel its active state.
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
  }, true);

  // Keyboard shortcuts — full implementation in
  // editor/keyboard-shortcuts.js.
  wireKeyboardShortcuts({
    state, root:container, lifetime:_coreLifetime, isFocused:_coreFocused,
    captureLassoPreview:()=>{_lassoPreview=_previewOrigin();}, captureClipboardOrigin:_writerOrigin, clipboardOriginCurrent:value=>_writerCurrent(value) && _sceneAuthority.canApplyAt(value.view.revision), cutSelection:_cutSelectionAtomic, clearSelection:_clearSelectionAtomic,
    toolbar, toolKeyMap: _toolKeyMap,
    composite, saveState: _saveState, undo, redo,
    toggleShortcuts: _toggleShortcuts,
    confirmTransform: _confirmTransform,
    cancelTransform: _cancelTransform,
    startTransform: _startTransform,
    resizeCustomPrompt: _resizeCustomPrompt,
    addEmptyLayer: _addEmptyLayer,
    brushSizeSync: _brushSizeSync,
    invertSelection: _invertSelection,
    wandDeleteSelection: _wandDeleteSelection,
    wandCopyToNewLayer: _wandCopyToNewLayer,
    lassoDeleteSelection: _lassoDeleteSelection,
    lassoCopyToLayer: _lassoCopyToLayer,
    lassoToMask: _lassoToMask,
    buildLassoMask: _buildLassoMask,
    drawLassoOverlay: _drawLassoOverlay,
    activeLayer,
    uiModule,
  });
  container.setAttribute('tabindex', '0');

  // Paste + drag-and-drop image import — full implementation in
  // editor/clipboard-and-drop.js.
  const clipboardWiring = _clipboardWiring = wireClipboardAndDrop({
    markSceneChanged: _markSceneChanged,
    state, container, captureImport, startImport, acceptLegacyBroadcast:legacy,
    isFocused: _coreFocused, capturePaste: () => ({view:_previewOrigin(), source:legacy ? null : JSON.stringify(session.captureTarget())}),
    pasteCurrent: value => !!value && _previewCurrent(value.view) && (legacy || value.source === JSON.stringify(session.captureTarget())), withSceneMutation: _withSceneMutation,
    saveState: _saveState,
    createLayer,
    renderLayerPanel: () => _renderLayerPanel(),
    composite,
    handleImportedImage: (img) => _handleImportedImage(img),
    uiModule,
    filesClient: filesFacadeClient,
    getContext: () => window.__openClankFilesTransferContext?.() || null,
  });
  _coreLifetime.add(() => { clipboardWiring.dispose(); if (_clipboardWiring===clipboardWiring) _clipboardWiring=null; });
  scopeIds(container);
  for (const portal of _ownedPortals) scopeIds(portal);
}

// ── Layer panel rendering ──

// Layer-panel renderer — implementation in editor/layer-panel.js.
// Wrap to the legacy name so the dozens of `_renderLayerPanel()` call
// sites scattered across the file keep working unchanged.
let _layerPanelRenderer;
function _initLayerPanel(container) { _layerPanelRenderer = createLayerPanelRenderer({
  root: container,
  withSceneMutation:_withSceneMutation, captureOrigin:_previewOrigin, originCurrent:_previewCurrent,
  canEditLayerContext:_canvasContextWritable,
  convertSelectionToMask:(layer,kind) => {
    if (!state.layers.includes(layer) || (kind==='lasso' && !_previewCurrent(_lassoPreview)) || (kind==='wand' && state.wandLayerId!==layer.id)) {uiModule?.showToast('The selection or target layer changed.'); return false;}
    return _withSceneMutation('selection-to-layer-mask',() => {if (!state.layers.includes(layer)) return false; _saveState('Selection to mask'); state.activeLayerId=layer.id; layer.activeMaskId=null; if (kind==='lasso') _lassoToMaskOwned({skipHistory:true}); else _wandToMaskOwned({skipHistory:true}); return true;});
  },
  markSceneChanged: _markSceneChanged,
  state, lifetime: () => _coreLifetime,
  composite,
  saveState: _saveState,
  showLayerThumb: (row, layer) => _showLayerThumb(row, layer),
  hideLayerThumb: () => _hideLayerThumb(),
  loadLayerAlphaAsSelection: (layer) => _loadLayerAlphaAsSelection(layer),
  openFxPopup: (layer, anchor) => _openFxPopup(layer, anchor),
  editAdjLayer: (layer, adj, anchor) => _editAdjLayer(layer, adj, anchor),
  createLayer,
  renderLayer: (layer) => _renderLayerWithAdjLayers(layer),
  lassoToMask: () => _lassoToMask(),
  wandToMask: () => _wandToMask(),
  getActiveMaskLayer: () => _getActiveMaskLayer(),
  syncFxPanelToActiveLayerIfPresent: () => _syncFxPanelToActiveLayerIfPresent(),
  dragSortModule,
  uiModule,
}); }
function _renderLayerPanel() { return _layerPanelRenderer?.render(); }

function _revealLayerPanel() {
  requestAnimationFrame(() => {
    const panel = state.container?.querySelector?.('.ge-right-panel') ||
      _editorQuery('.ge-right-panel');
    if (!panel) return;
    panel.classList.remove('minimized');
    panel.classList.add('expanded');
  });
}

// ── Flatten / Export ──

function flatten() {
  return flattenImageScene(state,document);
}

// Build the union of all "foreground" visible-layer alphas (binary).
// "Background" = the BOTTOMMOST visible layer (Harmonize's colour-match
// reference). Everything visible ABOVE it = foreground that goes into
// the body mask. Independent of the `isBase` flag, so reordering layers
// (or hiding the original photo after a bg-remove) doesn't break the
// semantics.
// Harmonize-pipeline mask builders live in editor/harmonize-masks.js.
// Thin wrappers translate module state into the pure helpers.
function _harmonizeLayerList() {
  return state.layers.map(l => ({
    visible: l.visible,
    id: l.id,
    canvas: l.canvas,
    offset: state.layerOffsets.get(l.id) || { x: 0, y: 0 },
  }));
}
function _buildLayerUnionAlpha() { return _layerUnionAlphaImpl(state.imgWidth, state.imgHeight, _harmonizeLayerList()); }
function _buildSeamMask(featherPx = 12) { return _seamMaskImpl(state.imgWidth, state.imgHeight, _harmonizeLayerList(), featherPx); }
function _buildLayerBodyMask(featherPx = 12) { return _layerBodyMaskImpl(state.imgWidth, state.imgHeight, _harmonizeLayerList(), featherPx); }

function exportPNG() {
  return flatten().toDataURL('image/png');
}

// Briefly turn the Save button green with a checkmark so the user can't
// miss a successful save (the toast alone is easy to miss on remote
// connections where focus drifts during the upload).
function _flashSaveButtonOk() {
  const btn = _editorFind('ge-save-menu-btn');
  if (!btn) return;
  const origHTML = btn.innerHTML;
  const origBg = btn.style.background;
  btn.style.background = '#3aa75a';
  btn.style.color = '#fff';
  btn.innerHTML = uiIcon("check", 14, {"style":"vertical-align:-3px;margin-right:4px;","role":"inherit"}) + "Saved";
  setTimeout(() => {
    btn.style.background = origBg;
    btn.style.color = '';
    btn.innerHTML = origHTML;
  }, 1800);
}

// Show whirlpool + label on the visible "Save ▾" topbar button while a
// save operation runs. Returns a function to call when done (or in finally).
function _saveButtonBusy(label) {
  const btn = _editorFind('ge-save-menu-btn');
  if (!btn) return () => {};
  const origHTML = btn.innerHTML;
  const origWidth = btn.offsetWidth;
  btn.disabled = true;
  btn.style.minWidth = origWidth + 'px';
  btn.innerHTML = '';
  let sp = null;
  try {
    sp = spinnerModule.create('', 'clean', 'whirlpool');
    btn.appendChild(sp.createElement());
    const txt = document.createElement('span');
    txt.className = 'ge-btn-busy-label';
    txt.textContent = label || 'Saving…';
    btn.appendChild(txt);
    sp.start();
  } catch { btn.textContent = label || 'Saving…'; }
  return () => {
    try { sp && sp.stop && sp.stop(); } catch {}
    btn.disabled = false;
    btn.innerHTML = origHTML;
    btn.style.minWidth = '';
  };
}

// ────────── Managed Imps project surface (/api/imps/projects/*) ──────────
// Save and Load go through the managed project API: versioned editable
// state bound to a stable image resource identity, with Lore recovery and
// optimistic concurrency. Image bytes and copies are owned by Files.

async function _impsRequest(path, options = {}) {
  const resp = await fetch(`${API_BASE}${path}`, {
    credentials: 'same-origin',
    ...options,
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.headers || {}),
    },
  });
  if (!resp.ok) {
    let detail = '';
    try { const j = await resp.json(); detail = j.detail || j.error || ''; } catch {}
    throw new Error(`HTTP ${resp.status}${detail ? `: ${detail}` : ''}`);
  }
  return resp.json();
}

function _serializeEditableState() {
  return serializeEditableImageScene(state);
}

async function _blobToBase64(blob) {
  const buf = await blob.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function _bindManagedIdentity(provider, resourceId) {
  if (state.managedProvider !== (provider || null) || state.managedResourceId !== (resourceId || null)) {
    state.projectId = null;
    state.projectRevision = 0;
    state.expectedImageRevision = '';
  }
  state.managedProvider = provider || null;
  state.managedResourceId = resourceId || null;
  const m = String(resourceId || '').match(/^image:(.+)$/);
  state.imageId = m ? m[1] : state.imageId || null;
}

async function _ensureManagedProject(expectedGeneration = null, expectedProvider = null, expectedResource = null, { forceLookup = false, identityOnly = false, saveSnapshot = null } = {}) {
  const origin = _captureOrigin();
  const stillCurrent = () => _originCurrent(origin) && (expectedGeneration == null
    || (expectedGeneration === _openGeneration && state.managedProvider === expectedProvider && state.managedResourceId === expectedResource));
  if (!stillCurrent()) throw new Error('managed project open superseded');
  const achievementAccount = achievementOwner();
  const observedImageRevision = state.expectedImageRevision || '';
  if (state.projectId && !forceLookup) return state.projectId;
  if (!state.managedProvider || !state.managedResourceId) return null;
  const lookup = await _impsRequest(
    `/api/imps/projects/for-image/${encodeURIComponent(state.managedProvider)}/${encodeURIComponent(state.managedResourceId)}`
  ).catch(error => {
    if (/^HTTP 404\b/.test(String(error?.message || ''))) return { project: null };
    throw error;
  });
  if (!stillCurrent()) throw new Error('managed project lookup superseded');
  if (lookup && lookup.project) {
    const storedRevision = String(lookup.project.expected_image_revision || '');
    if (storedRevision && observedImageRevision && storedRevision !== observedImageRevision) {
      throw new Error('managed image revision conflict; reload the source before restoring this project');
    }
    state.projectId = lookup.project.id;
    state.projectRevision = lookup.project.project_revision;
    // The bytes just opened are authoritative. An old or blank project
    // revision must never replace that observed hash and authorize a write
    // against different pixels.
    state.expectedImageRevision = observedImageRevision || storedRevision;
    if (!identityOnly && lookup.project.name) state.draftName = lookup.project.name;
    if (!identityOnly && lookup.project.state && lookup.project.state.layers?.length) {
      const restored = await _restoreDraft(lookup.project.state, expectedGeneration);
      if (!stillCurrent()) throw new Error('managed project restore superseded');
      composite();
      _renderLayerPanel();
      _fitZoom();
      if (state.editorOpen && restored.decoded) {
        await recordPresentation('imps.project.reopened', {
          projectId:state.projectId, revisionId:String(state.projectRevision), layerIds:state.layers.map(layer => String(layer.id)),
        }, { accountId:achievementAccount, kind:'R', occurrenceId:crypto.randomUUID() });
      }
    }
    return state.projectId;
  }
  if (state._legacyProjectPending || state._legacyCreatePending) throw new Error('project creation outcome requires reconciliation');
  const createOperationId = `imps:${crypto.randomUUID()}`;
  state._legacyCreatePending = { operationId:createOperationId, origin };
  state._legacyProjectPending=Object.freeze({operationId:createOperationId,kind:'create',phase:'issued',owner:origin.owner,provider:state.managedProvider,resourceId:state.managedResourceId,projectId:state.projectId});
  const createDraft=await _persistDraft(); if (createDraft?.status!=='saved' || !stillCurrent()) throw new Error('project operation draft unavailable');
  let created;
  try { created = await _impsRequest('/api/imps/projects', {
    method: 'POST',
    body: JSON.stringify({
      operation_id: createOperationId,
      provider: state.managedProvider,
      resource_id: state.managedResourceId,
      name: saveSnapshot?.name || state.draftName || 'Untitled',
      width: saveSnapshot?.width || state.imgWidth || null,
      height: saveSnapshot?.height || state.imgHeight || null,
      state: saveSnapshot?.stateDoc || _serializeEditableState(),
      expected_image_revision: observedImageRevision || state.expectedImageRevision || '',
    }),
  }); } catch (error) { if (/^HTTP 4\d{2}\b/.test(String(error?.message || ''))) state._legacyCreatePending=null; throw error; }
  const createLore=created?.lore_receipt;
  if (createLore?.action_id!==createOperationId || createLore?.outcome!=='committed' || !['complete','skipped','unavailable'].includes(createLore?.history_status)) throw new Error('project creation receipt requires reconciliation');
  if (!stillCurrent()) throw new Error('managed project creation superseded; reconciliation required');
  state._legacyCreatePending=null; state._legacyProjectPending=null;
  if ((await _persistDraft())?.status!=='saved') throw new Error('project created; draft reconciliation required');
  if (createLore.history_status==='unavailable') uiModule?.showToast('Project created, but History recovery is unavailable for this change.',6000);
  state.projectId = created.id;
  state.projectRevision = created.project_revision;
  state.expectedImageRevision = created.expected_image_revision || observedImageRevision || '';
  return state.projectId;
}

function _captureManagedSaveSnapshot({ stateDoc = null, name = null } = {}) {
  if (!legacy && !stateDoc && !name) return Object.freeze({...sessionPersistence.captureSnapshot(),origin:_captureOrigin()});
  const capturedState = JSON.parse(JSON.stringify(stateDoc || _serializeEditableState()));
  const capturedName = name || state.draftName || undefined;
  return Object.freeze({
    generation: _openGeneration,
    origin: _captureOrigin(),
    target: legacy ? null : session.captureTarget(),
    operationId: `imps-save-${++_saveSequence}`,
    editEpoch: state.editEpoch,
    sceneRevision: state.sceneRevision,
    provider: state.managedProvider,
    resource: state.managedResourceId,
    projectId: state.projectId,
    projectRevision: state.projectRevision,
    expectedImageRevision: state.expectedImageRevision || '',
    draftId: state.draftId,
    stateDoc: capturedState,
    name: capturedName,
    width: state.imgWidth || undefined,
    height: state.imgHeight || undefined,
  });
}

async function _managedSave({ blob = null, stateDoc = null, name = null, operation = null } = {}) {
  if (!legacy) { const snapshot = operation || _captureManagedSaveSnapshot({stateDoc,name}); return sessionPersistence.save(snapshot,blob); }
  const snapshot = operation || _captureManagedSaveSnapshot({ stateDoc, name });
  const origin = snapshot.origin;
  const key = `${origin.accountEpoch}:${origin.generation}`;
  let flight = _managedSaveFlights.get(key);
  if (!flight) {
    const sameProject = _originCurrent(origin) && snapshot.projectId && state.projectId === snapshot.projectId;
    flight = { tail: Promise.resolve(), projectId: snapshot.projectId, revision: sameProject ? state.projectRevision : snapshot.projectRevision, imageRevision: sameProject ? state.expectedImageRevision : snapshot.expectedImageRevision };
    _managedSaveFlights.set(key, flight);
  }
  const run = async () => {
    if (flight.uncertain || state._legacyProjectPending) throw new Error('managed save outcome unknown; reconcile the project before retrying');
    const sameResource = () => _originCurrent(origin) && state.editorOpen && state.managedProvider === snapshot.provider && state.managedResourceId === snapshot.resource;
    if (!sameResource()) throw new Error('managed save superseded before publication');
    if (readOnlyImageScenes.has(_sceneAuthority) || blob && (!state._legacyRasterMime || blob.type!==state._legacyRasterMime)) throw Error('Original encoding or write access is unavailable. Use Save as copy.');
    const projectId = flight.projectId || await _ensureManagedProject(snapshot.generation, snapshot.provider, snapshot.resource, {identityOnly:true, saveSnapshot:snapshot});
    if (!sameResource() || !projectId) throw new Error('managed save superseded before publication');
    if (!flight.projectId) { flight.projectId = projectId; flight.revision = state.projectRevision; flight.imageRevision = state.expectedImageRevision; }
    if (flight.projectId !== projectId) throw new Error('managed save project changed');
    const saveOperationId = `imps:${crypto.randomUUID()}`;
    const payload = { operation_id:saveOperationId, expected_project_revision: flight.revision, expected_image_revision: flight.imageRevision || '', new_image_revision: flight.imageRevision || 'uncommitted', state: snapshot.stateDoc, name: snapshot.name, width: snapshot.width, height: snapshot.height };
    if (blob) payload.image_bytes = await _blobToBase64(blob);
    if (!sameResource()) throw new Error('managed save superseded during encoding');
    let body;
    flight.operationId = saveOperationId;
    state._legacyProjectPending=Object.freeze({operationId:saveOperationId,kind:'save',phase:'issued',owner:origin.owner,provider:snapshot.provider,resourceId:snapshot.resource,projectId});
    const saveDraft=await _persistDraft(); if (saveDraft?.status!=='saved' || !sameResource()) throw new Error('project operation draft unavailable');
    try { body = await _impsRequest(`/api/imps/projects/${encodeURIComponent(projectId)}/save`, { method:'POST', body:JSON.stringify(payload) }); }
    catch (error) { if (!/^HTTP 4\d{2}\b/.test(String(error?.message || ''))) flight.uncertain = true; throw error; }
    if (body?.lore_receipt?.action_id!==saveOperationId || body.lore_receipt.outcome!=='committed' || !['complete','skipped','unavailable'].includes(body.lore_receipt.history_status) || !Number.isSafeInteger(body.project_revision) || body.project_revision<=flight.revision) { flight.uncertain=true; throw new Error('save receipt requires reconciliation'); }
    flight.revision = body.project_revision;
    flight.imageRevision = body.refresh_receipt?.image_revision || body.refresh_receipt?.image?.revision || flight.imageRevision;
    if (!sameResource() || state.projectId !== projectId) {
      if (_accountCurrent(origin)) {
        _managedSaveReceipts.set(snapshot.operationId, {origin, projectId, revision:flight.revision, imageRevision:flight.imageRevision, editEpoch:snapshot.editEpoch, receipt:body});
        while (_managedSaveReceipts.size > 16) _managedSaveReceipts.delete(_managedSaveReceipts.keys().next().value);
      }
      throw new Error(`managed save committed for a previous editor (local receipt ${snapshot.operationId})`);
    }
    state.projectRevision = flight.revision;
    state.expectedImageRevision = flight.imageRevision;
    state._legacyProjectPending=null; if ((await _persistDraft())?.status!=='saved') {flight.uncertain=true; throw new Error('save committed; draft reconciliation required');}
    if (state.sceneRevision !== snapshot.sceneRevision || (state.draftName || undefined) !== snapshot.name) { state.persistDirty = true; _schedulePersist(); }
    return body;
  };
  const result = flight.tail.catch(() => {}).then(run);
  flight.tail = result;
  result.finally(() => { if (flight.tail === result && !flight.uncertain && _managedSaveFlights.get(key) === flight) _managedSaveFlights.delete(key); }).catch(() => {});
  return result;
}

function getManagedSaveReceipt(operationId) {
  const record = _managedSaveReceipts.get(operationId);
  if (!record || !record.origin.owner || record.origin.owner !== (document.body.dataset.accountId || null)) return null;
  return Object.freeze({ operationId, projectId:record.projectId, projectRevision:record.revision, imageRevision:record.imageRevision, editEpoch:record.editEpoch, receipt:record.receipt });
}

async function _filesGalleryDestination(signal = null) {
  const roots = await filesFacadeClient.roots({ copalWorkspace: 'default', signal });
  const entries = Array.isArray(roots?.entries) ? roots.entries : [];
  const gallery = entries.find((entry) => (
    entry?.provider === 'files'
    && entry?.kind === 'provider_root'
    && typeof entry?.ref === 'string'
    && Array.isArray(entry?.capabilities)
    && entry.capabilities.includes('write')
  ));
  if (!gallery) throw new Error('Files Gallery destination is unavailable');
  const generation = Number(roots?.policy_generation);
  if (!Number.isSafeInteger(generation) || generation < 0) throw new Error('Files policy generation is unavailable');
  return { destinationRef: gallery.ref, generation };
}

async function _resolveImportedImpsResource(resourceRef, signal = null) {
  const opened = await filesFacadeClient.openResource(resourceRef, { signal });
  const resource = opened?.resource;
  const payload = opened?.payload;
  const provider = String(payload?.provider || '').trim();
  const resourceId = String(payload?.resource_id || '').trim();
  const revision = String(resource?.revision?.value || '').trim();
  if (provider !== 'files' || !resourceId || !revision) {
    throw new Error('Files import did not return a bindable Imps image');
  }
  return { provider, resourceId, revision, resourceRef };
}

async function _bindSaveCopy(pending) {
  if (!/^imps:[0-9a-f-]{36}$/.test(pending.binding_operation_id || '') || !['unissued','issued'].includes(pending.binding_phase)) throw new Error('Copy binding identity unavailable; reconciliation required');
  const origin=_captureOrigin();
  const current=()=>_originCurrent(origin) && state.editorOpen && _openGeneration===pending.source_generation && state.editEpoch===pending.source_edit_epoch && state.projectRevision===pending.source_project_revision && state.expectedImageRevision===pending.source_image_revision && state.projectId===pending.source_project_id && state.managedProvider===pending.source_provider && state.managedResourceId===pending.source_resource_id;
  if (!current()) throw new Error('Copy source changed; reconciliation required');
  const resource=await _resolveImportedImpsResource(pending.resource_ref);
  if (!current()) throw new Error('Copy source changed; reconciliation required');
  if (!pending.source_project_id && pending.unbound!==true) throw new Error('Copy source project is unavailable');
  let copyBody;
  if (pending.binding_phase==='issued') {
    const status=await _impsRequest('/api/imps/project-operations/status',{method:'POST',body:JSON.stringify({operation_id:pending.binding_operation_id})});
    if (!current() || status?.state!=='available' || status.action_id!==pending.binding_operation_id || status.outcome!=='committed' || !['complete','skipped','unavailable'].includes(status.history_status) || !status.project_id || !Number.isSafeInteger(status.project_revision)) throw new Error('Copy binding outcome remains unknown');
    const record=await _impsRequest(`/api/imps/projects/${encodeURIComponent(status.project_id)}`);
    const confirmed=await _resolveImportedImpsResource(pending.resource_ref);
    if (!current() || record?.id!==status.project_id || record.project_revision!==status.project_revision || record.image?.provider!=='files' || record.image?.resource_id!==resource.resourceId || record.expected_image_revision!==resource.revision || confirmed.resourceId!==resource.resourceId || confirmed.revision!==resource.revision) throw new Error('Copy binding identity changed');
    copyBody={project_id:status.project_id,project_revision:status.project_revision,lore_receipt:{action_id:status.action_id,outcome:status.outcome,history_status:status.history_status}};
  } else {
    pending.binding_phase='issued';
    // The durable marker precedes the effect; a storage failure refuses dispatch.
    localStorage.setItem('imps.pendingSaveCopy',JSON.stringify(pending));
    state.pendingSaveCopy=pending;
    if (!current()) throw new Error('Copy source changed; reconciliation required');
    copyBody=await _impsRequest(pending.source_project_id ? `/api/imps/projects/${encodeURIComponent(pending.source_project_id)}/save-copy` : '/api/imps/projects/save-copy', {
      method:'POST',body:JSON.stringify({operation_id:pending.binding_operation_id,provider:resource.provider,resource_id:resource.resourceId,operation_key:pending.operation_id,expected_image_revision:resource.revision,state:pending.state,name:pending.name,width:pending.width,height:pending.height}),
    });
    if (!current() || copyBody?.lore_receipt?.action_id!==pending.binding_operation_id || copyBody.lore_receipt.outcome!=='committed' || !['complete','skipped','unavailable'].includes(copyBody.lore_receipt.history_status) || !copyBody.project_id || !Number.isSafeInteger(copyBody.project_revision)) throw new Error('Copy binding receipt requires reconciliation');
  }
  return {copyBody,resource};
}

async function isolatedSaveCopy() {
  if (sessionPersistence.saveCount || sessionPersistence.draftCount || sessionPersistence.copyBusy || sessionPersistence.pendingCopy || !state.editorOpen) return {status:'unavailable',reason:'copy_pending_or_unavailable'};
  return sessionPersistence.saveCopyCurrent();
}

async function isolatedRetryCopy() { return sessionPersistence.retryCopy(); }

// Save a copy: allocate through the Files-owned Gallery provider, then bind
// the resulting provider identity to a separate managed Imps project.
async function exportToGallery() {
  if (!legacy) return isolatedSaveCopy();
  const endBusy = _saveButtonBusy('Saving copy…');
  let blob = null;
  let savedOk = false, copyRecoveryUnavailable = false;
  const t0 = performance.now();
  try {
    // toBlob() avoids the 2x peak memory of dataURL → fetch → blob. JPEG
    // re-encode for camera photos keeps uploads small enough to make it
    // through remote tunnels.
    const sourceProjectId = state.projectId;
    const unbound = !sourceProjectId && !state.managedProvider && !state.managedResourceId;
    if (!state.editorOpen || !state.layers.length || (!unbound && (!sourceProjectId || !state.managedProvider))) {
      throw new Error('Open an editable Imps project before saving a copy');
    }
    const snapshot = {
      generation: _openGeneration, editEpoch: state.editEpoch,
      projectId: sourceProjectId, provider: state.managedProvider, resourceId: state.managedResourceId,
      state: _serializeEditableState(), name: `${state.draftName || 'Untitled'} copy`,
      width: state.imgWidth || null, height: state.imgHeight || null, draftId: state.draftId,
    };
    const currentSnapshot = () => snapshot.generation === _openGeneration && snapshot.editEpoch === state.editEpoch
      && snapshot.projectId === state.projectId && snapshot.provider === state.managedProvider && snapshot.resourceId === state.managedResourceId;
    const flat = flatten();
    const ext = (state.originalExt || 'png').toLowerCase();
    const isJpeg = ext === 'jpg' || ext === 'jpeg';
    const mime = isJpeg ? 'image/jpeg' : 'image/png';
    const quality = isJpeg ? 0.92 : undefined;
    blob = await new Promise((resolve, reject) => {
      flat.toBlob(b => b ? resolve(b) : reject(new Error('Canvas encode failed')), mime, quality);
    });
    if (!currentSnapshot()) throw new Error('Editor changed before the copy was encoded; retry with the current document');
    if (unbound && (!state._unboundCopyOperation || state._unboundCopyOperation.editEpoch !== snapshot.editEpoch)) {
      state._unboundCopyOperation = { editEpoch: snapshot.editEpoch, id: `imps-copy:unbound:${crypto.randomUUID()}` };
    }
    const operationId = unbound ? state._unboundCopyOperation.id
      : `imps-copy:${sourceProjectId}:${state.projectRevision || 0}:${state.managedResourceId || 'image'}`.slice(0, 128);
    const owner = await _confirmedImpsOwner();
    if (!owner || !currentSnapshot()) throw new Error('Signed-in owner or editor changed before copy allocation');
    const itemId = `${operationId}:asset`.slice(0, 128);
    const destination = await _filesGalleryDestination();
    const imported = await filesFacadeClient.importFile(
      new File([blob], `edited.${isJpeg ? 'jpg' : 'png'}`, { type: mime }),
      {
        operationId,
        itemId,
        generation: destination.generation,
        destinationRef: destination.destinationRef,
        name: `edited.${isJpeg ? 'jpg' : 'png'}`,
        collision: 'fail',
      },
    );
    const importedItem = imported?.items?.find((item) => item?.item_id === itemId && item?.outcome === 'committed');
    if (!importedItem?.resource_ref) throw new Error('Files import did not commit an image resource');
    const pending = {
      owner, unbound,
      source_project_id: sourceProjectId || null,
      source_provider: snapshot.provider, source_resource_id: snapshot.resourceId,
      source_generation:snapshot.generation, source_edit_epoch:snapshot.editEpoch, source_project_revision:state.projectRevision, source_image_revision:state.expectedImageRevision,
      binding_operation_id: `imps:${crypto.randomUUID()}`, binding_phase:'unissued',
      operation_id: operationId,
      item_id: itemId,
      resource_ref: importedItem.resource_ref,
      resource_key: importedItem.resource_key || null,
      provider: 'files',
      name: snapshot.name,
      state: snapshot.state,
      width: snapshot.width,
      height: snapshot.height,
    };
    try {
      const { copyBody, resource } = await _bindSaveCopy(pending);
      copyRecoveryUnavailable=copyBody.lore_receipt.history_status==='unavailable';
      _clearPendingSaveCopy();
      if (currentSnapshot()) {
        _bindManagedIdentity(resource.provider, resource.resourceId);
        state.projectId = copyBody.project_id;
        state.projectRevision = copyBody.project_revision;
        state.expectedImageRevision = copyBody.refresh_receipt?.image_revision || resource.revision;
        state.imageId = resource.resourceId;
        state._unboundCopyOperation = null;
        if (snapshot.draftId) { _clearDraftServer(snapshot.draftId); state.draftId = null; }
      }
    } catch (e) {
      // The Files-owned image is durable, but the Imps project bind is a
      // separate operation. Keep its original identity for status-only reconciliation.
      state.pendingSaveCopy = pending;
      try { localStorage.setItem('imps.pendingSaveCopy', JSON.stringify(pending)); } catch (_) {}
      const retryButton = _editorFind('ge-retry-save-copy');
      if (retryButton) retryButton.hidden = false;
      if (uiModule) uiModule.showToast('Image imported; project binding requires reconciliation.', 5000);
      return;
    }
    const totalMs = Math.round(performance.now() - t0);
    window.dispatchEvent(new CustomEvent('imps-refresh'));
    if (uiModule) uiModule.showToast(copyRecoveryUnavailable ? 'Copy saved, but History recovery is unavailable for this change.' : `Saved copy to Gallery (${(blob.size / 1024 / 1024).toFixed(1)}MB · ${(totalMs / 1000).toFixed(1)}s)`, copyRecoveryUnavailable ? 6000 : 4000);
    savedOk = true;
  } catch (e) {
    console.error('[save-as-copy] error:', e);
    const sizeMB = blob ? ` (${(blob.size / 1024 / 1024).toFixed(1)}MB)` : '';
    let msg = e?.message || 'unknown';
    if (e?.name === 'TypeError' || /fetch|network|load failed/i.test(msg)) {
      msg = `network dropped${sizeMB} — check connection`;
    } else {
      msg += sizeMB;
    }
    if (uiModule) uiModule.showToast('Save failed: ' + msg, 6000);
  } finally {
    endBusy();
    if (savedOk) _flashSaveButtonOk();
  }
}

async function retryPendingSaveCopy() {
  if (!legacy) return isolatedRetryCopy();
  const pending = state.pendingSaveCopy || (() => {
    try { return JSON.parse(localStorage.getItem('imps.pendingSaveCopy') || 'null'); } catch (_) { return null; }
  })();
  if (!(pending?.source_project_id || pending?.unbound === true) || !pending.operation_id || !pending.resource_ref) throw new Error('No pending Imps copy binding');
  const owner = await _confirmedImpsOwner();
  if (!owner || !pending.owner || owner !== String(pending.owner).toLowerCase()) {
    _clearPendingSaveCopy();
    throw new Error('Pending copy belongs to another signed-in owner');
  }
  const { copyBody: body, resource } = await _bindSaveCopy(pending);
  if (body.lore_receipt.history_status==='unavailable') uiModule?.showToast('Copy saved, but History recovery is unavailable for this change.',6000);
  _clearPendingSaveCopy();
  _bindManagedIdentity(resource.provider, resource.resourceId);
  state.projectId = body.project_id;
  state.projectRevision = body.project_revision;
  state.expectedImageRevision = body.refresh_receipt?.image_revision || resource.revision;
  state.imageId = resource.resourceId;
  return body;
}

// Open the Cookbook modal scoped to img2img-capable models so the user
// can serve one in a few clicks. Falls back to plain Cookbook if the
// filter hook isn't available.
// Open Cookbook on its Dependencies tab and highlight a specific
// package row. Used for "rembg not installed" → install path.
function _openCookbookForDependency(pkgName) {
  // Use cookbookModule.open({ tab: 'Dependencies' }) so the intent is
  // honored after Cookbook's async render. The old path clicked the
  // sidebar button + polled for the modal, but Cookbook's _renderRecipes
  // runs AFTER an awaited _syncFromServer, so depsTab.click() often
  // raced and the user landed on Download.
  const cookbook = window.cookbookModule;
  if (!cookbook || typeof cookbook.open !== 'function') {
    // Fall back to the old click-then-poll path if the module isn't
    // on window for some reason.
    const btn = document.getElementById('tool-cookbook-btn');
    if (btn) btn.click();
    else if (uiModule) uiModule.showToast(`Open Cookbook to install ${pkgName}`, 6000);
    return;
  }
  cookbook.open({ tab: 'Dependencies' });
  // Now wait for the Dependencies group to render, switch the server
  // selector to Local, and highlight the package row.
  const cb = document.getElementById('cookbook-modal');
  if (cb) cb.style.zIndex = 260;
  const tryServer = (attempt = 0) => {
    const serverSel = document.getElementById('hwfit-deps-server');
    if (!serverSel) {
      if (attempt < 25) return setTimeout(() => tryServer(attempt + 1), 80);
      return;
    }
    if (serverSel.value !== 'local') {
      serverSel.value = 'local';
      serverSel.dispatchEvent(new Event('change', { bubbles: true }));
    }
  };
  tryServer();
  const tryHighlight = (a2 = 0) => {
    const rows = document.querySelectorAll('[data-pkg-name]');
    if (!rows.length) {
      if (a2 < 40) return setTimeout(() => tryHighlight(a2 + 1), 100);
      return;
    }
    const row = Array.from(rows).find(r => (r.dataset.pkgName || '').toLowerCase() === pkgName.toLowerCase());
    if (row) {
      row.scrollIntoView({ block: 'center' });
      row.classList.add('cookbook-pkg-flash');
      setTimeout(() => row.classList.remove('cookbook-pkg-flash'), 2000);
    }
  };
  tryHighlight();
}

// Async check whether `rembg` is installed on the Odysseus server.
// Toggles the "install rembg" notice + the Bg Remove run button. The
// `/api/cookbook/packages` endpoint is cheap (importlib calls only).
async function _checkRembgInstalled() {
  const noticeEl = _editorFind('ge-rembg-dep-missing');
  const runRow = _editorFind('ge-rembg-run-row');
  if (!noticeEl || !runRow) return;
  // Use cached result if we already checked this editor session.
  if (state.rembgInstalledCache !== null) {
    noticeEl.style.display = state.rembgInstalledCache ? 'none' : '';
    runRow.style.display = state.rembgInstalledCache ? '' : 'none';
    return;
  }
  try {
    const r = await fetch('/api/cookbook/packages', { credentials: 'same-origin' });
    if (!r.ok) throw new Error('packages query failed');
    const data = await r.json();
    const pkg = (data.packages || []).find(p => (p.name || '').toLowerCase() === 'rembg');
    state.rembgInstalledCache = pkg ? !!pkg.installed : null;
  } catch (e) {
    state.rembgInstalledCache = null; // unknown — fall back to silent
  }
  if (state.rembgInstalledCache === false) {
    noticeEl.style.display = '';
    runRow.style.display = 'none';
  } else {
    noticeEl.style.display = 'none';
    runRow.style.display = '';
  }
}

function _openCookbookForImg2img() {
  // Try multiple openers in order — the sidebar button may be hidden on
  // mobile so we fall back to the rail button, then to modalManager.
  let opened = false;
  const btn = document.getElementById('tool-cookbook-btn');
  const railBtn = document.getElementById('rail-cookbook');
  if (btn && btn.offsetParent !== null) { btn.click(); opened = true; }
  else if (railBtn) { railBtn.click(); opened = true; }
  else { try { modalManager.restore('cookbook-modal'); opened = true; } catch {} }
  if (opened) {
    // Two-stage navigation: 1) wait for modal mount, 2) click Serve tab,
    // 3) after the serve tag chips render, click the "image" one.
    const tryServe = (attempt = 0) => {
      const cb = document.getElementById('cookbook-modal');
      const serveTab = cb ? cb.querySelector('.cookbook-tab[data-backend="Serve"]') : null;
      // Retry until BOTH the modal mounts AND its tab bar has rendered.
      // Cookbook builds its body html after the modal opens, so we need
      // to wait a bit longer than just "modal exists".
      if (!cb || !serveTab) {
        if (attempt < 40) return setTimeout(() => tryServe(attempt + 1), 80);
        return;
      }
      cb.style.zIndex = 260;
      serveTab.click();
      // Now wait for the serve-tags container to populate (it lazy-loads
      // after the cached-models fetch resolves) and click the image chip.
      const tryImageFilter = (a2 = 0) => {
        const tags = document.getElementById('serve-tags');
        if (!tags || !tags.querySelector('.memory-cat-chip')) {
          if (a2 < 20) return setTimeout(() => tryImageFilter(a2 + 1), 100);
          return;
        }
        const imgChip = Array.from(tags.querySelectorAll('.memory-cat-chip'))
          .find(c => /^image$/i.test(c.dataset.serveTag || '') || /image/i.test(c.textContent || ''));
        if (imgChip) imgChip.click();
      };
      tryImageFilter();
    };
    tryServe();
    return;
  }
  if (uiModule) uiModule.showToast('Open Cookbook from the sidebar to serve an img2img model', 6000);
}

function downloadPNG() {
  const dataUrl = exportPNG();
  const a = document.createElement('a');
  a.href = dataUrl;
  a.download = 'edited-image.png';
  a.click();
}

// Save the entire layered editor state through the managed Imps project
// surface, then download the portable managed export. Each layer is
// encoded as a base64 PNG so transparency / partial alpha survives the
// round-trip. Use Load Project to restore.
async function _saveProject() {
  if (!legacy) return {status:'unavailable', reason:'isolated_save_pending'};
  if (!state.layers.length) {
    if (uiModule) uiModule.showToast('Nothing to save');
    return;
  }
  if (!state.managedResourceId && state.imageId) {
    _bindManagedIdentity('files', `image:${state.imageId}`);
  }
  try {
    const projectId = await _ensureManagedProject(null, null, null, {identityOnly:true});
    if (!projectId) {
      // Blank canvas with no bound resource: persist as a portable managed
      // export only — the caller decides where the bytes go.
      const bundle = {
        kind: 'imps-project',
        schema_version: 1,
        name: state.draftName || 'Untitled',
        width: state.imgWidth,
        height: state.imgHeight,
        image: { provider: state.managedProvider || '', resource_id: state.managedResourceId || '', revision: state.expectedImageRevision || '' },
        project_revision: state.projectRevision || 1,
        state: _serializeEditableState(),
        assets: [],
      };
      const completed = await _impsRequest('/api/imps/projects/export-bundle', { method: 'POST', body: JSON.stringify(bundle) });
      await _downloadJson(completed, 'project.imps.json');
      if (uiModule) uiModule.showToast('Project exported', 3000);
      return;
    }
    // Persist the editable state under CAS, then export the managed bundle.
    await _managedSave({ stateDoc: _serializeEditableState(), name: state.draftName || 'Untitled' });
    const bundle = await _impsRequest(`/api/imps/projects/${encodeURIComponent(projectId)}/export`);
    await _downloadJson(bundle, `${(state.draftName || 'project').replace(/[^\w.-]+/g, '_')}.imps.json`);
    if (uiModule) uiModule.showToast('Project saved', 3000);
  } catch (e) {
    console.error('[save-project] managed save failed:', e);
    if (uiModule) uiModule.showToast('Project save failed: ' + (e.message || e), 6000);
  }
}

async function _downloadJson(value, filename, isCurrent = () => true) {
  const json = JSON.stringify(value);
  const blob = new Blob([json], { type: 'application/json' });
  JSON.parse(await blob.text());
  const container = state.container?.querySelector('.ge-topbar-right');
  if (!state.editorOpen || !container?.isConnected || !isCurrent()) throw new Error('Editor changed before the export was ready');
  state._projectDownloadRelease?.();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.className = 'ge-btn ge-btn-sm';
  a.textContent = 'Download project'; a.title = filename;
  container.appendChild(a);
  state._projectDownloadRelease = () => { a.remove(); URL.revokeObjectURL(url); state._projectDownloadRelease = null; };
  if (!isCurrent()) { state._projectDownloadRelease(); throw new Error('Editor changed before the export was ready'); }
  a.click();
}

async function _exportIsolatedProject() {
  if (legacy || !state.editorOpen || !state.layers.length) return {status:'unavailable',reason:'image_project_export_unavailable'};
  const snapshot = _captureManagedSaveSnapshot(), target = JSON.stringify(session.captureTarget());
  const current = () => _originCurrent(snapshot.origin) && state.editorOpen && state.sceneRevision === snapshot.sceneRevision && JSON.stringify(session.captureTarget()) === target;
  const bundle = {kind:'imps-project',schema_version:1,name:snapshot.name || 'Untitled',width:snapshot.width,height:snapshot.height,
    image:{provider:snapshot.provider || '',resource_id:snapshot.resource || '',revision:snapshot.expectedImageRevision || ''},
    project_revision:snapshot.projectRevision || 1,state:snapshot.stateDoc,assets:[]};
  const artifact = await _impsRequest('/api/imps/projects/export-bundle',{method:'POST',body:JSON.stringify(bundle)});
  if (!current()) throw new Error('Image changed before the project export was ready');
  await _downloadJson(artifact,`${(snapshot.name || 'project').replace(/[^\w.-]+/g,'_')}.imps.json`,current);
  return {status:'exported'};
}

// Open-file picker for Load Project. Accepts the managed Imps export and
// the legacy ``project.geproj.json`` format. Restores layers + canvas size.
function _loadProjectPrompt() {
  if (!legacy) return {status:'unavailable', reason:'isolated_project_open_pending'};
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = 'application/json,.json';
  inp.addEventListener('change', async () => {
    const file = inp.files && inp.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const proj = JSON.parse(text);
      if (proj && proj.kind === 'imps-project') {
        // Managed portable export: import into the managed surface (a new
        // project bound to the stated image identity), then restore layers.
        const image = proj.image || {};
        const identity = image.resource_id
          ? { provider: image.provider || 'files', resource_id: image.resource_id }
          : (state.managedResourceId
            ? { provider: state.managedProvider || 'files', resource_id: state.managedResourceId }
            : null);
        const validated = await _impsRequest('/api/imps/projects/validate-bundle', { method: 'POST', body: JSON.stringify(proj) });
        if (!identity) {
          await _restoreDraft(validated.state);
          _bindManagedIdentity(null, null);
          state.imageId = null;
          composite(); _renderLayerPanel(); _fitZoom();
          if (uiModule) uiModule.showToast('Project loaded', 3000);
          return;
        }
        if (state._legacyProjectPending || state._legacyImportPending) throw new Error('Project import outcome requires reconciliation');
        const importOperationId=`imps:${crypto.randomUUID()}`;
        state._legacyImportPending={operationId:importOperationId,origin:_captureOrigin()};
        state._legacyProjectPending=Object.freeze({operationId:importOperationId,kind:'import',phase:'issued',owner:state._legacyImportPending.origin.owner,provider:state.managedProvider,resourceId:state.managedResourceId,projectId:state.projectId});
        const importDraft=await _persistDraft(); if (importDraft?.status!=='saved' || !_originCurrent(state._legacyImportPending.origin)) throw new Error('project operation draft unavailable');
        let imported;
        try { imported = await _impsRequest('/api/imps/projects/import', {
          method: 'POST',
          body: JSON.stringify({
            operation_id:importOperationId,
            provider: identity.provider,
            resource_id: identity.resource_id,
            bundle: proj,
            expected_image_revision: image.revision || '',
          }),
        }); } catch (error) { if (/^HTTP 4\d{2}\b/.test(String(error?.message || ''))) state._legacyImportPending=null; throw error; }
        const importLore=imported?.lore_receipt;
        if (importLore?.action_id!==importOperationId || importLore?.outcome!=='committed' || !['complete','skipped','unavailable'].includes(importLore?.history_status) || !_originCurrent(state._legacyImportPending.origin)) throw new Error('Project import receipt requires reconciliation');
        state._legacyImportPending=null; state._legacyProjectPending=null;
        if ((await _persistDraft())?.status!=='saved') throw new Error('project imported; draft reconciliation required');
        if (importLore.history_status==='unavailable') uiModule?.showToast('Project imported, but History recovery is unavailable for this change.',6000);
        _bindManagedIdentity(identity.provider, identity.resource_id);
        state.projectId = imported.id;
        state.projectRevision = imported.project_revision;
        state.expectedImageRevision = imported.expected_image_revision || '';
        await _restoreDraft(proj.state && proj.state.layers ? proj.state : proj);
        composite();
        _renderLayerPanel();
        _fitZoom();
        if (uiModule) uiModule.showToast('Project loaded', 3000);
        return;
      }
      if (proj.type !== 'openclank-imps-editor-project') {
        if (uiModule) uiModule.showToast('Not a project file', 5000);
        return;
      }
      await _restoreDraft(proj);
      composite();
      _renderLayerPanel();
      _fitZoom();
      if (uiModule) uiModule.showToast('Project loaded', 3000);
    } catch (e) {
      if (uiModule) uiModule.showToast('Load failed: ' + (e.message || e), 6000);
    }
  });
  inp.click();
}

// ── Public API ──

// Styled in-app prompt for canvas size — replaces the browser's
// native prompt() which doesn't follow the app theme. Returns a Promise
// resolving to {w, h} on submit, or null on cancel. Optional opts:
//   title, okLabel, initialW, initialH.
function _promptCanvasSize(opts) {
  opts = opts || {};
  const title    = opts.title    || 'New canvas';
  const okLabel  = opts.okLabel  || 'Create';
  const initialW = opts.initialW || 1024;
  const initialH = opts.initialH || 1024;
  return new Promise(resolve => {
    let overlay = _editorFind('ge-canvas-size-overlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'ge-canvas-size-overlay';
      overlay.className = 'modal';
      overlay.innerHTML = _canvasSizePromptHTML();
      scopeIds(overlay);
      _editorPortal?.appendChild(overlay);
    }
    overlay.style.display = '';
    overlay.classList.remove('hidden');
    const ownerLifetime = _coreLifetime;
    const wInput = overlay.querySelector(':is(#ge-canvas-prompt-w,[data-ge-id="ge-canvas-prompt-w"])');
    const hInput = overlay.querySelector(':is(#ge-canvas-prompt-h,[data-ge-id="ge-canvas-prompt-h"])');
    const okBtn = overlay.querySelector(':is(#ge-canvas-prompt-ok,[data-ge-id="ge-canvas-prompt-ok"])');
    const cancelBtn = overlay.querySelector(':is(#ge-canvas-prompt-cancel,[data-ge-id="ge-canvas-prompt-cancel"])');
    const titleEl = overlay.querySelector(':is(#ge-canvas-prompt-title,[data-ge-id="ge-canvas-prompt-title"])');
    if (titleEl) titleEl.textContent = title;
    if (okBtn) okBtn.textContent = okLabel;
    wInput.value = String(initialW);
    hInput.value = String(initialH);
    let settled = false;
    const timer = setTimeout(() => { if (!settled && !ownerLifetime?.disposed) { wInput.focus(); wInput.select(); } }, 0);
    function cleanup(result) {
      if (settled) return; settled = true; clearTimeout(timer);
      overlay.style.display = 'none';
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      overlay.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey);
      resolve(result);
    }
    function onOk() {
      const dims = _parseCanvasSizePrompt(wInput.value, hInput.value, initialW, initialH);
      if (!dims) { uiModule.showToast('Invalid size'); return; }
      cleanup(dims);
    }
    function onCancel() { cleanup(null); }
    function onBackdrop(e) { if (e.target === overlay) cleanup(null); }
    function onKey(e) {
      if (ownerLifetime?.disposed || !overlay.contains(document.activeElement)) return;
      if (e.key === 'Enter') { e.preventDefault(); onOk(); }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cleanup(null); }
    }
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    overlay.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey);
    ownerLifetime?.add(() => cleanup(null));
  });
}

function _parseCanvasSizePrompt(widthText, heightText, initialW = 1024, initialH = 1024) {
  const parseWhole = (value) => {
    const text = String(value || '').trim();
    if (!/^\d+$/.test(text)) return null;
    const n = Number(text);
    return Number.isSafeInteger(n) && n >= 1 && n <= 8192 ? n : null;
  };
  const parseRatio = (value) => {
    const m = String(value || '').trim().match(/^(\d+(?:\.\d+)?)\s*(?:x|×|:|\/)\s*(\d+(?:\.\d+)?)$/i);
    if (!m) return null;
    const rw = Number(m[1]);
    const rh = Number(m[2]);
    if (!Number.isFinite(rw) || !Number.isFinite(rh) || rw <= 0 || rh <= 0) return null;
    const w = Math.max(1, Math.min(8192, Math.round(initialW)));
    const h = Math.max(1, Math.min(8192, Math.round(w * rh / rw)));
    return { w, h };
  };
  const ratioDims = parseRatio(widthText) || parseRatio(heightText);
  if (ratioDims) return ratioDims;
  const w = parseWhole(widthText);
  const h = parseWhole(heightText);
  if (!w || !h) return null;
  return { w, h };
}

// imageUrl=null + presetSize={w,h} → skips the size prompt and creates a
// blank canvas at the given dimensions (used by template tiles in the
// retired modal landing). `displayName` is optional — when provided,
// the Imps title is updated to "Edit: <name>".
// Shared loading-overlay mount/unmount — used by the image-load path AND
// the draft-restore paths so every "we're waiting on something" moment
// in the editor surfaces the same whirlpool + label instead of a blank
// canvas that looks broken.
function _mountEditorLoading(label, dims) {
  if (!state.container) return;
  const area = state.container.querySelector('.ge-canvas-area');
  _unmountEditorLoading();
  // Cover the WHOLE editor (toolbar + canvas + panel), not just the canvas area
  // — otherwise the toolbar/old content shows above the overlay at the top while
  // a past project loads, which looks half-rendered.
  const el = document.createElement('div');
  el.className = 'ge-loading-overlay ge-loading-overlay-full';
  // Aspect-ratio placeholder so the user sees the shape of the canvas they're
  // about to land in. Sized to the canvas area but centered in the overlay.
  let placeholder = null;
  if (dims && dims.w > 0 && dims.h > 0 && area) {
    placeholder = document.createElement('div');
    placeholder.className = 'ge-canvas-placeholder';
    const areaRect = area.getBoundingClientRect();
    const maxW = Math.max(0, areaRect.width - 32);
    const maxH = Math.max(0, areaRect.height - 32);
    const ratio = dims.w / dims.h;
    let w = maxW;
    let h = w / ratio;
    if (h > maxH) { h = maxH; w = h * ratio; }
    placeholder.style.width = w + 'px';
    placeholder.style.height = h + 'px';
    el.appendChild(placeholder);
  }
  const inner = document.createElement('div');
  inner.className = 'ge-loading-inner';
  inner.innerHTML = `<span class="ge-loading-text">${label || 'Loading…'}</span>`;
  el.appendChild(inner);
  // Mount on the editor BODY (toolbar + canvas + panel) — it sits below the
  // old search/select bar, so the cover doesn't bleed up over those.
  const _mountTarget = state.container.querySelector('.ge-editor-body') || state.container;
  _mountTarget.appendChild(el);
  try {
    const sp = spinnerModule.create('', 'clean', 'whirlpool');
    inner.insertBefore(sp.createElement(), inner.firstChild);
    sp.start();
    el._spinner = sp;
  } catch {}
  el._placeholder = placeholder;
  state.editorLoadingEl = el;
}
function _unmountEditorLoading() {
  if (!state.editorLoadingEl) return;
  try { state.editorLoadingEl._spinner?.destroy(); } catch {}
  try { state.editorLoadingEl._placeholder?.remove(); } catch {}
  try { state.editorLoadingEl.remove(); } catch {}
  state.editorLoadingEl = null;
}

function openEditor(imageUrl, imageId, presetSize, displayName, draftId, openGeneration = null, sourceToken = null) {
  if (!legacy && (lifetime.disposed || authority.validate(session.captureTarget()) !== true || draftId || (imageUrl && sourceToken !== isolatedSourceToken))) return Promise.resolve({status:'unavailable', reason:'isolated_source_open_pending'});
  if (!legacy && retainedScene) return Promise.resolve({status:'unavailable', reason:'retained_scene_requires_mount'});
  if (!legacy && state.editorOpen) return Promise.resolve({status:'unavailable', reason:'close_current_image_first'});
  const generation = openGeneration ?? ++_openGeneration;
  for (const key of _managedSaveFlights.keys()) if (key !== `${_impsAccountEpoch}:${generation}`) _managedSaveFlights.delete(key);
  for (const key of _draftFlights.keys()) if (key !== generation) _draftFlights.delete(key);
  const openOrigin = _captureOrigin();
  state._projectDownloadRelease?.();
  state.editEpoch += 1;
  state.imageId = imageId || null;
  if (legacy) state._legacyRasterMime=null;
  // Legacy image opens still receive a raw image id; normalize them onto
  // the managed identity so Save uses /api/imps/projects/*.
  if (imageId && !state.managedResourceId) {
    _bindManagedIdentity('files', `image:${imageId}`);
  } else if (!imageId && !state.managedResourceId) {
    // Blank canvas: no bound image resource and no managed project yet.
    _bindManagedIdentity(null, null);
    state.imageRef = null;
    state.projectId = null;
    state.projectRevision = 0;
    state.expectedImageRevision = '';
  }
  // Track original file extension so save-over-original can re-encode in the
  // same format. JPEG re-encoding cuts upload size 5-10x for camera photos,
  // which matters over remote tunnels (Tailscale Funnel etc.).
  try {
    const m = (imageUrl || '').match(/\.([a-z0-9]{2,5})(?:\?|$)/i);
    state.originalExt = m ? m[1].toLowerCase() : 'png';
  } catch { state.originalExt = 'png'; }
  state.draftId = draftId || null;
  state.draftName = displayName || (presetSize ? `New ${presetSize.w}×${presetSize.h}` : 'Untitled');
  state.editorOpen = true;
  state._editableExtras = null;
  state._unboundCopyOperation = null;
  state.layers = [];
  state.undoStack = [];
  state.redoStack = [];
  state.layerOffsets.clear();
  state.nextLayerId = 1;
  state.tool = 'move';
  state.cropRect = null;
  state.lassoPoints = [];
  state.lassoActive = false;
  if (legacy) window.__impsEditLive = true;
  if (state.persistTimer) { clearTimeout(state.persistTimer); state.persistTimer = null; }
  state.persistDirty = false;
  state.persistInFlight = null;

  // Keep the canvas in the shared applet frame. Minimize hides the window
  // without rebuilding the editor, preserving layers, history and selection.
  if (legacy) {
  if (!_editorWindow) {
    _editorWindow = createOpenClankWindow({
      id: 'imps-editor-window',
      label: 'Image Editor',
      icon: 'image',
      subtitle: 'Imps',
      className: 'imps-window',
      minWidth: 680,
      minHeight: 420,
      onBeforeClose: () => _closeEditorContent(),
    });
    const container = document.createElement('div');
    container.id = 'imps-editor-container';
    _editorWindow.body.append(container);
  }
  state.container = _editorWindow.body.querySelector('#imps-editor-container');
  _editorWindow.setSubtitle(state.draftName);
  _editorWindow.show();
  } else {
    const container = root.ownerDocument.createElement('div');
    container.className = 'imps-runtime-container';
    state.container = container;
  }
  state.container.style.display = 'flex';

  try {
    _buildEditor(state.container);
    if (!legacy) { document.body.appendChild(_editorPortal); root.appendChild(state.container); }
    if (!legacy) for (const id of [...(typeof persistence.save === 'function' && typeof persistence.bindReceipt === 'function' ? [] : ['ge-save']), ...(typeof persistence.startCopy === 'function' && typeof persistence.retryCopy === 'function' ? [] : ['ge-export-gallery','ge-retry-save-copy']), 'ge-save-project','ge-load-project']) { const button = _editorFind(id); if (button) { button.disabled = true; button.title = 'Unavailable until session persistence is connected'; } }
  } catch (e) {
    console.error('[openEditor] _buildEditor threw:', e);
    if (uiModule) uiModule.showError('Editor failed to build: ' + (e?.message || 'unknown'));
    if (!legacy) { _coreLifetime?.dispose(); state.container?.remove(); state.container = null; state.editorOpen = false; }
    return;
  }

  function _initCanvas(w, h) {
    state.imgWidth = w;
    state.imgHeight = h;
    state.mainCanvas.width = w;
    state.mainCanvas.height = h;
    state.maskCanvas = document.createElement('canvas');
    state.maskCanvas.width = w;
    state.maskCanvas.height = h;
    state.maskCtx = state.maskCanvas.getContext('2d');
  }

  if (!imageUrl && draftId) {
    // Re-open a saved draft by its server-side id — covers the
    // "Resume" buttons on the Edit-tab landing.
    _mountEditorLoading('Loading draft…', presetSize || null);
    // Bail if the user closes the editor while the async load is in
    // flight — without this guard, the .then() callbacks fire after
    // closeEditor and re-mount the spinner / draw into a dead canvas,
    // leaving "stuck" preview artefacts on the next open.
    return _loadDraftById(draftId)
      .then(d => {
        if (!state.editorOpen || !_originCurrent(openOrigin)) return;
        if (!d) {
          _unmountEditorLoading();
          if (uiModule) uiModule.showToast('Draft not found');
          closeEditor();
          return;
        }
        state.draftId = d.id;
        state.draftName = d.name || 'Untitled';
        state.imageId = d.source_image_id || null;
        return _restoreDraft(d, generation, openOrigin).then(() => {
          if (!state.editorOpen || !_originCurrent(openOrigin)) return;
          composite();
          _renderLayerPanel();
          _fitZoom();
          const sizeLabel = _editorFind('ge-canvas-size');
          if (sizeLabel) sizeLabel.textContent = `${state.imgWidth}×${state.imgHeight}`;
          _unmountEditorLoading();
          if (uiModule) uiModule.showToast('Resumed draft');
        });
      })
      .catch(err => {
        if (!state.editorOpen || !_originCurrent(openOrigin)) return;
        _unmountEditorLoading();
        console.warn('[ge] draft load failed', err);
        if (uiModule) uiModule.showToast('Failed to load draft');
        closeEditor();
      });
  }

  if (!imageUrl) {
    // Empty canvas — use preset size if supplied, otherwise show the
    // styled prompt. Asynchronous: we promise-chain so callers can await
    // openEditor() and still rely on isEditorOpen() afterwards.
    const _finishBlank = (w, h) => {
      if (!state.editorOpen || !_originCurrent(openOrigin)) return;
      _initCanvas(w, h);
      // White-filled Background so the canvas is visible, then a separate
      // transparent Edit layer on top — keeps user's work isolated from
      // the underlying canvas, the standard editor pattern.
      const bgLayer = createLayer('Background', w, h);
      bgLayer.ctx.fillStyle = '#ffffff';
      bgLayer.ctx.fillRect(0, 0, w, h);
      const editLayer = createLayer('Edit', w, h);
      state.layers.push(bgLayer);
      state.layers.push(editLayer);
      state.activeLayerId = editLayer.id;
      _markSceneChanged();
      composite();
      _renderLayerPanel();
      _fitZoom();
      // First persist creates the server-side row (blank-canvas drafts).
      _schedulePersist();
      if (!legacy) sceneReady=true;
    };
    if (presetSize && presetSize.w > 0 && presetSize.h > 0) {
      _finishBlank(presetSize.w, presetSize.h);
      return;
    }
    return _promptCanvasSize().then(size => {
      if (!state.editorOpen || generation !== _openGeneration || !_originCurrent(openOrigin)) return;
      if (!size) { if (legacy) closeEditor(); else _closeEditorContent(); return {status:'cancelled'}; }
      _finishBlank(size.w, size.h);
    });
  }

  // Try to restore a previously-persisted draft for this image — that
  // way closing the editor mid-edit doesn't lose progress.
  // (Server-backed: look up by source_image_id.)
  _mountEditorLoading('Looking up draft…');
  return (legacy ? _findDraftForImage(imageId) : Promise.resolve(null)).then(_draft => {
    if (!state.editorOpen || generation !== _openGeneration || !_originCurrent(openOrigin)) return Promise.reject(new Error('editor open superseded'));
    if (!_draft) return null;
    state.draftId = _draft.id;
    state.draftName = _draft.name || displayName || 'Untitled';
    const innerLabel = state.editorLoadingEl?.querySelector('.ge-loading-text');
    if (innerLabel) innerLabel.textContent = 'Resuming draft…';
    return _restoreDraft(_draft, generation, openOrigin).then(() => {
      if (!state.editorOpen || generation !== _openGeneration || !_originCurrent(openOrigin)) return Promise.reject(new Error('editor open superseded'));
      // If the draft was broken/empty (0 layers reconstructed), fall
      // through to loading the source image as a normal edit. Without
      // this guard the editor would sit empty and the user would be
      // stuck with no way to recover.
      if (state.layers.length === 0) {
        console.warn('[openEditor] draft restored but produced 0 layers — falling back to source image');
        return null;
      }
      composite();
      _renderLayerPanel();
      _fitZoom();
      const sizeLabel = _editorFind('ge-canvas-size');
      if (sizeLabel) sizeLabel.textContent = `${state.imgWidth}×${state.imgHeight}`;
      _unmountEditorLoading();
      if (uiModule) uiModule.showToast('Resumed previous edit');
      return 'restored';
    });
  }).then(restored => {
    if (!state.editorOpen || generation !== _openGeneration || !_originCurrent(openOrigin)) return Promise.reject(new Error('editor open superseded'));
    if (restored) return;
    return _loadSourceImage();
  }).catch(err => {
    if (!state.editorOpen || generation !== _openGeneration || !_originCurrent(openOrigin)) throw err;
    _unmountEditorLoading();
    console.warn('[openEditor] draft lookup failed', err);
    return _loadSourceImage();
  });
  function _loadSourceImage() {

  // Loading overlay — whirlpool + "Loading" label while the source image
  // downloads / decodes. Especially important for multi-MB photos where
  // the canvas would otherwise sit blank for several seconds with no
  // feedback. If a draft-lookup overlay is already mounted, reuse it.
  if (!state.editorLoadingEl) _mountEditorLoading('Loading…');
  else {
    const inner = state.editorLoadingEl.querySelector('.ge-loading-text');
    if (inner) inner.textContent = 'Loading…';
  }
  const _removeLoading = () => _unmountEditorLoading();

  // Load image — single layer named "Photo" (no extra Edit layer; the
  // user can add one manually if they want isolated edits).
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    if (!state.editorOpen || !_originCurrent(openOrigin)) return;
    _initCanvas(img.naturalWidth, img.naturalHeight);
    const photoLayer = createLayer('Photo', state.imgWidth, state.imgHeight);
    photoLayer.ctx.drawImage(img, 0, 0);
    photoLayer.isBase = true;
    state.layers.push(photoLayer);
    state.activeLayerId = photoLayer.id;
    _markSceneChanged();
    composite();
    _renderLayerPanel();
    _fitZoom();
    _removeLoading();
    _schedulePersist();
  };
  img.onerror = (e) => {
    console.error('[_loadSourceImage] onerror — failed to load', imageUrl, e);
    _removeLoading();
    if (uiModule) uiModule.showToast('Failed to load image');
    closeEditor();
  };
  return new Promise((resolve, reject) => {
    const loaded = img.onload;
    const failed = img.onerror;
    img.onload = (...args) => {
      if (!state.editorOpen || generation !== _openGeneration || !_originCurrent(openOrigin)) return reject(new Error('editor open superseded'));
      if (!legacy && (!img.naturalWidth || !img.naturalHeight || img.naturalWidth > CANVAS_MAX_IMPORT_DIMENSION || img.naturalHeight > CANVAS_MAX_IMPORT_DIMENSION || img.naturalWidth * img.naturalHeight > CANVAS_MAX_IMPORT_PIXELS)) return reject(new Error('image dimensions exceed import limits'));
      try { loaded(...args); resolve(); } catch (error) { reject(error); }
    };
    img.onerror = (...args) => {
      if (!state.editorOpen || generation !== _openGeneration || !_originCurrent(openOrigin)) return reject(new Error('editor open superseded'));
      failed(...args); reject(new Error('failed to load image'));
    };
    img.src = imageUrl;
  });
  }
}

function closeEditor() {
  return legacy && _editorWindow ? _editorWindow.requestClose() : {status:'choice_required'};
}

function _clearSceneState() {
  if (state.persistTimer) { clearTimeout(state.persistTimer); state.persistTimer = null; }
  state.layers = []; state.undoStack = []; state.redoStack = []; state.layerOffsets.clear();
  if (!legacy) { state.wandMask = null; state.wandLayerId = null; state.wandLastSeed = null; }
  state._editableExtras = null; state.imageId = null; state.imageRef = null; state.managedProvider = null; state.managedResourceId = null;
  state.projectId = null; state.projectRevision = 0; state.expectedImageRevision = '';
  if (!legacy) { _draftFlights.clear(); _managedSaveFlights.clear(); _managedSaveReceipts.clear(); }
}

function _closeEditorContent() {
  if (legacy) readOnlyImageScenes.delete(_sceneAuthority);
  _activeFilterPreview?.finish();
  if (state.transformActive) _cancelTransform();
  // Flush any pending debounced persist + fire one final save so closing
  // the editor mid-stroke doesn't lose work. The call is fire-and-forget;
  // the server commit lands shortly after the modal hides.
  if (legacy && state.persistTimer) { clearTimeout(state.persistTimer); state.persistTimer = null; }
  if (legacy && state.layers.length) {
    try { _persistDraft(); } catch {}
  }
  _openGeneration += 1;
  _unmountEditorLoading();
  state.editorOpen = false;
  _closeTransformPopup();
  _coreLifetime?.dispose();
  _coreLifetime = null;
  // Drop every document-level click-away handler registered by this
  // openEditor invocation. Without this, dropdown closers accumulated
  // across reopens (six handlers × N opens).
  while (state.editorDocClickHandlers.length) {
    const h = state.editorDocClickHandlers.pop();
    try { document.removeEventListener('click', h); } catch {}
  }
  if (state.cursorEl) { state.cursorEl.remove(); state.cursorEl = null; }
  // Tear down all floating popups + the dock so closing the editor
  // doesn't leave stale chips/panels behind on top of Imps.
  try { _closeFxMenu(); } catch {}
  try { _closeAdjPopup(); } catch {}
  try { _historyPanel?.closeOwnedPanels(); } catch {}
  if (state.container) {
    state.container.style.display = 'none';
    state.container.innerHTML = '';
    if (!legacy) state.container.remove();
  }
  state._projectDownloadRelease?.();
  if (legacy) _clearSceneState();
  state.mainCanvas = null;
  state.mainCtx = null;
  state.maskCanvas = null;
  state.maskCtx = null;
  state.container = null;
  if (legacy) window.__impsEditLive = false;
  return true;
}

function isEditorOpen() {
  return state.editorOpen;
}

async function mountRetainedScene() {
  if (legacy || !retainedScene || !state.layers.length || lifetime.disposed || state.editorOpen) return {status:'unavailable',reason:'retained_scene_unavailable'};
  const target = session.captureTarget(), sibling = session.hasOpenRuntime();
  if (sibling && !session.hasReadyRuntime()) return {status:'unavailable',reason:'initial_scene_loading'};
  const proof = sibling ? {target} : await sessionPersistence.verifySource();
  if (proof.source?.readOnly) readOnlyImageScenes.add(_sceneAuthority);
  if (lifetime.disposed || authority.validate(target) !== true || authority.validateScope(session.captureTarget()) !== true || JSON.stringify(session.captureTarget()) !== JSON.stringify(proof.target)) throw new Error('retained image authority changed');
  const generation = ++_openGeneration, container = root.ownerDocument.createElement('div');
  state.container = container; state.editorOpen = true; container.style.display = 'flex';
  try {
    _buildEditor(container);
    document.body.appendChild(_editorPortal); root.appendChild(container);
    state.mainCanvas.width = state.imgWidth; state.mainCanvas.height = state.imgHeight;
    const mask = _getActiveMaskLayer();
    if (mask) { state.maskCanvas=mask.canvas; state.maskCtx=mask.ctx; }
    else { state.maskCanvas=document.createElement('canvas'); state.maskCanvas.width=state.imgWidth; state.maskCanvas.height=state.imgHeight; state.maskCtx=state.maskCanvas.getContext('2d'); }
    composite(); _renderLayerPanel(); _fitZoom();
    if (lifetime.disposed || generation !== _openGeneration || authority.validateScope(session.captureTarget()) !== true) throw new Error('retained image view superseded');
    if (!sibling) sessionPersistence.resumeResultPersistence();
    sceneReady=true; return {status:'mounted'};
  } catch (error) { if (state.editorOpen) _closeEditorContent(); throw error; }
}

// Exact-open entry for Files/Imps. Resolves an opaque Files resource ref to a
// preview URL and loads it into the editor. Legacy image links resolve here
// through the Files `imps` open target.
async function openResource(resourceRef) {
  if (legacy) return openResourceOwned(resourceRef);
  if (initialOpenPending || state.editorOpen || state.layers.length) return {status:'unavailable',reason:'image_scene_already_open'};
  initialOpenPending=true; try { return await openResourceOwned(resourceRef); } finally { initialOpenPending=false; }
}
async function openResourceOwned(resourceRef) {
  _feedbackSourceWitness = null;
  if (!legacy) {
    if (retainedScene) return {status:'unavailable',reason:'retained_scene_requires_mount'};
    const target = session.captureTarget();
    if (!persistence?.openSource || !target.opaqueRef || resourceRef !== target.opaqueRef) return {status:'unavailable', reason:'isolated_resource_open_pending'};
    const origin = _captureOrigin();
    const source = await persistence.openSource(target);
    if (!_originCurrent(origin)) throw new Error('resource open superseded');
    if (typeof persistence.openProject !== 'function') return {status:'unavailable', reason:'isolated_project_open_pending'};
    const project = await persistence.openProject(target, source);
    if (!_originCurrent(origin)) throw new Error('resource open superseded');
    // A first Save may have created the project after this draft was captured.
    // Bind the draft to its original identity until a new Save supplies its receipt.
    const projectIdentity=target.draftId ? {} : {projectId:project?.id,projectRevision:project?.project_revision};
    runtimeAttachment?.refresh(target, {key:target.sourceKey, opaqueRef:source.resourceRef, sourceRevision:source.revision, sourceDigest:source.digest,...projectIdentity});
    const blobUrl = URL.createObjectURL(new Blob([source.bytes], {type:source.mime}));
    const generation = ++_openGeneration;
    try {
      await openEditor(blobUrl, null, null, source.name, null, generation, isolatedSourceToken);
      if (!state.editorOpen || !_originCurrent(_captureOrigin())) throw new Error('resource open superseded');
      if (source.readOnly) readOnlyImageScenes.add(_sceneAuthority);
      state.imageRef = source.resourceRef; state.managedProvider = 'files'; state.managedResourceId = source.resourceId;
      const mime = String(source.mime || '').toLowerCase(); state.originalExt = mime.includes('jpeg') ? 'jpg' : mime.includes('webp') ? 'webp' : 'png';
      const refreshed = session.captureTarget(); state.projectId = refreshed.projectId; state.projectRevision = refreshed.projectRevision || 0; state.expectedImageRevision = refreshed.sourceDigest;
      if (project?.state?.layers?.length) {
        const restoreOrigin = _captureOrigin(); await _restoreDraft(project.state, generation, restoreOrigin);
        if (!_originCurrent(restoreOrigin)) throw new Error('project restore superseded');
        composite(); _renderLayerPanel(); _fitZoom();
      }
      sessionPersistence.savedRevision = state.sceneRevision; sceneReady=true;
      if (!project?.state?.layers?.length && !session.captureTarget().draftId) _feedbackSourceWitness = {revision:_sceneAuthority.revision,target:JSON.stringify(session.captureTarget())};
      return Object.freeze({resourceRef:source.resourceRef, name:source.name, projectId:state.projectId});
    } catch (error) { if (state.editorOpen && generation === _openGeneration) _closeEditorContent(); throw error; }
    finally { URL.revokeObjectURL(blobUrl); }
  }
  const generation = ++_openGeneration;
  const origin = _captureOrigin();
  const { filesFacadeClient } = await import('./filesFacadeClient.js');
  const response = await filesFacadeClient.openResource(resourceRef);
  if (generation !== _openGeneration || !_originCurrent(origin)) throw new Error('resource open superseded');
  const app = response?.target?.app;
  if ((app !== 'imps' && app !== 'files') || !response?.resource?.ref) {
    throw new Error('Imps resource response is invalid');
  }
  const currentRef = String(response.resource.ref);
  const payload = response.payload || {};
  const exactName = String(response.resource.name || payload.filename || payload.name || 'image');
  const previewUrl = filesFacadeClient.contentUrl(currentRef, { purpose: 'preview' });
  const sourceResponse = await fetch(previewUrl, { credentials: 'same-origin' });
  if (generation !== _openGeneration || !_originCurrent(origin)) throw new Error('resource open superseded');
  if (!sourceResponse.ok) throw new Error(`Unable to read image resource (${sourceResponse.status})`);
  const sourceBytes = await sourceResponse.arrayBuffer();
  const digest = await crypto.subtle.digest('SHA-256', sourceBytes);
  if (generation !== _openGeneration || !_originCurrent(origin)) throw new Error('resource open superseded');
  const observedRevision = `sha256:${[...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('')}`;
  const sourceUrl = URL.createObjectURL(new Blob([sourceBytes], { type: sourceResponse.headers.get('content-type') || 'image/*' }));
  const sourceMime = String(sourceResponse.headers.get('content-type') || '').toLowerCase();
  const originalMime=payload.read_only===false ? hostRasterMime(sourceBytes,sourceMime) : null;
  const originalExt=originalMime==='image/jpeg' ? 'jpg' : originalMime==='image/webp' ? 'webp' : 'png';
  // Bind the managed image identity BEFORE openEditor so Save/Load address
  // /api/imps/projects/* using the Files-owned resource identity.
  _bindManagedIdentity(
    payload.provider || 'files',
    payload.resource_id || null,
  );
  state.imageRef = currentRef;
  state.expectedImageRevision = observedRevision;
  try {
    await openEditor(sourceUrl, null, null, exactName, null, generation);
    if (generation !== _openGeneration || !_originCurrent(origin)) throw new Error('resource open superseded');
    if (payload.read_only) readOnlyImageScenes.add(_sceneAuthority);
    state.imageRef = currentRef;
    state.originalExt = originalExt; state._legacyRasterMime=originalMime;
    await _ensureManagedProject(generation, payload.provider || 'files', payload.resource_id || null, { forceLookup: true });
    if (generation !== _openGeneration || !_originCurrent(origin)) throw new Error('resource open superseded');
    return Object.freeze({
      resourceRef: currentRef,
      name: exactName,
      previewUrl,
      readOnly: !!payload.read_only,
      projectId: state.projectId,
    });
  } finally {
    URL.revokeObjectURL(sourceUrl);
  }
}

const impsModule = {
  openEditor,
  closeEditor,
  isEditorOpen,
  openResource,
  exportPNG,
  exportToGallery,
  retryPendingSaveCopy,
  downloadPNG,
  getManagedSaveReceipt,
};



  if (!legacy) lifetime.add(() => { if (state.editorOpen) _closeEditorContent(); else _coreLifetime?.dispose(); });
  if (!legacy) {
    const unavailable = () => Object.freeze({status:'unavailable', reason:'isolated_save_copy_pending'});
    const validClose = token => state.editorOpen && sessionPersistence.validateToken(token);
    const sharedControls = ['ge-undo','ge-redo','ge-history-btn','ge-zoom-out','ge-zoom-in','ge-zoom-fit','ge-zoom-100','ge-save-menu-btn','ge-image-menu-btn','ge-filter-menu-btn'];
    const commandSelectors = {history:'ge-history-btn','zoom-in':'ge-zoom-in','zoom-out':'ge-zoom-out','zoom-fit':'ge-zoom-fit','zoom-actual':'ge-zoom-100','layer-add':'ge-add-layer','layer-merge-down':'ge-merge-down','layer-merge-all':'ge-merge-all'};
    const runtime = Object.freeze({ session, root,
      retainedResultStatus:() => sessionPersistence.retainedResultStatus(),
      keepRetainedResult:() => sessionPersistence.persistRetainedResults(),
      discardRetainedResult:id => { sessionPersistence.resolveResult(id); return true; },
      exportRetainedResult:id => { const result=sessionPersistence.readRetainedResult(id), link=document.createElement('a'), ext=({'image/jpeg':'jpg','image/webp':'webp','image/gif':'gif','image/png':'png'})[result.mime.toLowerCase()] || 'image'; link.href=`data:${result.mime};base64,${result.bytes}`; link.download=`retained-image.${ext}`; link.click(); return true; },
      applyRetainedResult:async id => {
        const result=sessionPersistence.readRetainedResult(id), revision=_sceneAuthority.revision, origin=_captureOrigin();
        if (!state.editorOpen || !isolatedVisible || authority.isActive?.()===false || !_originCurrent(origin) || !_sceneAuthority.canApplyAt(revision)) throw new Error('Image is not ready to apply the retained result.');
        const img=await new Promise((resolve,reject)=>{const image=new Image(); image.onload=()=>resolve(image); image.onerror=()=>reject(new Error('Retained image could not be decoded.')); image.src=`data:${result.mime};base64,${result.bytes}`;});
        if (!state.editorOpen || !isolatedVisible || authority.isActive?.()===false || !_originCurrent(origin) || !_sceneAuthority.canApplyAt(revision)) throw new Error('Image changed before the result could be applied.');
        sessionPersistence.readRetainedResult(id);
        const token=_sceneAuthority.beginGesture(_gestureView,'apply-retained-result'); if (!token) throw new Error('Another image edit is active.');
        try { const width=img.naturalWidth || img.width, height=img.naturalHeight || img.height, fullSize=result.metadata?.inpaint || result.metadata?.mask || result.metadata?.fullSize; if (fullSize && (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width<1 || height<1 || width>16384 || height>16384 || width*height>67108864)) throw new Error('Retained image dimensions exceed the editor limit.'); _saveState('Apply retained result'); const layer=createLayer(result.metadata?.label || 'Retained result',fullSize ? width : state.imgWidth,fullSize ? height : state.imgHeight); if (result.kind==='import') { let w=width, h=height; if (w>state.imgWidth || h>state.imgHeight) {const scale=Math.min(state.imgWidth/w,state.imgHeight/h); w=Math.round(w*scale); h=Math.round(h*scale);} layer.ctx.drawImage(img,Math.round((state.imgWidth-w)/2),Math.round((state.imgHeight-h)/2),w,h); if (result.metadata?.provenance) layer.provenance=Object.freeze(result.metadata.provenance); } else layer.ctx.drawImage(img,0,0); state.layers.push(layer); state.activeLayerId=layer.id; _markSceneChanged(); sessionPersistence.resolveResult(id); composite(); _renderLayerPanel(); return true; }
        finally { _sceneAuthority.endGesture(token); }
      },
      capabilities: () => Object.freeze({runtime:true, canUndo:state.undoStack.length > 0, canRedo:state.redoStack.length > 0, exportProject:state.editorOpen && state.layers.length > 0, save:sessionPersistence.needsHostSaveDestination || sessionPersistence.canSaveHostOriginal || sessionPersistence.canSaveRecoveredCopy || sessionPersistence.canSaveManagedOriginal && session.captureTarget().sourceKey?.provider === 'files' && typeof persistence.save === 'function' && typeof persistence.bindReceipt === 'function' && typeof persistence.openProject === 'function', saveCopy:!sessionPersistence.pendingCopy && !sessionPersistence.copyBusy && typeof persistence.startCopy === 'function' && typeof persistence.retryCopy === 'function', retrySaveCopy:Boolean(sessionPersistence.pendingCopy) && !sessionPersistence.copyBusy && typeof persistence.retryCopy === 'function', sourceOpen:typeof persistence.openSource === 'function' && typeof persistence.openProject === 'function'}),
      openAttachmentSource:async (blob,name,provenance) => { const target=session.captureTarget(); if (!(blob instanceof Blob) || blob.size<1 || blob.size>CANVAS_MAX_IMPORT_BYTES || !/^image\//i.test(blob.type) || target.sourceKey || state.editorOpen || authority.validate(target)!==true) throw new Error('Attachment image is unavailable'); const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer())),byte=>byte.toString(16).padStart(2,'0')).join(''); if (digest!==provenance?.digest || authority.validate(target)!==true || JSON.stringify(session.captureTarget())!==JSON.stringify(target)) throw new Error('Attachment image origin changed'); const url=URL.createObjectURL(blob); try { await openEditor(url,null,null,name,null,null,isolatedSourceToken); if (!state.editorOpen || authority.validate(target)!==true || JSON.stringify(session.captureTarget())!==JSON.stringify(target)) throw new Error('Attachment image origin changed'); if (state.container) state.container.inert=true; sessionPersistence.setImageSourceProvenance(provenance); await sessionPersistence.saveDraft({...sessionPersistence.captureSnapshot(),confirmedHostPixels:Boolean(sessionPersistence.hostOriginal)}); if (authority.validate(session.captureTarget())!==true) throw new Error('Attachment image authority changed'); sessionPersistence.savedRevision=state.sceneRevision; sceneReady=true; return {status:'opened'}; } finally { if (state.container && state.editorOpen) state.container.inert=false; URL.revokeObjectURL(url); } },
      captureAttachmentImage:async () => { const target=session.captureTarget(), revision=_sceneAuthority.revision, origin=_captureOrigin(); if (!state.editorOpen || authority.validate(target)!==true || !_sceneAuthority.canApplyAt(revision)) throw new Error('Finish the current image edit first'); const token=_sceneAuthority.beginGesture(_gestureView,'attachment-snapshot'); if (!token) throw new Error('Another image edit is active'); let canvas; try {canvas=flatten();} finally {_sceneAuthority.endGesture(token);} const blob=await new Promise((resolve,reject)=>canvas.toBlob(value=>value ? resolve(value) : reject(new Error('Image encoding failed')),'image/png')); if (!_originCurrent(origin) || authority.validate(target)!==true || JSON.stringify(session.captureTarget())!==JSON.stringify(target) || !_sceneAuthority.canApplyAt(revision)) throw new Error('Image changed before attachment preparation'); return {blob,revision,target}; },
      isAttachmentSnapshotCurrent:snapshot => { const now=session.captureTarget(); return state.editorOpen && authority.validate(now)===true && JSON.stringify([now.sessionId,now.scope,now.sourceKey])===JSON.stringify([snapshot?.target?.sessionId,snapshot?.target?.scope,snapshot?.target?.sourceKey]) && _sceneAuthority.canApplyAt(snapshot?.revision); },
      attachmentApplyStatus:() => sessionPersistence.attachmentApply,
      persistAttachmentApply:async descriptor => { sessionPersistence.setAttachmentApply(descriptor); return sessionPersistence.saveDraft(sessionPersistence.captureSnapshot()); },
      createAttachmentOutcomeRecorder:descriptor => sessionPersistence.createAttachmentOutcomeRecorder(descriptor),
      persistRecordedAttachment:() => sessionPersistence.saveDraft(sessionPersistence.captureSnapshot()),
      cancelUnsubmittedAttachmentApply:async id => { sessionPersistence.clearUnsubmittedAttachmentApply(id); return sessionPersistence.saveDraft(sessionPersistence.captureSnapshot()); },
      importFilesImage:(payload,context,readContext) => { if (!state.editorOpen || !sceneReady || !_originCurrent(_captureOrigin()) || !_clipboardWiring) throw new Error('The target image view is unavailable.'); return _clipboardWiring.deliverFilesImage(payload,context,readContext); },
      importEditableCopy:async (payload, isCurrent) => {
        const target=session.captureTarget(), origin=_captureOrigin(), generation=_openGeneration, revision=_sceneAuthority.revision;
        const current=() => typeof isCurrent==='function' && isCurrent() && authority.validate(target)===true && JSON.stringify(session.captureTarget())===JSON.stringify(target);
        if (target.sourceKey || target.draftId || target.projectId || !sceneReady || !current() || !_sceneAuthority.canApplyAt(revision)) throw new Error('Image copy target is unavailable');
        await _restoreDraft(payload,generation,origin,current,false,revision);
        composite(); _renderLayerPanel(); _fitZoom();
        return {status:'imported'};
      },
      openEditor, openResource, mountRetained:mountRetainedScene, isSceneReady:() => sceneReady && state.editorOpen, closeEditor, exportPNG, exportProject:_exportIsolatedProject, exportToGallery, retryPendingSaveCopy, downloadPNG, isEditorOpen,
      setFocused:focused => {
        const wasFocused=isolatedFocused;
        isolatedFocused=focused === true;
        for (const controls of state.container?.querySelectorAll('.ge-topbar, .ge-toolbar, .ge-right-panel') || []) {
          controls.style.display=isolatedFocused ? '' : 'none';
        }
        if (wasFocused && !isolatedFocused) {
          _historyPanel?.closeOwnedPanels();
          _closeFxMenu(); _closeAdjPopup();
        }
        if (_editorPortal) _editorPortal.style.display=isolatedVisible && isolatedFocused ? 'contents' : 'none';
      },
      setVisible:visible => {
        const wasVisible = isolatedVisible;
        isolatedVisible = visible === true;
        if (_editorPortal) _editorPortal.style.display = isolatedVisible && isolatedFocused ? 'contents' : 'none';
        if (wasVisible && !isolatedVisible) {
          _closeTransformPopup(); _closeFxMenu(); _closeAdjPopup();
          _historyPanel?.closeOwnedPanels();
        } else if (!wasVisible && isolatedVisible && state.editorOpen) {
          _flushSceneProjection();
          composite();
        }
        return isolatedVisible;
      },
      setSharedCommands:enabled => { for (const id of sharedControls) { const node = _editorFind(id); if (node) node.style.display = enabled ? 'none' : ''; } },
      command:name => {
        if (!state.editorOpen || !isolatedVisible || !isolatedFocused || authority.isActive?.() === false || !_originCurrent(_captureOrigin())) return false;
        if (name === 'project-metadata') { _openProjectMetadata().catch(error=>uiModule?.showToast(error.message || 'Project metadata unavailable.',6000)); return true; }
        if (name === 'undo') return undo(); if (name === 'redo') return redo(); if (name === 'selection-invert') return _invertSelection();
        let node = commandSelectors[name] ? _editorFind(commandSelectors[name]) : null;
        if (!node && /^image-(resize|rotate-90|rotate-180|flip-h|flip-v)$/.test(name)) node = state.container.querySelector(`[data-image-action="${name.slice(6)}"]`);
        if (!node && /^filter-(blur-gaussian|blur-zoom)$/.test(name)) node = state.container.querySelector(`[data-filter-action="${name.slice(7)}"]`);
        if (!node && /^selection-(delete|copy|mask)$/.test(name)) node = _editorFind(`ge-${state.wandMask ? 'wand' : 'lasso'}-${name.slice(10)}`);
        if (!node || node.disabled) return false;
        if (name === 'history' && node.style.display === 'none') { node.style.display = ''; try { node.click(); } finally { node.style.display = 'none'; } }
        else node.click();
        return true;
      },
      projectOperationStatus:() => sessionPersistence.recoveryStatus().state==='save_uncertain',
      observeProjectOperation:() => sessionPersistence.observeSaveOutcome(),
      captureFeedbackRegion:_captureFeedbackRegion,
      isDirty:() => _sceneAuthority.dirty,
      prepareClose:async choice => {
        if (!state.editorOpen) return {status:'unavailable',reason:'image_not_open'};
        return sessionPersistence.prepareClose(choice,{save:() => runtime.save(),draft:() => _persistDraft()});
      },
      validateClose:validClose,
      commitClose:token => { if (!validClose(token) || !sessionPersistence.commitToken(token)) return false; _closeEditorContent(); return true; },
      beforeClose:async choice => { const token = await runtime.prepareClose(choice); return token?.status === 'prepared' && runtime.commitClose(token) ? {status:'closed'} : token; },
      restoreDraft:async () => {
        const target = session.captureTarget();
        if (!state.editorOpen || !target.draftId || authority.validate(target) !== true) return {status:'unavailable', reason:'draft_authority_unavailable'};
        const origin = _captureOrigin(), generation = _openGeneration;
        const draft = await _loadDraftById(target.draftId);
        const savedTarget = draft?.payload?._imageSessionRecovery;
        if (!draft || draft.id !== target.draftId || !_originCurrent(origin) || authority.validate(target) !== true || JSON.stringify(session.captureTarget()) !== JSON.stringify(target) || JSON.stringify({...savedTarget,opaqueRef:null}) !== JSON.stringify({...target,draftId:null,opaqueRef:null})) throw new Error('draft identity or authority changed');
        const source=draft.payload._imageSourceProvenance, apply=draft.payload._imageAttachmentApply;
        if (source && apply && Object.hasOwn(apply,'sourceKind') && (apply.sourceKind!==source.kind || apply.sourceId!==source.id || apply.sourceDigest!==source.digest)) throw new Error('Attachment source and receipt disagree');
        const restoreResults = sessionPersistence.prepareRestoreResults(draft.payload._imageRetainedResults || []);
        const restoreAttachment = sessionPersistence.prepareRestoreAttachmentApply(draft.payload._imageAttachmentApply || null);
        const restoreSource = sessionPersistence.prepareRestoreSourceProvenance(draft.payload._imageSourceProvenance || null);
        const restoreProjectOperation = sessionPersistence.prepareRestoreProjectOperation(draft.payload._imageProjectOperation || null);
        await _restoreDraft(draft, generation, origin, () => authority.validate(target) === true && JSON.stringify(session.captureTarget()) === JSON.stringify(target), true);
        sessionPersistence.pendingCopy = draft.payload._imagePendingCopy || null;
        restoreResults();
        restoreAttachment();
        restoreSource();
        restoreProjectOperation();
        state.draftId = target.draftId; state.draftName = draft.name || 'Untitled';
        await sessionPersistence.restoreHostOriginal(draft.payload._imageHostOriginal,target,draft.payload._imageHostPixelsSaved===true,draft.payload._imageHostPixelsPending===true);
        sessionPersistence.markRestoredDraft(target);
        composite(); _renderLayerPanel(); _fitZoom();
        return {status:'restored'};
      },
      save:async () => { if (!session.captureTarget().sourceKey && !sessionPersistence.canSaveHostOriginal && !sessionPersistence.canSaveRecoveredCopy && !sessionPersistence.needsHostSaveDestination) return {status:'unavailable',reason:'This image has no original. Use Save as copy.'}; if (!state.editorOpen || !_originCurrent(_captureOrigin()) || (typeof persistence.save !== 'function' || typeof persistence.bindReceipt !== 'function')) return unavailable();
        return sessionPersistence.saveCurrent(); },
      render:() => { if (!state.editorOpen || !_originCurrent(_captureOrigin())) return false; composite(); return true; },
      undo:() => state.editorOpen && _originCurrent(_captureOrigin()) ? undo() : false,
      redo:() => state.editorOpen && _originCurrent(_captureOrigin()) ? redo() : false,
      dispose:() => sessionPersistence.saveCount || sessionPersistence.draftCount || sessionPersistence.copyBusy ? false : lifetime.dispose?.(),
      retireView:() => { if (state.editorOpen) _closeEditorContent(); lifetime.dispose?.(); return true; },
    });
    runtimeAttachment = session.attachRuntime(runtime, {viewId:viewId || runtimePrefix});
    lifetime.add(() => runtimeAttachment?.detach());
    return runtime;
  }
  return Object.freeze({ ...impsModule, capabilities: () => ({runtime:true}) });
}
const defaultRuntime = createImageRuntime({legacy:true});
export const openEditor = (...args) => defaultRuntime.openEditor(...args);
export const closeEditor = (...args) => defaultRuntime.closeEditor(...args);
export const isEditorOpen = (...args) => defaultRuntime.isEditorOpen(...args);
export const openResource = (...args) => defaultRuntime.openResource(...args);
export const exportPNG = (...args) => defaultRuntime.exportPNG(...args);
export const exportToGallery = (...args) => defaultRuntime.exportToGallery(...args);
export const retryPendingSaveCopy = (...args) => defaultRuntime.retryPendingSaveCopy(...args);
export const downloadPNG = (...args) => defaultRuntime.downloadPNG(...args);
export const getManagedSaveReceipt = (...args) => defaultRuntime.getManagedSaveReceipt(...args);
export default defaultRuntime;
