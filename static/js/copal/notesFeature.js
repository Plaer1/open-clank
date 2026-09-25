import {
  NOTES_PANELS,
  activateWorkspaceLeaf,
  closeWorkspaceGroup,
  closeWorkspaceLeaf,
  closeWorkspaceOtherLeaves,
  findWorkspaceGroup,
  findWorkspaceLeaf,
  groupForLeaf,
  moveWorkspaceLeaf,
  navigationIntentFromEvent,
  normalizeNotesSettings,
  normalizeNotesWorkspace,
  noteViewType,
  openWorkspaceDocument,
  resizeWorkspaceSplit,
  serializeNotesWorkspace,
  setWorkspaceLeafMode,
  setWorkspacePanelPlacement,
  splitWorkspaceGroup,
  workspaceGroups,
  workspaceLeaves,
  workspacePanelsForSide,
} from './notesWorkspace.js';
import { showResourceInFiles } from '../showInFiles.js';
import {
  coercePropertyValue,
  databaseRelations,
  fuzzyScore,
  linkedMentions,
  moveHeadingSection,
  moveHeadingSectionTo,
  moveFrontmatterProperty,
  outlineEntries,
  parseCanvasDocument,
  parseFrontmatter,
  propertyType,
  renameFrontmatterProperty,
  removeFrontmatterProperty,
  resolveDocumentLink,
  setFrontmatterProperty,
  unlinkedMentions,
  wordCount,
} from './notesModel.js';
import { wireDialog, wirePopover } from './overlays.js';
import { copalStorageKey } from './storage.js';
import { registerMenuDismiss } from '../escMenuStack.js';
import { parseTable, createTableWidget, applyTableEdit } from './tableModel.js';
import { createBufferRegistry } from './documentBuffers.js';
import { createSheetController } from './sheetController.js';
import { mountSheet } from './sheetView.js';
import { cloneEnvelope, normalizeResourceHandle, sameResourceKey, snapshotEnvelope } from './resourceModel.js';
import { createSaveActionId, sameSaveScope } from './documentSave.js';
import { capturePanelPositions, restorePanelPositions } from './panelPosition.js';
import { createCodeMirrorContextAdapter } from '../custom-context-menu.js';
import { languageForPath, languageDialectForPath } from '../editor/entryModel.js';
// Keep model/save imports usable in Node renderers.  The prompt primitive
// touches the DOM only when invoked, while ui.js initializes the full shell.
import { styledPrompt } from '../dialogPrimitives.js';
import { filesFacadeClient } from '../filesFacadeClient.js';
import { createResourcePicker, normalizeAuthorizedResource } from './resourcePicker.js';
import {
  expandTemplate as expandTemplateModel,
  formatTemplateDate as formatTemplateDateModel,
  normalizeTemplateFolderSelection,
  createInsertionDescriptors,
} from './templateModel.js';
import { FILES_TRANSFER_MIME, parseInternalDragPayload, validateDropTarget, resourceKey, scopeKey } from '../filesSelectionModel.js';
import { createWindowNavigation } from './navigation.js';

const OPERATIONAL_KINDS = new Set(['planning', 'calendar-projection', 'treehouse-state', 'copal-operation', 'copal-tracks', 'copal-event', 'copal-migration']);
const PROPERTY_TYPES = ['text', 'list', 'number', 'checkbox', 'date', 'datetime', 'tags', 'object'];

function boundedEditorSelection(selection, length) {
  if (!selection || typeof selection !== 'object') return selection;
  const max = Math.max(0, Number(length) || 0);
  const sourceRanges = Array.isArray(selection.ranges) && selection.ranges.length
    ? selection.ranges
    : [{ anchor:selection.anchor, head:selection.head }];
  const ranges = sourceRanges.map((range) => {
    const anchor = Math.max(0, Math.min(max, Number(range.anchor) || 0));
    const head = Math.max(0, Math.min(max, Number(range.head) || 0));
    return { ...range, anchor, head };
  });
  const mainIndex = Math.max(0, Math.min(ranges.length - 1, Number(selection.mainIndex) || 0));
  return { ...selection, ranges, mainIndex, anchor:ranges[mainIndex].anchor, head:ranges[mainIndex].head };
}
const TIMELINE_DOCUMENT = Object.freeze({
  id:'copal:timeline', name:'Timeline', kind:'timeline', virtual:true, text:'', properties:{}, tags:[], links:[],
});
const NOTES_NARROW_QUERY = '(max-width: 760px)';
const NOTES_COMPACT_QUERY = '(max-width: 1100px)';

const TEMPLATE_TOKENS = Object.freeze({
  title: ({ title }) => String(title ?? ''),
  date: ({ date }) => String(date ?? ''),
  time: ({ time }) => String(time ?? ''),
  datetime: ({ datetime }) => String(datetime ?? ''),
});

function canonicalEditorResourceKey(value = {}, fallbackProvider = '') {
  // ResourceKey is owned by the Files selection model. Preserve its complete
  // account/workspace/provider identity and never stringify an object into the
  // ambiguous "[object Object]" form. A bare sealed ref is only a bounded
  // compatibility key for restored legacy state; it is reissued before use.
  if (typeof value === 'string' && value.trim()) return value.trim();
  const source = value && typeof value === 'object' ? value : { id:String(value || '') };
  const provider = String(source.provider || source.provider_id || fallbackProvider || '').trim();
  const account = String(source.accountId || source.account_id || source.owner || source.account || '').trim();
  const workspace = String(source.workspaceId || source.workspace_id || source.workspace || '').trim();
  const id = String(source.resourceId || source.resource_id || source.id || source.key || '').trim();
  if (!provider || !id) return '';
  const scope = [account, workspace].filter(Boolean).join('|');
  return `${scope ? `${scope}|` : ''}${provider}:${id}`;
}

/** Serialize only the strict S01 insertion descriptor into safe Markdown. */
export function serializeAttachmentInsertion(insertion, { mode = 'link' } = {}) {
  if (!insertion || typeof insertion !== 'object' || Array.isArray(insertion)
    || Object.keys(insertion).some(key => !['format', 'link_target', 'label', 'media_kind'].includes(key))
    || insertion.format !== 'markdown') throw new Error('Attachment insertion format is unsupported.');
  const target = String(insertion.link_target || '').trim();
  const label = String(insertion.label || '').trim();
  const mediaKind = String(insertion.media_kind || '').trim();
  if (!target || !label || !mediaKind || target.length > 2048 || label.length > 512 || /[\u0000-\u001f\u007f]/u.test(target)) throw new Error('Attachment insertion descriptor is invalid.');
  if (/^(?:javascript|data|vbscript):/iu.test(target) || /[<>]/u.test(target)) throw new Error('Attachment link target is unsafe.');
  if (!['link', 'embed'].includes(mode)) throw new Error('Attachment action is unsupported.');
  const safeLabel = label.replace(/[\\[\]]/gu, '\\$&').replace(/[\r\n]/gu, ' ');
  const safeTarget = target.replace(/[\\()\r\n]/gu, '\\$&');
  const embed = mode === 'embed' && /^(?:image|video|audio)(?:\/|$)/iu.test(mediaKind);
  return `${embed ? '!' : ''}[${safeLabel}](<${safeTarget}>)`;
}

function attachmentPreparationMayHaveCommitted(error) {
  if (!error || error.name === 'AbortError') return false;
  const code = String(error.code || '').toLowerCase();
  const message = String(error.message || '').toLowerCase();
  const status = Number(error.status || 0);
  if (status >= 400 && status < 500 && status !== 408 && status !== 429) return false;
  if (new Set(['resource_unavailable', 'resource_ref_stale', 'policy_generation_changed', 'permission_denied', 'access_denied', 'denied', 'conflict', 'invalid_resource_request', 'unsupported_provider_kind', 'upload_too_large']).has(code)) return false;
  if (/invalid|stale|denied|forbidden|unauthori[sz]ed|policy|revision|conflict|unsupported|too.large/.test(message)) return false;
  return true;
}

/**
 * Unwrap and bind the typed S01 preparation status returned after a lost POST.
 * S01 binds source/item/mode inside the operation; when a future descriptor
 * projects those fields, compare them here as well rather than accepting a
 * descriptor for a different source or target.
 */
export function validateAttachmentPreparationRecovery(result, {
  operationId, generation, sourceKey = '', sourceRef = '', sourceItemId = '', sourceRevision = null,
  targetKey = '', targetRef = '', targetRevision = null, mode = 'link',
  accountId = '', workspace = '', policyGeneration = null,
} = {}) {
  const operation = String(operationId || '');
  if (!result || typeof result !== 'object' || Array.isArray(result)
    || result.operation_id !== operation || result.generation !== Number(generation)
    || result.state !== 'complete' || !result.preparation || typeof result.preparation !== 'object') {
    throw new Error('Attachment preparation recovery is unavailable; retry the Files gesture.');
  }
  const preparation = result.preparation;
  if (preparation.operation_id !== operation || preparation.generation !== Number(generation)) throw new Error('Attachment preparation recovery belongs to another operation.');
  for (const [field, expected] of [['account_id', accountId], ['workspace_id', workspace], ['workspace', workspace], ['policy_generation', policyGeneration]]) {
    if (expected == null || expected === '') continue;
    if (preparation[field] != null && String(preparation[field]) !== String(expected)) throw new Error(`Attachment ${field.replace('_', ' ')} changed; retry the drop.`);
    if (result[field] != null && String(result[field]) !== String(expected)) throw new Error(`Attachment ${field.replace('_', ' ')} changed; retry the drop.`);
  }
  if (!['link', 'embed'].includes(String(mode || '').toLowerCase())) throw new Error('Attachment mode is invalid.');
  if (preparation.mode != null && String(preparation.mode).toLowerCase() !== String(mode).toLowerCase()) throw new Error('Attachment preparation mode changed; retry the drop.');
  if (JSON.stringify(preparation.source_revision) !== JSON.stringify(sourceRevision)) throw new Error('Attachment source revision changed; retry the drop.');
  const sourceIdentity = preparation.source_identity || preparation.sourceIdentity || preparation.source || null;
  const sourceRefValue = sourceIdentity?.resource_ref ?? sourceIdentity?.resourceRef ?? preparation.source_resource_ref ?? preparation.sourceRef;
  const sourceKeyValue = sourceIdentity?.resource_key ?? sourceIdentity?.resourceKey ?? preparation.source_resource_key ?? preparation.sourceKey;
  const sourceItemValue = sourceIdentity?.item_id ?? sourceIdentity?.itemId ?? preparation.item_id ?? preparation.itemId;
  if (sourceRefValue != null && String(sourceRefValue) !== String(sourceRef)) throw new Error('Attachment source changed; retry the drop.');
  if (sourceKeyValue != null && canonicalEditorResourceKey(sourceKeyValue) !== String(sourceKey)) throw new Error('Attachment source identity changed; retry the drop.');
  if (sourceItemValue != null && String(sourceItemValue) !== String(sourceItemId)) throw new Error('Attachment source item changed; retry the drop.');
  const identity = preparation.target_identity;
  const recoveredRef = String(identity?.resource_ref || identity?.resourceRef || '').trim();
  if (!['copal_document', 'host_document'].includes(String(identity?.kind || '')) || (targetRef && recoveredRef !== String(targetRef))) throw new Error('Attachment target changed; retry the drop.');
  const recoveredKey = identity?.resource_key || identity?.resourceKey;
  if (recoveredKey != null && canonicalEditorResourceKey(recoveredKey) !== String(targetKey)) throw new Error('Attachment target identity changed; retry the drop.');
  if (JSON.stringify(preparation.target_revision) !== JSON.stringify(targetRevision)) throw new Error('Attachment target revision changed; retry the drop.');
  // Validate the descriptor during recovery, before any target-buffer check or
  // transaction can consume it. This keeps malformed/legacy receipts inert.
  serializeAttachmentInsertion(preparation.insertion, { mode });
  return preparation;
}

function templateDateParts(timestamp, timeZone) {
  const date = timestamp instanceof Date ? timestamp : new Date(timestamp || Date.now());
  if (Number.isNaN(date.getTime())) throw new TypeError('Template timestamp is invalid');
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone:timeZone || undefined, year:'numeric', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false,
  });
  const parts = Object.fromEntries(formatter.formatToParts(date).filter(({ type }) => type !== 'literal').map(({ type, value }) => [type, value]));
  const dateValue = `${parts.year}-${parts.month}-${parts.day}`;
  const hour = parts.hour === '24' ? '00' : parts.hour;
  const timeValue = `${hour}:${parts.minute}:${parts.second}`;
  return { date:dateValue, time:timeValue, datetime:`${dateValue}T${timeValue}`, timestamp:date.toISOString() };
}

/** Format Obsidian-style date/time tokens without evaluating template code. */
export function formatTemplateDate(format = 'YYYY-MM-DD', timestamp = new Date(), timeZone = undefined) {
  // Compatibility export for older Notes callers. Keep one date/token engine
  // for Insert, New from Template, and Daily Note; the historical body below
  // remains as inert source context until the compatibility surface is retired.
  return formatTemplateDateModel(format, timestamp, timeZone);
  const values = templateDateParts(timestamp, timeZone);
  const tokenValues = {
    YYYY:values.date.slice(0, 4), YY:values.date.slice(2, 4), MM:values.date.slice(5, 7), DD:values.date.slice(8, 10),
    HH:values.time.slice(0, 2), mm:values.time.slice(3, 5), ss:values.time.slice(6, 8),
  };
  return String(format).replace(/YYYY|YY|MM|DD|HH|mm|ss/g, token => tokenValues[token]);
}

function validTemplateDateFormat(format) {
  return /^(?:(?:YYYY|YY|MM|DD|HH|mm|ss)|[-/.:_, T])+$/.test(String(format || ''));
}

/** Expand ordinary core variables and retain unsupported variables visibly. */
export function expandTemplate(source, { title = '', now = new Date(), timeZone = undefined } = {}) {
  // Compatibility export for older Notes callers. The canonical S04 model
  // owns token grammar, diagnostics, timezone handling, and safe inert syntax.
  return expandTemplateModel(source, { title, now, timeZone });
  const parts = templateDateParts(now, timeZone);
  const values = { ...parts, title:String(title ?? '') };
  const diagnostics = [];
  const text = String(source ?? '').replace(/\{\{\s*(title|date|time|datetime)(?::([^}]+))?\s*\}\}/gi, (whole, name, format) => {
    const key = String(name).toLowerCase();
    if (format) {
      const normalizedFormat = format.trim();
      if (!validTemplateDateFormat(normalizedFormat)) {
        diagnostics.push(`Unsupported template date format: {{${key}:${normalizedFormat}}}`);
        return whole;
      }
      return formatTemplateDate(normalizedFormat, now, timeZone);
    }
    return TEMPLATE_TOKENS[key]?.(values) ?? whole;
  }).replace(/\{\{\s*([^}]+?)\s*\}\}/g, (whole, name) => {
    diagnostics.push(`Unsupported template variable: {{${String(name).trim()}}}`);
    return whole;
  });
  if (/<%[\s\S]*?%>/.test(text)) diagnostics.push('Executable template expressions are unsupported and were left unchanged.');
  return { text, diagnostics, timestamp:parts.timestamp };
}

export function expandTemplateVariables(source, options = {}) {
  return expandTemplate(source, options).text;
}

/** Merge template metadata without silently replacing destination properties. */
export function mergeTemplateProperties(destination = {}, incoming = {}) {
  const properties = { ...(destination && typeof destination === 'object' ? destination : {}) };
  const collisions = [];
  for (const [key, value] of Object.entries(incoming && typeof incoming === 'object' ? incoming : {})) {
    if (key === 'type' || key === 'sourceDocumentId' || key === 'template') continue;
    if (Object.prototype.hasOwnProperty.call(properties, key) && JSON.stringify(properties[key]) !== JSON.stringify(value)) {
      collisions.push({ key, destination:properties[key], template:value });
      continue;
    }
    properties[key] = value;
  }
  return { properties, collisions };
}

/** Keep ordinary destination metadata while dropping template identity fields. */
export function copyTemplateProperties(incoming = {}) {
  return Object.fromEntries(Object.entries(incoming && typeof incoming === 'object' ? incoming : {})
    .filter(([key]) => !['type', 'sourceDocumentId', 'template'].includes(key)));
}

function templatePath(value) {
  return String(value || '').trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
}

function pathDirectory(value) {
  const normalized = templatePath(value);
  const index = normalized.lastIndexOf('/');
  return index < 0 ? '' : normalized.slice(0, index);
}

function normalizeRelativePath(base, value) {
  const parts = `${templatePath(base)}/${String(value || '')}`.split('/');
  const output = [];
  for (const part of parts) {
    if (!part || part === '.') continue;
    if (part === '..') output.pop();
    else output.push(part);
  }
  return output.join('/');
}

function relativePath(fromDirectory, target) {
  const from = templatePath(fromDirectory).split('/').filter(Boolean);
  const to = templatePath(target).split('/').filter(Boolean);
  while (from.length && to.length && from[0] === to[0]) { from.shift(); to.shift(); }
  return [...from.map(() => '..'), ...to].join('/') || './';
}

/** Rebase relative Markdown links and wiki embeds between ordinary resources. */
export function rebaseTemplateLinks(source, templateName, destinationName) {
  const text = String(source ?? '');
  const sourceDirectory = pathDirectory(templateName);
  const destinationDirectory = pathDirectory(destinationName);
  const isPortable = (target) => target && !/^(?:[a-z][a-z0-9+.-]*:|\/|#|data:)/i.test(target);
  const rebase = (target) => {
    if (!isPortable(target)) return target;
    const match = String(target).match(/^([^?#]*)([?#].*)?$/);
    const pathPart = match?.[1] || target;
    const suffix = match?.[2] || '';
    if (!pathPart || pathPart.startsWith('<')) return target;
    return `${relativePath(destinationDirectory, normalizeRelativePath(sourceDirectory, pathPart))}${suffix}`;
  };
  return text
    .replace(/(!?\[[^\]]*\]\()([^\s)]+)([^)]*\))/g, (_whole, prefix, target, suffix) => `${prefix}${rebase(target)}${suffix}`)
    .replace(/(!?\[\[)([^\]|#]+)([^\]]*\]\])/g, (_whole, prefix, target, suffix) => `${prefix}${rebase(target)}${suffix}`);
}

export function createNotesFeature({
  h, api, state, createMarkdownEditor, createSourceEditor = createMarkdownEditor, renderMarkdown, renderPreview = null, formatBaseCell,
  saveDocument, renameNote, deleteDocument, showHistory, showTrash, showForm,
  importVault, loadDocuments, openDocument:openOtherView, persistActiveContext, deleteDocuments,
  activateNotes, renderTimeline, openEventEditor, renderBaseEditor = null, baseAdapter = null, resourceBufferRegistry = null, saveResource = null, uploadAttachment = null, commitAttachment = null, abortAttachment = null,
}) {
  let persistTimer = null;
  const buffers = resourceBufferRegistry || createBufferRegistry();
  const previousResourceOpener = globalThis.__openClankOpenResourceHandle;
  const resourceOpener = async (input = {}) => {
    const resourceRef = String(input.resourceRef || input.resource_ref || input.ref || '').trim();
    if (!resourceRef) throw new TypeError('Files resource reference is required');
    const scope = currentScope();
    const response = await filesFacadeClient.openResource(resourceRef, { signal:input.signal || null });
    if (scope !== currentScope()) throw new Error('The active account or workspace changed. Open the file again.');
    const handle = normalizeResourceHandle(response?.payload?.resource || response?.resource);
    const payload = response?.payload || {};
    return openResource(handle, {
      ...payload,
      name:payload.name || input.name || handle.locator.displayName,
      text:payload.text ?? payload.content ?? '',
      parent_resource_ref:payload.parent_resource_ref || input.parentResourceRef || null,
      ...(input.intent ? { intent:input.intent } : {}),
    });
  };

  function resourceNavigation(current = context()) {
    if (!current) return null;
    const scope = bufferScope(current);
    const scopeKey = scope ? `${scope.accountId}:${scope.workspace}:${scope.epoch}` : '';
    if (!current.noteResourceNavigation || current.noteResourceNavigationScope !== scopeKey) {
      current.noteResourceNavigation?.dispose?.();
      current.noteResourceNavigationScope = scopeKey;
      current.noteResourceNavigation = createWindowNavigation({
        scope:{ account:scope?.accountId || '', workspace:scope?.workspace || '' },
        restore:async entry => restoreResourceFolder(current, entry),
      });
    }
    return current.noteResourceNavigation;
  }

  async function restoreResourceFolder(current, entry) {
    const workspace = current?.noteWorkspace;
    const ref = String(entry?.resource?.ref || entry?.ref || '').trim();
    const generation = Number(state.filesGeneration || state.contextEpoch || 0);
    if (!workspace || !ref || current !== context() || (entry?.generation != null && Number(entry.generation) !== generation)) return false;
    return loadEditorResourceFolder(workspace, entry.resource || entry, { query:String(entry.query || ''), commitHistory:false });
  }

  function context() {
    return state.windows.get('notes');
  }

  function revalidateSavedResourceRoot(workspace) {
    const current = context();
    const root = workspace?.left?.resourceRoot;
    const stableKey = canonicalEditorResourceKey(root?.resourceKey || root?.ref || '', root?.provider || 'unknown');
    const scopeToken = currentScope();
    const generation = Number(state.filesGeneration || state.contextEpoch || 0);
    const validationKey = `${scopeToken}:${generation}:${stableKey}`;
    if (!current || !root?.ref || current.noteResourceRootValidationKey === validationKey) return;
    current.noteResourceRootValidationController?.abort?.();
    const controller = new AbortController();
    const validationEpoch = Number(current.noteResourceRootValidationEpoch || 0) + 1;
    current.noteResourceRootValidationKey = validationKey;
    current.noteResourceRootValidationEpoch = validationEpoch;
    current.noteResourceRootValidationController = controller;
    current.noteResourceRootReady = false;
    current.noteResourceRootError = null;
    void filesFacadeClient.reissue(root.ref, { signal:controller.signal }).then(async response => {
      const ref = String(response?.resource?.ref || '').trim();
      if (!ref || controller.signal.aborted || current.noteResourceRootValidationKey !== validationKey || current.noteResourceRootValidationEpoch !== validationEpoch || scopeToken !== currentScope()) return;
      const stat = await filesFacadeClient.stat(ref, { signal:controller.signal });
      if (controller.signal.aborted || current.noteResourceRootValidationKey !== validationKey || current.noteResourceRootValidationEpoch !== validationEpoch || scopeToken !== currentScope()) return;
      const renewed = normalizeAuthorizedResource(stat?.resource || response.resource, {
        purpose:'folder', parentRef:root.parentRef || null, ...pickerScope(),
      });
      if (renewed.kind !== 'folder' || renewed.capabilities.children !== true) throw new Error('Saved Editor folder is no longer listable.');
      const page = await filesFacadeClient.children(renewed.ref, {
        limit:200, query:workspace.left.resourceQuery || '', signal:controller.signal,
        sort:{ key:'name', direction:'asc', directories_first:true },
      });
      if (controller.signal.aborted || current.noteResourceRootValidationKey !== validationKey || current.noteResourceRootValidationEpoch !== validationEpoch || scopeToken !== currentScope() || current !== context()) return;
      // Accept one renewed identity only after the complete validation/list
      // transaction. The stable key keeps render-triggered revalidation from
      // issuing the same sealed ref forever.
      workspace.left.resourceRoot = renewed;
      workspace.left.resourceRows = (page.entries || []).slice(0, 5001).map(item => normalizeAuthorizedResource(item, {
        purpose:'file', ...pickerScope(), parentRef:renewed.ref,
      }));
      workspace.left.resourceCursor = page.next_cursor || null;
      current.noteResourceRootReady = true; current.noteResourceRootError = null;
      persist(true); render();
    }).catch(error => {
      if (error?.name === 'AbortError' || controller.signal.aborted || current.noteResourceRootValidationKey !== validationKey || current.noteResourceRootValidationEpoch !== validationEpoch || scopeToken !== currentScope()) return;
      workspace.left.resourceRoot = null;
      workspace.left.resourceRows = [];
      current.noteResourceRootReady = false;
      current.noteResourceRootError = error?.message || 'Saved Editor folder is unavailable.';
      persist(true); render();
    }).finally(() => { if (current.noteResourceRootValidationController === controller) current.noteResourceRootValidationController = null; });
  }

  // All Notes resource-folder transitions share one abortable transaction.
  // The sidebar changes only after the renewed ref, authoritative stat, and
  // first page have succeeded, so a failed child/up/search load cannot erase
  // the folder the user was viewing.
  async function loadEditorResourceFolder(workspace, requested, { query = '', cursor = null, append = false, commitHistory = true } = {}) {
    const current = context();
    const requestedRef = String(requested?.ref || requested?.resourceRef || requested || '').trim();
    if (!current || !workspace || !requestedRef) return false;
    current.noteResourceRequestController?.abort?.();
    const controller = new AbortController();
    const epoch = Number(current.noteResourceRequestEpoch || 0) + 1;
    current.noteResourceRequestEpoch = epoch; current.noteResourceRequestController = controller;
    const scope = currentScope();
    const priorRoot = workspace.left.resourceRoot;
    const priorRows = workspace.left.resourceRows || [];
    try {
      const renewed = await filesFacadeClient.reissue(requestedRef, { signal:controller.signal });
      const renewedRef = String(renewed?.resource?.ref || requestedRef).trim();
      const stat = await filesFacadeClient.stat(renewedRef, { signal:controller.signal });
      const root = normalizeAuthorizedResource(stat?.resource || renewed?.resource || stat, { purpose:'folder', parentRef:requested?.parentRef || requested?.parent_ref || null, ...pickerScope() });
      if (root.kind !== 'folder' || root.capabilities.children !== true) throw new Error('This authorized folder cannot be listed.');
      const page = await filesFacadeClient.children(root.ref, { limit:200, cursor, query:String(query || ''), signal:controller.signal, sort:{ key:'name', direction:'asc', directories_first:true } });
      if (controller.signal.aborted || epoch !== current.noteResourceRequestEpoch || scope !== currentScope() || current !== context()) return false;
      const rows = (page.entries || []).map(item => normalizeAuthorizedResource(item, { purpose:'file', ...pickerScope(), parentRef:root.ref }));
      const seen = new Set(append ? priorRows.map(item => `${item.provider}:${item.resourceKey || item.ref}`) : []);
      const merged = append ? [...priorRows, ...rows.filter(item => { const key = `${item.provider}:${item.resourceKey || item.ref}`; if (seen.has(key)) return false; seen.add(key); return true; })] : rows;
      workspace.left.resourceRoot = root; workspace.left.resourceRows = merged.slice(0, 5001);
      workspace.left.resourceCursor = page.next_cursor || null; workspace.left.resourceQuery = String(query || '');
      current.noteResourceRootReady = true; current.noteResourceRootError = null; current.noteResourceLoading = false;
      if (commitHistory) resourceNavigation(current)?.commit({ resource:root, query:String(query || ''), generation:Number(state.filesGeneration || state.contextEpoch || 0), scope:{ account:state.accountId || '', workspace:state.workspace || '' } });
      persist(true); render(); return true;
    } catch (error) {
      if (error?.name === 'AbortError' || controller.signal.aborted || epoch !== current.noteResourceRequestEpoch || scope !== currentScope()) return false;
      current.noteResourceLoading = false; current.noteResourceRootError = error?.message || 'Folder listing failed.';
      // Keep the exact prior view on a failed transition.
      workspace.left.resourceRoot = priorRoot; workspace.left.resourceRows = priorRows;
      return false;
    } finally { if (current.noteResourceRequestController === controller) current.noteResourceRequestController = null; }
  }

  function shellViewport() {
    return {
      narrow:window.matchMedia(NOTES_NARROW_QUERY).matches,
      compact:window.matchMedia(NOTES_COMPACT_QUERY).matches,
    };
  }

  function toggleSidebar(side) {
    const current = context();
    const workspace = ensureWorkspace();
    if (!current || !workspace) return;
    const viewport = shellViewport();
    const drawer = viewport.narrow || (side === 'right' && viewport.compact);
    if (drawer) current.noteDrawer = current.noteDrawer === side ? null : side;
    else {
      workspace[side].open = !workspace[side].open;
      persist(true);
    }
    render();
  }

  function ensureShellControls() {
    const current = context();
    if (!current) return null;
    if (!current.noteShellControls) {
      const make = (side) => h('button', {
        id:`copal-notes-${side}-sidebar-toggle`,
        type:'button',
        class:`copal-btn copal-shell-toggle ${side}`,
        'data-shell-side':side,
        'aria-controls':`copal-notes-${side}-sidebar`,
        onclick:() => toggleSidebar(side),
      });
      current.noteShellControls = { left:make('left'), right:make('right') };
    }
    return current.noteShellControls;
  }

  function syncShellControl(button, side, expanded, slot) {
    const noun = side === 'left' ? 'files' : 'details';
    const action = expanded ? 'Collapse' : 'Expand';
    button.textContent = slot === 'sidebar' ? `Hide ${noun}` : side === 'left' ? 'Files' : 'Details';
    button.title = `${action} Editor ${noun}`;
    button.setAttribute('aria-label', `${action} Editor ${noun}`);
    button.setAttribute('aria-expanded', String(expanded));
    button.dataset.shellSlot = slot;
  }

  function finishShellControlMove(controls, before, focusedSide) {
    requestAnimationFrame(() => {
      const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      for (const side of ['left', 'right']) {
        const button = controls[side];
        if (focusedSide === side) button.focus({ preventScroll:true });
        const previous = before[side];
        if (!previous || reduced || !button.isConnected) continue;
        const next = button.getBoundingClientRect();
        const dx = previous.left - next.left; const dy = previous.top - next.top;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
        button.style.pointerEvents = 'none';
        const animation = button.animate(
          [{ transform:`translate(${dx}px, ${dy}px)` }, { transform:'translate(0, 0)' }],
          { duration:180, easing:'cubic-bezier(.2,.8,.2,1)' },
        );
        animation.finished.catch(() => {}).finally(() => { button.style.pointerEvents = ''; });
      }
    });
  }

  function documents() {
    return state.docs.filter((doc) => !OPERATIONAL_KINDS.has(doc.kind));
  }

  // Documents visible in the explorer: excludes operational kinds and,
  // when dot-folders are hidden, documents whose path starts with a dot-folder.
  function explorerDocs() {
    const workspace = ensureWorkspace();
    const docs = documents();
    if (workspace?.left?.showDotFolders) return docs;
    return docs.filter((doc) => {
      // Hide by path (.events/, .copal/, etc.)
      const parts = (doc.name || '').split('/');
      if (parts.some((part) => part.startsWith('.'))) return false;
      // Hide by kind (copal-event is always in .events)
      if (doc.kind === 'copal-event') return false;
      return true;
    });
  }

  // Check if a document lives in a dot-folder (hidden by the explorer toggle).
  function isHiddenDoc(doc) {
    const parts = (doc.name || '').split('/');
    return parts.some((part) => part.startsWith('.'));
  }

  function workspaceDocuments() {
    return [...documents(), TIMELINE_DOCUMENT];
  }

  function ensureWorkspace() {
    const current = context();
    if (!current) return null;
    syncBufferScope(current);
    const docs = workspaceDocuments();
    const signature = docs.map((doc) => doc.id).sort().join(':');
    const currentLeaf = current.noteWorkspace ? findWorkspaceLeaf(current.noteWorkspace) : null;
    const requested = current.selected ?? null;
    if (
      current.noteWorkspace
      && !current.noteSaved
      && current.noteDocsSignature === signature
      && (!requested || currentLeaf?.docId === requested)
    ) return current.noteWorkspace;
    const source = current.noteWorkspace || current.noteSaved || {};
    current.noteWorkspace = normalizeNotesWorkspace(source, docs, requested);
    current.noteDocsSignature = signature;
    current.noteSaved = null;
    current.noteLeafViews ||= new Map();
    current.noteBufferCreationListeners ||= new Map();
    current.noteDrafts ||= new Map();
    current.noteBuffers ||= new Map();
    current.noteAcceptedEnvelopes ||= new Map();
    for (const doc of documents()) if (!current.noteAcceptedEnvelopes.has(doc.id)) current.noteAcceptedEnvelopes.set(doc.id, cloneEnvelope({
      text:doc.text, properties:doc.properties, relations:doc.relations,
      ...(doc.extensions == null ? {} : { extensions:doc.extensions }),
    }));
    const recoveryScope = bufferScope(current);
    if (recoveryScope) for (const doc of documents()) {
      const resource = doc.resource;
      if (!resource?.key || current.noteBuffers.has(doc.id)) continue;
      try {
        const recovered = buffers.recoverDraft(resource, {
          envelope:current.noteAcceptedEnvelopes.get(doc.id),
          actorId:`${recoveryScope.accountId}:${recoveryScope.workspace}`,
          epoch:recoveryScope.epoch,
          scope:recoveryScope,
          save:async (snapshot, flushOptions = {}) => {
            if (flushOptions.scope && !sameSaveScope(flushOptions.scope, recoveryScope)) return false;
            if (!sameSaveScope(recoveryScope, bufferScope(current))) return false;
            return saveDocument({ ...doc, head:snapshot.expectedRevision.value }, snapshot.envelope?.text || '', false, 'notes', { snapshot, returnReceipt:true, scope:recoveryScope, viaBuffer:true, sheet:flushOptions.sheet === true });
          },
        });
        if (recovered?.state().dirty) {
          current.noteBuffers.set(doc.id, recovered);
          current.noteDrafts.set(doc.id, { doc, value:recovered.envelope?.text || '', envelope:cloneEnvelope(recovered.envelope), base:recovered.handle.revision.value, localRevision:recovered.localRevision, resourceKey:resource.key, scope:currentScope(current), scopeObject:recoveryScope });
        }
      } catch (_) { /* an unavailable draft remains isolated from accepted server state */ }
    }
    current.noteRevisionCounters ||= new Map();
    current.noteSaveRuns ||= new Map();
    current.noteSelection ||= new Set();
    current.noteRenderVersion ||= 0;
    current.noteMetrics ||= { renders:0, editorConstructions:0, lastRenderMs:0 };
    revalidateSavedResourceRoot(current.noteWorkspace);
    const leaf = findWorkspaceLeaf(current.noteWorkspace);
    current.selected = leaf?.docId || null;
    return current.noteWorkspace;
  }

  function bufferScope(current = context()) {
    const accountId = String(state.accountId || '');
    if (!accountId) return null;
    return {
      accountId,
      workspace:String(state.workspace || ''),
      storageNamespace:String(state.storageNamespace || ''),
      epoch:Number(state.contextEpoch ?? 0),
    };
  }

  function syncBufferScope(current = context()) {
    if (!current) return null;
    const scope = bufferScope(current);
    if (!scope) return null;
    const actorScope = `${scope.accountId}:${scope.workspace}`;
    const value = `${actorScope}:${scope.epoch}`;
    if (current.noteBufferScope == null) {
      // The first scope adopts loadSaved's layout/settings. Detachment only
      // applies after an authenticated scope was already attached.
      buffers.setScope(actorScope, scope.epoch);
      current.noteBufferScope = value;
      return scope;
    }
    if (current.noteBufferScope === value) return scope;
    for (const buffer of current.noteBuffers?.values() || []) buffers.persistDraft(buffer);
    buffers.setScope(actorScope, scope.epoch);
    // Detach all old in-memory owners after their dirty snapshots are safely
    // persisted. Late promises retain their old closures but cannot reach the
    // new maps or acknowledge a new owner's draft.
    current.noteDrafts?.clear();
    current.noteAcceptedEnvelopes?.clear();
    current.noteSaveRuns?.clear();
    current.noteBuffers?.clear();
    current.noteBufferCreationListeners?.clear();
    for (const leafId of [...(current.noteLeafViews?.keys() || [])]) disposeLeaf(leafId);
    current.noteSelection?.clear();
    current.noteWorkspace = null;
    current.noteSaved = null;
    current.noteDocsSignature = null;
    current.noteShellCache = null;
    current.noteBufferScope = value;
    return scope;
  }

  function suspendScope() {
    const current = context();
    if (!current) return false;
    for (const buffer of current.noteBuffers?.values() || []) buffers.persistDraft(buffer);
    clearTimeout(persistTimer);
    persistTimer = null;
    for (const timer of state.saveTimers?.values?.() || []) clearTimeout(timer);
    for (const leafId of [...(current.noteLeafViews?.keys() || [])]) disposeLeaf(leafId);
    current.noteKeyHandler && current.window?.root?.removeEventListener('keydown', current.noteKeyHandler);
    current.notePageHideHandler && window.removeEventListener('pagehide', current.notePageHideHandler);
    for (const entry of current.noteShellMedia || []) entry.query.removeEventListener('change', entry.handler);
    current.noteDrawerRelease?.();
    current.noteKeyHandler = null;
    current.notePageHideHandler = null;
    current.noteShellMedia = null;
    current.noteDrawer = null;
    current.noteDrawerRelease = null;
    current.noteShellCache = null;
    buffers.invalidateAll();
    current.noteSaveRuns?.clear();
    current.noteDrafts?.clear();
    current.noteAcceptedEnvelopes?.clear();
    current.noteBuffers?.clear();
    current.noteBufferCreationListeners?.clear();
    current.noteSelection?.clear();
    current.noteBufferScope = null;
    return true;
  }

  function currentScope(current = context()) {
    const scope = bufferScope(current);
    return scope ? `${scope.accountId}:${scope.workspace}:${scope.storageNamespace}:${scope.epoch}` : null;
  }

  // Sheet leaves can mount before their first dirty edit creates a shared
  // ResourceBuffer. Keep this notification scoped to the Notes context so a
  // later buffer is attached to every sibling leaf without a polling loop.
  function subscribeToDocumentBuffer(docId, listener) {
    const current = context();
    if (!current || typeof listener !== 'function') return () => {};
    current.noteBufferCreationListeners ||= new Map();
    const listeners = current.noteBufferCreationListeners.get(docId) || new Set();
    listeners.add(listener);
    current.noteBufferCreationListeners.set(docId, listeners);
    return () => {
      listeners.delete(listener);
      if (!listeners.size) current.noteBufferCreationListeners.delete(docId);
    };
  }

  function notifyDocumentBufferCreated(docId, buffer) {
    const listeners = context()?.noteBufferCreationListeners?.get(docId);
    for (const listener of [...(listeners || [])]) listener(buffer);
  }

  function persist(immediate = false) {
    const current = context();
    if (!current?.noteWorkspace) return;
    const write = () => {
      persistTimer = null;
      localStorage.setItem(copalStorageKey('odysseus-copal-notes-layout', state.workspace), serializeNotesWorkspace(current.noteWorkspace));
    };
    clearTimeout(persistTimer);
    if (immediate) write();
    else persistTimer = setTimeout(write, 80);
  }

  function activeLeaf(workspace = ensureWorkspace()) {
    return workspace ? findWorkspaceLeaf(workspace) : null;
  }

  function activeDoc(workspace = ensureWorkspace()) {
    const leaf = activeLeaf(workspace);
    return leaf ? workspaceDocuments().find((doc) => doc.id === leaf.docId) || null : null;
  }

  function inspectorDoc(workspace) {
    const id = workspace.right.pinnedDocId || activeLeaf(workspace)?.docId;
    return workspaceDocuments().find((doc) => doc.id === id) || null;
  }

  function setActive(groupId, leafId) {
    const workspace = ensureWorkspace();
    if (!workspace || !activateWorkspaceLeaf(workspace, groupId, leafId)) return;
    const leaf = findWorkspaceLeaf(workspace, leafId);
    context().selected = leaf?.docId || null;
    state.selected = context().selected;
    persistActiveContext();
    persist(true);
    render();
  }

  function open(id, options = {}) {
    const doc = workspaceDocuments().find((item) => item.id === id);
    const workspace = ensureWorkspace();
    if (!doc || !workspace) return null;
    // Route copal-event documents to the native event editor.
    if (noteViewType(doc) === 'event' && openEventEditor) {
      openEventEditor(doc.id);
      return null;
    }
    let leaf = options.leafId ? workspaceLeaves(workspace).find((item) => item.id === options.leafId && item.docId === doc.id) : doc.virtual ? workspaceLeaves(workspace).find((item) => item.docId === doc.id) : null;
    if (options.leafId && !leaf) return null;
    if (leaf) {
      const group = groupForLeaf(workspace, leaf.id);
      if (options.mode && ['live', 'source', 'reading'].includes(options.mode)) leaf.mode = options.mode;
      if (group) activateWorkspaceLeaf(workspace, group.id, leaf.id);
    } else leaf = openWorkspaceDocument(workspace, doc, options);
    if (leaf && (leaf.view === 'base' || leaf.view === 'canvas')) {
      // Base/Canvas source is a presentation of the addressed leaf rather
      // than the Markdown mode flag. Explicit source opens enter raw mode;
      // every typed open clears it so /bases can deterministically remount
      // the typed surface after a raw-source detour.
      leaf.rawSource = options.mode === 'source';
    }
    revealInExplorer(doc, workspace);
    context().selected = id;
    state.selected = id;
    persistActiveContext();
    persist(true);
    render();
    return leaf;
  }

  function openResource(resource, payload = {}) {
    const handle = normalizeResourceHandle(resource);
    const key = handle.key;
    if (!key?.resourceId) throw new TypeError('Editor resource key is required');
    const existing = state.docs.find((doc) => doc.resource?.key && sameResourceKey(doc.resource.key, key));
    const id = existing?.id || `resource:${encodeURIComponent(key.accountId)}:${encodeURIComponent(key.workspaceId)}:${encodeURIComponent(key.provider)}:${encodeURIComponent(key.resourceId)}`;
    const current = context();
    const existingBuffer = existing && current?.noteBuffers?.get(existing.id);
    const existingDraft = existing && current?.noteDrafts?.get(existing.id);
    const existingDirty = Boolean(existingDraft || existingBuffer?.state?.()?.dirty);
    if (existing && existingDirty) {
      // A second Files open refreshes focus only. Preserve the dirty envelope,
      // selections, and CAS base until the existing buffer is explicitly saved.
      open(id, payload.intent ? { intent:payload.intent } : { reuse:true });
      return id;
    }
    const representation = handle.representation;
    const snapshot = snapshotEnvelope({
      key:handle.key,
      expectedRevision:handle.revision,
      envelope:cloneEnvelope({
        text:String(payload.text ?? payload.content ?? ''),
        ...(handle.metadata ? { metadata:handle.metadata } : {}),
        properties:payload.properties == null ? undefined : payload.properties,
        relations:payload.relations == null ? undefined : payload.relations,
      }),
    });
    const doc = existing || {
      id,
      name:String(payload.name || handle.locator?.displayName || key.resourceId),
      // The adapter's representation is authoritative. A filename suffix is
      // presentation metadata and must not turn a source resource into live
      // Markdown widgets; host adapters may opt in with representation:
      // "markdown" when that representation is actually supported.
      kind:representation === 'base' ? 'base' : representation === 'markdown' ? 'markdown' : 'text',
      text:snapshot.envelope.text, properties:payload.properties || {}, relations:payload.relations || [], tags:[],
      resource:handle, resourceKey:key, savePolicy:key.provider === 'host' ? 'explicit' : 'autosave', sourceKind:key.provider === 'host' ? 'host' : 'copal',
      sourceMetadata:handle.metadata || null,
      readOnly:handle.capabilities?.write !== true && handle.capabilities?.edit !== true,
      resourceSnapshot:snapshot,
      hostParentResourceRef:payload.parent_resource_ref || null,
    };
    if (!existing) state.docs.push(doc);
    else Object.assign(existing, {
      ...payload,
      text:snapshot.envelope.text,
      // Exact-open through Files is intentionally read-only until a provider
      // exposes an Editor mutation adapter.  Refresh this on resource
      // replacement too; an already loaded Copal note may have been opened
      // through the narrower Files capability surface.
      readOnly:handle.capabilities?.write !== true && handle.capabilities?.edit !== true,
      sourceMetadata:handle.metadata || existing.sourceMetadata || null,
      resource:handle, resourceKey:key, resourceSnapshot:snapshot,
      savePolicy:key.provider === 'host' ? 'explicit' : existing.savePolicy,
      hostParentResourceRef:payload.parent_resource_ref || existing.hostParentResourceRef || null,
    });
    // An opaque Files handoff can replace the presentation and capability
    // envelope of an already-open document without changing its id, active
    // leaf, workspace layout, or the documents array identity. Bump the local
    // render revision so the shell cannot reuse an editable view after the
    // resource has become a read-only Files projection.
    if (current) current.noteRenderVersion = (current.noteRenderVersion || 0) + 1;
    open(id, payload.intent ? { intent:payload.intent } : { reuse:true });
    return id;
  }

  function pickerScope() {
    const scope = bufferScope();
    return {
      accountScope:scope?.accountId || state.accountId || '',
      workspaceScope:scope?.workspace || state.workspace || '',
      generation:Number(state.filesGeneration || state.contextEpoch || 0),
    };
  }

  function showResourcePicker(purpose, onSelect) {
    const pickerContext = pickerScope();
    const picker = createResourcePicker({
      client:filesFacadeClient, purpose, ...pickerContext,
      getGeneration:() => Number(state.filesGeneration || state.contextEpoch || 0),
      getAccountScope:() => String(state.accountId || ''),
      onSelect, rootLabel:'Editor',
    });
    void picker.open();
    return picker;
  }

  async function openFileFromPicker() {
    showResourcePicker('file', async (selected) => {
      try {
        await resourceOpener({ resourceRef:selected.ref, name:selected.name, parentResourceRef:selected.parentRef });
      } catch (error) {
        context()?.window?.setStatus(error?.message || 'File could not be opened.', true);
      }
    });
  }

  async function openFolderFromPicker() {
    showResourcePicker('folder', async (selected) => {
      const workspace = ensureWorkspace();
      if (!workspace) return;
      const current = context();
      if (!current) return;
      current.noteResourceRootValidationKey = null;
      current.noteResourceRootReady = false;
      current.noteResourceRootError = null;
      const loaded = await loadEditorResourceFolder(workspace, selected, { query:'', commitHistory:true });
      if (!loaded) context()?.window?.setStatus(current.noteResourceRootError || 'Folder could not be opened.', true);
    });
  }

  async function configureTemplateFolder() {
    showResourcePicker('template-folder', async (selected) => {
      try {
        const normalized = normalizeTemplateFolderSelection({
          resourceRef:selected.ref, resourceKey:selected.resourceKey, provider:selected.provider,
          revision:selected.revision, kind:'folder', logicalPath:selected.logicalPath,
          capabilities:Object.keys(selected.capabilities || {}),
          accountScope:selected.accountScope, workspaceScope:selected.workspaceScope,
          generation:selected.generation, policyGeneration:state.filesPolicyGeneration,
        }, { purpose:'create' });
        const workspace = ensureWorkspace();
        workspace.settings.templateFolder = normalized.logicalPath;
        workspace.settings.templateFolderRef = normalized;
        persist(true); render();
        context()?.window?.setStatus(`Template folder: ${selected.name}`);
      } catch (error) {
        context()?.window?.setStatus(error?.message || 'Template folder is not authorized for writing.', true);
      }
    });
  }

  function revealInExplorer(doc, workspace = ensureWorkspace()) {
    const parts = String(doc?.name || '').split('/').slice(0, -1);
    for (let index = 1; index <= parts.length; index += 1) {
      const path = parts.slice(0, index).join('/');
      if (!workspace.left.expanded.includes(path)) workspace.left.expanded.push(path);
    }
  }

  function disposeLeaf(leafId) {
    const current = context();
    const cache = current?.noteLeafViews?.get(leafId);
    if (!cache) return;
    disposeSheet(cache);
    disposeRawEditor(cache);
    current.noteLeafViews.delete(leafId);
  }

  function disposeSheet(cache) {
    if (!cache) return;
    cache.sheetCleanup?.(); cache.sheetCleanup = null; cache.sheetController = null;
    cache.sheetBufferUnsubscribe?.(); cache.sheetBufferUnsubscribe = null; cache.sheetBuffer = null;
    cache.sheetBufferCreationUnsubscribe?.(); cache.sheetBufferCreationUnsubscribe = null;
    cache.sheetBufferCreationDocId = null;
    if (cache.sheetBufferRefreshTimer) { clearTimeout(cache.sheetBufferRefreshTimer); cache.sheetBufferRefreshTimer = null; }
    cache.sheetResourceKey = null;
  }

  function disposeRawEditor(cache) {
    if (!cache) return;
    cache.contextMenuDispose?.(); cache.contextMenuDispose = null;
    if (cache.editor) {
      cache.editor.destroy?.();
      state.noteEditors.delete(cache.editor);
    }
    cache.editor = null; cache.editorWrap = null; cache.host = null; cache.preview = null; cache.reading = null;
  }

  function publishRawBaseDefinition(doc, value) {
    if (doc?.kind !== 'base') return;
    const current = context();
    const resourceKey = doc.resource?.key || doc.resourceKey || { provider:'copal', accountId:String(state.accountId || ''), workspaceId:String(state.workspace || ''), resourceId:String(doc.id) };
    current.baseDefinitionStores ||= new Map();
    const key = JSON.stringify(resourceKey);
    let store = current.baseDefinitionStores.get(key);
    if (!store) {
      const listeners = new Set();
      store = { value:state.baseDefinition || {}, revision:null, authoritativeLocal:null, listeners, subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }, set(next, revision = null) { this.value = next; if (revision != null) this.revision = revision; this.authoritativeLocal = null; for (const listener of [...listeners]) listener(next, revision); }, publishLocal(next, revision, scope) { this.value = next; this.revision = revision; this.authoritativeLocal = { definition:next, revision, scope, source:'raw-editor' }; for (const listener of [...listeners]) listener(next, revision); } };
      current.baseDefinitionStores.set(key, store);
    }
    const draft = current.noteDrafts?.get(doc.id); const buffer = current.noteBuffers?.get(doc.id);
    const snapshot = getDraftSnapshot(doc.id);
    const revision = snapshot?.localRevision ?? buffer?.localRevision ?? draft?.localRevision;
    if (!Number.isSafeInteger(Number(revision)) || typeof store.publishLocal !== 'function') return;
    // The Base source is YAML or JSON and its canonical parser is server
    // owned. Publish only an explicit local revision barrier here; the next
    // typed preview supplies the parsed definition or a useful parse error.
    store.publishLocal(store.value || state.baseDefinition || {}, Number(revision), bufferScope(current));
  }

  function openBaseSource(row) {
    const rowResourceKey = row?.resourceKey || row?.resource?.key || null;
    const candidates = documents();
    // A projected row's opaque key is authoritative. If it cannot be matched
    // to a currently authorized document, fail closed instead of opening a
    // different document that happens to share a stale id.
    let source = null;
    if (rowResourceKey) {
      source = candidates.find((doc) => sameResourceKey(doc.resource?.key || doc.resourceKey, rowResourceKey)) || null;
      if (!source) {
        context()?.window?.setStatus('The Base row source is no longer authorized. Refresh the sheet and try again.', true);
        return false;
      }
      if (row.documentId != null && String(source.id) !== String(row.documentId)) {
        context()?.window?.setStatus('The Base row source identity changed. Refresh the sheet and try again.', true);
        return false;
      }
    } else if (row?.documentId != null) {
      source = candidates.find((doc) => String(doc.id) === String(row.documentId)) || null;
      if (!source) {
        context()?.window?.setStatus('The Base row source is unavailable. Refresh the sheet and try again.', true);
        return false;
      }
    }
    if (!source || typeof openOtherView !== 'function') {
      context()?.window?.setStatus('The Base row source could not be opened.', true);
      return false;
    }
    openOtherView(source.id, 'notes');
    return true;
  }

  async function invalidateBaseLeaves(documentId = null) {
    const current = context();
    if (!current) return 0;
    const token = documentId == null ? null : String(documentId);
    const refreshes = [];
    for (const cache of current.noteLeafViews.values()) {
      const controller = cache.sheetController;
      if (!controller) continue;
      const controllerState = controller.getState?.() || {};
      const rows = controllerState.rows || [];
      const baseMatch = token == null || String(cache.docId) === token;
      const rowMatch = token != null && rows.some((row) => String(row.documentId ?? '') === token || String(row.resourceKey?.resourceId ?? '') === token);
      if (!baseMatch && !rowMatch) continue;
      controller.invalidateQueries?.();
      refreshes.push(controller.refresh('watch'));
    }
    await Promise.allSettled(refreshes);
    return refreshes.length;
  }

  async function saveDraft(docId, { returnReceipt = false } = {}) {
    const current = context();
    const running = current?.noteSaveRuns?.get(docId);
    if (running) {
      const result = await running;
      return result && current.noteDrafts.has(docId) ? saveDraft(docId, { returnReceipt }) : returnReceipt ? result : Boolean(result);
    }
    const draft = current?.noteDrafts?.get(docId);
    if (!draft) return true;
    if (draft.scope && draft.scope !== currentScope(current)) return false;
    const resourceBuffer = current.noteBuffers?.get(docId);
    if (resourceBuffer) {
      // ResourceBuffer serializes its own drain, but saveDraft can be entered
      // concurrently by blur, Cmd/Ctrl-S, and the autosave timer. Keep the
      // document-level run barrier in place for buffered resources too, so a
      // second caller cannot observe the first caller's pre-acknowledgement
      // base and start a parallel transport write.
      const flushRun = (async () => {
        try { return await resourceBuffer.flush({ scope:draft.scopeObject }); }
        catch (error) {
          setLeafSaveState(docId, resourceBuffer.state().status === 'conflict' ? 'conflict' : 'error');
          return returnReceipt ? { outcome:'failed', message:resourceBuffer.state().error?.message || error?.message || 'The draft could not be saved' } : false;
        }
      })();
      current.noteSaveRuns.set(docId, flushRun);
      let receipt;
      try { receipt = await flushRun; }
      finally { if (current.noteSaveRuns.get(docId) === flushRun) current.noteSaveRuns.delete(docId); }
      if (receipt !== false && receipt?.outcome !== 'conflict') {
        if (draft.scope && draft.scope !== currentScope(current)) return false;
        if (!resourceBuffer.state().dirty) {
          current.noteDrafts.delete(docId);
          buffers.discardDraft(resourceBuffer);
          current.noteAcceptedEnvelopes?.set(docId, cloneEnvelope(resourceBuffer.envelope));
        }
        const bufferState = resourceBuffer.state();
        setLeafSaveState(docId, bufferState.status === 'conflict' ? 'conflict' : bufferState.status === 'error' ? 'error' : bufferState.dirty ? 'unsaved' : 'saved');
        return returnReceipt ? receipt : !resourceBuffer.state().dirty;
      }
      return returnReceipt ? receipt : false;
    }
    const run = (async () => {
      clearTimeout(state.saveTimers.get(docId));
      state.saveTimers.delete(docId);
      const latest = state.docs.find((doc) => doc.id === docId) || draft.doc;
      const guarded = { ...latest, head:draft.base };
      setLeafSaveState(docId, 'saving');
      const saved = await saveDocument(guarded, draft.value, false, 'notes', {
        snapshot:cloneEnvelope({ key:draft.resourceKey || guarded.resource?.key, expectedRevision:{ kind:'copalHead', value:String(draft.base || '') }, localRevision:draft.localRevision, actionId:draft.actionId, scope:draft.scopeObject, envelope:draft.envelope }),
        returnReceipt:true, scope:draft.scopeObject, viaBuffer:true, sheet:draft.sheet === true,
      });
      if (draft.scope && draft.scope !== currentScope(current)) return false;
      const queued = current.noteDrafts.get(docId);
      if (saved?.outcome !== 'applied' || !saved?.revision) { setLeafSaveState(docId, saved?.outcome === 'conflict' ? 'conflict' : 'error'); return returnReceipt ? saved : false; }
      const fresh = saved.doc || state.docs.find((doc) => doc.id === docId) || guarded;
      for (const cache of current.noteLeafViews.values()) if (cache.docId === docId) cache.doc = fresh;
      // A body string is not a revision: property/relation edits can race with
      // this request, and a later body edit may intentionally equal this one.
      if (queued?.localRevision === draft.localRevision) current.noteDrafts.delete(docId);
      else if (queued) { queued.base = fresh.head; queued.doc = fresh; }
      setLeafSaveState(docId, current.noteDrafts.has(docId) ? 'unsaved' : 'saved');
      if (!current.noteDrafts.has(docId)) current.noteAcceptedEnvelopes?.set(docId, cloneEnvelope(draft.envelope));
      return returnReceipt ? saved : true;
    })();
    current.noteSaveRuns.set(docId, run);
    let succeeded = false;
    try { succeeded = await run; } finally { if (current.noteSaveRuns.get(docId) === run) current.noteSaveRuns.delete(docId); }
    // A conflict/failed receipt is a terminal result for this explicit flush;
    // retrying merely because the draft remains would hide CAS failures and
    // can overwrite a competing edit without a user decision.
    return succeeded === true && current.noteDrafts.has(docId) ? saveDraft(docId, { returnReceipt }) : returnReceipt ? succeeded : Boolean(succeeded);
  }

  async function prepareDelete(docId) {
    if (!await saveDraft(docId)) {
      context()?.window?.setStatus('Could not save the latest edit, so the note was not moved to Trash.', true);
      return false;
    }
    clearTimeout(state.saveTimers.get(docId));
    state.saveTimers.delete(docId);
    context()?.noteDrafts?.delete(docId);
    return true;
  }

  // Shared source mutation entry point for Base commands and other projected
  // views. It joins the resource buffer before flushing, so a command applied
  // while another write is in flight composes with the newest local envelope.
  function queueDocumentSave(doc, value, { flush = true, snapshot = null, baseRevision = null } = {}) {
    queueSave(doc, value, {
      baseRevision:baseRevision ?? snapshot?.expectedRevision?.value,
      rebase:snapshot,
    });
    return flush ? saveDraft(doc.id, { returnReceipt:true }) : true;
  }

  function queueSave(doc, value, options = {}) {
    const current = context();
    if (!current) return;
    // Wiki can be the first Copal view opened. Its editor shares the Notes
    // draft queue, so initialize the queue maps even when Notes has not yet
    // mounted its workspace shell.
    current.noteLeafViews ||= new Map();
    current.noteDrafts ||= new Map();
    current.noteSaveRuns ||= new Map();
    current.noteRevisionCounters ||= new Map();
    current.noteBuffers ||= new Map();
    current.noteBufferCreationListeners ||= new Map();
    current.noteAcceptedEnvelopes ||= new Map();
    const existing = current.noteDrafts.get(doc.id);
    const previousDraft = existing || null;
    const indexed = state.docs.find((item) => item.id === doc.id) || doc;
    const localRevision = Math.max(existing?.localRevision || 0, current.noteRevisionCounters.get(doc.id) || 0) + 1;
    current.noteRevisionCounters.set(doc.id, localRevision);
    const envelope = cloneEnvelope({
      text:value,
      ...(doc.sourceMetadata ? { metadata:doc.sourceMetadata } : {}),
      properties:doc.properties == null ? undefined : doc.properties,
      relations:doc.relations == null ? undefined : doc.relations,
      ...(doc.extensions == null ? {} : { extensions:doc.extensions }),
    });
    const scopeObject = bufferScope(current);
    const scope = currentScope(current);
    current.noteDrafts.set(doc.id, {
      doc:indexed, value, envelope, base:options.baseRevision || existing?.base || indexed.head, sheet:options.sheet === true,
      localRevision, actionId:createSaveActionId(`document-${doc.id}`), resourceKey:doc.resource?.key || doc.resourceKey || null, scope, scopeObject,
    });
    try {
      const resource = doc.resource || (doc.resourceKey ? { key:doc.resourceKey, revision:{ kind:'copalHead', value:String(indexed.head || '0') }, locator:{ displayName:doc.name || doc.id, locationLabel:doc.name || doc.id }, representation:'nativeNote', capabilities:{ read:true, edit:doc.readOnly !== true } } : null);
      if (resource?.key && scopeObject) {
        const accepted = current.noteAcceptedEnvelopes.get(doc.id) || envelope;
        const buffer = buffers.acquire(resource, accepted, { actorId:`${scopeObject.accountId}:${scopeObject.workspace}`, epoch:scopeObject.epoch, scope:scopeObject, save:async (snapshot, flushOptions = {}) => {
          if (flushOptions.scope && !sameSaveScope(flushOptions.scope, scopeObject)) return false;
          if (scope !== currentScope(current)) return false;
          const target = state.docs.find((item) => item.id === doc.id) || indexed;
          const targetResource = target.resource || (target.resourceKey ? { key:target.resourceKey } : null);
          if (snapshot.key?.provider === 'host' && typeof saveResource === 'function') return saveResource(snapshot, { resource:target.resource || targetResource, document:target, scope:scopeObject });
          return saveDocument({ ...target, head:snapshot.expectedRevision.value }, snapshot.envelope?.text ?? value, false, 'notes', { snapshot, returnReceipt:true, scope:scopeObject, viaBuffer:true, sheet:flushOptions.sheet === true });
        } });
        if (options.rebase) {
          const rebasedRevision = options.rebase.expectedRevision;
          if (!Number.isSafeInteger(Number(options.rebase.localRevision)) || !buffer.retryAtRevision(Number(options.rebase.localRevision), rebasedRevision)) {
            throw new Error('The reviewed version is no longer current; compare it again before saving over it.');
          }
        }
        buffer.apply(envelope, { origin:options.origin || 'local', history:options.history !== false, sheet:options.sheet === true });
        current.noteBuffers.set(doc.id, buffer);
        notifyDocumentBufferCreated(doc.id, buffer);
        buffers.persistDraft(buffer);
      }
    } catch (error) {
      // A reviewed/rebased snapshot is an explicit CAS decision. Preserve the
      // prior draft when that decision is stale; silently retaining the newly
      // queued legacy draft would let an unseen remote head be overwritten.
      if (options.rebase) {
        if (previousDraft) current.noteDrafts.set(doc.id, previousDraft);
        else current.noteDrafts.delete(doc.id);
        throw error;
      }
      /* legacy documents continue through the existing notes queue */
    }
    clearTimeout(state.saveTimers.get(doc.id));
    if (doc.savePolicy !== 'explicit') state.saveTimers.set(doc.id, setTimeout(() => saveDraft(doc.id), 700));
    setLeafSaveState(doc.id, 'unsaved');
  }

  // Public source transaction seam for projections such as Mind.  It captures
  // the current draft envelope and its CAS base, then enters the same buffer
  // history/autosave path used by CodeMirror and Wiki textareas.
  function applyDocumentTransaction(doc, transform, { origin = 'projection' } = {}) {
    if (!doc || typeof transform !== 'function') return { outcome:'failed', message:'A document and source transaction are required' };
    if (doc.readOnly === true || doc.builtin === true || doc.note_error || doc.rawPreserved === true) return { outcome:'failed', message:'This source is read-only until it is recovered or converted' };
    const snapshot = getDraftSnapshot(doc.id);
    const source = String(snapshot?.envelope?.text ?? doc.text ?? '');
    const next = transform(source);
    if (typeof next !== 'string') return { outcome:'failed', message:'Source transaction must return text' };
    if (next === source) return { outcome:'unchanged', source, snapshot };
    queueSave(doc, next);
    doc.text = next;
    syncDocumentEditors(doc.id, next);
    setLeafSaveState(doc.id, 'unsaved');
    const transactionId = `notes-tx-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    return { outcome:'queued', source, content:next, snapshot, origin, transactionId };
  }

  function syncDocumentEditors(docId, value, source = null) {
    for (const cache of context()?.noteLeafViews?.values() || []) {
      if (cache.docId !== docId || cache.editor === source) continue;
      cache.editor?.setValue(value);
      if (cache.reading?.isConnected) { cache.reading.replaceChildren(renderMarkdown(value, new Set([docId]))); applyCompletedVisibility(cache.reading); }
      if (cache.preview && !cache.preview.hidden) { cache.preview.replaceChildren(renderMarkdown(value, new Set([docId]))); applyCompletedVisibility(cache.preview); }
    }
  }

  function applyDocumentSource(doc, content, selection = null) {
    const current = context();
    const leaf = workspaceLeaves(ensureWorkspace()).find((item) => item.docId === doc.id && current.noteLeafViews.get(item.id)?.editor);
    const editor = leaf ? current.noteLeafViews.get(leaf.id)?.editor : null;
    current.noteRenderVersion = (current.noteRenderVersion || 0) + 1;
    doc.text = content;
    if (editor) editor.applyValue(content, selection || editor.getSelection());
    else queueSave(doc, content);
    syncDocumentEditors(doc.id, content, editor);
  }

  function setLeafSaveState(docId, value) {
    for (const cache of context()?.noteLeafViews?.values() || []) {
      if (cache.docId !== docId) continue;
      cache.saveState = value;
      updateLeafStatus(cache);
    }
  }

  async function flushAll() {
    const ids = [...(context()?.noteDrafts?.keys() || [])];
    return Promise.all(ids.map(saveDraft));
  }

  function destroy() {
    void flushAll();
    const current = context();
    if (current) current.noteShellCache = null;
    for (const leafId of [...(current?.noteLeafViews?.keys() || [])]) disposeLeaf(leafId);
    if (current?.noteKeyHandler) current.window.root.removeEventListener('keydown', current.noteKeyHandler);
    if (current?.notePageHideHandler) window.removeEventListener('pagehide', current.notePageHideHandler);
    for (const entry of current?.noteShellMedia || []) entry.query.removeEventListener('change', entry.handler);
    if (current) {
      current.notePageHideHandler = null; current.noteShellMedia = null; current.noteDrawer = null;
      current.noteDrawerRelease?.(); current.noteDrawerRelease = null;
      current.noteResourceRequestController?.abort?.(); current.noteResourceRequestController = null;
      current.noteResourceRootValidationController?.abort?.(); current.noteResourceRootValidationController = null;
      clearTimeout(current.noteResourceSearchTimer); current.noteResourceSearchTimer = null;
      current.noteResourceNavigation?.dispose?.(); current.noteResourceNavigation = null; current.noteResourceNavigationScope = null;
    }
    clearTimeout(persistTimer);
    persist(true);
    if (globalThis.__openClankOpenResourceHandle === resourceOpener) globalThis.__openClankOpenResourceHandle = previousResourceOpener;
  }

  function getDraftSnapshot(docId) {
    const current = context();
    const buffer = current?.noteBuffers?.get(docId);
    if (buffer?.state?.().dirty) return cloneEnvelope(buffer.snapshot());
    const draft = current?.noteDrafts?.get(docId);
    return draft ? cloneEnvelope(draft) : null;
  }

  // A clean buffer still owns the latest acknowledged provider head.  Callers
  // that issue a serialized command need that CAS token even after the draft
  // has been acknowledged; the ordinary draft accessor intentionally hides
  // clean buffers so read/preview paths continue to use indexed documents.
  function getAuthoritativeSnapshot(docId) {
    const current = context();
    const buffer = current?.noteBuffers?.get(docId);
    return buffer ? cloneEnvelope(buffer.snapshot()) : null;
  }

  function rotateResourceRef(docId, resourceRef, nextResource = null) {
    const value = String(resourceRef || '').trim();
    const doc = state.docs.find((item) => item.id === docId);
    if (!value || !doc) return false;
    const normalized = nextResource ? normalizeResourceHandle(nextResource) : null;
    // A rotated opaque ref may update a locator, but it must never retarget a
    // leaf to a different resource key while a save is pending.
    if (normalized && doc.resource?.key && !sameResourceKey(doc.resource.key, normalized.key)) return false;
    doc.resourceRef = value;
    if (normalized) {
      doc.resource = { ...doc.resource, ...normalized, locator:{ ...normalized.locator, opaqueRef:value } };
      doc.resourceKey = normalized.key;
      const current = context();
      const buffer = current?.noteBuffers?.get(docId);
      const draft = current?.noteDrafts?.get(docId);
      const dirty = !!buffer?.state?.().dirty || !!draft;
      if (buffer) {
        // A Files rename/move rotates the opaque locator, but the follow-up
        // openResource response may carry a newer head than this dirty draft
        // has seen. Keep the dirty CAS base until an authoritative envelope
        // comparison or save acknowledgement advances it; rebasing here could
        // authorize overwriting an unseen remote edit.
        buffer.handle = {
          ...buffer.handle, ...normalized,
          ...(dirty ? { revision:buffer.handle.revision } : {}),
          locator:{ ...normalized.locator, opaqueRef:value },
        };
      }
      if (draft && !dirty && normalized.revision?.kind === 'copalHead') draft.base = normalized.revision.value;
    } else if (doc.resource) {
      doc.resource = { ...doc.resource, locator:{ ...doc.resource.locator, opaqueRef:value } };
    }
    const timer = state.saveTimers?.get(docId);
    if (timer) { clearTimeout(timer); state.saveTimers.delete(docId); }
    if (doc.savePolicy !== 'explicit' && context()?.noteDrafts?.has(docId)) state.saveTimers.set(docId, setTimeout(() => saveDraft(docId), 700));
    return true;
  }

  async function retrySaveAtRevision(docId, localRevision, revision) {
    const current = context();
    const buffer = current?.noteBuffers?.get(docId);
    if (!buffer) return null;
    if (!buffer.retryAtRevision(localRevision, revision)) return false;
    const draft = current.noteDrafts.get(docId);
    if (draft) draft.base = revision.value;
    clearTimeout(state.saveTimers.get(docId)); state.saveTimers.delete(docId);
    buffers.persistDraft(buffer);
    return saveDraft(docId);
  }

  // An uncertain transport result must replay the buffer's immutable pending
  // submission. Do not queue a second local envelope here: the ResourceBuffer
  // keeps the original action id and CAS head for this exact retry.
  async function retryDocumentSave(docId) {
    const buffer = context()?.noteBuffers?.get(docId);
    const state = buffer?.state?.();
    const retryState = documentRetryState(docId);
    if (!buffer || !state?.dirty || state.status !== 'error' || !retryState.replayable) {
      return { outcome:'failed', code:'no-pending-replay', message:retryState.message || 'There is no uncertain pending save to replay.' };
    }
    return saveDraft(docId, { returnReceipt:true });
  }

  function documentRetryState(docId) {
    const buffer = context()?.noteBuffers?.get(docId);
    const state = buffer?.state?.();
    if (!buffer || !state?.dirty || state.status !== 'error' || !buffer.pending) return { replayable:false, message:'There is no uncertain pending save to replay.' };
    if (buffer.error?.retryable === false || buffer.error?.result?.retryable === false) return { replayable:false, message:buffer.error?.message || 'This save failure requires review before retrying.' };
    return { replayable:true };
  }

  // Sheet conflict resolution is deliberately separate from response-loss
  // replay. The caller has shown the remote envelope and received an explicit
  // user decision to apply its cell intent to that reviewed version.
  async function rebaseDocumentAtRevision(docId, revision, envelope, { sheet = false } = {}) {
    const current = context();
    const buffer = current?.noteBuffers?.get(docId);
    const value = String(revision?.value ?? revision ?? '');
    if (!buffer || !value || buffer.state().status !== 'conflict') {
      return { outcome:'failed', message:'The source conflict is no longer available; compare the latest version again.' };
    }
    const remote = buffer.state().conflict?.remote;
    const remoteHead = String(remote?.head || remote?.revision?.value || '');
    if (remoteHead && remoteHead !== value) {
      return { outcome:'conflict', remote, message:'The source changed again; compare the newest version.' };
    }
    const next = cloneEnvelope(envelope);
    if (!next || typeof next !== 'object') return { outcome:'failed', message:'The reviewed source envelope is unavailable.' };
    const draft = current.noteDrafts?.get(docId);
    clearTimeout(state.saveTimers.get(docId)); state.saveTimers.delete(docId);
    // force=true replaces the conflicted local base with the reviewed remote
    // envelope. The following apply creates one fresh local action at that
    // head; it never overwrites a competing revision implicitly.
    buffer.resolveExternal(next, { kind:'copalHead', value }, { force:true });
    const snapshot = buffer.apply(next, { origin:'sheet-conflict-rebase', history:true, sheet:sheet === true });
    if (draft) {
      draft.base = value; draft.localRevision = snapshot.localRevision; draft.actionId = snapshot.actionId;
      draft.envelope = cloneEnvelope(next); draft.value = String(next.text ?? ''); draft.sheet = sheet === true;
    }
    buffers.persistDraft(buffer);
    return saveDraft(docId, { returnReceipt:true });
  }

  function acceptSavedDocument(docId, content = null, options = {}) {
    const current = context();
    const doc = state.docs.find((item) => item.id === docId);
    const expected = options?.expectedLocalRevision;
    const buffer = current?.noteBuffers?.get(docId);
    const draft = current?.noteDrafts?.get(docId);
    const localRevision = buffer?.localRevision ?? draft?.localRevision ?? 0;
    const authoritative = options?.document && typeof options.document === 'object' ? options.document : doc;
    if (expected !== undefined && Number(expected) !== Number(localRevision)) {
      // A confirmed write of the reviewed version advances the base while
      // preserving any edits made after the comparison was approved.
      if (options.acknowledge === true && buffer && authoritative?.head && buffer.acknowledge(Number(expected), { kind:'copalHead', value:authoritative.head })) {
        if (draft) draft.base = authoritative.head;
        current.noteAcceptedEnvelopes?.set(docId, cloneEnvelope({ text:authoritative.text, properties:authoritative.properties, relations:authoritative.relations }));
        buffers.persistDraft(buffer);
      }
      return false;
    }
    const value = content ?? String((authoritative?.text ?? doc?.text) || '');
    if (buffer && authoritative) {
      buffer.resolveExternal({
        text:value,
        properties:cloneEnvelope(authoritative.properties),
        relations:cloneEnvelope(authoritative.relations),
        ...(authoritative.extensions == null ? {} : { extensions:cloneEnvelope(authoritative.extensions) }),
      }, { kind:'copalHead', value:String(authoritative.head || buffer.handle.revision.value) }, { force:true });
      buffers.discardDraft(buffer);
      current.noteAcceptedEnvelopes?.set(docId, cloneEnvelope(buffer.envelope));
    }
    current?.noteDrafts?.delete(docId);
    if (doc && authoritative) {
      Object.assign(doc, authoritative, { text:value });
      for (const cache of current?.noteLeafViews?.values() || []) if (cache.docId === docId) cache.doc = doc;
    }
    syncDocumentEditors(docId, value);
    setLeafSaveState(docId, 'saved');
    return true;
  }

  /** Overlay a pending complete envelope on a fresh indexed document. */
  function projectDocument(fresh, scope = null) {
    const source = fresh && typeof fresh === 'object' ? fresh : {};
    const current = context();
    if (scope && !sameSaveScope(scope, bufferScope(current))) return source;
    current.noteAcceptedEnvelopes ||= new Map();
    const authoritative = cloneEnvelope({
      text:source.text, properties:source.properties, relations:source.relations,
      ...(source.extensions == null ? {} : { extensions:source.extensions }),
    });
    current.noteAcceptedEnvelopes.set(source.id, authoritative);
    const draft = current?.noteDrafts?.get(source.id);
    const buffer = current?.noteBuffers?.get(source.id);
    if (buffer && !buffer.state().dirty) {
      const head = String(source.head || '');
      const knownHead = String(buffer.handle?.revision?.value || '');
      if (head && knownHead !== head || JSON.stringify(buffer.envelope) !== JSON.stringify(authoritative)) {
        buffer.resolveExternal(authoritative, { kind:'copalHead', value:head || knownHead }, { force:false });
      }
    }
    const envelope = buffer?.state().dirty ? buffer.envelope : draft?.envelope;
    if (!envelope) return source;
    return {
      ...source,
      text:envelope.text ?? source.text,
      ...(envelope.properties === undefined ? {} : { properties:cloneEnvelope(envelope.properties) }),
      ...(envelope.relations === undefined ? {} : { relations:cloneEnvelope(envelope.relations) }),
      ...(envelope.extensions === undefined ? {} : { extensions:cloneEnvelope(envelope.extensions) }),
    };
  }

  function commandButton(label, run, attrs = {}) {
    return h('button', { type:'button', class:'copal-btn', text:label, ...attrs, onclick:run });
  }

  function showChooser({ title = 'Quick switcher', docs = documents(), choose = null, allowCreate = true } = {}) {
    const dialog = h('dialog', { class:'copal-dialog copal-quick-switcher' }, h('h2', { text:title }));
    const search = h('input', { type:'search', placeholder:'Type a note name…', 'aria-label':title, autocomplete:'off' });
    const list = h('div', { class:'copal-switcher-results', role:'listbox' });
    let buttons = [];
    let active = 0;
    const select = (index) => {
      active = Math.max(0, Math.min(buttons.length - 1, index));
      buttons.forEach((button, position) => button.classList.toggle('active', position === active));
      buttons[active]?.scrollIntoView({ block:'nearest' });
    };
    const draw = () => {
      const query = search.value.trim();
      const recent = ensureWorkspace().recent;
      const ranked = docs
        .map((doc) => ({ doc, score:fuzzyScore(doc.name, query), recent:recent.indexOf(doc.id) }))
        .filter((item) => item.score >= 0)
        .sort((a, b) => b.score - a.score || (a.recent < 0 ? 999 : a.recent) - (b.recent < 0 ? 999 : b.recent) || a.doc.name.localeCompare(b.doc.name))
        .slice(0, 80);
      list.replaceChildren();
      buttons = ranked.map(({ doc }) => {
        const button = h('button', { class:'copal-doc-row', role:'option', type:'button', onclick:(event) => {
          if (choose) choose(doc, event);
          else open(doc.id, { intent:navigationIntentFromEvent(event) });
          dialog.close();
        } },
          h('span', { text:doc.name }), h('small', { text:noteViewType(doc) }));
        list.append(button);
        return button;
      });
      if (!buttons.length && query && allowCreate) {
        const name = query;
        const create = h('button', { class:'copal-doc-row', type:'button', text:`Create “${name}”`, onclick:() => {
          dialog.close(); createNew(name);
        } });
        list.append(create); buttons = [create];
      }
      select(0);
    };
    search.addEventListener('input', draw);
    search.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') { event.preventDefault(); select(active + 1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); select(active - 1); }
      else if (event.key === 'Enter') {
        event.preventDefault();
        buttons[active]?.dispatchEvent(new MouseEvent('click', { bubbles:true, ctrlKey:event.ctrlKey, metaKey:event.metaKey, shiftKey:event.shiftKey, altKey:event.altKey }));
      }
    });
    dialog.append(search, list, h('footer', { class:'copal-dialog-hint', text:'Enter open · Ctrl new tab · Shift split right · Alt split below · Esc close' }));
    wireDialog(dialog); document.body.append(dialog);
    draw(); dialog.showModal(); search.focus();
  }

  async function createDatabaseNote(name, content = '', properties = {}) {
    const relations = databaseRelations(content, state.docs);
    const result = await api('/documents', { method:'POST', body:JSON.stringify({ name, kind:'note', content, properties, relations }) });
    await loadDocuments(false);
    open(result.doc.id, { intent:'newTab' });
    return result.doc;
  }

  function templateDocuments() {
    const folder = templatePath(getSettings().templateFolder);
    const configuredFolder = getSettings().templateFolderRef;
    return documents().filter((doc) => {
      if (doc.kind !== 'note' && !(doc.sourceKind === 'host' && /\.(?:md|markdown)$/i.test(doc.name || ''))) return false;
      const name = templatePath(doc.name);
      if (doc.properties?.type === 'template') return !folder || name === folder || name.startsWith(`${folder}/`);
      // Host Markdown has no separate template table. A configured folder is
      // the explicit opt-in that makes its ordinary files discoverable.
      return doc.sourceKind === 'host' && !!folder && !!configuredFolder && (name === folder || name.startsWith(`${folder}/`));
    });
  }

  function chooseTemplateCollisions(collisions) {
    return new Promise((resolve) => {
      const dialog = h('dialog', { class:'copal-dialog copal-template-collisions' },
        h('h2', { text:'Template properties need a choice' }),
        h('p', { text:'These destination values already exist. Choose which values to keep.' }),
        h('ul', {}, collisions.map((item) => h('li', {}, h('strong', { text:item.key }), h('span', { text:` · destination: ${JSON.stringify(item.destination)} · template: ${JSON.stringify(item.template)}` })))),
      );
      const finish = (choice) => { dialog.close(); dialog.remove(); resolve(choice); };
      dialog.append(h('footer', { class:'copal-dialog-actions' },
        commandButton('Keep destination', () => finish('destination')),
        commandButton('Use template values', () => finish('template')),
        commandButton('Cancel', () => finish('cancel')),
      ));
      wireDialog(dialog); document.body.append(dialog); dialog.addEventListener('cancel', () => { dialog.remove(); resolve('cancel'); }, { once:true }); dialog.showModal();
    });
  }

  function captureTemplateTarget() {
    const workspace = ensureWorkspace();
    const leaf = activeLeaf(workspace);
    const doc = activeDoc(workspace);
    const cache = leaf ? context()?.noteLeafViews?.get(leaf.id) : null;
    if (!doc || doc.virtual || doc.readOnly || !cache?.editor) return null;
    return {
      docId:doc.id,
      leafId:leaf.id,
      editor:cache.editor,
      revision:doc.head || doc.resource?.revision?.value || '',
      selection:cache.editor.getSelection?.() || null,
      text:cache.editor.getValue?.() ?? sourceValue(doc),
      scope:currentScope(),
      sourceKind:doc.sourceKind,
      hostParentResourceRef:doc.hostParentResourceRef,
    };
  }

  function insertTemplate() {
    const templates = templateDocuments();
    const target = captureTemplateTarget();
    if (!target) {
      context()?.window?.setStatus('Open an editable document before inserting a template.', true);
      return;
    }
    if (!templates.length) {
      context()?.window?.setStatus('No templates yet. Set a note’s type property to template.', true);
      return;
    }
    showChooser({ title:'Insert template', docs:templates, allowCreate:false, choose:async (template) => {
      const current = context();
      const active = current && current.noteLeafViews?.get(target.leafId);
      const currentSelection = active?.editor?.getSelection?.() || null;
      const selectionUnchanged = !target.selection || (currentSelection?.mainIndex === target.selection.mainIndex
        && JSON.stringify(currentSelection?.ranges) === JSON.stringify(target.selection.ranges));
      const textUnchanged = target.text === (active?.editor?.getValue?.() ?? '');
      const revisionUnchanged = target.revision === (activeDoc()?.head || activeDoc()?.resource?.revision?.value || '');
      if (!active || active.editor !== target.editor || !target.editor.view?.dom?.isConnected || activeDoc()?.id !== target.docId
        || !selectionUnchanged || !textUnchanged || !revisionUnchanged || target.scope !== currentScope()) {
        current?.window?.setStatus('The captured template destination is stale. Reopen the command and try again.', true);
        return;
      }
      const now = new Date();
      // S04 owns token expansion.  Keep the local compatibility export above
      // for older callers, but use the published model for this mutation.
      const expanded = expandTemplateModel(template.text, { title:displayName(activeDoc()), now, timeZone:Intl.DateTimeFormat().resolvedOptions().timeZone });
      let merged = mergeTemplateProperties(activeDoc()?.properties, template.properties);
      if (merged.collisions.length) {
        const collisionChoice = await chooseTemplateCollisions(merged.collisions);
        if (collisionChoice === 'cancel') return;
        if (collisionChoice === 'template') {
          const properties = { ...merged.properties };
          for (const item of merged.collisions) properties[item.key] = item.template;
          merged = { properties, collisions:[] };
        }
      }
      // CodeMirror's transaction replaces every captured range in one undo
      // step and leaves all other selections mapped through the edit.
      activeDoc().properties = merged.properties;
      const ranges = active.editor.getSelections?.() || [{ from:active.editor.getSelection?.()?.anchor ?? 0, to:active.editor.getSelection?.()?.head ?? 0 }];
      // Descriptor creation validates the captured multicursor transaction;
      // the editor remains responsible for applying one atomic history item.
      try { createInsertionDescriptors(expanded.text, ranges); } catch (error) {
        current.window.setStatus(error?.message || 'Template selections are stale.', true); return;
      }
      active.editor.insertText(rebaseTemplateLinks(expanded.text, template.name, activeDoc().name));
      current.window.setStatus(expanded.diagnostics.length ? expanded.diagnostics.join(' ') : `Inserted ${displayName(template)}.`);
    } });
  }

  function createTemplateFromCurrent() {
    const target = captureTemplateTarget();
    if (!target) {
      context()?.window?.setStatus('Open an editable document before creating a template.', true);
      return;
    }
    const selection = target.editor.getSelectedText?.() || '';
    const source = selection || target.editor.getValue?.() || '';
    showForm('Create template from Editor', [['name', 'Template name', `${displayName(activeDoc())} template`]], async ({ name }) => {
      const properties = { type:'template', sourceDocumentId:target.docId };
      const requested = templatePath(String(name || 'Untitled template'));
      if (target.sourceKind === 'host') {
        const configured = getSettings().templateFolderRef;
        let parentRef = String(configured?.resourceRef || '').trim();
        if (configured) {
          const checked = normalizeTemplateFolderSelection(configured, { purpose:'create' });
          parentRef = checked.resourceRef;
        }
        if (!parentRef) {
          context()?.window?.setStatus('Choose an authorized writable template folder first.', true);
          return;
        }
        const current = context();
        const currentScopeAtCreate = currentScope();
        const targetStillCurrent = () => currentScopeAtCreate === currentScope()
          && target.scope === currentScope() && context() === current
          && context()?.noteLeafViews?.get(target.leafId)?.editor === target.editor
          && target.editor?.view?.dom?.isConnected;
        const verified = await filesFacadeClient.stat(parentRef);
        if (!targetStillCurrent()) {
          context()?.window?.setStatus('The captured template destination is stale. Reopen the command and try again.', true);
          return;
        }
        const authorized = normalizeAuthorizedResource(verified?.resource || verified, { purpose:'template-folder', ...pickerScope() });
        if (authorized.kind !== 'folder' || authorized.capabilities.children !== true
          || !(authorized.capabilities.write === true || authorized.capabilities.create === true || authorized.capabilities.edit === true)) {
          context()?.window?.setStatus('The configured template folder is no longer writable.', true);
          return;
        }
        parentRef = authorized.ref;
        const filename = requested.split('/').at(-1) || 'Untitled template.md';
        const markdownName = /\.(?:md|markdown)$/i.test(filename) ? filename : `${filename}.md`;
        const created = await filesFacadeClient.createResource(parentRef, {
          name:markdownName,
          text:source,
          actionId:`template-create-${target.docId}-${Date.now()}`,
        });
        if (!targetStillCurrent()) {
          context()?.window?.setStatus('The captured template destination became stale; verify the folder before retrying.', true);
          return;
        }
        const opened = await filesFacadeClient.openResource(created?.resource?.ref);
        if (!targetStillCurrent()) {
          context()?.window?.setStatus('The captured template destination became stale; the created resource was not opened.', true);
          return;
        }
        if (opened?.payload?.resource) openResource(opened.payload.resource, opened.payload);
        context()?.window?.setStatus(`Created ${markdownName} in the Host template folder.`);
        return;
      }
      await createDatabaseNote(folder && !requested.startsWith(`${folder}/`) ? `${folder}/${requested}` : requested, source, properties);
    });
  }

  function createNew(initial = 'Untitled') {
    showForm('New Copal note', [['name', 'Name', initial], ['content', 'Starting text', '', 'textarea']], async ({ name, content }) => {
      await createDatabaseNote(name, content);
    });
  }

  function localDate(now = new Date()) {
    const part = (value) => String(value).padStart(2, '0');
    return `${now.getFullYear()}-${part(now.getMonth() + 1)}-${part(now.getDate())}`;
  }

  async function openDailyNote() {
    const now = new Date();
    const date = localDate(now);
    const name = `Daily/${date}`;
    const existing = documents().find((doc) => doc.kind === 'note' && doc.name === name);
    if (existing) { open(existing.id); return; }
    const configured = getSettings().dailyTemplateId;
    const template = templateDocuments().find((doc) => configured && doc.id === configured)
      || templateDocuments().find((doc) => doc.properties?.daily === true || doc.properties?.daily === 'true');
    const expanded = template ? expandTemplate(template.text, { title:name, now, timeZone:Intl.DateTimeFormat().resolvedOptions().timeZone }) : { text:'', diagnostics:[] };
    await createDatabaseNote(name, expanded.text, { type:'daily', date });
    if (expanded.diagnostics.length) context()?.window?.setStatus(expanded.diagnostics.join(' '), true);
  }

  function createFromTemplate() {
    const templates = templateDocuments();
    if (!templates.length) {
      context().window.setStatus('No templates yet. Set a note’s type property to template.');
      return;
    }
    showChooser({ title:'New from template', docs:templates, allowCreate:false, choose:(template) => {
      const initial = `${displayName(template)} copy`;
      showForm('New from template', [['name', 'Name', initial]], async ({ name }) => {
        const properties = copyTemplateProperties(template.properties);
        const expanded = expandTemplate(template.text, { title:name, now:new Date(), timeZone:Intl.DateTimeFormat().resolvedOptions().timeZone });
        await createDatabaseNote(name, expanded.text, properties);
        if (expanded.diagnostics.length) context()?.window?.setStatus(expanded.diagnostics.join(' '), true);
      });
    } });
  }

  function toggleBookmark(docId) {
    const workspace = ensureWorkspace();
    workspace.bookmarks ||= [];
    workspace.bookmarks = workspace.bookmarks.includes(docId)
      ? workspace.bookmarks.filter((id) => id !== docId)
      : [docId, ...workspace.bookmarks];
    persist(true); render();
  }

  function renameWithForm(doc) {
    showForm(`Rename ${doc.name}`, [['name', 'Path', doc.name]], async ({ name }) => {
      await renameNote(doc, name);
      render();
    });
  }

  function activeGroup(workspace = ensureWorkspace()) {
    return workspace ? groupForLeaf(workspace, workspace.activeLeafId) || workspaceGroups(workspace)[0] : null;
  }

  function syncSelectionToModel(workspace) {
    const current = context();
    if (!current) return;
    current.selected = findWorkspaceLeaf(workspace)?.docId || null;
    state.selected = current.selected;
    persistActiveContext();
  }

  function splitActive(orientation) {
    const workspace = ensureWorkspace();
    const group = activeGroup(workspace);
    if (!workspace || !group) return;
    showChooser({
      title:orientation === 'vertical' ? 'Split below' : 'Split right',
      choose:(doc) => { splitWorkspaceGroup(workspace, group.id, doc, orientation); persist(true); render(); },
      allowCreate:false,
    });
  }

  function setMode(mode) {
    const workspace = ensureWorkspace();
    if (setWorkspaceLeafMode(workspace, workspace.activeLeafId, mode)) { persist(true); render(); }
  }

  function getSettings() {
    const workspace = ensureWorkspace();
    return normalizeNotesSettings(workspace?.settings);
  }

  function updateSettings(patch = {}) {
    const workspace = ensureWorkspace();
    if (!workspace) return normalizeNotesSettings(patch);
    workspace.settings = normalizeNotesSettings({ ...workspace.settings, ...patch });
    persist(true);
    if (context()?.window?.visible) render();
    return { ...workspace.settings };
  }

  function setPreviewLayout(layout) {
    updateSettings({ previewLayout:layout });
  }

  function showSettings() {
    if (window.settingsModule?.open) { window.settingsModule.open('appearance'); return; }
    throw new Error('Appearance settings are still loading');
    /*
    const workspace = ensureWorkspace();
    const dialog = h('dialog', { class:'copal-dialog copal-notes-settings' }, h('h2', { text:'Editor settings' }));
    const layout = h('select', { 'aria-label':'Preview layout' },
      h('option', { value:'inline', text:'Inline Live Preview (default)' }),
      h('option', { value:'side-by-side', text:'Side-by-side source and preview' }));
    layout.value = workspace.settings.previewLayout;
    const lineNumbers = h('input', { type:'checkbox', 'aria-label':'Show line numbers' }); lineNumbers.checked = workspace.settings.lineNumbers;
    const readable = h('input', { type:'checkbox', 'aria-label':'Readable line width' }); readable.checked = workspace.settings.readableLineWidth;
    const ribbon = h('input', { type:'checkbox', 'aria-label':'Show Editor ribbon' }); ribbon.checked = workspace.settings.ribbon;
    const hideCompleted = h('input', { type:'checkbox', 'aria-label':'Hide completed tasks' }); hideCompleted.checked = workspace.settings.completedVisibility === 'hide';

    // Sidebar panels section
    const movablePanels = Object.entries(NOTES_PANELS).filter(([, def]) => def.allowedSides.length > 1);
    const panelControls = h('div', { class:'copal-settings-panels' });
    const panelState = {};
    for (const [id, def] of movablePanels) {
      const current = workspace.panels?.[id] || { side:def.defaultSide, hidden:false };
      const sideSelect = h('select', { 'aria-label':`${def.label} side` },
        h('option', { value:'left', text:'Left' }),
        h('option', { value:'right', text:'Right' }));
      sideSelect.value = current.side;
      const hiddenCheck = h('input', { type:'checkbox', 'aria-label':`Hide ${def.label}` }); hiddenCheck.checked = current.hidden === true;
      const orderUp = commandButton('↑', () => {
        const entries = Object.entries(panelState).filter(([, s]) => s.side === sideSelect.value && !s.hidden);
        const idx = entries.findIndex(([eid]) => eid === id);
        if (idx > 0) { const prev = entries[idx - 1][0]; panelState[prev].order = panelState[id].order; panelState[id].order = panelState[id].order - 1; rebuildPanelOrder(); }
      }, { title:'Move up', 'aria-label':`Move ${def.label} up` });
      const orderDown = commandButton('↓', () => {
        const entries = Object.entries(panelState).filter(([, s]) => s.side === sideSelect.value && !s.hidden);
        const idx = entries.findIndex(([eid]) => eid === id);
        if (idx >= 0 && idx < entries.length - 1) { const next = entries[idx + 1][0]; panelState[next].order = panelState[id].order; panelState[id].order = panelState[id].order + 1; rebuildPanelOrder(); }
      }, { title:'Move down', 'aria-label':`Move ${def.label} down` });
      panelState[id] = { side:current.side, order:current.order ?? def.defaultOrder, hidden:current.hidden };
      const row = h('div', { class:'copal-settings-panel-row' },
        h('span', { class:'copal-settings-panel-label', text:def.label }),
        sideSelect, hiddenCheck, h('span', { text:'Hidden' }), orderUp, orderDown);
      panelControls.append(row);
    }
    const rebuildPanelOrder = () => {
      for (const [id, def] of movablePanels) {
        const row = panelControls.querySelector(`[aria-label="${def.label} side"]`)?.closest('.copal-settings-panel-row');
        if (!row) continue;
        const sideSelect = row.querySelector('[aria-label$=" side"]');
        const hiddenCheck = row.querySelector('[aria-label^="Hide"]');
        if (sideSelect) { panelState[id].side = sideSelect.value; sideSelect.value = panelState[id].side; }
        if (hiddenCheck) { panelState[id].hidden = hiddenCheck.checked; hiddenCheck.checked = panelState[id].hidden; }
      }
    };

    dialog.append(
      h('label', {}, h('span', { text:'Preview layout' }), layout),
      h('label', { class:'copal-check' }, lineNumbers, h('span', { text:'Show line numbers' })),
      h('label', { class:'copal-check' }, readable, h('span', { text:'Use readable line width' })),
      h('label', { class:'copal-check' }, ribbon, h('span', { text:'Show optional Editor ribbon' })),
      h('label', { class:'copal-check' }, hideCompleted, h('span', { text:'Hide completed tasks in reading mode' })),
      h('p', { class:'copal-dialog-hint', text:'Document mode and preview layout are independent. Side-by-side is preserved but never the clean-profile default.' }),
      h('h3', { text:'Sidebar panels' }),
      h('p', { class:'copal-dialog-hint', text:'Choose which side each panel lives on and whether it is visible. Files and Search are always on the left.' }),
      panelControls,
      h('div', { class:'copal-dialog-actions' }, commandButton('Cancel', () => dialog.close()), commandButton('Save', () => {
        updateSettings({
          previewLayout:layout.value,
          lineNumbers:lineNumbers.checked,
          readableLineWidth:readable.checked,
          ribbon:ribbon.checked,
          completedVisibility:hideCompleted.checked ? 'hide' : 'show',
        });
        // Read current panel state from DOM before applying
        for (const [id, def] of movablePanels) {
          const row = panelControls.querySelector(`[aria-label="${def.label} side"]`)?.closest('.copal-settings-panel-row');
          if (!row) continue;
          const sideSel = row.querySelector('[aria-label$=" side"]');
          const hiddenChk = row.querySelector('[aria-label^="Hide"]');
          if (sideSel) panelState[id].side = sideSel.value;
          if (hiddenChk) panelState[id].hidden = hiddenChk.checked;
        }
        // Apply panel placements
        for (const [id] of movablePanels) {
          setWorkspacePanelPlacement(workspace, id, panelState[id]);
        }
        // Ensure active tab is still valid on each side
        const leftPanels = workspacePanelsForSide(workspace, 'left');
        if (leftPanels.length && !leftPanels.includes(workspace.left.tab)) workspace.left.tab = leftPanels[0];
        const rightPanels = workspacePanelsForSide(workspace, 'right');
        if (rightPanels.length && !rightPanels.includes(workspace.right.tab)) workspace.right.tab = rightPanels[0];
        persist(true);
        dialog.close();
        render();
      }, { class:'copal-btn primary' })),
    );
    wireDialog(dialog); document.body.append(dialog); dialog.showModal(); layout.focus();
    */
  }

  function showSyntaxGallery() {
    const dialog = h('dialog', { class:'copal-dialog copal-syntax-gallery' }, h('h2', { text:'Syntax Gallery' }));
    const search = h('input', { type:'search', placeholder:'Filter syntax…', 'aria-label':'Filter syntax examples', autocomplete:'off' });
    const list = h('div', { class:'copal-gallery-list' });
    const examples = [
      { category:'Text', title:'Headings', source:'# Heading 1\n## Heading 2\n### Heading 3' },
      { category:'Text', title:'Emphasis', source:'**bold** and *italic* and ~~strikethrough~~ and ==highlight==' },
      { category:'Text', title:'Inline code', source:'Use `code` inline' },
      { category:'Text', title:'Links', source:'[Link text](https://example.com)\n[[Other Note]]' },
      { category:'Text', title:'Blockquote', source:'> A blockquote\n> with multiple lines' },
      { category:'Text', title:'Horizontal rule', source:'---' },
      { category:'Tasks', title:'Task list', source:'- [ ] Incomplete task\n- [x] Completed task\n- [ ] Another task' },
      { category:'Tables', title:'Table', source:'| Name | Status | Value |\n| :--- | :---: | ---: |\n| Alpha | open | 42 |\n| Beta | done | 7 |' },
      { category:'Math', title:'Inline math', source:'The equation $a^2 + b^2 = c^2$ is Pythagoras.' },
      { category:'Math', title:'Block math', source:'$$\nE = mc^2\n$$' },
      { category:'Structure', title:'Callout', source:'> [!info] Info callout\n> This is an informational callout.' },
      { category:'Structure', title:'Footnote', source:'Text with a footnote[^1].\n\n[^1]: Footnote content here.' },
      { category:'Structure', title:'Frontmatter', source:'---\ntitle: My Note\ntags: [reference, draft]\ndate: 2026-07-19\n---' },
      { category:'Structure', title:'Transclusion', source:'![[Other Note]]' },
      { category:'Plugins', title:'Dataview block', source:'```dataview\nLIST FROM #project\nWHERE status != "done"\n```' },
      { category:'Plugins', title:'Tasks query', source:'```tasks\nnot done\ntag includes #tasks\n```' },
      { category:'Plugins', title:'Templater', source:'<% tp.date.now("YYYY-MM-DD") %>' },
    ];
    const doc = activeDoc(ensureWorkspace());
    const insertAtCursor = (source) => {
      if (!doc) return;
      const leaf = activeLeaf();
      if (!leaf) return;
      const cache = context().noteLeafViews.get(leaf.id);
      if (!cache?.editor) return;
      // CodeMirror replaces every active range in one history transaction;
      // this keeps syntax-gallery insertion useful with multiple cursors.
      if (cache.editor.insertText) cache.editor.insertText(source);
      else {
        const sel = cache.editor.getSelection();
        const value = sourceValue(doc);
        const pos = sel?.anchor ?? value.length;
        applyDocumentSource(doc, `${value.slice(0, pos)}${source}${value.slice(pos)}`, { anchor:pos + source.length, head:pos + source.length });
      }
    };
    const copyToClipboard = async (source, button) => {
      try {
        await navigator.clipboard.writeText(source);
        button.textContent = 'Copied!';
        setTimeout(() => { button.textContent = 'Copy'; }, 1500);
      } catch {
        button.textContent = 'Denied';
        setTimeout(() => { button.textContent = 'Copy'; }, 1500);
      }
    };
    const draw = () => {
      const query = search.value.trim().toLowerCase();
      list.replaceChildren();
      for (const example of examples) {
        if (query && !example.title.toLowerCase().includes(query) && !example.source.toLowerCase().includes(query) && !example.category.toLowerCase().includes(query)) continue;
        const pre = h('pre', { class:'copal-gallery-source', tabindex:'0' }, h('code', { text:example.source }));
        const copyBtn = h('button', { type:'button', class:'copal-btn', text:'Copy', onclick:() => copyToClipboard(example.source, copyBtn) });
        const insertBtn = h('button', { type:'button', class:'copal-btn copal-btn primary', text:'Insert at cursor', disabled:!doc, onclick:() => { insertAtCursor(example.source); dialog.close(); } });
        const card = h('div', { class:'copal-gallery-card' },
          h('div', { class:'copal-gallery-card-header' },
            h('span', { class:'copal-gallery-badge', text:example.category }),
            h('strong', { text:example.title })),
          pre,
          h('div', { class:'copal-gallery-actions' }, copyBtn, insertBtn));
        list.append(card);
      }
      if (!list.children.length) list.append(h('p', { class:'copal-empty-inline', text:'No matching examples.' }));
    };
    search.addEventListener('input', draw);
    dialog.append(search, list, h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Close', onclick:() => dialog.close() })));
    wireDialog(dialog); document.body.append(dialog); draw(); dialog.showModal(); search.focus();
  }

  function showCommands() {
    const workspace = ensureWorkspace();
    const doc = activeDoc(workspace);
    const actions = [
      ['New note', 'Ctrl+N', () => createNew()],
      ['Open today’s note', '', openDailyNote],
      ['New from template', '', createFromTemplate],
      ['Insert template', '', insertTemplate],
      ['Create template from current document', '', createTemplateFromCurrent],
      ['Quick switcher', 'Ctrl+O', () => showChooser()],
      ['Search documents', 'Ctrl+Shift+F', () => showSearch()],
      ['Open Timeline', '', () => open(TIMELINE_DOCUMENT.id)],
      ['Editor settings', '', showSettings],
      ...(doc?.virtual || doc?.readOnly ? [] : [[doc?.kind === 'note' ? 'Editing mode' : 'Live Preview mode', '', () => setMode('live')]]),
      ...(doc?.virtual || doc?.readOnly || doc?.kind === 'note' ? [] : [['Source mode', '', () => setMode('source')]]),
      ...(doc?.virtual ? [] : [['Reading mode', '', () => setMode('reading')]]),
      ['Inline preview layout', '', () => setPreviewLayout('inline')],
      ['Side-by-side preview layout', '', () => setPreviewLayout('side-by-side')],
      ['Split right', '', () => splitActive('horizontal')],
      ['Split below', '', () => splitActive('vertical')],
      ['Toggle Editor sidebar', '', () => { workspace.left.open = !workspace.left.open; persist(true); render(); }],
      ['Toggle linked sidebar', '', () => { workspace.right.open = !workspace.right.open; persist(true); render(); }],
      ...(doc?.virtual ? [] : [['Toggle bookmark', '', () => doc && toggleBookmark(doc.id)]]),
      ['Reopen closed note', '', reopenClosed],
      ...(doc?.virtual ? [] : [['History', '', () => doc && showHistory(doc)]]),
      ['Open Trash', '', () => showTrash()],
      ...(doc && !doc.virtual && !doc.readOnly ? [['Move current note to Trash', '', () => deleteDocument(doc)]] : []),
      ['Import Markdown or Obsidian backup', '', importVault],
      ['Syntax gallery', '', () => { const gallery = state.docs.find((d) => d.name.includes('Syntax Gallery') || d.name.includes('syntax-gallery')); if (gallery) open(gallery.id); else showSyntaxGallery(); }],
      ['Export Markdown backup', '', () => { window.location.href = `/api/copal/export/obsidian?workspace=${encodeURIComponent(state.workspace)}`; }],
    ];
    const recentCommands = context().noteRecentCommands ||= [];
    const dialog = h('dialog', { class:'copal-dialog copal-command-palette' }, h('h2', { text:'Editor commands' }));
    const search = h('input', { type:'search', placeholder:'Run a command…', 'aria-label':'Command palette', autocomplete:'off' });
    const list = h('div', { class:'copal-switcher-results', role:'listbox' });
    let buttons = []; let active = 0;
    const select = (index) => { active = Math.max(0, Math.min(buttons.length - 1, index)); buttons.forEach((button, position) => button.classList.toggle('active', position === active)); buttons[active]?.scrollIntoView({ block:'nearest' }); };
    const draw = () => {
      const query = search.value.trim();
      list.replaceChildren();
      buttons = actions.map(([label, shortcut, run]) => ({ label, shortcut, run, score:fuzzyScore(`${label} ${shortcut}`, query), recent:recentCommands.indexOf(label) }))
        .filter((item) => item.score >= 0).sort((a, b) => b.score - a.score || (a.recent < 0 ? 999 : a.recent) - (b.recent < 0 ? 999 : b.recent))
        .map((item) => {
          const button = h('button', { class:'copal-command-row', role:'option', type:'button', onclick:() => {
            context().noteRecentCommands = [item.label, ...recentCommands.filter((label) => label !== item.label)].slice(0, 12);
            dialog.close(); item.run();
          } }, h('span', { text:item.label }), h('kbd', { text:item.shortcut }));
          list.append(button); return button;
        });
      select(0);
    };
    search.addEventListener('input', draw);
    search.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') { event.preventDefault(); select(active + 1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); select(active - 1); }
      else if (event.key === 'Enter') { event.preventDefault(); buttons[active]?.click(); }
    });
    dialog.append(search, list); wireDialog(dialog); document.body.append(dialog);
    draw(); dialog.showModal(); search.focus();
  }

  function showSearch() {
    const workspace = ensureWorkspace();
    workspace.left.open = true;
    workspace.left.tab = 'search';
    persist(true); render();
    requestAnimationFrame(() => context()?.window.root.querySelector('.copal-note-search-input')?.focus());
  }

  function reopenClosed() {
    const workspace = ensureWorkspace();
    while (workspace.closed.length) {
      const id = workspace.closed.shift();
      const doc = workspaceDocuments().find((item) => item.id === id);
      if (!doc) continue;
      const leaf = workspaceLeaves(workspace).find((item) => item.docId === id);
      if (leaf) {
        const group = groupForLeaf(workspace, leaf.id);
        if (group) { setActive(group.id, leaf.id); return; }
      }
      open(id, { intent:'newTab' });
      return;
    }
    persist(true); render();
  }

  function focusEmptyWorkspaceIfIdle() {
    const workspace = ensureWorkspace();
    if (!workspace || workspaceLeaves(workspace).length) return;
    requestAnimationFrame(() => context()?.window.root.querySelector('.copal-notes-empty-workspace')?.focus({ preventScroll:true }));
  }

  function emptyWorkspace(workspace) {
    return h('section', { class:'copal-empty copal-notes-empty-workspace', tabindex:'-1', role:'region', 'aria-label':'No open documents' },
      h('h2', { text:'No open documents' }),
      h('p', { text:'The workspace is empty. Nothing was reopened for you — pick what to open next.' }),
      h('div', { class:'copal-empty-workspace-actions' },
        commandButton('New note', () => createNew(), { class:'copal-btn primary' }),
        commandButton('Quick switcher', () => showChooser()),
        commandButton('Reopen closed', reopenClosed, workspace.closed.length ? {} : { disabled:true }),
        commandButton('Open Timeline', () => open(TIMELINE_DOCUMENT.id)),
        commandButton('Import backup', importVault)));
  }

  function bindKeys(workspace, doc) {
    const current = context();
    if (current.noteKeyHandler) current.window.root.removeEventListener('keydown', current.noteKeyHandler);
    current.noteKeyHandler = (event) => {
      if (event.isComposing || !(event.ctrlKey || event.metaKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === 'o') { event.preventDefault(); event.stopPropagation(); showChooser(); }
      else if (key === 'p') { event.preventDefault(); event.stopPropagation(); showCommands(); }
      else if (key === 'f' && event.shiftKey) { event.preventDefault(); event.stopPropagation(); showSearch(); }
      else if (key === 'n') { event.preventDefault(); event.stopPropagation(); createNew(); }
      else if (key === 's' && doc) { event.preventDefault(); event.stopPropagation(); void saveDraft(doc.id); }
    };
    current.window.root.addEventListener('keydown', current.noteKeyHandler);
  }

  function fileTree(docs, workspace) {
    const current = context();
    // Reuse the immutable tree between shell renders when the document set
    // and navigation state are unchanged.  Shell refreshes otherwise rebuild
    // thousands of rows even though every row's source and handlers remain
    // valid; rename, selection, expansion, and source revisions are in the
    // key so navigation never observes stale identity.
    const cacheKey = JSON.stringify([
      currentScope(current),
      docs.map((doc) => [doc.id, doc.name, doc.head, doc.ts]),
      workspace.left.sort, workspace.left.showDotFolders, workspace.left.expanded,
      workspace.left.selected, activeLeaf(workspace)?.docId || null,
    ]);
    if (current?.noteFileTreeCache?.key === cacheKey) return current.noteFileTreeCache.tree;
    const root = { folders:new Map(), docs:[] };
    for (const doc of docs) {
      const parts = doc.name.split('/'); let node = root;
      for (const folder of parts.slice(0, -1)) {
        if (!node.folders.has(folder)) node.folders.set(folder, { folders:new Map(), docs:[] });
        node = node.folders.get(folder);
      }
      node.docs.push(doc);
    }
    const expanded = new Set(workspace.left.expanded);
    const selectedIds = new Set(workspace.left.selected);
    const sorted = (values) => [...values].sort((a, b) => workspace.left.sort === 'modified'
      ? String(b.ts || '').localeCompare(String(a.ts || '')) || a.name.localeCompare(b.name)
      : a.name.localeCompare(b.name));
    const draw = (node, parent, path = []) => {
      for (const [name, folder] of [...node.folders].sort(([a], [b]) => a.localeCompare(b))) {
        const full = [...path, name].join('/'); const isOpen = expanded.has(full);
        const row = h('div', { class:'copal-folder-row', role:'treeitem', 'data-note-tree-key':`folder:${full}`, 'data-note-parent':path.join('/'), 'aria-expanded':String(isOpen), tabindex:'0' }, h('span', { class:'copal-tree-toggle', text:isOpen ? '▾' : '▸' }), h('span', { text:name }));
        const children = h('div', { class:'copal-tree-children', role:'group' }); children.hidden = !isOpen;
        const toggle = () => {
          if (isOpen) {
            expanded.delete(full);
            workspace.left.expanded = [...expanded];
            persist(true); render();
            // A focused descendant is removed from the DOM when its folder
            // closes. Restore keyboard focus to the disclosure row after the
            // render so collapse never leaves focus on a hidden/stale node.
            context()?.window.root.querySelector(`[data-note-tree-key="folder:${CSS.escape(full)}"]`)?.focus({ preventScroll:true });
          } else {
            expanded.add(full); workspace.left.expanded = [...expanded]; persist(true); render();
          }
        };
        row.addEventListener('click', toggle);
        row.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ' || (event.key === 'ArrowRight' && !isOpen) || (event.key === 'ArrowLeft' && isOpen)) { event.preventDefault(); toggle(); }
        });
        row.addEventListener('dragover', (event) => event.preventDefault());
        row.addEventListener('drop', async (event) => {
          event.preventDefault(); event.stopPropagation();
          const id = event.dataTransfer.getData('text/x-copal-document');
          const doc = state.docs.find((item) => item.id === id);
          if (doc) {
            try { await renameNote(doc, `${full}/${doc.name.split('/').pop()}`); }
            catch (error) { context().window.setStatus(error.message, true); }
          }
        });
        parent.append(row, children); draw(folder, children, [...path, name]);
      }
      for (const doc of sorted(node.docs)) {
        const selected = activeLeaf(workspace)?.docId === doc.id;
        const chosen = selectedIds.has(doc.id);
        const row = h('div', { class:`copal-file-entry${selected ? ' active' : ''}${chosen ? ' selected' : ''}` });
        const openButton = h('button', { class:'copal-file-row', role:'treeitem', 'data-copal-context-object':'file', 'data-document-id':doc.id, 'data-file-capabilities':'open', 'data-note-tree-key':`document:${doc.id}`, 'data-note-parent':path.join('/'), 'aria-selected':String(chosen || selected), draggable:doc.readOnly ? false : 'true', title:doc.name, onclick:(event) => {
          if (!doc.readOnly && (event.ctrlKey || event.metaKey)) {
            chosen ? selectedIds.delete(doc.id) : selectedIds.add(doc.id);
            workspace.left.selected = [...selectedIds]; persist(true); render(); return;
          }
          workspace.left.selected = []; open(doc.id);
        } },
          h('span', { class:'copal-file-kind', text:fileGlyph(doc) }), h('span', { text:displayName(doc) }));
        if (!doc.readOnly) openButton.addEventListener('dragstart', (event) => event.dataTransfer.setData('text/x-copal-document', doc.id));
        const menuItems = [
          ...(doc.readOnly ? [] : [commandButton('Rename or move', () => renameWithForm(doc))]),
          commandButton('Open in new tab', () => open(doc.id, { intent:'newTab' })),
          commandButton('Open right', () => open(doc.id, { intent:'splitRight' })),
          commandButton('Open below', () => open(doc.id, { intent:'splitBelow' })),
          commandButton('Reveal path', () => { revealInExplorer(doc, workspace); persist(true); render(); }),
          ...(doc.readOnly ? [] : [commandButton('Trash', () => deleteDocument(doc), { class:'copal-btn danger' })]),
        ];
        const menu = wirePopover(h('details', { class:'copal-file-menu' }, h('summary', { title:`Actions for ${doc.name}`, 'aria-label':`Actions for ${doc.name}`, text:'⋯' }),
          h('div', { class:'copal-popover-menu' }, menuItems)));
        row.append(openButton, menu); parent.append(row);
      }
    };
    const tree = h('div', { class:'copal-file-tree', 'data-note-panel':'files', role:'tree', 'aria-label':'Editor documents', tabindex:'-1' });
    tree.addEventListener('keydown', (event) => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      const items = [...tree.querySelectorAll('[role="treeitem"]')].filter((item) => item.offsetParent !== null);
      const current = items.indexOf(document.activeElement);
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : Math.max(0, Math.min(items.length - 1, current + (event.key === 'ArrowDown' ? 1 : -1)));
      if (items[index]) { event.preventDefault(); items[index].focus(); }
    });
    draw(root, tree);
    if (current) current.noteFileTreeCache = { key:cacheKey, tree };
    return tree;
  }

  function fileGlyph(doc) {
    return ({ note:'◆', markdown:'◇', canvas:'⌘', base:'▦', image:'▧', audio:'♪', video:'▶', pdf:'▤', asset:'·' })[noteViewType(doc)] || '◇';
  }

  function displayName(doc) {
    const name = String(doc?.name || '').split('/').pop();
    if (noteViewType(doc) === 'base') return name.replace(/\.base$/i, '');
    return ['note', 'markdown'].includes(noteViewType(doc)) ? name.replace(/\.md$/i, '') : name;
  }

  // Roving tabindex keyboard navigation for a tab strip.
  // Tabs is a container with role="tablist"; panelIds is the ordered list of
  // visible panel ids matching the buttons in DOM order; onSelect fires when
  // the user activates a tab (Enter/Space).
  function addRovingFocus(tabs, panelIds, onSelect) {
    const buttons = () => [...tabs.querySelectorAll('[role="tab"]')];
    tabs.addEventListener('keydown', (event) => {
      const btns = buttons();
      const current = btns.indexOf(document.activeElement);
      if (current < 0) return;
      let next = current;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (current + 1) % btns.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (current - 1 + btns.length) % btns.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = btns.length - 1;
      else return;
      event.preventDefault();
      btns.forEach((btn, i) => { btn.tabIndex = i === next ? 0 : -1; });
      btns[next].focus();
      const id = panelIds[next];
      if (id && typeof onSelect === 'function') onSelect(id);
    });
  }

  function leftSidebar(workspace, docs, shellState) {
    const aside = h('aside', { id:'copal-notes-left-sidebar', class:'copal-notes-explorer' });
    aside.style.setProperty('--copal-pane-width', `${workspace.left.width}px`);
    const closeMenu = (menu) => { if (menu?.open) menu.open = false; };
    const fileMenu = h('details', { class:'copal-editor-file-menu' });
    const summary = h('summary', { class:'copal-btn', 'aria-label':'Editor File menu', text:'File' });
    fileMenu.append(summary);
    const menuItems = [
      ['Open File', openFileFromPicker],
      ['Open Folder', openFolderFromPicker],
      ['New', () => createNew()],
      ['New from template', createFromTemplate],
      ['Insert template', insertTemplate],
      ['Choose template folder', configureTemplateFolder],
    ];
    const menu = h('div', { class:'copal-editor-file-menu-items', role:'menu', 'aria-label':'Editor File actions' });
    for (const [label, action] of menuItems) {
      menu.append(h('button', { type:'button', role:'menuitem', text:label, onclick:() => { closeMenu(fileMenu); void action(); } }));
    }
    fileMenu.append(menu);
    const sideHead = h('header', { class:'copal-shell-side-header left' },
      ...(shellState.narrow ? [] : [shellState.controls.left]), h('strong', { text:'Files' }), fileMenu,
      commandButton(workspace.left.showDotFolders ? '◉' : '○', () => { workspace.left.showDotFolders = !workspace.left.showDotFolders; persist(true); render(); }, { title:workspace.left.showDotFolders ? 'Hide hidden folders' : 'Show hidden folders', 'aria-label':workspace.left.showDotFolders ? 'Hide hidden folders' : 'Show hidden folders' }));
    const tabs = h('div', { class:'copal-side-tabs', role:'tablist', 'aria-label':'Editor navigation' });
    const panelIds = workspacePanelsForSide(workspace, 'left');
    for (const key of panelIds) {
      const def = NOTES_PANELS[key];
      if (!def) continue;
      tabs.append(h('button', { class:workspace.left.tab === key ? 'active' : '', role:'tab', 'aria-selected':String(workspace.left.tab === key), text:def.label, 'data-panel-id':key, onclick:() => { workspace.left.tab = key; persist(true); render(); } }));
    }
    addRovingFocus(tabs, panelIds, (id) => { workspace.left.tab = id; persist(true); render(); });
    const visibleDocs = explorerDocs();
    const body = h('div', { class:'copal-side-body', 'data-note-panel':workspace.left.tab === 'files' ? false : workspace.left.tab });
    if (workspace.left.tab === 'files') {
      const sort = h('select', { class:'copal-file-sort', 'aria-label':'Sort files' }, h('option', { value:'name', text:'Name' }), h('option', { value:'modified', text:'Modified' }));
      sort.value = workspace.left.sort;
      sort.addEventListener('change', () => { workspace.left.sort = sort.value === 'modified' ? 'modified' : 'name'; persist(true); render(); });
      const allFolders = [...new Set(visibleDocs.flatMap((doc) => {
        const parts = doc.name.split('/').slice(0, -1); return parts.map((_, index) => parts.slice(0, index + 1).join('/'));
      }))];
      const expand = () => { workspace.left.expanded = workspace.left.expanded.length === allFolders.length ? [] : allFolders; persist(true); render(); };
      body.append(h('header', {}, h('strong', { text:'Editor' }), sort,
        commandButton('↔', () => open(TIMELINE_DOCUMENT.id), { title:'Open Timeline', 'aria-label':'Open Timeline' }),
        commandButton(workspace.left.expanded.length === allFolders.length ? '−' : '+', expand, { title:'Expand or collapse all collections', 'aria-label':'Expand or collapse all collections' }),
        commandButton('＋', () => createNew(), { title:'New note', 'aria-label':'New note' }),
        commandButton('▱+', () => createNew('New collection/Untitled'), { title:'New collection with note', 'aria-label':'New collection with note' })));
      if (workspace.left.selected.length) {
        const chosen = workspace.left.selected.map((id) => state.docs.find((doc) => doc.id === id)).filter(Boolean);
        body.append(h('div', { class:'copal-file-selection', role:'status' }, h('span', { text:`${chosen.length} selected` }),
          commandButton('Clear', () => { workspace.left.selected = []; persist(true); render(); }),
          commandButton('Trash', async () => {
            if (deleteDocuments) await deleteDocuments(chosen);
            else for (const doc of chosen) await deleteDocument(doc);
            workspace.left.selected = []; persist(true);
          }, { class:'copal-btn danger' })));
      }
      if (workspace.left.resourceRoot) {
        const root = workspace.left.resourceRoot;
        const current = context();
        const resourceList = h('div', { class:'copal-resource-folder-list', role:'tree', 'aria-label':`Files in ${root.name}` });
        const folderSearch = h('input', { type:'search', class:'copal-resource-folder-search', placeholder:'Filter this authorized folder…', 'aria-label':'Filter authorized folder', value:workspace.left.resourceQuery || '' });
        const loadPage = (options = {}) => {
          if (!current.noteResourceRootReady || (options.append && current.noteResourceLoading)) return false;
          current.noteResourceLoading = true;
          const query = options.query ?? folderSearch.value.trim();
          void loadEditorResourceFolder(workspace, root, { cursor:options.cursor || null, query, append:!!options.append, commitHistory:false }).then(loaded => {
            if (!loaded && current === context()) context()?.window?.setStatus(current.noteResourceRootError || 'Folder listing failed.', true);
          });
          return true;
        };
        folderSearch.addEventListener('input', () => {
          workspace.left.resourceQuery = folderSearch.value;
          clearTimeout(current.noteResourceSearchTimer);
          current.noteResourceSearchTimer = setTimeout(() => {
            current.noteResourceSearchTimer = null;
            if (folderSearch.isConnected && current === context()) loadPage({ query:folderSearch.value.trim() });
          }, 120);
        });
        const navigation = resourceNavigation(current);
        const folderControls = h('div', { class:'copal-resource-folder-controls' },
          commandButton('Back', () => { void navigation?.back(); }, { disabled:!navigation?.canGoBack?.(), title:'Previous authorized folder', 'aria-label':'Previous authorized folder' }),
          commandButton('Forward', () => { void navigation?.forward(); }, { disabled:!navigation?.canGoForward?.(), title:'Next authorized folder', 'aria-label':'Next authorized folder' }),
          root.parentRef ? commandButton('Up', () => {
            const parent = navigation?.snapshot?.().entries?.map(entry => entry.resource).find(resource => resource?.ref === root.parentRef) || { ref:root.parentRef };
            void loadEditorResourceFolder(workspace, parent, { query:'', commitHistory:true });
          }, { title:'Open authorized parent folder', 'aria-label':'Open authorized parent folder' }) : null,
          commandButton('Reload', () => loadPage(), { title:'Reload authorized folder contents', 'aria-label':'Reload authorized folder contents' }),
          workspace.left.resourceCursor ? commandButton('Load more', () => loadPage({ cursor:workspace.left.resourceCursor, append:true }), { title:'Load more authorized entries', 'aria-label':'Load more authorized entries' }) : null,
        );
        if (!current.noteResourceRootReady) resourceList.append(h('p', { class:'copal-empty-inline', text:current.noteResourceRootError || 'Revalidating authorized folder…' }), commandButton('Retry', () => { current.noteResourceRootValidationKey = null; revalidateSavedResourceRoot(workspace); }, { title:'Retry folder authorization', 'aria-label':'Retry folder authorization' }));
        const rowViewport = h('div', { class:'copal-resource-folder-viewport', tabindex:'0', role:'group', 'aria-label':'Authorized folder entries' });
        const rowHost = h('div', { class:'copal-resource-folder-rows' });
        const drawRows = () => {
          const rows = workspace.left.resourceRows || [];
          const rowHeight = 34;
          const start = Math.max(0, Math.min(Math.max(0, rows.length - 240), Math.floor((rowViewport.scrollTop || 0) / rowHeight)));
          const visible = rows.slice(start, start + 240);
          rowHost.replaceChildren();
          if (start) rowHost.append(h('div', { class:'copal-resource-folder-spacer', style:`height:${start * rowHeight}px`, 'aria-hidden':'true' }));
          for (const row of visible) {
            const canUse = row.kind === 'folder' ? row.capabilities?.children === true : row.capabilities?.open === true && row.capabilities?.read === true;
            const status = canUse ? '' : row.capabilities?.read === true ? 'unavailable' : 'read-only';
            rowHost.append(h('button', { type:'button', class:`copal-doc-row${canUse ? '' : ' disabled'}`, role:'treeitem', disabled:!canUse, 'data-resource-row-index':start + visible.indexOf(row), onclick:() => {
              if (row.kind === 'folder') {
                void loadEditorResourceFolder(workspace, row, { query:'', commitHistory:true }).then(loaded => {
                  if (!loaded && current === context()) context()?.window?.setStatus(current.noteResourceRootError || 'Folder listing failed.', true);
                });
              } else void resourceOpener({ resourceRef:row.ref, name:row.name }).catch(error => context()?.window?.setStatus(error?.message || 'File could not be opened.', true));
            } }, h('strong', { text:row.name }), h('small', { text:`${row.provider} · ${row.kind}${status ? ` · ${status}` : ''}` })));
          }
          if (start + visible.length < rows.length) rowHost.append(h('div', { class:'copal-resource-folder-spacer', style:`height:${(rows.length - start - visible.length) * rowHeight}px`, 'aria-hidden':'true' }));
        };
        rowViewport.addEventListener('scroll', drawRows, { passive:true });
        rowViewport.addEventListener('keydown', event => {
          const focused = event.target?.closest?.('[data-resource-row-index]');
          if (!focused || !['PageDown', 'PageUp', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const currentIndex = Number(focused.dataset.resourceRowIndex);
          const last = Math.max(0, (workspace.left.resourceRows || []).length - 1);
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? last : Math.max(0, Math.min(last, currentIndex + (event.key === 'PageDown' ? 20 : -20)));
          rowViewport.scrollTop = next * 34;
          drawRows();
          rowHost.querySelector(`[data-resource-row-index="${next}"]`)?.focus();
        });
        rowViewport.append(rowHost);
        resourceList.append(h('div', { class:'copal-dialog-hint', text:`Authorized folder: ${root.name}` }), folderSearch, folderControls, rowViewport);
        drawRows();
        if (!workspace.left.resourceRows?.length) resourceList.append(h('p', { class:'copal-empty-inline', text:'Loading authorized folder contents…' }));
        body.append(resourceList);
      } else body.append(fileTree(visibleDocs, workspace));
    } else if (workspace.left.tab === 'search') {
      const input = h('input', { class:'copal-note-search-input', type:'search', placeholder:'Search documents and properties…', 'aria-label':'Search documents', value:context().noteSearch || '' });
      const results = h('div', { class:'copal-search-results' });
      const draw = () => {
        context().noteSearch = input.value;
        const query = input.value.trim().toLowerCase();
        // Search ALL documents, including those in hidden dot-folders.
        const allDocs = documents();
        const matches = query ? allDocs.map((doc) => {
          const source = String(doc.text || ''); const lower = source.toLowerCase(); const at = lower.indexOf(query);
          const metadata = `${JSON.stringify(doc.properties || {})} ${(doc.tags || []).join(' ')}`.toLowerCase();
          const nameScore = fuzzyScore(doc.name, query); const contentScore = at >= 0 ? 500 - Math.min(400, at) : metadata.includes(query) ? 450 : -1;
          const start = Math.max(0, at - 55); const snippet = at >= 0 ? source.slice(start, at + query.length + 85).replace(/\s+/g, ' ').trim() : '';
          return { doc, score:Math.max(nameScore, contentScore), snippet };
        }).filter((item) => item.score >= 0).sort((a, b) => b.score - a.score || a.doc.name.localeCompare(b.doc.name)).slice(0, 100) : [];
        results.replaceChildren(...matches.map(({ doc, snippet }) => {
          const hidden = isHiddenDoc(doc);
          const cls = 'copal-doc-row' + (hidden ? ' copal-doc-row--hidden' : '');
          return h('button', { class:cls, onclick:() => open(doc.id) },
            h('strong', { text:doc.name }),
            hidden ? h('small', { class:'copal-hidden-badge', text:'hidden' }) : null,
            snippet ? h('small', { text:snippet }) : null);
        }));
        if (query && !matches.length) results.append(h('p', { class:'copal-empty-inline', text:'No matches' }));
      };
      input.addEventListener('input', draw); body.append(input, results); draw();
    } else if (workspace.left.tab === 'tags') {
      appendNavigationPanel(body, 'tags', workspace);
    } else if (['properties', 'links', 'outline'].includes(workspace.left.tab)) {
      const doc = inspectorDoc(workspace);
      if (!doc) body.append(h('p', { class:'copal-empty-inline', text:'No active document.' }));
      else if (doc.virtual) body.append(h('p', { class:'copal-empty-inline', text:'Timeline is a canonical database view.' }));
      else if (workspace.left.tab === 'properties') body.append(propertiesPane(doc));
      else if (workspace.left.tab === 'links') body.append(linksPane(doc));
      else body.append(outlinePane(doc, workspace));
    } else {
      appendNavigationPanel(body, workspace.left.tab, workspace);
    }
    const handle = resizeHandle('left', workspace);
    aside.append(sideHead, tabs, body, handle);
    return aside;
  }

  function resizeHandle(side, workspace) {
    const minimum = side === 'left' ? 150 : 190; const maximum = side === 'left' ? 420 : 480;
    const handle = h('div', { class:`copal-sidebar-resize ${side}`, role:'separator', tabindex:'0', 'aria-label':`Resize ${side} Editor sidebar`, 'aria-orientation':'vertical', 'aria-valuemin':minimum, 'aria-valuemax':maximum, 'aria-valuenow':workspace[side].width });
    let activePointerId = null; let startX = 0; let startWidth = workspace[side].width; let frame = 0; let pending = null;
    let finishActive = () => {};
    handle._copalResizeCleanup = () => finishActive();
    const apply = (value) => {
      workspace[side].width = Math.max(minimum, Math.min(maximum, value));
      handle.setAttribute('aria-valuenow', String(Math.round(workspace[side].width)));
      const shell = context()?.window.root.querySelector('.copal-notes-workspace');
      const pane = shell?.querySelector(side === 'left' ? '.copal-notes-explorer' : '.copal-notes-right-sidebar');
      pane?.style.setProperty('--copal-pane-width', `${workspace[side].width}px`);
    };
    handle.addEventListener('keydown', (event) => {
      const delta = event.key === 'ArrowLeft' ? -12 : event.key === 'ArrowRight' ? 12 : 0;
      const next = event.key === 'Home' ? minimum : event.key === 'End' ? maximum : null;
      if (!delta && next == null) return;
      event.preventDefault(); apply(next == null ? workspace[side].width + (side === 'right' ? -delta : delta) : next); persist(true);
    });
    handle.addEventListener('pointerdown', (event) => {
      if (activePointerId != null || event.isPrimary === false || (event.button != null && event.button !== 0)) return;
      event.preventDefault(); activePointerId = event.pointerId; startX = event.clientX; startWidth = workspace[side].width;
      try { handle.setPointerCapture(event.pointerId); } catch (_) {}
      const move = (next) => {
        if (next.pointerId !== activePointerId) return;
        pending = startWidth + (side === 'right' ? startX - next.clientX : next.clientX - startX);
        if (!frame) frame = (globalThis.requestAnimationFrame || ((callback) => setTimeout(callback, 0)))(() => { frame = 0; if (pending != null) { apply(pending); pending = null; } });
      };
      const finish = (next) => {
        if (next?.pointerId != null && next.pointerId !== activePointerId) return;
        if (frame) { globalThis.cancelAnimationFrame?.(frame); frame = 0; }
        if (pending != null) { apply(pending); pending = null; }
        const pointerId = activePointerId; activePointerId = null;
        finishActive = () => {};
        handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', finish); handle.removeEventListener('pointercancel', finish); handle.removeEventListener('lostpointercapture', finish); window.removeEventListener('blur', finish);
        try { if (pointerId != null) handle.releasePointerCapture?.(pointerId); } catch (_) {}
        persist(true);
      };
      finishActive = finish;
      handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', finish); handle.addEventListener('pointercancel', finish); handle.addEventListener('lostpointercapture', finish); window.addEventListener('blur', finish, { once:true });
    });
    return handle;
  }

  function groupTabs(group, workspace, docs, shellState) {
    const bar = h('div', { class:'copal-note-tabs' });
    const ownsShellControls = shellState.controlGroupId === group.id;
    const leftSlot = h('div', { class:'copal-shell-tab-slot left', 'data-shell-slot':'left' });
    if (ownsShellControls && (shellState.narrow || !workspace.left.open)) leftSlot.append(shellState.controls.left);
    const tablist = h('div', { class:'copal-note-tab-scroll', role:'tablist', 'aria-label':'Open Editor tabs' });
    bar.append(leftSlot, tablist);
    for (const leaf of group.tabs) {
      const doc = docs.find((item) => item.id === leaf.docId); if (!doc) continue;
      const tab = h('div', { class:`copal-note-tab${leaf.id === group.activeLeafId ? ' active' : ''}${leaf.pinned ? ' pinned' : ''}`, role:'tab', 'aria-selected':String(leaf.id === group.activeLeafId), draggable:'true', 'data-leaf-id':leaf.id },
        h('button', { class:'copal-note-tab-label', text:displayName(doc), title:doc.name, 'aria-label':`Open ${doc.name}`, onclick:() => setActive(group.id, leaf.id) }),
        h('button', { class:'copal-note-tab-pin', text:leaf.pinned ? '●' : '○', title:leaf.pinned ? 'Unpin tab' : 'Pin tab', 'aria-pressed':String(leaf.pinned), onclick:() => { leaf.pinned = !leaf.pinned; persist(true); render(); } }),
        h('button', { class:'copal-note-tab-close', text:'×', disabled:leaf.pinned, 'aria-label':`Close ${doc.name}`, onclick:async () => {
          if (!await saveDraft(doc.id)) return;
          const closed = closeWorkspaceLeaf(workspace, leaf.id); if (closed) disposeLeaf(leaf.id);
          syncSelectionToModel(workspace); persist(true); render(); focusEmptyWorkspaceIfIdle();
        } }));
      tab.addEventListener('auxclick', async (event) => {
        if (event.button !== 1 || leaf.pinned) return;
        event.preventDefault(); if (!await saveDraft(doc.id)) return;
        if (closeWorkspaceLeaf(workspace, leaf.id)) disposeLeaf(leaf.id);
        syncSelectionToModel(workspace); persist(true); render(); focusEmptyWorkspaceIfIdle();
      });
      tab.addEventListener('dragstart', (event) => event.dataTransfer.setData('text/x-copal-note-leaf', leaf.id));
      tab.addEventListener('dragover', (event) => {
        const internalRaw = event.dataTransfer?.getData?.(FILES_TRANSFER_MIME);
        const hasInternal = [...(event.dataTransfer?.types || [])].includes(FILES_TRANSFER_MIME);
        if (internalRaw || hasInternal) {
          event.preventDefault();
          const checked = validateEditorTransfer(internalRaw, { sourceKind:'file' });
          if (checked.ok && checked.payload.sources.length === 1) { event.dataTransfer.dropEffect = 'copy'; }
          else event.dataTransfer.dropEffect = 'none';
          return;
        }
        if (event.dataTransfer?.types?.includes?.('text/x-copal-note-leaf')) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; }
        else event.dataTransfer.dropEffect = 'none';
      });
      tab.addEventListener('drop', (event) => {
        const internalRaw = event.dataTransfer?.getData?.(FILES_TRANSFER_MIME);
        const hasInternal = [...(event.dataTransfer?.types || [])].includes(FILES_TRANSFER_MIME);
        if (internalRaw || hasInternal) {
          event.preventDefault();
          const checked = validateEditorTransfer(internalRaw, { sourceKind:'file' });
          if (!checked.ok || checked.payload.sources.length !== 1) {
            context()?.window?.setStatus(checked.reason || 'This Files drop cannot open a resource.', true); return;
          }
          void openFilesResourceDrop(checked.payload.sources[0], checked.context).catch(error => context()?.window?.setStatus(error?.message || 'Files resource could not be opened.', true));
          return;
        }
        event.preventDefault(); const source = event.dataTransfer.getData('text/x-copal-note-leaf');
        const index = group.tabs.findIndex((item) => item.id === leaf.id);
        if (source && moveWorkspaceLeaf(workspace, source, group.id, index)) { persist(true); render(); }
      });
      tablist.append(tab);
    }
    const otherGroups = () => workspaceGroups(workspace).filter((candidate) => candidate.id !== group.id);
    const moveToGroupItems = (leafId) => otherGroups().map((target, index) => commandButton(
      `Move to group ${index + 1}`,
      () => { if (moveWorkspaceLeaf(workspace, leafId, target.id)) { persist(true); render(); } },
    ));
    const groupMenu = wirePopover(h('details', { class:'copal-leaf-menu copal-group-menu' }, h('summary', { text:'⋯', title:'Tab group actions', 'aria-label':'Tab group actions' }), h('div', { class:'copal-popover-menu' },
      ...((() => {
        const activeId = group.activeLeafId;
        const activeLeaf = group.tabs.find((leaf) => leaf.id === activeId);
        return activeLeaf ? [
          commandButton('Split right', () => { const doc = workspaceDocuments().find((item) => item.id === activeLeaf.docId); if (doc && splitWorkspaceGroup(workspace, group.id, doc, 'horizontal')) { persist(true); render(); } }),
          commandButton('Split below', () => { const doc = workspaceDocuments().find((item) => item.id === activeLeaf.docId); if (doc && splitWorkspaceGroup(workspace, group.id, doc, 'vertical')) { persist(true); render(); } }),
          ...moveToGroupItems(activeId),
        ] : [];
      })()),
      commandButton('Close other tabs', async () => {
        const activeId = group.activeLeafId; const candidates = group.tabs.filter((leaf) => leaf.id !== activeId && !leaf.pinned);
        const results = await Promise.all(candidates.map((leaf) => saveDraft(leaf.docId)));
        if (results.some((value) => !value)) return;
        for (const closed of closeWorkspaceOtherLeaves(workspace, activeId)) disposeLeaf(closed.id);
        syncSelectionToModel(workspace); persist(true); render();
      }),
      commandButton('Close tab group', async () => {
        const candidates = group.tabs.filter((leaf) => !leaf.pinned);
        const results = await Promise.all(candidates.map((leaf) => saveDraft(leaf.docId)));
        if (results.some((value) => !value)) return;
        for (const closed of closeWorkspaceGroup(workspace, group.id)) disposeLeaf(closed.id);
        syncSelectionToModel(workspace); persist(true); render(); focusEmptyWorkspaceIfIdle();
      }))));
    const controls = h('div', { class:'copal-tab-group-controls' },
      commandButton('+', () => showChooser({ title:'Open note in this group', choose:(doc) => { openWorkspaceDocument(workspace, doc, { groupId:group.id, intent:'newTab' }); persist(true); render(); } }), { title:'Open note', 'aria-label':'Open note' }),
      commandButton('↔', () => showChooser({ title:'Split right', choose:(doc) => { splitWorkspaceGroup(workspace, group.id, doc, 'horizontal'); persist(true); render(); }, allowCreate:false }), { title:'Split right', 'aria-label':'Split right' }),
      commandButton('↕', () => showChooser({ title:'Split below', choose:(doc) => { splitWorkspaceGroup(workspace, group.id, doc, 'vertical'); persist(true); render(); }, allowCreate:false }), { title:'Split below', 'aria-label':'Split below' }),
      groupMenu);
    const rightSlot = h('div', { class:'copal-shell-tab-slot right', 'data-shell-slot':'right' });
    if (ownsShellControls && (shellState.compact || !workspace.right.open)) rightSlot.append(shellState.controls.right);
    bar.append(controls, rightSlot); return bar;
  }

  function renderNode(node, workspace, docs, shellState) {
    if (node.type === 'group') {
      const group = h('section', { class:`copal-note-group${node.tabs.some((leaf) => leaf.id === workspace.activeLeafId) ? ' active-group' : ''}`, 'data-group-id':node.id });
      const active = node.tabs.find((leaf) => leaf.id === node.activeLeafId) || node.tabs[0] || null;
      if (active && node.activeLeafId !== active.id) node.activeLeafId = active.id;
      group.append(groupTabs(node, workspace, docs, shellState));
      const body = h('div', { class:'copal-note-group-body' });
      if (active) {
        const doc = docs.find((item) => item.id === active.docId);
        if (doc) body.append(renderLeaf(active, doc, workspace, node));
      } else if (!workspaceLeaves(workspace).length) body.append(emptyWorkspace(workspace));
      else body.append(h('div', { class:'copal-empty' }, h('p', { text:'This tab group is empty.' }), commandButton('Open note', () => showChooser({ choose:(doc) => { openWorkspaceDocument(workspace, doc, { groupId:node.id }); persist(true); render(); } }))));
      group.addEventListener('dragover', (event) => event.preventDefault());
      group.addEventListener('drop', (event) => {
        const leafId = event.dataTransfer.getData('text/x-copal-note-leaf');
        if (leafId && moveWorkspaceLeaf(workspace, leafId, node.id)) { event.preventDefault(); persist(true); render(); }
      });
      group.append(body); return group;
    }
    const renderedOrientation = node.orientation === 'horizontal' && window.matchMedia('(max-width: 760px)').matches ? 'vertical' : node.orientation;
    const split = h('div', { class:`copal-note-split ${renderedOrientation}`, 'data-split-id':node.id, 'data-stored-orientation':node.orientation });
    node.children.forEach((child, index) => {
      const wrap = h('div', { class:'copal-note-split-child', style:`flex-basis:${node.sizes[index] || 50}%` }, renderNode(child, workspace, docs, shellState));
      split.append(wrap);
      if (index >= node.children.length - 1) return;
      const handle = h('div', { class:'copal-note-splitter', role:'separator', tabindex:'0', 'aria-label':`Resize ${renderedOrientation} split`, 'aria-orientation':renderedOrientation === 'horizontal' ? 'vertical' : 'horizontal', 'aria-valuemin':'15', 'aria-valuemax':'85', 'aria-valuenow':String(Math.round(node.sizes[0])) });
      handle.addEventListener('keydown', (event) => {
        const negative = renderedOrientation === 'horizontal' ? event.key === 'ArrowLeft' : event.key === 'ArrowUp';
        const positive = renderedOrientation === 'horizontal' ? event.key === 'ArrowRight' : event.key === 'ArrowDown';
        if (!negative && !positive) return; event.preventDefault();
        resizeWorkspaceSplit(workspace, node.id, node.sizes[0] + (negative ? -5 : 5)); handle.setAttribute('aria-valuenow', String(Math.round(node.sizes[0]))); persist(true); render();
      });
      handle.addEventListener('pointerdown', (event) => {
        event.preventDefault(); handle.setPointerCapture(event.pointerId);
        const rect = split.getBoundingClientRect();
        const move = (next) => {
          const value = renderedOrientation === 'horizontal' ? ((next.clientX - rect.left) / rect.width) * 100 : ((next.clientY - rect.top) / rect.height) * 100;
          if (resizeWorkspaceSplit(workspace, node.id, value)) {
            handle.setAttribute('aria-valuenow', String(Math.round(node.sizes[0])));
            split.children[0].style.flexBasis = `${node.sizes[0]}%`; split.children[2].style.flexBasis = `${node.sizes[1]}%`;
          }
        };
        const up = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); persist(true); };
        handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up);
      });
      split.append(handle);
    });
    return split;
  }

  function leafHeader(cache, leaf, doc, workspace, group) {
    const parts = doc.name.split('/');
    const breadcrumb = h('div', { class:'copal-note-breadcrumb', 'aria-label':'Document path' });
    parts.slice(0, -1).forEach((part, index) => breadcrumb.append(h('span', { text:part }), h('span', { text:index < parts.length - 2 ? ' / ' : '' })));
    if (doc.readOnly) {
      const resourceReadOnly = !!doc.resourceRef;
      const menu = wirePopover(h('details', { class:'copal-leaf-menu' }, h('summary', { text:'⋯', title:'Knowledge note actions', 'aria-label':'Knowledge note actions' }), h('div', { class:'copal-popover-menu' },
        ...(resourceReadOnly ? [commandButton('Show in Files', () => { void showResourceInFiles(doc.resourceRef); })] : [commandButton('History', () => showHistory(doc))]),
        commandButton('Split right', () => { if (splitWorkspaceGroup(workspace, group.id, doc, 'horizontal')) { persist(true); render(); } }),
        commandButton('Split below', () => { if (splitWorkspaceGroup(workspace, group.id, doc, 'vertical')) { persist(true); render(); } }),
        ...workspaceGroups(workspace).filter((target) => target.id !== group.id).map((target, index) => commandButton(
          `Move to group ${index + 1}`,
          () => { if (moveWorkspaceLeaf(workspace, leaf.id, target.id)) { persist(true); render(); } },
        )),
        commandButton(workspace.bookmarks?.includes(doc.id) ? 'Remove bookmark' : 'Add bookmark', () => toggleBookmark(doc.id)),
        commandButton('Reveal in Editor', () => { workspace.left.open = true; workspace.left.tab = 'files'; revealInExplorer(doc, workspace); persist(true); render(); }))));
      cache.header.replaceChildren(breadcrumb, h('strong', { class:'copal-inline-title', text:displayName(doc) }), h('span', { class:'copal-leaf-mode', text:resourceReadOnly ? 'Files resource · read only' : 'Built-in knowledge · read only' }), menu);
      return;
    }
    const fileName = parts.at(-1); const extension = /\.[A-Za-z0-9]+$/.exec(fileName)?.[0] || '';
    const title = h('input', { class:'copal-inline-title', value:displayName(doc), 'aria-label':'Note title' });
    title.addEventListener('change', async () => {
      const entered = title.value.trim();
      const nextFile = entered && /\.[A-Za-z0-9]+$/.test(entered) ? entered : `${entered}${extension}`;
      const name = [...parts.slice(0, -1), nextFile].filter(Boolean).join('/');
      if (!entered) { title.value = displayName(doc); return; }
      if (name !== doc.name) {
        try { await renameNote(doc, name); }
        catch (error) { title.value = displayName(doc); context().window.setStatus(error.message, true); }
      }
    });
    const menu = wirePopover(h('details', { class:'copal-leaf-menu' }, h('summary', { text:'⋯', title:'Note actions', 'aria-label':'Note actions' }), h('div', { class:'copal-popover-menu' },
      ...(doc.sourceKind === 'host' && doc.kind !== 'markdown'
        ? [commandButton('Source mode', () => { setWorkspaceLeafMode(workspace, leaf.id, 'source'); persist(true); render(); })]
        : [commandButton(doc.kind === 'note' ? 'Editing mode' : 'Live Preview', () => { setWorkspaceLeafMode(workspace, leaf.id, 'live'); persist(true); render(); }), ...(doc.kind === 'note' ? [] : [commandButton('Source mode', () => { setWorkspaceLeafMode(workspace, leaf.id, 'source'); persist(true); render(); })])]),
      commandButton('Reading mode', () => { setWorkspaceLeafMode(workspace, leaf.id, 'reading'); persist(true); render(); }),
      commandButton(workspace.settings.previewLayout === 'inline' ? 'Use side-by-side preview' : 'Use inline preview', () => setPreviewLayout(workspace.settings.previewLayout === 'inline' ? 'side-by-side' : 'inline')),
      ...(doc.savePolicy === 'explicit' ? [commandButton('Save', () => void saveDraft(doc.id))] : []),
      commandButton('Find and replace', () => cache.editor && showFindReplace(cache.editor)),
      commandButton('History', () => showHistory(doc)),
      commandButton('Split right', () => { if (splitWorkspaceGroup(workspace, group.id, doc, 'horizontal')) { persist(true); render(); } }),
      commandButton('Split below', () => { if (splitWorkspaceGroup(workspace, group.id, doc, 'vertical')) { persist(true); render(); } }),
      commandButton('Move tab left', () => { const index = group.tabs.findIndex((item) => item.id === leaf.id); if (index > 0 && moveWorkspaceLeaf(workspace, leaf.id, group.id, index - 1)) { persist(true); render(); } }),
      commandButton('Move tab right', () => { const index = group.tabs.findIndex((item) => item.id === leaf.id); if (index >= 0 && index < group.tabs.length - 1 && moveWorkspaceLeaf(workspace, leaf.id, group.id, index + 1)) { persist(true); render(); } }),
      ...workspaceGroups(workspace).filter((target) => target.id !== group.id).map((target, index) => commandButton(
        `Move to group ${index + 1}`,
        () => { if (moveWorkspaceLeaf(workspace, leaf.id, target.id)) { persist(true); render(); } },
      )),
      commandButton(workspace.bookmarks?.includes(doc.id) ? 'Remove bookmark' : 'Add bookmark', () => toggleBookmark(doc.id)),
      ...(doc.resourceRef ? [commandButton('Show in Files', () => { void showResourceInFiles(doc.resourceRef); })] : []),
      commandButton('Reveal in Editor', () => { workspace.left.open = true; workspace.left.tab = 'files'; revealInExplorer(doc, workspace); persist(true); render(); }),
      ...(leaf.view === 'canvas' || leaf.view === 'base' ? [commandButton(leaf.rawSource ? 'Back to typed view' : 'View raw source', () => { leaf.rawSource = !leaf.rawSource; persist(true); render(); })] : []),
      commandButton('Rename', () => renameWithForm(doc)),
      commandButton('Move to trash', () => deleteDocument(doc), { class:'copal-btn danger' }))));
    const actions = h('div', { class:'copal-note-header-actions' },
      ...(uploadAttachment ? [commandButton('Attach file', () => chooseAttachment(cache, doc), { title:'Attach a file at the captured cursor', 'aria-label':'Attach file' })] : []),
      commandButton('Trash', () => deleteDocument(doc), {
        class:'copal-btn copal-note-trash-button danger',
        title:'Move this note to Trash',
        'aria-label':`Move ${doc.name} to Trash`,
      }),
      menu);
    const mode = doc.sourceKind === 'host' && doc.kind !== 'markdown' ? 'Source' : leaf.mode === 'live' ? doc.kind === 'note' ? 'Editing' : 'Live Preview' : leaf.mode === 'source' ? 'Source' : 'Reading';
    cache.header.replaceChildren(breadcrumb, title, h('span', { class:'copal-leaf-mode', text:mode }), actions);
  }

  function renderLeaf(leaf, doc, workspace, group) {
    const current = context();
    let cache = current.noteLeafViews.get(leaf.id);
    if (!cache || cache.docId !== doc.id || cache.view !== leaf.view) {
      if (cache) disposeLeaf(leaf.id);
      const root = h('article', { class:'copal-note-leaf', 'data-leaf-id':leaf.id, 'data-view-type':leaf.view });
      const header = h('header', { class:'copal-note-view-header' });
      const body = h('div', { class:'copal-note-leaf-content' });
      const propsFooter = h('div', { class:'copal-note-props-footer' });
      const status = h('footer', { class:'copal-note-status', role:'status', 'aria-live':'polite' });
      root.append(header, body, propsFooter, status);
      cache = { root, header, body, propsFooter, status, docId:doc.id, view:leaf.view, saveState:'saved', cursorLine:1, editor:null };
      current.noteLeafViews.set(leaf.id, cache);
    }
    cache.leaf = leaf; cache.doc = doc;
    if (leaf.view === 'base' || leaf.view === 'canvas') {
      if (cache.rawSourceActive !== leaf.rawSource) {
        if (leaf.rawSource) disposeSheet(cache);
        else disposeRawEditor(cache);
        cache.rawSourceActive = leaf.rawSource;
      }
    }
    if (leaf.view === 'event') {
      // Auto-open the native event editor for copal-event documents.
      if (openEventEditor) openEventEditor(doc.id);
      cache.header.replaceChildren(
        h('strong', { class:'copal-inline-title', text:doc.name.split('/').pop().replace(/\.md$/i, '') }),
        h('span', { class:'copal-leaf-mode', text:'Event' }),
      );
      cache.body.replaceChildren();
      cache.status.replaceChildren(h('span', { text:'Event · opened in event editor' }));
      return cache.root;
    }
    if (leaf.view === 'timeline') {
      cache.header.replaceChildren(
        h('strong', { class:'copal-inline-title', text:'Timeline' }),
        h('span', { class:'copal-leaf-mode', text:'Canonical Redb view' }),
      );
      renderTimeline?.(cache.body);
      cache.status.replaceChildren(h('span', { text:'Timeline · canonical Copal planning records' }));
      return cache.root;
    }
    leafHeader(cache, leaf, doc, workspace, group);
    if (doc.readOnly) {
      cache.body.replaceChildren(renderMarkdown(sourceValue(doc), new Set([doc.id])));
      cache.status.replaceChildren(h('span', { text:`${doc.resourceRef ? 'Files resource' : 'Built-in knowledge'} · ${wordCount(sourceValue(doc))} words · read only` }));
      return cache.root;
    }
    if (doc.note_error) cache.body.replaceChildren(h('div', { class:'copal-inspector-error' },
      h('strong', { text:'This database note could not be decoded' }),
      h('p', { text:'Its stored record is preserved and has not been opened for editing.' }),
      h('p', { text:doc.note_error })));
    else if (leaf.view === 'markdown' || leaf.view === 'note') updateMarkdownLeaf(cache, leaf, doc, workspace);
    else if (leaf.rawSource) updateSourceLeaf(cache, leaf, doc, workspace);
    else if (leaf.view === 'canvas') updateCanvasLeaf(cache, doc);
    else if (leaf.view === 'base') updateBaseLeaf(cache, doc);
    else updateAssetLeaf(cache, doc, leaf.view);
    // Properties footer for note/markdown views — inline editable card
    if ((leaf.view === 'markdown' || leaf.view === 'note') && !doc.readOnly && !doc.note_error) {
      const props = buildInlineProps(doc);
      cache.propsFooter.replaceChildren(props);
      cache.propsFooter.style.display = '';
    } else {
      cache.propsFooter.replaceChildren();
      cache.propsFooter.style.display = 'none';
    }
    updateLeafStatus(cache);
    return cache.root;
  }

  function sourceValue(doc) {
    return context().noteDrafts.get(doc.id)?.value ?? String(doc.text || '');
  }

  // The global Copal menu captures a CodeMirror selection before it moves
  // focus into its own DOM. Commands resolve through this adapter instead of
  // execCommand, so an async clipboard permission prompt cannot retarget a
  // split editor or dialog.
  function registerEditorContextMenu(cache) {
    if (cache.contextMenuDispose || !cache.editor?.view) return;
    const host = cache.editor.view.dom;
    const register = window.openClankContextMenu?.registerAdapter;
    if (!register || !host) return;
    cache.contextMenuDispose = register(host, createCodeMirrorContextAdapter(cache.editor, {
      // The menu may await clipboard permission while another leaf, account,
      // or workspace changes.  Keep the captured command bound to this exact
      // buffer and save scope instead of relying on document length alone.
      bufferIdentity:() => `${cache.doc?.id || ''}:${JSON.stringify(cache.doc?.resource?.key || cache.doc?.resourceKey || {})}`,
      revision:() => cache.doc?.head || cache.doc?.resource?.revision?.value || '',
      scope:() => `${state.accountId || ''}:${state.workspace || ''}:${state.contextEpoch || 0}`,
      onCommand:async (command) => {
        if (command === 'insert-template') { insertTemplate(); return true; }
        if (command === 'new-from-template') { createFromTemplate(); return true; }
        return false;
      },
    }));
  }

  function attachmentCursor(cache, event = null, doc = cache.doc) {
    const editor = cache.editor;
    if (!editor?.view) return null;
    const selection = editor.getSelection();
    let from = selection.anchor;
    let to = selection.head;
    if (event?.clientX != null && event?.clientY != null) {
      const position = editor.view.posAtCoords({ x:event.clientX, y:event.clientY });
      if (Number.isInteger(position)) from = to = position;
    }
    if (to < from) [from, to] = [to, from];
    const current = context();
    const buffer = current?.noteBuffers?.get(doc?.id);
    const sourceDocument = editor.view.state.doc;
    return {
      from, to, length:editor.view.state.doc.length, docId:doc?.id || cache.docId,
      resourceKey:doc?.resource?.key || doc?.resourceKey || null,
      revision:doc?.head || doc?.resource?.revision || null,
      scope:currentScope(), localRevision:buffer?.state?.().localRevision || current?.noteDrafts?.get(doc?.id)?.localRevision || 0,
      sourceDocument, sourceText:sourceDocument.toString(),
      selectionGeneration:Number(cache.selectionGeneration || 0),
      selection:{ anchor:selection.anchor, head:selection.head },
    };
  }

  function attachmentCursorCurrent(cache, doc, cursor) {
    const current = context();
    const key = doc?.resource?.key || doc?.resourceKey || null;
    const currentRevision = doc?.head || doc?.resource?.revision || null;
    const buffer = current?.noteBuffers?.get(doc?.id);
    const localRevision = buffer?.state?.().localRevision || current?.noteDrafts?.get(doc?.id)?.localRevision || 0;
    return current && cursor?.scope === currentScope() && cursor.docId === doc?.id
      && JSON.stringify(cursor.resourceKey) === JSON.stringify(key)
      && JSON.stringify(cursor.revision) === JSON.stringify(currentRevision)
      && cursor.localRevision === localRevision && cache.editor?.view?.dom?.isConnected
      && cache.editor.view.state.doc.length === cursor.length
      && cache.editor.view.state.doc === cursor.sourceDocument
      && cache.editor.view.state.doc.toString() === cursor.sourceText
      && Number(cache.selectionGeneration || 0) === Number(cursor.selectionGeneration || 0)
      && cache.editor.getSelection?.().anchor === cursor.selection.anchor
      && cache.editor.getSelection?.().head === cursor.selection.head;
  }

  /** Workspace-relative media stem for a document: ``projects/design.md`` -> ``projects/design``. */
  function documentMediaStem(doc) {
    const raw = String(doc?.name || doc?.path || 'untitled').replace(/\\/g, '/').replace(/^\/+/, '');
    const withoutExt = raw.replace(/\.[^./]+$/, '');
    return withoutExt || 'untitled';
  }

  function attachmentName(file, doc) {
    const name = String(file?.name || 'attachment').replace(/\\/g, '/').split('/').pop().replace(/[^\w.()\- ]+/g, '_').trim() || 'attachment';
    return `media/${documentMediaStem(doc)}/${name}`;
  }

  /** Direct inline image syntax with a correctly escaped relative reference. */
  function attachmentReference(target, label, mediaKind, mode = 'embed') {
    return serializeAttachmentInsertion(
      { format: 'markdown', link_target: target, label: label || 'attachment', media_kind: mediaKind || 'application/octet-stream' },
      { mode },
    );
  }

  /**
   * Paste-image placement for programming source: inside a documentation
   * region the Markdown embeds as-is; outside, wrap it in the language's
   * comment syntax at a safe line boundary. Strict JSON reports its real
   * limitation and refuses to mutate the file.
   */
  function placeAttachmentMarkdown(cache, doc, cursor, markdown) {
    const editor = cache.editor;
    const isSourceDoc = doc.sourceKind === 'host' && doc.kind !== 'markdown';
    if (!isSourceDoc || typeof editor.safeCommentInsertion !== 'function') return { ok:true, text:markdown };
    const placement = editor.safeCommentInsertion(cursor?.from ?? 0);
    if (!placement.ok) return placement;
    if (placement.comment === '') return { ok:true, text:markdown };
    const wrapped = editor.wrapMarkdownAsComment?.(markdown, placement.indent || '');
    if (!wrapped?.ok) return wrapped || { ok:false, error:'Comment wrapping failed.' };
    return { ok:true, text:`\n${wrapped.text}`, replaceFrom:placement.from, replaceTo:placement.to };
  }

  function attachmentDialog(cache, doc, file, cursor) {
    if (!uploadAttachment || !cache.editor?.view || !cursor) return false;
    const caption = h('input', { class:'copal-attachment-caption', type:'text', value:String(file.name || ''), 'aria-label':'Attachment caption' });
    const progress = h('p', { class:'copal-attachment-progress', role:'status', 'aria-live':'polite', text:'Ready to attach.' });
    const usage = h('p', { class:'copal-attachment-usage', role:'status' });
    const attach = h('button', { class:'copal-btn primary', text:'Attach' });
    const retry = h('button', { class:'copal-btn', text:'Retry', hidden:true });
    const dialog = h('dialog', { class:'copal-dialog copal-attachment-dialog' },
      h('h2', { text:`Attach ${file.name || 'file'}` }),
      h('p', { text:`${file.type || 'application/octet-stream'} · ${file.size || 0} bytes` }),
      h('label', {}, h('span', { text:'Caption' }), caption), progress, usage,
      h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Cancel', onclick:() => dialog.close() }), retry, attach));
    wireDialog(dialog); document.body.append(dialog); dialog.showModal();
    let actionId = `attachment-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const mediaKind = String(file.type || 'application/octet-stream');
    const reference = () => {
      const name = attachmentName(file, doc);
      const label = caption.value.trim() || String(file.name || 'attachment');
      // Inline image syntax for image media; other types stay a plain link.
      const mode = /^(?:image|video|audio)(?:\/|$)/i.test(mediaKind) ? 'embed' : 'link';
      return attachmentReference(name, label, mediaKind, mode);
    };
    const run = async () => {
      const current = cache.editor.view.state.doc.toString() || cache.host?.querySelector('.cm-content')?.textContent || sourceValue(doc);
      const editorLength = cache.editor?.view?.state?.doc?.length;
      if (!attachmentCursorCurrent(cache, doc, cursor) || editorLength !== cursor.length) {
        progress.textContent = 'The captured cursor is stale. Reopen the attachment action and try again.';
        attach.disabled = false; retry.hidden = false; return;
      }
      const inserted = reference();
      const placed = placeAttachmentMarkdown(cache, doc, cursor, inserted);
      if (!placed.ok) {
        progress.textContent = placed.error || 'This format cannot hold an image comment.';
        attach.disabled = false; retry.hidden = false;
        return;
      }
      const insertAt = placed.replaceFrom ?? cursor.from;
      const insertEnd = placed.replaceTo ?? cursor.to;
      const insertedText = placed.text ?? inserted;
      const content = `${current.slice(0, insertAt)}${insertedText}${current.slice(insertEnd)}`;
      attach.disabled = true; retry.hidden = true; caption.disabled = true;
      progress.textContent = 'Uploading attachment and saving the reference…';
      let prepared = null;
      let result = null;
      let lockedEditor = null;
      try {
        prepared = await uploadAttachment({ actionId, documentId:doc.id, name:attachmentName(file, doc), mime:file.type || 'application/octet-stream', bytes:file, content, sourceText:current, base:doc.head, caption:caption.value.trim() });
        if (!attachmentCursorCurrent(cache, doc, cursor)) throw new Error('The captured editor state changed; retry the attachment.');
        const lifecycle = prepared?.preparation;
        if (commitAttachment && lifecycle?.asset_id && lifecycle?.asset_name && lifecycle?.source_text_hash) {
          lockedEditor = cache.editor?.view?.dom?.querySelector?.('[contenteditable]') || cache.editor?.view?.dom;
          if (lockedEditor) { lockedEditor.dataset.copalAttachmentLocked = 'true'; lockedEditor.style.pointerEvents = 'none'; lockedEditor.setAttribute('contenteditable', 'false'); }
          result = await commitAttachment({ actionId, documentId:doc.id, content, base:doc.head, sourceTextHash:lifecycle.source_text_hash, assetId:lifecycle.asset_id, assetName:lifecycle.asset_name });
        } else {
          // Older embedders may expose only the upload endpoint. They cannot
          // safely prove the prepared asset/target tuple, so fail closed
          // instead of inserting locally and preserving the old race.
          throw new Error('Attachment lifecycle commit is unavailable; reopen this Editor and retry.');
        }
        if (!cache.editor?.view?.dom.isConnected) throw new Error('The Editor leaf closed before the attachment completed. Retry from the document.');
        // The drop/paste target is intentionally the captured primary range.
        // CodeMirror maps every other cursor through the same change and keeps
        // those ranges alive while the upload/save receipt completes.
        if (cache.editor.replaceRange) cache.editor.replaceRange(insertAt, insertEnd, insertedText);
        else cache.editor.view.dispatch({ changes:{ from:insertAt, to:insertEnd, insert:insertedText }, userEvent:'input' });
        progress.textContent = `Attached ${file.name || 'file'} · ${result.receipt?.outcome || result.outcome || 'saved'}`;
        const usageResult = await api(`/attachments/usage?name=${encodeURIComponent(attachmentName(file, doc))}`);
        usage.textContent = `${usageResult.count} document usage${usageResult.count === 1 ? '' : 's'}`;
        retry.hidden = true; attach.hidden = true;
      } catch (error) {
        if (prepared?.preparation && abortAttachment) {
          try { await abortAttachment(actionId); } catch (_) { /* typed status/reaper owns eventual cleanup */ }
        }
        progress.textContent = `Attachment failed: ${error?.message || 'unknown error'}`;
        retry.hidden = false; attach.disabled = false; caption.disabled = false;
      } finally {
        if (lockedEditor) { lockedEditor.style.pointerEvents = ''; lockedEditor.removeAttribute('data-copal-attachment-locked'); lockedEditor.setAttribute('contenteditable', 'true'); }
      }
    };
    attach.addEventListener('click', () => void run());
    retry.addEventListener('click', () => { void run(); });
    return true;
  }

  function chooseAttachment(cache, doc, event = null) {
    const input = h('input', { type:'file', hidden:true, accept:'*/*' });
    input.addEventListener('change', () => {
      const file = input.files?.[0];
      input.remove();
      if (file) attachmentDialog(cache, doc, file, attachmentCursor(cache, event));
    });
    document.body.append(input); input.click();
  }

  function currentFilesTransferContext() {
    const getter = globalThis.__openClankFilesTransferContext;
    if (typeof getter !== 'function') return { ok:false, reason:'Files transfer authority is unavailable; retry from the Files window.' };
    let value;
    try { value = getter(); } catch (_) { return { ok:false, reason:'Files transfer authority could not be read; retry.' }; }
    const commandId = String(value?.commandId || value?.gestureId || value?.operationId || '').trim();
    const generation = value?.generation ?? value?.filesGeneration;
    const policyGeneration = value?.policyGeneration ?? value?.filesPolicyGeneration;
    const selectionEpoch = value?.selectionEpoch ?? value?.filesSelectionEpoch ?? value?.filesEpoch;
    const owner = value?.owner || value?.filesOwner || value?.scope?.owner;
    const workspace = value?.workspace || value?.filesWorkspace || value?.scope?.workspace;
    const pane = value?.pane || value?.filesPane || value?.scope?.pane;
    const provider = value?.provider || value?.scope?.provider;
    const allowedKeys = new Set(['commandId', 'generation', 'policyGeneration', 'selectionEpoch', 'owner', 'workspace', 'pane', 'provider', 'parent', 'scopeKey', 'selectedKeys', 'sourceCapabilities', 'allowCopy', 'allowMove']);
    const selectedKeys = value?.selectedKeys;
    const sourceCapabilities = value?.sourceCapabilities;
    const capabilityKeys = sourceCapabilities && typeof sourceCapabilities === 'object' ? Object.keys(sourceCapabilities) : [];
    const validCapabilityMap = sourceCapabilities && typeof sourceCapabilities === 'object' && !Array.isArray(sourceCapabilities)
      && Object.isFrozen(sourceCapabilities)
      && capabilityKeys.every(key => {
        const capability = sourceCapabilities[key];
        return Object.isFrozen(capability) && capability && typeof capability === 'object' && !Array.isArray(capability)
          && Object.keys(capability).every(name => ['read', 'open', 'download', 'export'].includes(name))
          && ['read', 'open', 'download', 'export'].every(name => typeof capability[name] === 'boolean');
      });
    if (!value || typeof value !== 'object' || Array.isArray(value) || !Object.isFrozen(value)
      || [...Object.keys(value)].some(key => !allowedKeys.has(key))
      || !Number.isSafeInteger(generation) || !Number.isSafeInteger(policyGeneration)
      || !Number.isSafeInteger(selectionEpoch) || !String(owner || '').trim()
      || !String(workspace || '').trim() || !String(pane || '').trim() || !String(provider || '').trim() || !commandId
      || commandId.length > 128 || /[\u0000-\u001f\u007f]/u.test(commandId)
      || !String(value?.parent || '').trim() || !String(value?.scopeKey || '').trim()
      || !Object.isFrozen(selectedKeys) || !Array.isArray(selectedKeys) || !selectedKeys.length
      || selectedKeys.some(key => typeof key !== 'string' || !key.trim()) || new Set(selectedKeys).size !== selectedKeys.length
      || !validCapabilityMap || capabilityKeys.some(key => !selectedKeys.includes(key))
      || selectedKeys.some(key => !capabilityKeys.includes(key))
      || typeof value?.allowCopy !== 'boolean' || typeof value?.allowMove !== 'boolean') {
      return { ok:false, reason:'Files transfer authority is incomplete; retry from the Files window.' };
    }
    return { ok:true, value:Object.freeze({ ...value, commandId, generation:Number(generation), policyGeneration:Number(policyGeneration), selectionEpoch:Number(selectionEpoch), owner:String(owner), workspace:String(workspace), pane:String(pane), provider:String(provider), sourceCapabilities }) };
  }

  function validateEditorTransfer(raw, { targetRef = '', targetKey = '', sourceKind = 'file' } = {}) {
    const payload = parseInternalDragPayload(raw);
    if (!payload) return { ok:false, reason:'This Files drop is invalid or expired.' };
    const live = currentFilesTransferContext();
    if (!live.ok) return live;
    const current = live.value;
    const generation = Number(current.generation);
    const policyGeneration = Number(current.policyGeneration);
    const selectionEpoch = Number(current.selectionEpoch);
    const owner = String(current.owner);
    const workspace = String(current.workspace);
    if (payload.owner !== owner) return { ok:false, reason:'This Files drop belongs to another account.' };
    if (payload.generation !== generation || payload.policy_generation !== policyGeneration || payload.selection_epoch !== selectionEpoch || payload.workspace !== workspace || payload.pane !== String(current.pane) || payload.provider !== String(current.provider)) {
      return { ok:false, reason:'Files access or selection changed; choose the resource again.' };
    }
    if (payload.parent_ref !== String(current.parent)
      || scopeKey({ owner:payload.owner, workspace:payload.workspace, provider:payload.provider, parentRef:payload.parent_ref, query:payload.query, column:payload.column }) !== String(current.scopeKey)
      || payload.sources.length !== current.selectedKeys.length
      || payload.sources.some((source, index) => String(source.resource_key) !== String(current.selectedKeys[index]))) {
      return { ok:false, reason:'The Files selection changed; choose the resource again.' };
    }
    if (payload.kind === 'move') return { ok:false, reason:'Move drops cannot be inserted into an Editor document; use Copy or Attach.' };
    if (!payload.sources.length || payload.sources.some(source => !source.resource_ref || !source.resource_key)) return { ok:false, reason:'This Files drop has no authorized resource.' };
    const selectedKeys = Array.isArray(current.selectedKeys) ? current.selectedKeys.map(String) : null;
    if (!selectedKeys || payload.sources.some(source => !selectedKeys.includes(String(source.resource_key)))) return { ok:false, reason:'The Files selection changed; choose the resource again.' };
    const sourceCapabilities = current.sourceCapabilities && typeof current.sourceCapabilities === 'object' ? current.sourceCapabilities : null;
    if (!sourceCapabilities || payload.sources.some(source => {
      const capability = sourceCapabilities[String(source.resource_key)];
      return !capability || (capability.read !== true && capability.open !== true && capability.download !== true);
    })) return { ok:false, reason:'This Files resource cannot be attached from the current authorization.' };
    if (sourceKind === 'file' && payload.sources.some(source => source.resource_ref === targetRef || source.resource_key === targetKey)) return { ok:false, reason:'A resource cannot be opened into itself.' };
    const checked = validateDropTarget(payload, { resource_ref:targetRef, resource_key:targetKey }, {
      generation, policyGeneration, selectionEpoch, owner, pane:String(current.pane), provider:String(current.provider), allowCopy:current.allowCopy === true, allowMove:false,
    });
    return checked.ok ? { ok:true, payload, context:current } : checked;
  }

  async function openFilesResourceDrop(source, handoff = null) {
    const checked = currentFilesTransferContext();
    if (!checked.ok) throw new Error(checked.reason);
    if (handoff && (handoff.commandId !== checked.value.commandId
      || handoff.generation !== checked.value.generation
      || handoff.policyGeneration !== checked.value.policyGeneration
      || handoff.selectionEpoch !== checked.value.selectionEpoch
      || handoff.owner !== checked.value.owner
      || handoff.pane !== checked.value.pane
      || handoff.provider !== checked.value.provider
      || handoff.workspace !== checked.value.workspace)) {
      throw new Error('The Files selection changed; choose the resource again.');
    }
    const scope = currentScope();
    const controller = new AbortController();
    const stat = await filesFacadeClient.stat(source.resource_ref, { signal:controller.signal });
    const afterStat = currentFilesTransferContext();
    if (scope !== currentScope()) throw new Error('The active account or workspace changed; retry the drop.');
    if (!afterStat.ok || (handoff && afterStat.value.commandId !== handoff.commandId)) throw new Error('The Files selection changed; choose the resource again.');
    const authorized = normalizeAuthorizedResource(stat?.resource || stat, { purpose:'file', ...pickerScope() });
    if (authorized.kind === 'folder' || authorized.capabilities.open !== true || authorized.capabilities.read !== true) throw new Error('This Files resource cannot be opened in Editor.');
    return resourceOpener({ resourceRef:authorized.ref, name:authorized.name, signal:controller.signal });
  }

  function preparedAttachmentInsertion(result, { operationId, sourceRevision, targetKey, targetRef, targetRevision }) {
    if (!result || typeof result.operation_id !== 'string' || result.operation_id !== String(operationId || '') || typeof result.preparation_receipt_id !== 'string' || !result.preparation_receipt_id.trim()) throw new Error('Attachment preparation receipt is missing.');
    if (JSON.stringify(result.source_revision) !== JSON.stringify(sourceRevision)) throw new Error('Attachment source revision changed; retry the drop.');
    const identity = result.target_identity;
    const resultKey = identity?.resource_key || identity?.resourceKey || identity?.key
      || (identity?.provider && (identity?.resourceId || identity?.resource_id) ? identity : null);
    const comparableResultKey = resultKey && typeof resultKey === 'object'
      ? canonicalEditorResourceKey(resultKey, identity?.provider) : String(resultKey || '');
    const resultRef = String(identity?.resource_ref || identity?.resourceRef || '').trim();
    if (!['copal_document', 'host_document'].includes(String(identity?.kind || ''))) throw new Error('Attachment target identity changed; retry the drop.');
    if ((targetRef && resultRef !== String(targetRef)) || (comparableResultKey && targetKey && comparableResultKey !== String(targetKey)) || (!resultRef && !comparableResultKey)) throw new Error('Attachment target identity changed; retry the drop.');
    if (JSON.stringify(result.target_revision) !== JSON.stringify(targetRevision)) throw new Error('Attachment target revision changed; retry the drop.');
    const history = result.history;
    const receipt = history?.receipt || history;
    const terminal = String(receipt?.outcome || '').toLowerCase();
    const status = String(history?.status || '').toLowerCase();
    const phase = String(history?.phase || '').toLowerCase();
    const actionId = String(receipt?.action_id || receipt?.receipt_id || '').trim();
    const completed = ['applied', 'committed', 'unchanged'].includes(terminal)
      || (status === 'complete' && (!phase || phase === 'complete'));
    if (!history || typeof history !== 'object' || !actionId || !completed) throw new Error('Attachment action receipt is unavailable; nothing was inserted.');
    return serializeAttachmentInsertion(result.insertion, { mode:'link' });
  }

  async function reconcileAttachment(operationId, workspace, signal) {
    // Generic Files operation receipts contain transfer item status and are
    // never preparation descriptors. Recovery uses only S01's typed endpoint,
    // with the original Copal workspace bound into the request.
    const reconcile = filesFacadeClient.attachmentPreparationReceipt;
    if (typeof reconcile !== 'function') return null;
    try { return await reconcile.call(filesFacadeClient, operationId, { workspace, signal }); } catch (_) { return null; }
  }

  function wireAttachmentInput(cache, doc) {
    if (!cache.host || cache.attachmentInputWired) return;
    cache.attachmentInputWired = true;
    cache.host.addEventListener('paste', (event) => {
      const file = [...(event.clipboardData?.files || [])][0];
      if (!file) return;
      event.preventDefault(); event.stopPropagation();
      attachmentDialog(cache, doc, file, attachmentCursor(cache));
    });
    const hasInternalType = (event) => [...(event.dataTransfer?.types || [])].includes(FILES_TRANSFER_MIME);
    cache.host.addEventListener('dragover', (event) => {
      const internal = hasInternalType(event);
      if (internal) {
        event.preventDefault();
        const checked = validateEditorTransfer(event.dataTransfer?.getData?.(FILES_TRANSFER_MIME), { targetRef:doc.resource?.locator?.opaqueRef || doc.resourceRef, targetKey:canonicalEditorResourceKey(doc.resource?.key || doc.resourceKey || {}, doc.resource?.key?.provider || 'copal') });
        if (checked.ok) { event.dataTransfer.dropEffect = 'copy'; }
        else event.dataTransfer.dropEffect = 'none';
        return;
      }
      if ([...(event.dataTransfer?.files || [])].length) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }
    });
    cache.host.addEventListener('drop', (event) => {
      const internalRaw = event.dataTransfer?.getData?.(FILES_TRANSFER_MIME);
      if (internalRaw || hasInternalType(event)) {
        event.preventDefault(); event.stopPropagation();
        const targetRef = doc.resource?.locator?.opaqueRef || doc.resourceRef;
        const targetKey = canonicalEditorResourceKey(doc.resource?.key || doc.resourceKey || {}, doc.resource?.key?.provider || 'copal');
        const checked = validateEditorTransfer(internalRaw, { targetRef, targetKey });
        const source = checked.ok && checked.payload.sources.length === 1 ? checked.payload.sources[0] : null;
        const cursor = attachmentCursor(cache, event);
        if (!checked.ok || !source || !targetRef || !cursor || typeof filesFacadeClient.prepareAttachment !== 'function') {
          context()?.window?.setStatus(checked.reason || 'This resource drop is unsupported here; use Attach file or retry.', true); return;
        }
        const scope = currentScope();
        const binding = Object.freeze({
          accountId:checked.context.owner,
          workspace:checked.context.workspace,
          policyGeneration:Number(checked.context.policyGeneration),
          generation:Number(checked.context.generation),
          selectionEpoch:Number(checked.context.selectionEpoch),
          commandId:checked.context.commandId,
          sourceKey:String(source.resource_key),
          sourceRef:String(source.resource_ref),
          sourceItemId:String(source.item_id),
          sourceRevision:source.revision ? JSON.parse(JSON.stringify(source.revision)) : null,
          targetKey:String(targetKey),
          targetRef:String(targetRef),
          targetRevision:cursor.revision ? JSON.parse(JSON.stringify(cursor.revision)) : null,
          mode:'link',
        });
        const operationId = `editor-attachment-${checked.context.commandId}`;
        const ownerContext = context();
        if (!(ownerContext.attachmentOperations instanceof Map)) ownerContext.attachmentOperations = new Map();
        const attachmentOperations = ownerContext.attachmentOperations;
        // The Files handoff command id is stable for one gesture. Reserve it
        // before any await so repeated drop events cannot start a second
        // preparation or insertion; a later gesture receives a new id.
        if (attachmentOperations.has(operationId)) return;
        attachmentOperations.set(operationId, { state:'pending', binding });
        void (async () => {
          const controller = new AbortController();
          const sourceStat = await filesFacadeClient.stat(source.resource_ref, { signal:controller.signal });
          if (scope !== currentScope()) throw new Error('The Editor account changed; retry the drop.');
          const afterStatContext = currentFilesTransferContext();
          if (!afterStatContext.ok || afterStatContext.value.commandId !== binding.commandId
            || afterStatContext.value.owner !== binding.accountId || afterStatContext.value.workspace !== binding.workspace
            || afterStatContext.value.policyGeneration !== binding.policyGeneration
            || afterStatContext.value.selectionEpoch !== binding.selectionEpoch) throw new Error('The Files selection changed; retry the drop.');
          const authorizedSource = normalizeAuthorizedResource(sourceStat?.resource || sourceStat, { purpose:'file', ...pickerScope() });
          if (authorizedSource.kind === 'folder' || authorizedSource.capabilities.read !== true) throw new Error('This Files item is unavailable for attachment.');
          if (authorizedSource.ref !== String(source.resource_ref) || authorizedSource.resourceKey !== String(source.resource_key)
            || (source.revision && JSON.stringify(authorizedSource.revision) !== JSON.stringify(source.revision))) {
            throw new Error('The Files source changed; retry the drop.');
          }
          const sourceRevision = source.revision || authorizedSource.revision;
          let result;
          try {
            result = await filesFacadeClient.prepareAttachment({
              operationId, generation:checked.context.generation,
              source:{ resourceRef:authorizedSource.ref, ...(sourceRevision ? { expectedRevision:sourceRevision } : {}) },
              target:{ kind:'copal_document', resource_ref:targetRef, expected_revision:cursor.revision }, mode:'link', workspace:checked.context.workspace,
            }, { signal:controller.signal });
          } catch (error) {
            if (!attachmentPreparationMayHaveCommitted(error)) throw error;
            const recovered = await reconcileAttachment(operationId, checked.context.workspace, controller.signal);
            result = validateAttachmentPreparationRecovery(recovered, {
              operationId, generation:binding.generation,
              sourceKey:binding.sourceKey, sourceRef:binding.sourceRef, sourceItemId:binding.sourceItemId,
              sourceRevision, targetKey:binding.targetKey, targetRef:binding.targetRef,
              targetRevision:binding.targetRevision, mode:binding.mode,
              accountId:binding.accountId, workspace:binding.workspace, policyGeneration:binding.policyGeneration,
            });
          }
          if (scope !== currentScope()) throw new Error('The Editor account changed; retry the drop.');
          const live = validateEditorTransfer(checked.payload, { targetRef, targetKey, sourceKind:'file' });
          if (!live.ok || live.context.commandId !== binding.commandId || live.context.owner !== binding.accountId
            || live.context.workspace !== binding.workspace || live.context.policyGeneration !== binding.policyGeneration
            || live.context.selectionEpoch !== binding.selectionEpoch) throw new Error(live.reason || 'The Files selection changed; retry the drop.');
          if (!attachmentCursorCurrent(cache, doc, cursor)) throw new Error('The captured editor range changed; retry the drop.');
          const insertion = preparedAttachmentInsertion(result, { operationId, sourceRevision, targetKey, targetRef, targetRevision:cursor.revision });
          if (!attachmentCursorCurrent(cache, doc, cursor)) throw new Error('The captured editor range changed; retry the drop.');
          const transaction = applyDocumentTransaction(doc, sourceText => {
            if (sourceText !== cache.editor.view.state.doc.toString()) throw new Error('The captured editor content changed; retry the drop.');
            return `${sourceText.slice(0, cursor.from)}${insertion}${sourceText.slice(cursor.to)}`;
          }, { origin:'files-attachment' });
          if (!['queued', 'unchanged'].includes(transaction?.outcome)) throw new Error(transaction?.message || 'Attachment insertion was not applied.');
          attachmentOperations.set(operationId, { state:'completed', transactionId:transaction.transactionId || operationId });
          context()?.window?.setStatus('Attachment inserted.');
        })().catch(error => {
          attachmentOperations.set(operationId, { state:'failed', reason:error?.message || 'Attachment preparation failed' });
          context()?.window?.setStatus(error?.message || 'Attachment preparation failed; retry.', true);
        });
        return;
      }
      const file = [...(event.dataTransfer?.files || [])][0];
      if (!file) return;
      event.preventDefault(); event.stopPropagation();
      attachmentDialog(cache, doc, file, attachmentCursor(cache, event));
    });
  }

  function ensureEditor(cache, leaf, doc, workspace) {
    if (!cache.editorWrap) {
      cache.editorWrap = h('div', { class:'copal-note-editing-surface' });
      cache.host = h('div', { class:'copal-codemirror-host' });
      cache.preview = h('article', { class:'copal-note-live-preview' });
      cache.editorWrap.append(cache.host, cache.preview);
    }
    if (!cache.editor) {
      const editorSource = sourceValue(doc);
      const frontmatter = parseFrontmatter(editorSource);
      const defaultCursor = frontmatter.valid && frontmatter.present ? Math.min(editorSource.length, frontmatter.end + 1) : 0;
      const editorSelection = boundedEditorSelection(leaf.selection, editorSource.length) || { anchor:defaultCursor, head:defaultCursor };
      const editorFactory = doc.sourceKind === 'host' ? createSourceEditor : createMarkdownEditor;
      const hostPath = String(doc.name || doc.path || '');
      const hostLanguage = doc.sourceKind === 'host' ? languageForPath(hostPath) : 'Markdown';
      const hostDialect = doc.sourceKind === 'host' ? languageDialectForPath(hostPath) : '';
      cache.editor = editorFactory({
        parent:cache.host, doc:editorSource, label:`Edit ${doc.name}`, selection:editorSelection,
        scrollTop:leaf.scrollTop,
        mode:doc.sourceKind === 'host' && doc.kind !== 'markdown' ? 'source' : leaf.mode === 'live' && workspace.settings.previewLayout === 'inline' ? 'live' : 'source',
        lineNumbers:workspace.settings.lineNumbers, readableLineWidth:workspace.settings.readableLineWidth,
        language:hostLanguage,
        languageDialect:hostDialect,
        languagePath:hostPath,
        richComments:doc.sourceKind === 'host' && doc.kind !== 'markdown',
        // The live CodeMirror surface must use the same resolver and origin
        // semantics as the rendered Notes/Wiki view. Tests and alternate
        // hosts may still inject a specialized preview callback.
        renderPreview:renderPreview || ((source) => renderMarkdown(source, new Set([doc.id]), doc)),
        onSeeSource:(range) => {
          cache.editor?.revealCommentSource?.(range.from, range.to);
          context()?.window?.setStatus('Showing comment source.');
        },
        onSelection:(selection) => { leaf.selection = selection; cache.selectionGeneration = Number(cache.selectionGeneration || 0) + 1; cache.cursorLine = selection.line; persist(); updateLeafStatus(cache); },
        onScroll:(scrollTop) => { leaf.scrollTop = scrollTop; persist(); },
        onChange:(value) => { doc.text = value; queueSave(doc, value, { origin:'typing', history:false }); if (leaf.rawSource) publishRawBaseDefinition(doc, value); syncDocumentEditors(doc.id, value, cache.editor); updatePreview(cache, doc, value); updateLeafStatus(cache); },
        onCommand:(command) => {
          if (command === 'save') void saveDraft(doc.id);
          else if (command === 'quick-open') showChooser();
          else if (command === 'palette') showCommands();
          else if (command === 'search') showSearch();
        },
      });
      state.noteEditors.add(cache.editor);
      registerEditorContextMenu(cache);
      wireAttachmentInput(cache, doc);
      context().noteMetrics.editorConstructions += 1;
    } else if (!context().noteDrafts.has(doc.id)) cache.editor.setValue(String(doc.text || ''));
    cache.editor.setMode(doc.sourceKind === 'host' && doc.kind !== 'markdown' ? 'source' : leaf.mode === 'live' && workspace.settings.previewLayout === 'inline' ? 'live' : 'source');
    cache.editor.setLineNumbers(workspace.settings.lineNumbers);
    cache.editor.setReadableLineWidth(workspace.settings.readableLineWidth);
    return cache.editorWrap;
  }

  function updatePreview(cache, doc, value = sourceValue(doc)) {
    if (!cache.preview) return;
    cache.preview.replaceChildren(renderMarkdown(value, new Set([doc.id])));
    applyCompletedVisibility(cache.preview);
    wireInteractiveTables(cache.preview, value, doc);
  }

  function wireInteractiveTables(container, source, doc) {
    // Find all table blocks in the source
    const sourceLines = source.split('\n');
    const sourceTables = [];
    let i = 0;
    while (i < sourceLines.length) {
      if (sourceLines[i].includes('|') && sourceLines[i].trim() && i + 1 < sourceLines.length) {
        // Check if next line is a separator
        const sepLine = sourceLines[i + 1] || '';
        if (/^\|?\s*:?-{3,}/.test(sepLine.trim())) {
          const block = [sourceLines[i], sourceLines[i + 1]];
          let j = i + 2;
          while (j < sourceLines.length && sourceLines[j].includes('|') && sourceLines[j].trim()) {
            block.push(sourceLines[j]);
            j++;
          }
          sourceTables.push({ text: block.join('\n'), startLine: i });
          i = j;
          continue;
        }
      }
      i++;
    }
    // Replace static tables with interactive widgets
    const staticTables = container.querySelectorAll('.copal-markdown-table');
    let tableIdx = 0;
    for (const st of staticTables) {
      const src = sourceTables[tableIdx];
      tableIdx++;
      if (!src) continue;
      const model = parseTable(src.text, src.startLine);
      if (!model.valid) {
        // Malformed: wrap with warning, keep raw source
        const wrapper = document.createElement('div');
        wrapper.className = 'copal-table-malformed';
        wrapper.textContent = src.text;
        st.replaceWith(wrapper);
        continue;
      }
      const onEdit = (edit) => {
        const result = applyTableEdit(source, model, edit);
        if (result.changes.length) {
          // Try CodeMirror dispatch for atomic undo, fall back to setValue
          if (cache.editor?.view?.dispatch) {
            cache.editor.view.dispatch({ changes: result.changes });
          } else if (cache.editor?.dispatch) {
            cache.editor.dispatch({ changes: result.changes });
          } else if (cache.editor?.setValue) {
            cache.editor.setValue(result.newText);
          }
        }
      };
      const widget = createTableWidget(model, onEdit);
      st.replaceWith(widget);
    }
  }

  function applyCompletedVisibility(container) {
    if (!container) return;
    const workspace = ensureWorkspace();
    if (workspace?.settings?.completedVisibility !== 'hide') return;
    const tasks = container.querySelectorAll('.copal-markdown-task');
    let hidden = 0;
    for (const task of tasks) {
      const checkbox = task.querySelector('input[type="checkbox"]');
      if (checkbox?.checked) { task.hidden = true; hidden += 1; }
    }
    if (hidden) {
      const status = context()?.window?.root?.querySelector('.copal-notes-workspace');
      if (status) status.setAttribute('aria-label', `${hidden} completed task${hidden === 1 ? '' : 's'} hidden`);
    }
  }

  function updateMarkdownLeaf(cache, leaf, doc, workspace) {
    const editorWrap = ensureEditor(cache, leaf, doc, workspace);
    cache.root.dataset.mode = leaf.mode;
    cache.root.dataset.previewLayout = workspace.settings.previewLayout;
    if (leaf.mode === 'reading') {
      cache.reading ||= h('article', { class:'copal-note-reading' });
      cache.reading.replaceChildren(renderMarkdown(sourceValue(doc), new Set([doc.id])));
      applyCompletedVisibility(cache.reading);
      cache.body.replaceChildren(cache.reading);
      return;
    }
    const sideBySide = leaf.mode === 'live' && workspace.settings.previewLayout === 'side-by-side';
    editorWrap.classList.toggle('side-by-side', sideBySide);
    cache.preview.hidden = !sideBySide;
    if (sideBySide) updatePreview(cache, doc);
    cache.body.replaceChildren(editorWrap);
  }

  function updateSourceLeaf(cache, leaf, doc, workspace) {
    const editorWrap = ensureEditor(cache, { ...leaf, mode:'source' }, doc, workspace);
    editorWrap.classList.remove('side-by-side'); cache.preview.hidden = true;
    cache.body.replaceChildren(editorWrap);
  }

  function updateCanvasLeaf(cache, doc) {
    const parsed = parseCanvasDocument(sourceValue(doc));
    const surface = h('div', { class:'copal-canvas-view' });
    if (!parsed.valid) surface.append(h('div', { class:'copal-empty' }, h('h2', { text:'Canvas needs repair' }), h('p', { text:parsed.error }), h('p', { text:'Use Note actions → View raw source to repair it without losing data.' })));
    else if (!parsed.nodes.length) surface.append(h('div', { class:'copal-empty', text:'This Canvas contains no nodes.' }));
    else {
      const minX = Math.min(...parsed.nodes.map((node) => node.x)); const minY = Math.min(...parsed.nodes.map((node) => node.y));
      const maxX = Math.max(...parsed.nodes.map((node) => node.x + node.width)); const maxY = Math.max(...parsed.nodes.map((node) => node.y + node.height));
      const board = h('div', { class:'copal-canvas-board', style:`width:${Math.max(700, maxX - minX + 160)}px;height:${Math.max(480, maxY - minY + 160)}px` });
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('class', 'copal-canvas-edges');
      const byId = new Map(parsed.nodes.map((node) => [node.id, node]));
      for (const edge of parsed.edges) {
        const from = byId.get(edge.from); const to = byId.get(edge.to); if (!from || !to) continue;
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', String(from.x - minX + 80 + from.width / 2)); line.setAttribute('y1', String(from.y - minY + 80 + from.height / 2));
        line.setAttribute('x2', String(to.x - minX + 80 + to.width / 2)); line.setAttribute('y2', String(to.y - minY + 80 + to.height / 2));
        svg.append(line);
      }
      board.append(svg);
      for (const node of parsed.nodes) board.append(h('article', { class:'copal-canvas-node', style:`left:${node.x - minX + 80}px;top:${node.y - minY + 80}px;width:${node.width}px;min-height:${node.height}px` }, h('small', { text:node.type }), h('p', { text:node.label })));
      surface.append(board);
    }
    cache.body.replaceChildren(surface);
  }

  function updateBaseLeaf(cache, doc) {
    if (baseAdapter) {
      const resourceKey = doc.resource?.key || doc.resourceKey || { provider:'copal', accountId:String(state.accountId || ''), workspaceId:String(state.workspace || ''), resourceId:String(doc.id) };
      const current = context(); current.baseDefinitionStores ||= new Map();
      const definitionStoreKey = JSON.stringify(resourceKey);
      let definitionStore = current.baseDefinitionStores.get(definitionStoreKey);
      if (!definitionStore) {
        const listeners = new Set();
        definitionStore = { value:state.baseDefinition || {}, revision:null, authoritativeLocal:null, listeners, subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }, set(value, revision = null) { this.value = value; if (revision != null) this.revision = revision; this.authoritativeLocal = null; for (const listener of [...listeners]) listener(value, revision); }, publishLocal(value, revision, scope) { this.value = value; this.revision = revision; this.authoritativeLocal = { definition:value, revision, scope, source:'raw-editor' }; for (const listener of [...listeners]) listener(value, revision); } };
        current.baseDefinitionStores.set(definitionStoreKey, definitionStore);
      }
      if (!cache.sheetController || JSON.stringify(cache.sheetResourceKey) !== JSON.stringify(resourceKey)) {
      cache.sheetCleanup?.();
      cache.sheetBufferUnsubscribe?.(); cache.sheetBufferUnsubscribe = null; cache.sheetBuffer = null;
      if (cache.sheetBufferRefreshTimer) { clearTimeout(cache.sheetBufferRefreshTimer); cache.sheetBufferRefreshTimer = null; }
      const draft = context()?.noteDrafts?.get(doc.id);
      const resourceBuffer = context()?.noteBuffers?.get(doc.id);
      cache.sheetController = createSheetController({
          resourceKey, scope:bufferScope(context()), definition:definitionStore.value || state.baseDefinition || {}, definitionStore, definitionRevision:resourceBuffer?.state?.().dirty ? resourceBuffer.localRevision : draft?.localRevision || null, schema:doc.propertySchema || {},
          query:(request) => baseAdapter.query(doc, request),
          onCellEdit:(request) => baseAdapter.cellEdit?.(doc, request) || { outcome:'unavailable' },
          onDefinitionCommand:(request) => baseAdapter.command?.(doc, request) || { outcome:'unavailable' },
        });
        cache.sheetResourceKey = resourceKey;
        cache.sheetCleanup = mountSheet(cache.body, cache.sheetController, {
          schema:doc.propertySchema || {},
          onToolbar:(action) => baseAdapter.toolbar?.(doc, action, cache.sheetController),
          onOverflow:(sheetState) => baseAdapter.overflow?.(doc, cache.sheetController, sheetState, cache.leaf?.id),
          onColumnMenu:(column) => baseAdapter.columnMenu?.(doc, column, cache.sheetController),
          onColumnResize:(column, width, sheetState) => baseAdapter.columnResize?.(doc, column, width, cache.sheetController, sheetState),
          onColumnReorder:(fromProperty, toProperty, sheetState, payloadScope) => baseAdapter.columnReorder?.(doc, fromProperty, toProperty, cache.sheetController, sheetState, payloadScope),
          onViewCommand:(command, view) => baseAdapter.viewCommand?.(doc, command, view, cache.sheetController),
          onContextCommand:(command, node) => baseAdapter.contextCommand?.(doc, command, node, cache.sheetController),
          onOpenSource:openBaseSource,
          onClear:(cells) => baseAdapter.clear?.(doc, cells, cache.sheetController),
          onPaste:(preview) => baseAdapter.paste?.(doc, preview, cache.sheetController),
          onCopy:(text) => baseAdapter.copy?.(doc, text),
          onValidationError:(error) => context()?.window?.setStatus(error.message, true),
        });
        void cache.sheetController.refresh('mount');
      }
      const attachSheetBuffer = (resourceBuffer) => {
        if (!resourceBuffer || cache.sheetBuffer === resourceBuffer) return;
        cache.sheetBufferUnsubscribe?.();
        cache.sheetBuffer = resourceBuffer;
        cache.sheetBufferUnsubscribe = resourceBuffer.subscribe(() => {
          if (!cache.sheetController || cache.sheetBufferRefreshTimer) return;
          cache.sheetBufferRefreshTimer = setTimeout(() => { cache.sheetBufferRefreshTimer = null; void cache.sheetController.refresh('shared-buffer'); }, 0);
        });
      };
      if (cache.sheetBufferCreationDocId !== doc.id) {
        cache.sheetBufferCreationUnsubscribe?.();
        cache.sheetBufferCreationDocId = doc.id;
        cache.sheetBufferCreationUnsubscribe = subscribeToDocumentBuffer(doc.id, attachSheetBuffer);
      }
      attachSheetBuffer(context()?.noteBuffers?.get(doc.id));
      return;
    }
    // The canonical Base Editor renderer owns query, view configuration,
    // inline cell editing, summaries, and paging. The leaf keeps the selected
    // Base identity while reusing that component so legacy /bases links do
    // not fall back to a generic document editor.
    if (renderBaseEditor) { renderBaseEditor(doc, cache.body); return; }
    const token = (cache.queryToken || 0) + 1; cache.queryToken = token;
    cache.basePage ||= 1; cache.basePageSize ||= 100; cache.baseQuery ||= '';
    const queryInput = h('input', { class:'copal-base-query', type:'search', value:cache.baseQuery, placeholder:'Filter rows…', 'aria-label':'Filter Base rows' });
    const pageSize = h('select', { class:'copal-base-page-size', 'aria-label':'Base page size' });
    for (const size of [25, 50, 100, 200, 500]) pageSize.append(h('option', { value:String(size), text:`${size}/page`, selected:size === cache.basePageSize }));
    const host = h('div', { class:'copal-base-leaf' }, h('div', { class:'copal-empty', text:'Querying live Redb Base…' }));
    const refresh = () => { cache.baseQuery = queryInput.value.trim(); cache.basePage = 1; updateBaseLeaf(cache, doc); };
    queryInput.addEventListener('input', () => { clearTimeout(cache.baseQueryTimer); cache.baseQueryTimer = setTimeout(refresh, 180); });
    pageSize.addEventListener('change', () => { cache.basePageSize = Number(pageSize.value); cache.basePage = 1; updateBaseLeaf(cache, doc); });
    cache.body.replaceChildren(host);
    api(`/bases/${encodeURIComponent(doc.id)}/query?page=${cache.basePage}&page_size=${cache.basePageSize}&query=${encodeURIComponent(cache.baseQuery)}`).then((result) => {
      if (cache.queryToken !== token) return;
      const toolbar = h('div', { class:'copal-base-leaf-toolbar' }, h('strong', { text:result.view?.name || doc.name }), h('span', { text:`${result.total} live result${result.total === 1 ? '' : 's'}` }), queryInput, pageSize);
      if (!result.rows?.length) { host.replaceChildren(toolbar, h('div', { class:'copal-empty', text:'This live query returned no rows.' })); return; }
      const table = h('table', { class:'copal-table copal-base-table' });
      table.append(h('thead', {}, h('tr', {}, result.view.columns.map((column) => h('th', { text:column.label }))))) ;
      const body = h('tbody');
      for (const row of result.rows) body.append(h('tr', {}, result.view.columns.map((column) => h('td', {}, h('button', { class:'copal-base-cell', text:formatBaseCell(row.values?.[column.property]), onclick:() => open(row.documentId) })))));
      table.append(body);
      const pagination = h('nav', { class:'copal-base-pagination', 'aria-label':'Base result pages' },
        h('button', { class:'copal-btn', text:'Previous', disabled:result.page <= 1, onclick:() => { cache.basePage = Math.max(1, result.page - 1); updateBaseLeaf(cache, doc); } }),
        h('span', { text:`Page ${result.page} of ${result.pages}` }),
        h('button', { class:'copal-btn', text:'Next', disabled:result.page >= result.pages, onclick:() => { cache.basePage = Math.min(result.pages, result.page + 1); updateBaseLeaf(cache, doc); } }));
      host.replaceChildren(toolbar, h('div', { class:'copal-base-table-wrap' }, table), pagination);
    }).catch((error) => {
      if (cache.queryToken === token) host.replaceChildren(h('div', { class:'copal-empty' }, h('h2', { text:'Base query failed' }), h('p', { text:error.message }), h('p', { text:'Use Note actions → View raw source to repair the definition.' })));
    });
  }

  function updateAssetLeaf(cache, doc, view) {
    const url = `${state.api}/api/copal/assets/${encodeURIComponent(doc.id)}?workspace=${encodeURIComponent(state.workspace)}`;
    const host = h('div', { class:`copal-asset-view ${view}` });
    if (view === 'image') host.append(h('img', { src:url, alt:doc.name }));
    else if (view === 'audio') host.append(h('audio', { src:url, controls:true, 'aria-label':doc.name }));
    else if (view === 'video') host.append(h('video', { src:url, controls:true, 'aria-label':doc.name }));
    else if (view === 'pdf') host.append(h('iframe', { src:url, title:doc.name }));
    else host.append(h('div', { class:'copal-empty' }, h('p', { text:`No inline viewer for ${doc.name}.` }), h('a', { class:'copal-btn', href:url, download:doc.name, text:'Download attachment' })));
    cache.body.replaceChildren(host);
  }

  function updateLeafStatus(cache) {
    if (!cache?.status || !cache.doc) return;
    const buffer = context()?.noteBuffers?.get(cache.docId);
    const recoveryError = buffer?.state().recoveryError;
    const value = cache.editor?.getValue?.() ?? sourceValue(cache.doc);
    const selection = cache.editor?.getSelection?.();
    const selected = selection?.ranges?.reduce((total, range) => total + Math.abs(Number(range.head) - Number(range.anchor)), 0)
      ?? (selection ? Math.abs(selection.head - selection.anchor) : 0);
    const mode = cache.leaf?.mode === 'live' && ensureWorkspace().settings.previewLayout === 'side-by-side' ? 'Editing · side-by-side' : cache.leaf?.mode === 'live' ? cache.doc.kind === 'note' ? 'Editing' : 'Live Preview' : cache.leaf?.mode === 'source' ? 'Source' : cache.leaf?.mode === 'reading' ? 'Reading' : cache.view;
    cache.status.replaceChildren(
      h('span', { class:`copal-save-state ${cache.saveState}`, text:cache.saveState === 'saved' ? 'Saved' : cache.saveState === 'saving' ? 'Saving…' : cache.saveState === 'conflict' ? 'Conflict' : 'Unsaved' }),
      ...(recoveryError ? [h('span', { class:'copal-save-state error', text:buffer.state().dirty ? 'Restart recovery unavailable — save before closing' : 'Saved; old recovery draft could not be cleared', title:String(recoveryError.message || recoveryError) })] : []),
      h('span', { text:`${wordCount(value)} words` }), h('span', { text:`${value.length} characters` }),
      ...(cache.cursorLine ? [h('span', { text:`Ln ${cache.cursorLine}` })] : []), ...(selected ? [h('span', { text:`${selected} selected` })] : []), h('span', { text:`${mode} · ${cache.doc.kind === 'note' ? 'database record' : cache.doc.kind} · Redb` }));
  }

  function showFindReplace(editor) {
    const dialog = h('dialog', { class:'copal-dialog copal-find-replace' }, h('h2', { text:'Find and replace' }));
    const find = h('input', { type:'text', placeholder:'Find', 'aria-label':'Find' });
    const replacement = h('input', { type:'text', placeholder:'Replace', 'aria-label':'Replace' });
    const feedback = h('span', { role:'status' });
    dialog.append(find, replacement, h('div', { class:'copal-dialog-actions' },
      commandButton('Find next', () => { feedback.textContent = editor.find(find.value) ? '' : 'No match'; }),
      commandButton('Replace', () => { feedback.textContent = editor.replace(find.value, replacement.value) ? 'Replaced' : 'No match'; }),
      commandButton('Replace all', () => { feedback.textContent = `${editor.replace(find.value, replacement.value, true)} replaced`; }),
      commandButton('Close', () => dialog.close())), feedback);
    wireDialog(dialog); document.body.append(dialog); dialog.showModal(); find.focus();
  }

  function propertyInput(entry, type) {
    if (type === 'checkbox') { const input = h('input', { type:'checkbox' }); input.checked = entry.value === true; return input; }
    if (type === 'list' || type === 'tags') return h('input', { type:'text', value:Array.isArray(entry.value) ? entry.value.join(', ') : String(entry.value || '') });
    if (type === 'object') return h('input', { type:'text', value:JSON.stringify(entry.value || {}) });
    return h('input', { type:type === 'datetime' ? 'datetime-local' : type, value:entry.value == null ? '' : String(entry.value) });
  }

  // Inline properties card for the editor footer — compact, editable, WYSIWYG-integrated
  function buildInlineProps(doc) {
    const pane = h('div', { class:'copal-inline-props' });
    const native = doc.kind === 'note';
    const entries = native
      ? Object.entries(doc.properties || {})
      : (() => { const p = parseFrontmatter(sourceValue(doc)); return p.valid ? p.entries.map((e) => [e.key, e.value]) : []; })();
    if (!entries.length && doc.readOnly) {
      pane.append(h('span', { class:'copal-inline-props-empty', text:'No properties' }));
      return pane;
    }
    const commitNative = (pairs) => {
      doc.properties = Object.fromEntries(pairs);
      doc.frontmatter = doc.properties;
      queueSave(doc, sourceValue(doc));
      render();
    };
    // Render each property as an inline chip
    for (const [key, value] of entries) {
      const display = value == null ? '' : Array.isArray(value) ? value.join(', ') : typeof value === 'object' ? JSON.stringify(value) : String(value);
      const chip = h('span', { class:'copal-inline-props-chip', tabindex:'0' },
        h('strong', { text:key }),
        h('span', { text:display || '—' }));
      if (!doc.readOnly) {
        chip.addEventListener('dblclick', async () => {
          const newVal = await styledPrompt(`Edit ${key}.`, { title: 'Edit property', defaultValue: display, confirmText: 'Save', maxLength: 512 });
          if (newVal === null) return;
          if (native) {
            try { commitNative(entries.map(([k, v]) => [k, k === key ? newVal : v])); } catch (e) { context().window.setStatus(e.message, true); }
          } else {
            try { applyDocumentSource(doc, setFrontmatterProperty(sourceValue(doc), key, newVal)); render(); } catch (e) { context().window.setStatus(e.message, true); }
          }
        });
        const removeBtn = h('button', { class:'copal-inline-props-remove', text:'×', title:`Remove ${key}`, 'aria-label':`Remove ${key}`, onclick:() => {
          if (native) commitNative(entries.filter(([k]) => k !== key));
          else { applyDocumentSource(doc, removeFrontmatterProperty(sourceValue(doc), key)); render(); }
        } });
        chip.append(removeBtn);
      }
      pane.append(chip);
    }
    // Add property form
    if (!doc.readOnly) {
      const addBtn = h('button', { class:'copal-inline-props-add', text:'＋', title:'Add property', 'aria-label':'Add property' });
      addBtn.addEventListener('click', async () => {
        const key = await styledPrompt('Property name', { title: 'Add property', confirmText: 'Next', maxLength: 96 });
        if (!key?.trim()) return;
        const val = await styledPrompt('Property value', { title: `Add ${key.trim()}`, confirmText: 'Add', maxLength: 1024 });
        if (val === null) return;
        if (native) {
          try { commitNative([...entries, [key.trim(), val]]); } catch (e) { context().window.setStatus(e.message, true); }
        } else {
          try { applyDocumentSource(doc, setFrontmatterProperty(sourceValue(doc), key.trim(), val)); render(); } catch (e) { context().window.setStatus(e.message, true); }
        }
      });
      pane.append(addBtn);
    }
    return pane;
  }

  function propertiesPane(doc) {
    const pane = h('div', { class:'copal-properties-pane' });
    const native = doc.kind === 'note';
    const parsed = native
      ? { valid:true, entries:Object.entries(doc.properties || {}).map(([key, value]) => ({ key, value })) }
      : parseFrontmatter(sourceValue(doc));
    if (!parsed.valid) return h('div', { class:'copal-inspector-error' }, h('strong', { text:'Properties unavailable' }), h('p', { text:parsed.error }), h('p', { text:'Source is preserved. Repair the opening/closing --- markers in Source mode.' }));
    if (doc.readOnly) {
      pane.append(h('p', { class:'copal-empty-inline', text:'Built-in shared knowledge · read only' }));
      for (const entry of parsed.entries) pane.append(h('div', { class:'copal-property-editor' },
        h('strong', { text:entry.key }), h('span', { text:typeof entry.value === 'string' ? entry.value : JSON.stringify(entry.value) })));
      return pane;
    }
    const commitNative = (pairs) => {
      const keys = pairs.map(([key]) => key);
      if (keys.some((key) => !/^[A-Za-z0-9_.-]+$/.test(key))) throw new Error('Property names may contain letters, numbers, dots, dashes, and underscores.');
      if (new Set(keys).size !== keys.length) throw new Error('Property names must be unique.');
      doc.properties = Object.fromEntries(pairs);
      doc.frontmatter = doc.properties;
      queueSave(doc, sourceValue(doc));
      render();
    };
    for (const entry of parsed.entries) {
      const type = native && entry.value && typeof entry.value === 'object' && !Array.isArray(entry.value) ? 'object' : propertyType(entry.value, entry.key);
      const keyInput = h('input', { class:'copal-property-key', value:entry.key, 'aria-label':`Property name ${entry.key}` });
      keyInput.addEventListener('change', () => {
        try {
          if (native) commitNative(Object.entries(doc.properties || {}).map(([key, value]) => [key === entry.key ? keyInput.value.trim() : key, value]));
          else { applyDocumentSource(doc, renameFrontmatterProperty(sourceValue(doc), entry.key, keyInput.value.trim())); render(); }
        }
        catch (error) { keyInput.value = entry.key; context().window.setStatus(error.message, true); }
      });
      const row = h('div', { class:'copal-property-editor' }, keyInput);
      if (type === 'source') {
        row.append(h('span', { text:'Complex value—edit in Source mode.' }));
      } else {
        const select = h('select', { 'aria-label':`Type for ${entry.key}` }, PROPERTY_TYPES.map((item) => h('option', { value:item, text:item })));
        select.value = type;
        let input = propertyInput(entry, type);
        const commit = () => {
          try {
            const nextType = select.value;
            const value = nextType === 'checkbox' ? input.checked : input.value;
            if (native) commitNative(Object.entries(doc.properties || {}).map(([key, current]) => [key, key === entry.key ? coercePropertyValue(value, nextType) : current]));
            else { const content = setFrontmatterProperty(sourceValue(doc), entry.key, value, nextType); applyDocumentSource(doc, content); render(); }
          } catch (error) {
            context().window.setStatus(error.message, true);
          }
        };
        input.setAttribute('aria-label', `Value for ${entry.key}`); input.addEventListener('change', commit);
        select.addEventListener('change', commit);
        row.append(select, input);
      }
      const move = (direction) => {
        if (!native) { applyDocumentSource(doc, moveFrontmatterProperty(sourceValue(doc), entry.key, direction)); render(); return; }
        const pairs = Object.entries(doc.properties || {}); const index = pairs.findIndex(([key]) => key === entry.key); const target = index + direction;
        if (index < 0 || target < 0 || target >= pairs.length) return;
        [pairs[index], pairs[target]] = [pairs[target], pairs[index]]; commitNative(pairs);
      };
      row.append(commandButton('↑', () => move(-1), { title:`Move ${entry.key} up`, 'aria-label':`Move ${entry.key} up` }));
      row.append(commandButton('↓', () => move(1), { title:`Move ${entry.key} down`, 'aria-label':`Move ${entry.key} down` }));
      row.append(commandButton('×', () => {
        if (native) commitNative(Object.entries(doc.properties || {}).filter(([key]) => key !== entry.key));
        else { const content = removeFrontmatterProperty(sourceValue(doc), entry.key); applyDocumentSource(doc, content); render(); }
      }, { title:`Remove ${entry.key}`, 'aria-label':`Remove ${entry.key}` }));
      pane.append(row);
    }
    const add = h('form', { class:'copal-property-add' });
    const key = h('input', { placeholder:'property', 'aria-label':'New property name' });
    const value = h('input', { placeholder:'value', 'aria-label':'New property value' });
    const type = h('select', { 'aria-label':'New property type' }, PROPERTY_TYPES.map((item) => h('option', { value:item, text:item })));
    add.append(key, type, value, commandButton('Add', () => {}, { type:'submit' }));
    add.addEventListener('submit', async (event) => {
      event.preventDefault(); if (!key.value.trim()) return;
      try {
        if (native) commitNative([...Object.entries(doc.properties || {}), [key.value.trim(), coercePropertyValue(value.value, type.value)]]);
        else { const content = setFrontmatterProperty(sourceValue(doc), key.value.trim(), value.value, type.value); applyDocumentSource(doc, content); render(); }
      } catch (error) { context().window.setStatus(error.message, true); }
    });
    pane.append(add);
    if (!parsed.entries.length) pane.prepend(h('p', { class:'copal-empty-inline', text:'No properties yet.' }));
    return pane;
  }

  function linksPane(doc) {
    const pane = h('div', { class:'copal-links-pane' });
    const filter = h('input', { class:'copal-links-filter', type:'search', placeholder:'Filter links…', 'aria-label':'Filter linked views' });
    const sort = h('select', { class:'copal-links-sort', 'aria-label':'Sort linked views' }, h('option', { value:'name', text:'Name' }), h('option', { value:'path', text:'Path' }));
    pane.append(h('div', { class:'copal-links-controls' }, filter, sort));
    const section = (title) => { const root = h('section', {}, h('h3', { text:title })); pane.append(root); return root; };
    const outgoing = section('Outgoing links');
    const relations = doc.kind === 'note'
      ? (doc.relations || []).filter((relation) => ['link', 'embed'].includes(relation.kind))
      : (doc.links || []).map((target) => ({ kind:'link', target }));
    for (const relation of [...relations].sort((a, b) => a.target.localeCompare(b.target))) {
      const target = state.docs.find((candidate) => candidate.id === relation.targetDocumentId) || resolveDocumentLink(state.docs, relation.target);
      const label = relation.kind === 'embed' ? `Embed · ${relation.target}` : relation.target;
      outgoing.append(h('button', { class:'copal-doc-row copal-link-result', 'data-sort-name':target ? displayName(target) : relation.target, 'data-sort-path':target?.name || relation.target, disabled:!target, onclick:() => target && open(target.id) }, h('strong', { text:target ? label : `${label} · unresolved` }), target ? h('small', { text:target.name }) : null));
    }
    if (!relations.length) outgoing.append(h('p', { class:'copal-empty-inline', text:'No outgoing links.' }));
    const backlinks = section('Linked mentions');
    const incoming = linkedMentions(state.docs, doc);
    for (const mention of incoming) backlinks.append(h('button', { class:'copal-doc-row copal-link-result', 'data-sort-name':displayName(mention.doc), 'data-sort-path':mention.doc.name, onclick:() => {
      const leaf = open(mention.doc.id); if (mention.line) requestAnimationFrame(() => context().noteLeafViews.get(leaf?.id)?.editor?.focusLine(mention.line));
    } }, h('strong', { text:mention.doc.name }), h('small', { text:mention.snippet })));
    if (!incoming.length) backlinks.append(h('p', { class:'copal-empty-inline', text:'No linked mentions.' }));
    const unlinked = section('Unlinked mentions');
    for (const mention of unlinkedMentions(state.docs, doc)) unlinked.append(h('button', { class:'copal-mention-row copal-link-result', 'data-sort-name':displayName(mention.doc), 'data-sort-path':mention.doc.name, onclick:() => open(mention.doc.id) }, h('strong', { text:mention.doc.name }), h('span', { text:mention.snippet })));
    if (unlinked.children.length === 1) unlinked.append(h('p', { class:'copal-empty-inline', text:'No unlinked mentions.' }));
    const refresh = () => {
      const query = filter.value.trim().toLowerCase();
      for (const result of pane.querySelectorAll('.copal-link-result')) result.hidden = !!query && !result.textContent.toLowerCase().includes(query);
      for (const root of pane.querySelectorAll('section')) {
        const rows = [...root.querySelectorAll('.copal-link-result')].sort((a, b) => String(a.dataset[sort.value === 'path' ? 'sortPath' : 'sortName']).localeCompare(String(b.dataset[sort.value === 'path' ? 'sortPath' : 'sortName'])));
        root.append(...rows);
      }
    };
    filter.addEventListener('input', refresh); sort.addEventListener('change', refresh);
    return pane;
  }

  function focusOutline(leaf, entry) {
    const cache = context().noteLeafViews.get(leaf.id);
    if (leaf.mode === 'reading') {
      cache?.reading?.querySelector(`[data-line="${entry.line}"]`)?.scrollIntoView({ block:'center' });
      return;
    }
    cache?.editor?.focusLine(entry.line);
  }

  function focusSourceLine(docId, line) {
    const workspace = ensureWorkspace();
    const leaf = activeLeaf(workspace);
    if (!leaf || leaf.docId !== docId) return false;
    const current = context();
    requestAnimationFrame(() => { if (current === context() && activeLeaf(workspace)?.id === leaf.id) focusOutline(leaf, { line }); });
    return true;
  }

  function outlinePane(doc, workspace) {
    const pane = h('div', { class:'copal-outline-pane' });
    const entries = outlineEntries(sourceValue(doc));
    const leaf = workspaceLeaves(workspace).find((item) => item.docId === doc.id) || activeLeaf(workspace);
    const currentEntry = (entry, source) => {
      const current = outlineEntries(source);
      return current.find((item) => item.line === entry.line && item.text === entry.text)
        || current.filter((item) => item.level === entry.level && item.text === entry.text).sort((a, b) => Math.abs(a.line - entry.line) - Math.abs(b.line - entry.line))[0]
        || null;
    };
    const move = (entry, direction) => {
      const source = sourceValue(doc); const actual = currentEntry(entry, source); if (!actual) return;
      const content = moveHeadingSection(source, actual.line, direction);
      if (content === source) return;
      applyDocumentSource(doc, content); render();
    };
    const moveTo = (entry, target) => {
      const source = sourceValue(doc); const actual = currentEntry(entry, source); const actualTarget = currentEntry(target, source);
      if (!actual || !actualTarget) return;
      const content = moveHeadingSectionTo(source, actual.line, actualTarget.line);
      if (content === source) return;
      applyDocumentSource(doc, content); render();
    };
    for (const entry of entries) {
      const row = h('div', { class:'copal-outline-entry', draggable:'true', style:`--depth:${entry.level}`, 'data-line':entry.line },
        h('button', { class:'copal-outline-row', text:entry.text, onclick:() => focusOutline(leaf, entry) }),
        commandButton('↑', () => move(entry, -1), { 'aria-label':`Move ${entry.text} up` }),
        commandButton('↓', () => move(entry, 1), { 'aria-label':`Move ${entry.text} down` }));
      row.addEventListener('dragstart', (event) => event.dataTransfer.setData('text/x-copal-heading-line', String(entry.line)));
      row.addEventListener('dragover', (event) => event.preventDefault());
      row.addEventListener('drop', (event) => { event.preventDefault(); const from = Number(event.dataTransfer.getData('text/x-copal-heading-line')); const source = entries.find((item) => item.line === from); if (source && source.line !== entry.line) moveTo(source, entry); });
      pane.append(row);
    }
    if (!entries.length) pane.append(h('p', { class:'copal-empty-inline', text:'No headings.' }));
    return pane;
  }

  function appendNavigationPanel(body, tab, workspace) {
    const button = (doc) => h('button', { class:'copal-doc-row', 'data-note-tree-key':`document:${doc.id}`, text:doc.name, onclick:() => open(doc.id) });
    if (tab === 'tags') {
      const tagged = new Map();
      for (const doc of explorerDocs()) for (const tag of doc.tags || []) {
        const name = String(tag).replace(/^#/, '');
        if (!tagged.has(name)) tagged.set(name, []);
        if (!tagged.get(name).includes(doc)) tagged.get(name).push(doc);
      }
      const expanded = context().noteTagGroups ||= new Set();
      body.append(h('header', {}, h('strong', { text:'Tags' })));
      for (const [tag, matches] of [...tagged].sort(([a], [b]) => a.localeCompare(b))) {
        const section = h('details', { class:'copal-tag-group', open:expanded.has(tag) }, h('summary', { 'data-note-tree-key':`tag:${tag}`, text:`#${tag} · ${matches.length}` }));
        section.addEventListener('toggle', () => { if (section.isConnected) section.open ? expanded.add(tag) : expanded.delete(tag); });
        section.append(...matches.map(button)); body.append(section);
      }
      if (!tagged.size) body.append(h('p', { class:'copal-empty-inline', text:'No tags yet.' }));
      return;
    }
    const bookmarks = tab === 'bookmarks';
    const unique = [...new Set(bookmarks ? workspace.bookmarks : workspace.recent)].map(id => state.docs.find(doc => doc.id === id)).filter(Boolean);
    body.append(h('header', {}, h('strong', { text:bookmarks ? 'Bookmarks' : 'Recent documents' })), ...unique.map(button));
    if (!unique.length) body.append(h('p', { class:'copal-empty-inline', text:bookmarks ? 'Bookmark a document from its actions menu.' : 'No recent documents.' }));
  }

  function rightSidebar(workspace, shellState) {
    const doc = inspectorDoc(workspace);
    const aside = h('aside', { id:'copal-notes-right-sidebar', class:'copal-notes-sidebar' });
    aside.style.setProperty('--copal-pane-width', `${workspace.right.width}px`);
    const tabs = h('div', { class:'copal-inspector-tabs', role:'tablist' });
    const panelIds = workspacePanelsForSide(workspace, 'right');
    for (const key of panelIds) {
      const def = NOTES_PANELS[key];
      if (!def) continue;
      tabs.append(h('button', { class:workspace.right.tab === key ? 'active' : '', role:'tab', 'aria-selected':String(workspace.right.tab === key), text:def.label, 'data-panel-id':key, onclick:() => { workspace.right.tab = key; persist(true); render(); } }));
    }
    addRovingFocus(tabs, panelIds, (id) => { workspace.right.tab = id; persist(true); render(); });
    const pin = commandButton(workspace.right.pinnedDocId ? 'Unpin' : 'Pin', () => { workspace.right.pinnedDocId = workspace.right.pinnedDocId ? null : activeLeaf(workspace)?.docId || null; persist(true); render(); }, { 'aria-pressed':String(!!workspace.right.pinnedDocId) });
    const body = h('div', { class:'copal-inspector-body', 'data-note-panel':workspace.right.tab });
    if (['tags', 'bookmarks', 'recent'].includes(workspace.right.tab)) appendNavigationPanel(body, workspace.right.tab, workspace);
    else if (!doc) body.append(h('p', { class:'copal-empty-inline', text:'No active document.' }));
    else if (doc.virtual) body.append(h('p', { class:'copal-empty-inline', text:'Timeline is a canonical database view. Select a note to inspect properties, links, or outline.' }));
    else if (workspace.right.tab === 'properties') body.append(propertiesPane(doc));
    else if (workspace.right.tab === 'links') body.append(linksPane(doc));
    else if (workspace.right.tab === 'outline') body.append(outlinePane(doc, workspace));
    aside.append(h('header', { class:'copal-shell-side-header right' }, tabs, pin,
      ...(shellState.compact ? [] : [shellState.controls.right])), body, resizeHandle('right', workspace));
    return aside;
  }

  function ribbon(workspace) {
    return h('nav', { class:'copal-notes-ribbon', 'aria-label':'Editor actions' },
      h('button', { text:'＋', title:'New note', 'aria-label':'New note', onclick:() => createNew() }),
      h('button', { text:'↔', title:'Open Timeline', 'aria-label':'Open Timeline', onclick:() => open(TIMELINE_DOCUMENT.id) }),
      h('button', { text:'⌕', title:'Quick switcher', 'aria-label':'Quick switcher', onclick:() => showChooser() }),
      h('button', { text:'⌘', title:'Command palette', 'aria-label':'Command palette', onclick:showCommands }));
  }

  function render() {
    const started = performance.now();
    activateNotes?.();
    const current = context();
    const workspace = ensureWorkspace();
    if (!current || !workspace || !current.window.body) return;
    current.notePanelPositions = capturePanelPositions(current.window.body, current.notePanelPositions);
    const docs = documents();
    const workspaceDocs = workspaceDocuments();
    const leaf = activeLeaf(workspace);
    const doc = leaf ? workspaceDocs.find((item) => item.id === leaf.docId) || null : null;
    current.selected = doc?.id || null;
    state.selected = current.selected;
    persistActiveContext();
    bindKeys(workspace, doc);
    if (!current.notePageHideHandler) {
      current.notePageHideHandler = () => { persist(true); void flushAll(); };
      window.addEventListener('pagehide', current.notePageHideHandler);
    }
    if (!current.noteShellMedia) {
      current.noteShellMedia = [NOTES_NARROW_QUERY, NOTES_COMPACT_QUERY].map((value) => {
        const query = window.matchMedia(value);
        const handler = () => { current.noteDrawer = null; render(); };
        query.addEventListener('change', handler);
        return { query, handler };
      });
    }

    const viewport = shellViewport();
    current.noteDrawer ||= null;
    if (!viewport.compact) current.noteDrawer = null;
    if (!viewport.narrow && current.noteDrawer === 'left') current.noteDrawer = null;
    if (current.noteDrawer && !current.noteDrawerRelease) {
      current.noteDrawerRelease = registerMenuDismiss(() => {
        current.noteDrawerRelease = null;
        current.noteDrawer = null;
        render();
      });
    } else if (!current.noteDrawer && current.noteDrawerRelease) {
      current.noteDrawerRelease();
      current.noteDrawerRelease = null;
    }
    // A large explorer is expensive to reconstruct. Keep the complete shell
    // attached for no-op renders; workspace, document-array identity, and the
    // local source revision make the cache invalid whenever visible state can
    // have changed. This preserves focus and live editor nodes while avoiding
    // repeated 5k-entry DOM construction during warm interaction cycles.
    const renderCacheKey = JSON.stringify([
      currentScope(current),
      serializeNotesWorkspace(workspace),
      current.selected,
      current.noteDrawer || null,
      viewport.narrow,
      viewport.compact,
      current.noteRenderVersion || 0,
    ]);
    const cached = current.noteShellCache;
    if (cached && cached.key === renderCacheKey && cached.docs === state.docs && cached.shell?.isConnected) {
      current.noteMetrics.renders += 1;
      current.noteMetrics.lastRenderMs = performance.now() - started;
      cached.shell.dataset.renderMs = current.noteMetrics.lastRenderMs.toFixed(2);
      return;
    }
    const controls = ensureShellControls();
    for (const button of Object.values(controls)) {
      for (const animation of button.getAnimations()) animation.cancel();
      button.style.pointerEvents = '';
    }
    const before = Object.fromEntries(Object.entries(controls).map(([side, button]) => [side, button.isConnected ? button.getBoundingClientRect() : null]));
    const focusedSide = document.activeElement === controls.left ? 'left' : document.activeElement === controls.right ? 'right' : null;
    const leftVisible = viewport.narrow ? current.noteDrawer === 'left' : workspace.left.open;
    const rightVisible = viewport.compact ? current.noteDrawer === 'right' : workspace.right.open;
    const controlGroupId = groupForLeaf(workspace, workspace.activeLeafId)?.id || workspaceGroups(workspace)[0]?.id || null;
    const shellState = { ...viewport, controls, controlGroupId };
    syncShellControl(controls.left, 'left', leftVisible, !viewport.narrow && leftVisible ? 'sidebar' : 'tabs');
    syncShellControl(controls.right, 'right', rightVisible, !viewport.compact && rightVisible ? 'sidebar' : 'tabs');

    const shell = h('div', {
      class:`copal-notes-workspace${leftVisible ? '' : ' left-closed'}${rightVisible ? '' : ' right-closed'}${workspace.settings.ribbon ? ' ribbon-open' : ''}${viewport.narrow ? ' narrow' : viewport.compact ? ' compact' : ''}`,
      style:`--copal-right-width:${workspace.right.width}px`,
      'data-preview-layout':workspace.settings.previewLayout,
      'data-editor-constructions':current.noteMetrics.editorConstructions,
      'data-drawer':current.noteDrawer || 'none',
    });
    if (workspace.settings.ribbon) shell.append(ribbon(workspace));
    if (leftVisible) shell.append(leftSidebar(workspace, docs, shellState));
    const main = h('main', { class:'copal-notes-main' }, renderNode(workspace.root, workspace, workspaceDocs, shellState));
    shell.append(main);
    if (rightVisible) shell.append(rightSidebar(workspace, shellState));
    if (current.noteDrawer) shell.append(h('button', { class:'copal-shell-scrim', type:'button', 'aria-label':'Close Editor sidebar', onclick:() => { current.noteDrawer = null; render(); } }));

    const validLeaves = new Set(workspaceLeaves(workspace).map((item) => item.id));
    for (const leafId of [...current.noteLeafViews.keys()]) if (!validLeaves.has(leafId)) disposeLeaf(leafId);
    const previousFocus = current.window.body.contains(document.activeElement) ? document.activeElement : null;
    current.window.body.querySelectorAll('.copal-sidebar-resize').forEach(handle => handle._copalResizeCleanup?.());
    current.window.body.replaceChildren(shell);
    restorePanelPositions(current.window.body, current.notePanelPositions);
    if (previousFocus?.isConnected) {
      const editorHost = previousFocus.closest?.('.cm-editor');
      const editorCache = editorHost
        ? [...current.noteLeafViews.values()].find((cache) => cache.editor && cache.root.contains(editorHost))
        : null;
      // A reattached CodeMirror surface must be refocused through the view so
      // its input binding (EditContext) re-attaches; a raw element focus on an
      // already-active node is a no-op and leaves typing dead until blur.
      if (editorCache) { previousFocus.blur?.(); editorCache.editor.focus(); }
      else previousFocus.focus({ preventScroll:true });
    }
    finishShellControlMove(controls, before, focusedSide);
    current.noteShellCache = { key:renderCacheKey, docs:state.docs, shell };
    current.noteMetrics.renders += 1;
    current.noteMetrics.lastRenderMs = performance.now() - started;
    shell.dataset.renderMs = current.noteMetrics.lastRenderMs.toFixed(2);
    shell.dataset.editorConstructions = String(current.noteMetrics.editorConstructions);
    persist();
  }

  function loadSaved(current, saved) {
    current.noteSaved = saved && typeof saved === 'object' ? saved : {};
  }

  function getNotesPanels() {
    const workspace = ensureWorkspace();
    return Object.entries(NOTES_PANELS).map(([id, def]) => ({ id, label:def.label, allowedSides:[...def.allowedSides], side:workspace?.panels?.[id]?.side || def.defaultSide, order:workspace?.panels?.[id]?.order ?? def.defaultOrder, hidden:workspace?.panels?.[id]?.hidden === true }));
  }

  function updateNotesPanel(id, patch = {}) {
    const workspace = ensureWorkspace();
    const def = NOTES_PANELS[id];
    if (!workspace || !def || !workspace.panels?.[id]) throw new Error('Unknown Editor panel');
    const current = workspace.panels[id];
    if (patch.side && !def.allowedSides.includes(patch.side)) throw new Error('Panel side is not allowed');
    if (patch.side && patch.side !== current.side) {
      const destination = Object.entries(workspace.panels).filter(([key, item]) => key !== id && item.side === patch.side).map(([, item]) => item.order);
      current.side = patch.side; current.order = destination.length ? Math.max(...destination) + 1 : 0;
    }
    if (typeof patch.hidden === 'boolean') current.hidden = patch.hidden;
    if (!current.hidden && (patch.move === 'up' || patch.move === 'down')) {
      const peers = Object.entries(workspace.panels).filter(([key, item]) => key !== id && item.side === current.side && !item.hidden && !['files', 'search'].includes(key)).sort(([, a], [, b]) => a.order - b.order);
      const index = peers.findIndex(([key]) => key === id);
      const ordered = [...peers, [id, current]].sort(([, a], [, b]) => a.order - b.order);
      const own = ordered.findIndex(([key]) => key === id);
      const target = patch.move === 'up' ? own - 1 : own + 1;
      if (target >= 0 && target < ordered.length) [ordered[own][1].order, ordered[target][1].order] = [ordered[target][1].order, ordered[own][1].order];
    }
    // One normalization/persist/render pass. Locked panels remain left and
    // visible; active tabs fall back when their panel was hidden/moved.
    for (const [key, item] of Object.entries(workspace.panels)) if (['files', 'search'].includes(key)) { item.side = 'left'; item.hidden = false; }
    for (const side of ['left', 'right']) Object.entries(workspace.panels).filter(([, item]) => item.side === side).sort(([, a], [, b]) => a.order - b.order).forEach(([, item], index) => { item.order = index; });
    const leftVisible = workspacePanelsForSide(workspace, 'left'); const rightVisible = workspacePanelsForSide(workspace, 'right');
    if (!leftVisible.includes(workspace.left.tab)) workspace.left.tab = leftVisible[0] || null;
    if (!rightVisible.includes(workspace.right.tab)) workspace.right.tab = rightVisible[0] || null;
    persist(true); render();
    return getNotesPanels();
  }

  // Files uses this single exact-open seam for Open in Editor and Files row
  // actions.  The callback still reopens through Files-v1, preserving one
  // ResourceHandle/buffer identity and current CAS revision.
  globalThis.__openClankOpenResourceHandle = resourceOpener;

  return {
    render, open, openResource, destroy, flushAll, queueSave, queueDocumentSave, suspendScope, loadSaved, showChooser, showCommands, showSearch, showSettings, insertTemplate, createTemplateFromCurrent, createFromTemplate, openDailyNote,
    acceptSavedDocument, getDraftSnapshot, getAuthoritativeSnapshot, applyDocumentTransaction, rotateResourceRef, retrySaveAtRevision, retryDocumentSave, documentRetryState, rebaseDocumentAtRevision, projectDocument, prepareDelete, getSettings, updateSettings, getNotesPanels, updateNotesPanel, focusSourceLine, flushDocument:saveDraft,
    subscribeToDocumentBuffer, invalidateBaseLeaves,
    mergeTemplateProperties,
    toggleLeft:() => toggleSidebar('left'),
    toggleRight:() => toggleSidebar('right'),
  };
}
