import { createExplorerLayout, showExplorerCustomization, createExplorerDivider } from '../editor/explorerLayout.js';
import { dispatchFilesDestination, isChatResource, editorDestinationReason } from './resourceDestinations.js';
import { recordPresentation, acknowledgeVisible, achievementOwner } from '../achievementProducer.js';
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
import { wireDialog, wirePopover as wireBasePopover } from './overlays.js';
import { LANGUAGE_REGISTRY } from '../editor/languageRegistry.js';
import { copalStorageKey } from './storage.js';
import { registerMenuDismiss } from '../escMenuStack.js';
import { parseTable, createTableWidget, applyTableEdit, evaluateFormula } from './tableModel.js';
import { createBufferRegistry } from './documentBuffers.js';
import { createSheetController } from './sheetController.js';
import { mountSheet } from './sheetView.js';
import { cloneEnvelope, normalizeResourceHandle, sameResourceKey, snapshotEnvelope } from './resourceModel.js';
import { createSaveActionId, sameSaveScope } from './documentSave.js';
import { capturePanelPositions, restorePanelPositions } from './panelPosition.js';
import { createCodeMirrorContextAdapter, registerAdapter } from '../custom-context-menu.js';
import { languageForPath, languageDialectForPath } from '../editor/entryModel.js';
// Keep model/save imports usable in Node renderers.  The prompt primitive
// touches the DOM only when invoked, while ui.js initializes the full shell.
import { styledPrompt, styledConfirm } from '../dialogPrimitives.js';
import { filesFacadeClient } from '../filesFacadeClient.js';
import { createResourcePicker, normalizeAuthorizedResource } from './resourcePicker.js';
import { filesBrowserResource, installEditorFilesStyles, mountEditorFilesBrowser, runFilesNavigation } from './editorFilesBrowser.js';
import { fileIcon, uiIcon } from '../langIcons.js';
import {
  expandTemplate as expandTemplateModel,
  formatTemplateDate as formatTemplateDateModel,
  normalizeTemplateFolderSelection,
  createInsertionDescriptors,
} from './templateModel.js';
import { FILES_TRANSFER_MIME, parseInternalDragPayload, validateDropTarget, resourceKey, scopeKey } from '../filesSelectionModel.js';
import { createWindowNavigation } from './navigation.js';
import { isOfficialDocument } from './graphModel.js';
import { openResourceHistory } from '../historyView.js';
import { visibleWindowBounds } from '../windowResize.js';

// Existing dismissal/keyboard ownership stays in overlays; only presentation
// is clamped to the owning applet when its local action menu opens.
function wirePopover(details) {
  wireBasePopover(details);
  details.addEventListener('toggle', () => {
    if (!details.open || !details.isConnected) return;
    const menu = details.querySelector('.copal-popover-menu');
    if (!menu) return;
    const bounds = visibleWindowBounds(details);
    const anchor = details.querySelector('summary').getBoundingClientRect();
    const left = bounds.left + 8, top = bounds.top + 8;
    const right = Math.max(left, bounds.right - 8), bottom = Math.max(top, bounds.bottom - 8);
    menu.style.position = 'fixed'; menu.style.right = 'auto';
    menu.style.maxWidth = `${Math.max(1, Math.min(280, right - left))}px`;
    menu.style.maxHeight = `${Math.max(1, bottom - top)}px`;
    menu.style.left = `${Math.max(left, Math.min(anchor.right - menu.offsetWidth, right - menu.offsetWidth))}px`;
    menu.style.top = `${Math.max(top, Math.min(anchor.bottom + 3, bottom - menu.offsetHeight))}px`;
  });
  return details;
}
const SELECTABLE_LANGUAGES = LANGUAGE_REGISTRY.filter(entry => entry.selectable).sort((a, b) => (a.modeName || a.displayName).localeCompare(b.modeName || b.displayName));

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
  // compatibility key for restored legacy state; it is revalidated through
  // the provider's exact authorized flow before use.
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

function isHostDocument(doc) {
  return doc?.sourceKind === 'host' || (doc?.resource?.key || doc?.resourceKey)?.provider === 'host';
}

function canManageCopalDocument(doc) {
  // Content write authority does not grant Copal's note rename/trash adapter
  // authority over a Host file. Host listing actions use the Files facade.
  return Boolean(doc && !doc.readOnly && !doc.virtual && !isHostDocument(doc));
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
  h, api, state, createMarkdownEditor, createSourceEditor = createMarkdownEditor, renderMarkdown, renderPreview = null, renderComment = null, formatBaseCell,
  saveDocument, renameNote, deleteDocument, showHistory, showTrash, showForm,
  importVault, loadDocuments, openDocument:openOtherView, persistActiveContext, deleteDocuments,
  activateNotes, renderTimeline, openEventEditor, renderBaseEditor = null, baseAdapter = null, resourceBufferRegistry = null, saveResource = null, uploadAttachment = null, commitAttachment = null, abortAttachment = null,
  makeEditableWikiCopy = null, canCopyWikiArticle = null, getContext = null, registerGlobalOpener = true,
  presentationId = 'notes', getSharedDocumentState = null, onSourceChanged = null, onSaveStateChanged = null, afterRender = null,
  createWikiArticle = null, importWikiMemes = null, exportWikiMemes = null,
}) {
  let persistTimer = null;
  const buffers = resourceBufferRegistry || createBufferRegistry();
  const previousResourceOpener = globalThis.__openClankOpenResourceHandle;
  const resourceOpener = async (input = {}) => {
    const resourceRef = String(input.resourceRef || input.resource_ref || input.ref || '').trim();
    if (!resourceRef) throw new TypeError('Files resource reference is required');
    const owner = input.origin?.current || context();
    const scope = currentScope(owner);
    if (input.signal?.aborted || input.isCurrent && !input.isCurrent() || input.origin && !pickerOriginCurrent(input.origin)) throw new Error('The original Editor pane is unavailable. Open the file again.');
    const generation = state.filesGeneration;
    const activation = owner ? (owner.noteActivationSequence = Number(owner.noteActivationSequence || 0) + 1) : 0;
    const response = await filesFacadeClient.openResource(resourceRef, { signal:input.signal || null });
    if (owner !== context() || generation !== state.filesGeneration || scope !== currentScope()
      || input.signal?.aborted || input.isCurrent && !input.isCurrent() || input.origin && !pickerOriginCurrent(input.origin)) throw new Error('The active account or workspace changed. Open the file again.');
    return dispatchFilesDestination(response, {
      resourceRef, surface:'editor', selectionOnly:input.selectionOnly === true,
      openEditor:(resource, payload) => {
        const handle = normalizeResourceHandle(resource);
        return openResource(handle, {
          ...payload, name:payload.name || input.name || handle.locator.displayName,
          text:payload.text ?? payload.content ?? '',
          parent_resource_ref:payload.parent_resource_ref || input.parentResourceRef || null,
          ...(input.intent ? { intent:input.intent } : {}),
          ...(input.origin?.groupId ? { groupId:input.origin.groupId } : {}),
        }, { activate:owner?.noteActivationSequence === activation });
      },
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
    return typeof getContext === 'function' ? getContext() : state.windows.get('notes');
  }

  function normalizedHostRelativePath(value) {
    if (typeof value !== 'string') return null;
    const relative = value;
    if (relative.length > 4096 || relative.startsWith('/') || /^[A-Za-z]:/.test(relative) || relative.includes('\\')) return null;
    if (!relative) return '';
    const parts = relative.split('/');
    if (parts.some((part) => !part || part === '.' || part === '..' || /[\\\0]/.test(part))) return null;
    return parts.join('/');
  }

  function savedHostRelativePath(resource) {
    if (!resource || resource.provider !== 'host') return { present:false, path:'' };
    if (!Object.hasOwn(resource, 'workspaceRelativePath')) return { present:false, path:'' };
    const path = normalizedHostRelativePath(resource.workspaceRelativePath);
    if (path == null) throw new Error('Saved Host folder locator is invalid. Reopen it from Open Folder.');
    return { present:true, path };
  }

  function withHostRelativePath(resource, relativePath = '') {
    if (!resource || resource.provider !== 'host') return resource;
    const path = normalizedHostRelativePath(relativePath);
    if (path == null) throw new Error('Host folder locator is invalid.');
    return { ...resource, workspaceRelativePath:path };
  }

  function withHostChildRelativePath(parent, resource, rawName = resource?.name) {
    if (!parent || !resource || parent.provider !== 'host' || resource.provider !== 'host') return resource;
    const locator = savedHostRelativePath(parent);
    if (!locator.present) return resource;
    const parentPath = locator.path;
    const name = typeof rawName === 'string' ? rawName : String(resource.name || '');
    if (!name || /[\\/\0]/.test(name) || name === '.' || name === '..') return resource;
    return { ...withHostRelativePath(resource, parentPath ? `${parentPath}/${name}` : name), name };
  }

  function hostParentNavigationTarget(resource) {
    const locator = savedHostRelativePath(resource);
    if (!locator.present || !locator.path) return null;
    const parts = locator.path.split('/');
    parts.pop();
    return { provider:'host', workspaceRelativePath:parts.join('/') };
  }

  function isHostLocationsRoot(resource) {
    const kind = String(resource?.kind ?? resource?.resource?.kind ?? '').trim().toLowerCase();
    const provider = String(resource?.provider ?? resource?.resource?.provider ?? '').trim().toLowerCase();
    return provider === 'host' && kind === 'provider_root';
  }

  function revalidateSavedResourceRoot(workspace) {
    const current = context();
    const root = workspace?.left?.resourceRoot || workspace?.left?.folderWorkspaceRoot;
    const savedRoot = workspace?.left?.resourceRoot;
    const savedAnchor = workspace?.left?.folderWorkspaceRoot;
    const savedWorkspaceId = workspace?.left?.folderWorkspaceId;
    const requestEpoch = Number(current?.noteResourceRequestEpoch || 0);
    const stableKey = canonicalEditorResourceKey(root?.resourceKey || root?.ref || '', root?.provider || 'unknown');
    const scopeToken = currentScope();
    const generation = Number(state.filesGeneration || state.contextEpoch || 0);
    const validationKey = `${scopeToken}:${generation}:${stableKey}`;
    const sameValidationKey = current?.noteResourceRootValidationKey === validationKey;
    const sameInFlightValidation = sameValidationKey
      && current.noteResourceRootValidationController
      && current.noteResourceRootValidationWorkspace === workspace;
    if (!current || current.noteWorkspace !== workspace || !root?.ref) return Promise.resolve(false);
    if (sameInFlightValidation) return current.noteResourceRootValidationPromise || Promise.resolve(false);
    if (sameValidationKey && current.noteResourceRootReady === true) return Promise.resolve(true);
    current.noteResourceRootValidationController?.abort?.();
    const controller = new AbortController();
    const validationEpoch = Number(current.noteResourceRootValidationEpoch || 0) + 1;
    current.noteResourceRootValidationKey = validationKey;
    current.noteResourceRootValidationEpoch = validationEpoch;
    current.noteResourceRootValidationWorkspace = workspace;
    current.noteResourceRootValidationController = controller;
    current.noteResourceRootReady = false;
    current.noteResourceRootError = null;
    const validation = (async () => {
      const stillCurrent = () => !controller.signal.aborted && current === context()
        && current.noteWorkspace === workspace
        && current.noteResourceRootValidationWorkspace === workspace
        && current.noteResourceRootValidationKey === validationKey
        && current.noteResourceRootValidationEpoch === validationEpoch
        && scopeToken === currentScope()
        && generation === Number(state.filesGeneration || state.contextEpoch || 0)
        && workspace.left.resourceRoot === savedRoot && workspace.left.folderWorkspaceRoot === savedAnchor
        && workspace.left.folderWorkspaceId === savedWorkspaceId && requestEpoch === Number(current.noteResourceRequestEpoch || 0);
      let renewedAnchor = workspace.left.folderWorkspaceRoot;
      try {
        let raw;
        let hostRelative = '';
        let hostWorkspaceId = '';
        let recoveredLocationError = null;
        if (root.provider === 'host') {
          const locator = savedHostRelativePath(root);
          hostWorkspaceId = String(workspace.left.folderWorkspaceId || '').trim();
          // The chosen Workspace has durable authority independent of a
          // transient Favorite/current-directory ref.
          if (hostWorkspaceId) {
            const response = await filesFacadeClient.workspaceResource(hostWorkspaceId, '', { signal:controller.signal });
            if (!stillCurrent()) return false;
            if (isHostLocationsRoot(response?.resource)) throw new Error('Host locations is browse-only. Choose a folder beneath it.');
            const anchor = normalizeAuthorizedResource(response?.resource, { purpose:'folder', ...pickerScope() });
            if (anchor.provider !== 'host' || anchor.kind !== 'folder' || !anchor.capabilities.children) throw new Error('Saved Host workspace root is no longer listable.');
            renewedAnchor = withHostRelativePath(anchor, '');
          }
          if (hostWorkspaceId && locator.present) {
            raw = locator.path === '' ? filesBrowserResource(renewedAnchor)
              : (await filesFacadeClient.workspaceResource(hostWorkspaceId, locator.path, { signal:controller.signal }))?.resource;
            hostRelative = locator.path;
          } else {
            try {
              const response = await filesFacadeClient.stat(root.ref, { signal:controller.signal });
              raw = response?.resource || response;
            } catch (error) {
              if (error?.name === 'AbortError' || !stillCurrent() || !hostWorkspaceId || !renewedAnchor) throw error;
              // Host refs have no reissue lane. Keep the independently renewed
              // chosen anchor and report the outside location's real failure.
              recoveredLocationError = error;
              raw = filesBrowserResource(renewedAnchor); hostRelative = '';
            }
          }
        } else {
          const renewed = await filesFacadeClient.reissue(root.ref, { signal:controller.signal });
          if (!stillCurrent()) return false;
          const ref = String(renewed?.resource?.ref || '').trim();
          if (!ref) throw new Error('Saved Editor folder is unavailable.');
          const stat = await filesFacadeClient.stat(ref, { signal:controller.signal });
          raw = stat?.resource || renewed.resource;
        }
        if (!stillCurrent()) return false;
        if (isHostLocationsRoot(raw)) throw new Error('Host locations is browse-only. Choose a folder beneath it.');
        let renewed = normalizeAuthorizedResource(raw, {
          purpose:'folder', parentRef:root.parentRef || null, ...pickerScope(),
        });
        if (renewed.provider === 'host' && (savedHostRelativePath(root).present || recoveredLocationError)) renewed = withHostRelativePath(renewed, hostRelative);
        if (renewed.kind !== 'folder' || renewed.capabilities.children !== true) throw new Error('Saved Editor folder is no longer listable.');
        const page = await filesFacadeClient.children(renewed.ref, {
          limit:200, query:recoveredLocationError ? '' : workspace.left.resourceQuery || '', signal:controller.signal,
          sort:{ key:'name', direction:'asc', directories_first:true },
        });
        if (!stillCurrent()) return false;
        workspace.left.resourceRoot = renewed;
        workspace.left.resourceRows = (page.entries || []).slice(0, 5001).map(item => {
          const child = normalizeAuthorizedResource(item, { purpose:'file', ...pickerScope(), parentRef:renewed.ref });
          return withHostChildRelativePath(renewed, child, item?.name ?? item?.display_name ?? item?.resource?.name);
        });
        if (renewedAnchor) workspace.left.folderWorkspaceRoot = renewedAnchor;
        if (recoveredLocationError) workspace.left.resourceQuery = '';
        workspace.left.resourceCursor = page.next_cursor || null;
        current.noteResourceRootValidationKey = `${scopeToken}:${generation}:${canonicalEditorResourceKey(renewed.resourceKey, renewed.provider)}`;
        current.noteResourceRootReady = true; current.noteResourceRootError = null;
        persist(true); render();
        if (recoveredLocationError) current.window?.setStatus(`${recoveredLocationError.message || 'The previous outside folder is unavailable.'} Returned to the chosen Editor workspace.`, true);
        return true;
      } catch (error) {
        if (error?.name === 'AbortError' || !stillCurrent()) return false;
        workspace.left.resourceRows = [];
        if (renewedAnchor) workspace.left.folderWorkspaceRoot = renewedAnchor;
        current.noteResourceRootReady = false;
        const message = error?.message || 'Saved Editor folder is unavailable.';
        current.noteResourceRootError = root.provider === 'host'
          ? `${message} Reopen it from Open Folder.`
          : message;
        persist(true); render();
        return false;
      } finally {
        if (current.noteResourceRootValidationController === controller) current.noteResourceRootValidationController = null;
      }
    })();
    current.noteResourceRootValidationPromise = validation;
    return validation;
  }

  // All Notes resource-folder transitions share one abortable transaction.
  // The sidebar changes only after the renewed ref, authoritative stat, and
  // first page have succeeded, so a failed child/up/search load cannot erase
  // the folder the user was viewing.
  async function loadEditorResourceFolder(workspace, requested, { query = '', cursor = null, append = false, commitHistory = true, establishWorkspace = false, signal = null } = {}) {
    const current = context();
    const requestedRef = String(requested?.ref || requested?.resourceRef || requested || '').trim();
    const registeredHostTarget = String(requested?.provider || '').toLowerCase() === 'host'
      && Boolean(String(workspace?.left?.folderWorkspaceId || '').trim());
    if (signal?.aborted || !current || !workspace || current.noteWorkspace !== workspace || (!requestedRef && !registeredHostTarget)) return false;
    // Explicit navigation supersedes any background saved-root renewal before
    // it can publish a stale listing into this workspace object.
    current.noteResourceRootValidationController?.abort?.();
    current.noteResourceRootValidationController = null;
    current.noteResourceRootValidationWorkspace = null;
    current.noteResourceRootValidationKey = null;
    current.noteResourceRootValidationEpoch = Number(current.noteResourceRootValidationEpoch || 0) + 1;
    if (!workspace.left.folderWorkspaceRoot && workspace.left.resourceRoot?.ref) {
      workspace.left.folderWorkspaceRoot = workspace.left.resourceRoot;
    }
    current.noteResourceRequestController?.abort?.();
    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal?.addEventListener('abort', cancel, { once:true });
    const epoch = Number(current.noteResourceRequestEpoch || 0) + 1;
    current.noteResourceRequestEpoch = epoch; current.noteResourceRequestController = controller;
    current.noteResourceLoading = true;
    const scope = currentScope();
    const priorRoot = workspace.left.resourceRoot;
    const priorRows = workspace.left.resourceRows || [];
    try {
      const requestedProvider = String(requested?.provider || workspace.left.resourceRoot?.provider || '').toLowerCase();
      const requestedLocator = requestedProvider === 'host' ? savedHostRelativePath(requested) : { present:false, path:'' };
      const registeredWorkspaceId = String(workspace.left.folderWorkspaceId || '').trim();
      const useRegisteredHost = requestedProvider === 'host' && !establishWorkspace && requestedLocator.present && registeredWorkspaceId;
      if (!requestedRef && !useRegisteredHost) throw new Error('Authorized folder reference is unavailable.');
      let raw;
      if (useRegisteredHost) {
        const response = await filesFacadeClient.workspaceResource(registeredWorkspaceId, requestedLocator.path, { signal:controller.signal });
        raw = response?.resource;
      } else if (requestedProvider === 'host') {
        const stat = await filesFacadeClient.stat(requestedRef, { signal:controller.signal });
        raw = stat?.resource || stat;
      } else {
        const renewed = await filesFacadeClient.reissue(requestedRef, { signal:controller.signal });
        const renewedRef = String(renewed?.resource?.ref || requestedRef).trim();
        const stat = await filesFacadeClient.stat(renewedRef, { signal:controller.signal });
        raw = stat?.resource || renewed?.resource || stat;
      }
      if (controller.signal.aborted || epoch !== current.noteResourceRequestEpoch || scope !== currentScope() || current !== context() || current.noteWorkspace !== workspace) return false;
      if (establishWorkspace && isHostLocationsRoot(raw)) throw new Error('Host locations is browse-only. Choose a folder beneath it.');
      let root = normalizeAuthorizedResource(raw, { purpose:'folder', parentRef:requested?.parentRef || requested?.parent_ref || null, ...pickerScope() });
      if (root.kind !== 'folder' || root.capabilities.children !== true) throw new Error('This authorized folder cannot be listed.');
      if (establishWorkspace && root.provider !== 'host') throw new Error('Choose a Host folder, or use Close Folder to return to Copal.');
      if (root.provider === 'host' && (establishWorkspace || requestedLocator.present)) root = withHostRelativePath(root, establishWorkspace ? '' : requestedLocator.path);
      const registration = establishWorkspace
        ? await filesFacadeClient.workspace(root.ref, 'app_folder', { signal:controller.signal })
        : null;
      const workspaceId = String(registration?.workspace?.id || registration?.id || '').trim();
      if (establishWorkspace && !workspaceId) throw new Error('Host folder registration did not return a workspace.');
      if (establishWorkspace) {
        // Creating a Workspace can advance file-policy generation. Reacquire
        // its root through that Workspace before issuing children, so the
        // first listing carries a current generation-bound Host reference.
        const registeredRoot = await filesFacadeClient.workspaceResource(workspaceId, '', { signal:controller.signal });
        if (controller.signal.aborted || epoch !== current.noteResourceRequestEpoch || scope !== currentScope() || current !== context() || current.noteWorkspace !== workspace) return false;
        if (isHostLocationsRoot(registeredRoot?.resource)) throw new Error('Host locations is browse-only. Choose a folder beneath it.');
        root = normalizeAuthorizedResource(registeredRoot?.resource, { purpose:'folder', ...pickerScope() });
        if (root.kind !== 'folder' || root.capabilities.children !== true || root.provider !== 'host') throw new Error('Registered Host folder is no longer listable.');
        root = withHostRelativePath(root, '');
      }
      const page = await filesFacadeClient.children(root.ref, { limit:200, cursor, query:String(query || ''), signal:controller.signal, sort:{ key:'name', direction:'asc', directories_first:true } });
      if (controller.signal.aborted || epoch !== current.noteResourceRequestEpoch || scope !== currentScope() || current !== context() || current.noteWorkspace !== workspace) return false;
      const rows = (page.entries || []).map(item => {
        const child = normalizeAuthorizedResource(item, { purpose:'file', ...pickerScope(), parentRef:root.ref });
        return withHostChildRelativePath(root, child, item?.name ?? item?.display_name ?? item?.resource?.name);
      });
      const seen = new Set(append ? priorRows.map(item => `${item.provider}:${item.resourceKey || item.ref}`) : []);
      const merged = append ? [...priorRows, ...rows.filter(item => { const key = `${item.provider}:${item.resourceKey || item.ref}`; if (seen.has(key)) return false; seen.add(key); return true; })] : rows;
      workspace.left.resourceRoot = root; workspace.left.resourceRows = merged.slice(0, 5001);
      workspace.left.resourceCursor = page.next_cursor || null; workspace.left.resourceQuery = String(query || '');
      if (establishWorkspace) {
        workspace.left.folderWorkspaceRoot = root;
        workspace.left.folderWorkspaceId = workspaceId;
        resourceNavigation(current)?.reset?.();
      }
      const stableKey = canonicalEditorResourceKey(root.resourceKey || root.ref, root.provider || 'unknown');
      current.noteResourceRootValidationKey = `${scope}:${Number(state.filesGeneration || state.contextEpoch || 0)}:${stableKey}`;
      current.noteResourceRootValidationWorkspace = workspace;
      current.noteResourceRootReady = true; current.noteResourceRootError = null; current.noteResourceLoading = false;
      if (commitHistory) resourceNavigation(current)?.commit({ resource:root, query:String(query || ''), generation:Number(state.filesGeneration || state.contextEpoch || 0), scope:{ account:state.accountId || '', workspace:state.workspace || '' } });
      current.noteFilesExplorer?.sync?.();
      persist(true); render(); return true;
    } catch (error) {
      if (error?.name === 'AbortError' || controller.signal.aborted || epoch !== current.noteResourceRequestEpoch || scope !== currentScope() || current !== context() || current.noteWorkspace !== workspace) return false;
      current.noteResourceLoading = false; current.noteResourceRootError = error?.message || 'Folder listing failed.';
      // Keep the exact prior view on a failed transition.
      workspace.left.resourceRoot = priorRoot; workspace.left.resourceRows = priorRows;
      render();
      return false;
    } finally {
      signal?.removeEventListener('abort', cancel);
      if (current.noteResourceRequestController === controller) {
        current.noteResourceRequestController = null;
        current.noteResourceLoading = false;
      }
    }
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
    return state.docs.filter((doc) => !OPERATIONAL_KINDS.has(doc.kind) && !isChatResource(doc));
  }

  // Documents visible in the explorer: excludes operational kinds and,
  // when dot-folders are hidden, documents whose path starts with a dot-folder.
  function explorerDocs() {
    const workspace = ensureWorkspace();
    const docs = documents();
    return docs.filter((doc) => {
      const sharedTutorial = doc.owner === 'shared' && String(doc.name || '').startsWith('OpenClank/');
      if (workspace?.settings?.showTutorialFolder === false && (isOfficialDocument(doc) || sharedTutorial)) return false;
      if (workspace?.left?.showDotFolders) return true;
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
    bindSharedDocumentState(current);
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
    if (source && Object.keys(source).length && (source.version !== 3 || !source.root)) {
      current.noteStorageNeedsMigration = true;
      console.info('Saved Notes layout preserved; convert its export with .clanker/tools/browser-storage.mjs (see browser-storage.md).');
    }
    current.noteWorkspace = normalizeNotesWorkspace(source, docs, requested);
    current.noteDocsSignature = signature;
    current.noteSaved = null;
    current.noteLeafViews ||= new Map();
    current.noteBufferCreationListeners ||= new Map();
    current.noteDrafts ||= new Map();
    current.noteBuffers ||= new Map();
    current.noteAcceptedEnvelopes ||= new Map();
    for (const doc of documents()) doc.savePolicy = 'explicit';
    for (const doc of documents()) if (!current.noteAcceptedEnvelopes.has(doc.id)) current.noteAcceptedEnvelopes.set(doc.id, cloneEnvelope({
      text:doc.text, ...(doc.sourceMetadata ? { metadata:doc.sourceMetadata } : {}), properties:doc.properties, relations:doc.relations,
      ...(doc.extensions == null ? {} : { extensions:doc.extensions }),
    }));
    const recoveryScope = bufferScope(current);
    if (recoveryScope) for (const doc of documents()) {
      const resource = doc.resource;
      if (!resource?.key || current.noteBuffers.has(doc.id)) continue;
      try {
        const recoveryOptions = {
          envelope:current.noteAcceptedEnvelopes.get(doc.id),
          actorId:`${recoveryScope.accountId}:${recoveryScope.workspace}`,
          epoch:recoveryScope.epoch,
          scope:recoveryScope,
          save:async (snapshot, flushOptions = {}) => {
            if (flushOptions.scope && !sameSaveScope(flushOptions.scope, recoveryScope)) return false;
            if (!sameSaveScope(recoveryScope, bufferScope(current))) return false;
            if (snapshot.key?.provider === 'host') {
              if (typeof saveResource !== 'function') return { outcome:'failed', retryable:false, message:'Host resource saving is unavailable' };
              return saveHostBuffer(snapshot, { resource:doc.resource, document:doc, scope:recoveryScope });
            }
            return saveDocument({ ...doc, head:snapshot.expectedRevision.value }, snapshot.envelope?.text || '', false, 'notes', { snapshot, returnReceipt:true, scope:recoveryScope, viaBuffer:true, sheet:flushOptions.sheet === true });
          },
        };
        current.noteRecoveryChoices ||= new Set();
        if (!current.noteRecoveryChoices.has(doc.id)) {
          const offer = buffers.recoverDraft(resource, { ...recoveryOptions, offerOnly:true });
          if (offer) {
            current.noteRecoveryChoices.add(doc.id);
            current.noteRecoveryRun = (current.noteRecoveryRun || Promise.resolve()).then(() => offerRecovery(doc, resource, recoveryOptions, recoveryScope));
          }
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

  function bindSharedDocumentState(current = context()) {
    if (!current || !getSharedDocumentState) return;
    const shared = getSharedDocumentState();
    if (current.noteSharedDocumentState && current.noteSharedDocumentState !== shared) for (const buffer of current.noteBuffers?.values() || []) buffers.persistDraft(buffer);
    current.noteSharedDocumentState = shared;
    for (const key of ['noteDrafts', 'noteSaveRuns', 'noteSaveTargets', 'noteRevisionCounters', 'noteBuffers', 'noteAcceptedEnvelopes', 'noteBufferCreationListeners', 'noteRecoveryChoices']) {
      shared[key] ||= key === 'noteRecoveryChoices' ? new Set() : new Map(); current[key] = shared[key];
    }
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
    if (!getSharedDocumentState) {
      current.noteDrafts?.clear(); current.noteAcceptedEnvelopes?.clear(); current.noteSaveRuns?.clear();
      current.noteBuffers?.clear(); current.noteBufferCreationListeners?.clear();
    }
    for (const leafId of [...(current.noteLeafViews?.keys() || [])]) disposeLeaf(leafId);
    current.noteSelection?.clear();
    disposeEditorFiles(current);
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
    current.noteBeforeUnloadHandler && window.removeEventListener('beforeunload', current.noteBeforeUnloadHandler);
    for (const entry of current.noteShellMedia || []) entry.query.removeEventListener('change', entry.handler);
    current.noteDrawerRelease?.();
    current.noteKeyHandler = null;
    current.notePageHideHandler = null;
    current.noteShellMedia = null;
    current.noteDrawer = null;
    current.noteDrawerRelease = null;
    current.noteShellCache = null;
    disposeEditorFiles(current);
    current.noteWorkbenchMenu?.dispose(); current.noteWorkbenchMenu = null;
    current.noteWorkbenchMountToken = null;
    current.noteWorkbenchDispose?.(); current.noteWorkbenchDispose = null; current.noteWorkbenchHost = null;
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
    if (current.noteStorageNeedsMigration) {
      current.window?.setStatus('Saved Notes layout needs explicit workspace conversion. Its stored value is preserved.');
      return;
    }
    const write = () => {
      persistTimer = null;
      localStorage.setItem(copalStorageKey(`odysseus-copal-${presentationId}-layout`, state.workspace), serializeNotesWorkspace(current.noteWorkspace));
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
    context().noteActivationSequence = Number(context().noteActivationSequence || 0) + 1;
    persistActiveContext();
    persist(true);
    render();
  }

  function open(id, options = {}) {
    const doc = workspaceDocuments().find((item) => item.id === id);
    const workspace = ensureWorkspace();
    if (!doc || !workspace) return null;
    const activate = options.activate !== false;
    const scopeAtOpen = currentScope();
    const replacing = options.intent === 'current' && activeLeaf(workspace);
    if (replacing && !replacing.pinned && replacing.docId !== doc.id && !options.resolvedDirty) {
      void requestCloseLeaves([replacing], { title:'Replace editor tab' }).then(allowed => { if (allowed && scopeAtOpen === currentScope() && activeLeaf(workspace)?.id === replacing.id && activeLeaf(workspace)?.docId === replacing.docId) open(id, { ...options, resolvedDirty:true }); });
      return null;
    }
    const previousActive = workspace.activeLeafId;
    const previousGroups = new Map(workspaceGroups(workspace).map(group => [group.id, group.activeLeafId]));
    if (activate) context().noteActivationSequence = Number(context().noteActivationSequence || 0) + 1;
    if (options.revealHandbook && doc.readOnly === true && isOfficialDocument(doc)) {
      // Help returns to the Copal explorer without closing any document tabs.
      if (workspace.left.folderWorkspaceRoot || workspace.left.resourceRoot) closeFolderWorkspace(workspace);
      workspace.settings.showTutorialFolder = true;
      setWorkspacePanelPlacement(workspace, 'files', { side:'left', hidden:false });
      workspace.left.open = true;
      workspace.left.tab = 'files';
    }
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
    if (!activate && previousActive) {
      for (const group of workspaceGroups(workspace)) {
        const previous = previousGroups.get(group.id);
        if (group.tabs.some(tab => tab.id === previous)) group.activeLeafId = previous;
      }
      workspace.activeLeafId = previousActive;
    }
    if (activate) revealInExplorer(doc, workspace);
    context().selected = findWorkspaceLeaf(workspace)?.docId || id;
    state.selected = context().selected;
    persistActiveContext();
    persist(true);
    render();
    return leaf;
  }

  function openResource(resource, payload = {}, { activate = true } = {}) {
    const openOptions = { ...(activate ? (payload.intent ? { intent:payload.intent } : { reuse:true }) : { intent:'newTab', activate:false }), ...(payload.groupId ? { groupId:payload.groupId } : {}) };
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
      open(id, openOptions);
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
      kind:representation === 'base' ? 'base' : representation === 'wikiArticle' ? 'wiki' : representation === 'markdown' ? 'markdown' : 'text',
      text:snapshot.envelope.text, properties:payload.properties || {}, relations:payload.relations || [], tags:[],
      resource:handle, resourceKey:key, savePolicy:'explicit', sourceKind:key.provider === 'host' ? 'host' : 'copal',
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
      savePolicy:'explicit',
      hostParentResourceRef:payload.parent_resource_ref || existing.hostParentResourceRef || null,
    });
    // An opaque Files handoff can replace the presentation and capability
    // envelope of an already-open document without changing its id, active
    // leaf, workspace layout, or the documents array identity. Bump the local
    // render revision so the shell cannot reuse an editable view after the
    // resource has become a read-only Files projection.
    if (current) current.noteRenderVersion = (current.noteRenderVersion || 0) + 1;
    open(id, openOptions);
    return id;
  }

  function hostHistoryResourceForDocument(doc) {
    const handle = doc?.resource || null;
    const key = handle?.key || doc?.resourceKey || null;
    const resourceId = String(key?.resourceId || '').trim();
    const resourceRef = String(doc?.resourceRef || handle?.locator?.opaqueRef || '').trim();
    if (key?.provider !== 'host' || !resourceId || !resourceRef) return null;
    return { key, resourceId, resourceRef };
  }

  async function refreshHostDocumentAfterHistoryRestore(doc, target, restored) {
    if (!target.isCurrent()) return { status: 'context_changed' };
    const current = target.context;
    if (current.noteDrafts?.has(doc.id)
      || current.noteBuffers?.get(doc.id)?.state?.()?.dirty === true) {
      return { status: 'draft_preserved' };
    }
    const renewed = restored?.receipt?.refreshed_resource;
    if (restored?.receipt?.refresh_error) throw new Error(restored.receipt.refresh_error);
    if (renewed && (renewed.provider !== 'host' || renewed.id !== target.resourceId)) {
      throw new Error('The restored file identity changed; its open view was left untouched.');
    }
    const resourceRef = String(renewed?.ref || doc.resourceRef || doc.resource?.locator?.opaqueRef || target.resourceRef);
    const response = await filesFacadeClient.openResource(resourceRef);
    if (!target.isCurrent()) return { status: 'context_changed' };
    const payload = response?.payload || {};
    const handle = normalizeResourceHandle(payload.resource || response?.resource);
    if (!sameResourceKey(handle.key, target.key)) return { status: 'context_changed' };
    if (current.noteDrafts?.has(doc.id)
      || current.noteBuffers?.get(doc.id)?.state?.()?.dirty === true) {
      return { status: 'draft_preserved' };
    }
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
    Object.assign(doc, {
      name:String(payload.name || doc.name),
      text:snapshot.envelope.text,
      properties:payload.properties || {},
      relations:payload.relations || [],
      resource:handle,
      resourceKey:handle.key,
      resourceRef:handle.locator?.opaqueRef || resourceRef,
      resourceSnapshot:snapshot,
      sourceMetadata:handle.metadata || null,
      readOnly:handle.capabilities?.write !== true && handle.capabilities?.edit !== true,
    });
    current.noteRenderVersion = (current.noteRenderVersion || 0) + 1;
    persist(true);
    render();
    return { status: 'refreshed' };
  }

  function openEditorResourceHistory(doc) {
    const resource = hostHistoryResourceForDocument(doc);
    if (!resource) {
      context()?.window?.setStatus('Lore History is available for registered Host files.', true);
      return false;
    }
    const capturedContext = context();
    const capturedScope = currentScope();
    const isCurrent = () => {
      const live = context();
      const liveDoc = state.docs.find((candidate) => candidate.id === doc.id);
      return live === capturedContext
        && currentScope() === capturedScope
        && liveDoc === doc
        && sameResourceKey(liveDoc.resource?.key || liveDoc.resourceKey || {}, resource.key);
    };
    const target = {
      ...resource,
      name:String(doc.name || 'Selected file'),
      provider:'host',
      context:capturedContext,
      isCurrent,
    };
    const opened = openResourceHistory({
      resourceId:target.resourceId,
      name:target.name,
      provider:target.provider,
      isContextCurrent:isCurrent,
      onRestored:(restored) => refreshHostDocumentAfterHistoryRestore(doc, target, restored),
    }, document.activeElement);
    if (!opened) context()?.window?.setStatus('History could not open for this Editor resource.', true);
    return opened;
  }

  function pickerScope() {
    const scope = bufferScope();
    return {
      accountScope:scope?.accountId || state.accountId || '',
      workspaceScope:scope?.workspace || state.workspace || '',
      generation:Number(state.filesGeneration || state.contextEpoch || 0),
    };
  }

  function capturePickerOrigin() {
    const current = context();
    const workspace = ensureWorkspace();
    const leaf = activeLeaf(workspace);
    return Object.freeze({ current, window:current?.window, scope:currentScope(current),
      generation:Number(state.filesGeneration || state.contextEpoch || 0),
      groupId:leaf ? groupForLeaf(workspace, leaf.id)?.id || null : workspaceGroups(workspace)[0]?.id || null,
      leafId:leaf?.id || null,
    });
  }

  function pickerOriginCurrent(origin) {
    const current = context();
    if (!origin || current !== origin.current || current?.window !== origin.window || !origin.window?.visible
      || !origin.window.root?.isConnected || origin.scope !== currentScope(current)
      || origin.generation !== Number(state.filesGeneration || state.contextEpoch || 0)) return false;
    const workspace = current.noteWorkspace;
    return (!origin.groupId || !!findWorkspaceGroup(workspace, origin.groupId))
      && (!origin.leafId || !!findWorkspaceLeaf(workspace, origin.leafId));
  }

  function showResourcePicker(purpose, onSelect, { isContextCurrent = () => true } = {}) {
    const current = context();
    const origin = capturePickerOrigin();
    const pickerContext = pickerScope();
    current.noteResourcePicker?.destroy();
    let picker;
    picker = createResourcePicker({
      client:filesFacadeClient, purpose, ...pickerContext, originWindow:origin.window,
      initialDirectory:current.noteWorkspace?.left?.resourceRoot || null,
      getGeneration:() => Number(state.filesGeneration || state.contextEpoch || 0),
      getAccountScope:() => String(state.accountId || ''),
      getWorkspaceScope:() => String(state.workspace || ''),
      isOriginCurrent:() => pickerOriginCurrent(origin) && isContextCurrent(),
      isCompatible:resource => editorDestinationReason(resource) || (purpose === 'folder' && resource.provider !== 'host'
        ? 'Choose a Host folder, or return to Copal with Close Folder.'
        : purpose === 'file' && !['host', 'copal'].includes(resource.provider)
          ? 'Open this resource in its source applet.' : ''),
      onSelect:async (selected, selectionContext) => {
        if (!pickerOriginCurrent(origin) || !isContextCurrent()) throw new Error('The original Editor pane changed. Reopen the picker.');
        const result = await onSelect(selected, origin, selectionContext);
        if (current.noteResourcePicker === picker && result !== false) current.noteResourcePicker = null;
        return result;
      },
      onCancel:() => { if (current.noteResourcePicker === picker) current.noteResourcePicker = null; },
      onClose:() => {
        if (origin.current !== context() || origin.scope !== currentScope(current) || !origin.window.visible) return;
        const leaf = origin.leafId ? findWorkspaceLeaf(current.noteWorkspace, origin.leafId) : activeLeaf(current.noteWorkspace);
        const editor = current.noteLeafViews?.get(leaf?.id)?.editor;
        if (editor?.view?.dom?.isConnected) editor.focus();
        else origin.window.focus();
      },
      rootLabel:'Editor',
    });
    current.noteResourcePicker = picker;
    void picker.open();
    return picker;
  }

  async function openFileFromPicker(options = {}) {
    return showResourcePicker('file', (selected, origin, selectionContext) => resourceOpener({
      resourceRef:selected.ref, name:selected.name, parentResourceRef:selected.parentRef, origin, ...selectionContext, selectionOnly:true,
    }), options);
  }

  async function openFolderFromPicker(options = {}) {
    return showResourcePicker('folder', async (selected, origin, selectionContext) => {
      const current = origin.current;
      const workspace = current.noteWorkspace;
      const priorReady = current.noteResourceRootReady;
      const priorError = current.noteResourceRootError;
      current.noteResourceRootValidationKey = null;
      current.noteResourceRootValidationController?.abort?.();
      current.noteResourceRootReady = false;
      current.noteResourceRootError = null;
      const expectedEpoch = Number(current.noteResourceRequestEpoch || 0) + 1;
      const loaded = await loadEditorResourceFolder(workspace, selected, { query:'', commitHistory:true, establishWorkspace:true, signal:selectionContext.signal });
      if (!loaded) {
        const failure = current.noteResourceRootError || 'Folder could not be opened.';
        if (current === context() && current.noteWorkspace === workspace && Number(current.noteResourceRequestEpoch || 0) === expectedEpoch) {
          current.noteResourceRootReady = priorReady;
          current.noteResourceRootError = priorError;
          render();
        }
        throw new Error(failure);
      }
      return true;
    }, options);
  }

  function closeFolderWorkspace(workspace) {
    const current = context();
    current?.noteResourceRequestController?.abort?.();
    current?.noteResourceRootValidationController?.abort?.();
    if (current) current.noteResourceRequestEpoch = Number(current.noteResourceRequestEpoch || 0) + 1;
    current?.noteResourceNavigation?.reset?.();
    workspace.left.resourceRoot = null;
    workspace.left.resourceRows = [];
    workspace.left.resourceCursor = null;
    workspace.left.resourceQuery = '';
    workspace.left.folderWorkspaceRoot = null;
    workspace.left.folderWorkspaceId = '';
    if (current) {
      current.noteResourceRootReady = false;
      current.noteResourceRootError = null;
      const explorer = current.noteFilesExplorer;
      if (explorer) {
        explorer.filterQuery = ''; explorer.failedSync = null;
        explorer.displayedRoot = null; explorer.directoryReady = false;
        explorer.browser?.clearFilter();
        void explorer.navigate(() => runFilesNavigation(explorer.browser, 'home'), 'Editor places could not be shown.');
      }
    }
    persist(true); render();
  }

  async function configureTemplateFolder(options = {}) {
    return showResourcePicker('template-folder', (selected, origin) => {
      const normalized = normalizeTemplateFolderSelection({
        resourceRef:selected.ref, resourceKey:selected.resourceKey, provider:selected.provider,
        revision:selected.revision, kind:'folder', logicalPath:selected.logicalPath,
        capabilities:Object.keys(selected.capabilities || {}),
        accountScope:selected.accountScope, workspaceScope:selected.workspaceScope,
        generation:selected.generation, policyGeneration:state.filesPolicyGeneration,
      }, { purpose:'create' });
      const workspace = origin.current.noteWorkspace;
      workspace.settings.templateFolder = normalized.logicalPath;
      workspace.settings.templateFolderRef = normalized;
      persist(true); render();
      origin.window.setStatus(`Template folder: ${selected.name}`);
      return true;
    }, options);
  }

  function revealInExplorer(doc, workspace = ensureWorkspace()) {
    if (isHostDocument(doc)) {
      const ref = doc.resourceRef || doc.resource?.locator?.opaqueRef;
      if (ref) context()?.noteFilesExplorer?.reveal?.(ref);
      return;
    }
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
    cache.wikiArticleContextDispose?.(); cache.wikiArticleContextDispose = null;
    disposeSheet(cache);
    disposeRawEditor(cache);
    current.noteLeafViews.delete(leafId);
    pruneSessionHistory();
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
    if (cache.previewFrame != null) cancelAnimationFrame(cache.previewFrame);
    cache.previewFrame = null; cache.previewPending = null;
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

  async function recordSavedEvidence(docId, draft, receipt) {
    const revision = receipt?.revision?.value || receipt?.revision;
    if (receipt?.outcome !== 'applied' || !revision || draft.scope !== currentScope()) return;
    const accountId = achievementOwner(), workspaceId = state.workspace;
    const text = String(draft.envelope?.text ?? draft.value ?? '');
    const options = { accountId, workspaceId, kind:'R', occurrenceId:`${docId}:${revision}` };
    const cache = [...(context()?.noteLeafViews?.values() || [])].find(item => item.docId === docId && item.editor?.getValue?.() === text);
    const grammarId = cache?.editor?.getStatus?.()?.language?.id;
    if (grammarId && LANGUAGE_REGISTRY.some(item => item.id === grammarId) && cache?.editor?.getCommentSourceMapAsync) {
      try {
        const regions = await cache.editor.getCommentSourceMapAsync();
        if (draft.scope !== currentScope() || cache.editor.getValue() !== text) return;
        const region = regions.find(item => ['comment', 'docstring'].includes(item.kind) && item.markdown?.trim());
        if (region) await recordPresentation('document.rich-region.saved', {
          documentId:docId, revisionId:String(revision), grammarId, regionKind:region.kind, supportedGrammar:true, markdownNonempty:true,
        }, options);
      } catch (_) { /* Changed source / unavailable syntax cannot prove a region. */ }
    }
    const lines = text.split('\n');
    let fence = null;
    for (let line = 0; line < lines.length; line++) {
      const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(lines[line]);
      if (marker) { if (!fence) fence = marker[1]; else if (marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = null; continue; }
      if (fence) continue;
      // Start at genuine headers; include immediately preceding metadata.
      if (!lines[line].includes('|') || !/^\s*\|?\s*:?-{3,}/.test(lines[line + 1] || '')) continue;
      let from = line;
      if (line > 0 && lines[line - 1].trim().endsWith('-->')) {
        for (let candidate = line - 1; candidate >= 0; candidate--) {
          if (lines[candidate].includes('<!-- clank-table')) { from = candidate; break; }
          if (lines[candidate].includes('<!--')) break;
        }
      }
      const table = parseTable(lines.slice(from).join('\n'), from);
      if (table.valid && table.metadata?.columns?.some(column => ['date', 'currency'].includes(column.type))) {
        const formulas = table.rows.slice(1).flatMap(row => row.cells).filter(cell => typeof cell === 'string' && cell.startsWith('='));
        const results = formulas.map(formula => evaluateFormula(formula, table));
        const tableId = table.metadata.id || `${docId}:${line}`;
        if (formulas.length && results.every(result => !result.error)) await recordPresentation('table.revision.saved', {
          tableId, revisionId:String(revision), hasTypedDateOrCurrency:true, formulaEvaluated:true, formulaErrors:0,
        }, { ...options, occurrenceId:`${docId}:${revision}:${tableId}` });
      }
      if (table.valid) line = Math.max(line, table.blockRange?.to || line);
    }
  }

  async function saveDraft(docId, { returnReceipt = false, snapshot = null } = {}) {
    const current = context();
    if (!flushPendingSourceEdits(docId)) return returnReceipt ? { outcome:'failed', message:'Correct or discard the staged comment before saving.' } : false;
    const draft = current?.noteDrafts?.get(docId);
    if (!draft) return true;
    if (draft.scope && draft.scope !== currentScope(current)) return false;
    const buffer = current.noteBuffers?.get(docId);
    const target = snapshot || (buffer ? buffer.snapshot() : cloneEnvelope(draft));
    current.noteSaveTargets ||= new Map();
    const targetKey = `${docId}:${target.localRevision}`;
    let run = current.noteSaveTargets.get(targetKey);
    if (!run) {
      const previous = current.noteSaveRuns.get(docId);
      run = (async () => {
        let receipt;
        if (buffer) receipt = await buffer.flush({ scope:draft.scopeObject, snapshot:target });
        else {
          if (previous) { const prior = await previous; if (prior === false || prior?.outcome === 'conflict') return prior; }
          if (draft.scope !== currentScope(current)) return false;
          const latest = state.docs.find(item => item.id === docId) || draft.doc;
          receipt = await saveDocument({ ...latest, head:previous ? latest.head : draft.base }, draft.value, false, 'notes', {
            snapshot:cloneEnvelope({ key:draft.resourceKey || latest.resource?.key, expectedRevision:{ kind:'copalHead', value:String((previous ? latest.head : draft.base) || '') }, localRevision:target.localRevision, actionId:target.actionId, scope:draft.scopeObject, envelope:target.envelope }),
            returnReceipt:true, scope:draft.scopeObject, viaBuffer:true, sheet:draft.sheet === true,
          });
        }
        if (draft.scope !== currentScope(current)) return false;
        if (receipt === false || receipt?.outcome === 'conflict' || receipt?.outcome === 'failed') {
          setLeafSaveState(docId, receipt?.outcome === 'conflict' || buffer?.state().status === 'conflict' ? 'conflict' : 'error');
          return receipt || false;
        }
        const queued = current.noteDrafts.get(docId);
        const clean = buffer ? !buffer.state().dirty : queued?.localRevision === target.localRevision;
        const accepted = buffer?.acceptedEnvelope || target.envelope;
        current.noteAcceptedEnvelopes.set(docId, cloneEnvelope(accepted));
        const doc = workspaceDocuments().find(item => item.id === docId) || draft.doc;
        if (buffer && doc?.resource) { doc.resource = buffer.handle; doc.resourceKey = buffer.key; if (buffer.handle.revision.kind === 'copalHead') doc.head = buffer.handle.revision.value; }
        if (clean) {
          current.noteDrafts.delete(docId);
          if (buffer && !buffers.discardDraft(buffer)) current.window?.setStatus('Saved. Recovery cleanup failed; retry before discarding or closing.', true);
        } else {
          if (queued && buffer) queued.base = buffer.handle.revision.value;
          if (buffer) buffers.scheduleDraft(buffer);
        }
        if (receipt?.outcome === 'applied') {
          void recordSavedEvidence(docId, draft, receipt).catch(() => {});
          for (const evidence of draft.savedEvidence || []) void recordPresentation(evidence.type, { ...evidence.fields, revisionId:String(receipt.revision?.value || '') }, { ...evidence.options, occurrenceId:`${docId}:${receipt.revision?.value || ''}` }).catch(() => {});
        }
        setLeafSaveState(docId, clean ? 'saved' : 'unsaved');
        return receipt;
      })().catch(error => { setLeafSaveState(docId, 'error'); current.window?.setStatus(error?.message || 'Save failed', true); return false; });
      current.noteSaveTargets.set(targetKey, run); current.noteSaveRuns.set(docId, run);
      run.finally(() => { current.noteSaveTargets.delete(targetKey); if (current.noteSaveRuns.get(docId) === run) current.noteSaveRuns.delete(docId); });
    }
    const receipt = await run;
    return returnReceipt ? receipt : receipt !== false && receipt?.outcome !== 'conflict' && receipt?.outcome !== 'failed';
  }

  async function prepareDelete(docId) {
    if (!await resolveDirtyDocuments([docId], { force:true, title:'Move document to Trash' })) {
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
  function queueDocumentSave(doc, value, { flush = false, snapshot = null, baseRevision = null } = {}) {
    queueSave(doc, value, {
      baseRevision:baseRevision ?? snapshot?.expectedRevision?.value,
      rebase:snapshot,
    });
    return flush ? saveDraft(doc.id, { returnReceipt:true }) : { outcome:'queued', localRevision:context()?.noteBuffers?.get(doc.id)?.localRevision, snapshot:getDraftSnapshot(doc.id) };
  }

  function queueSave(doc, value, options = {}) {
    const current = context();
    if (!current) return;
    bindSharedDocumentState(current);
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
      doc:indexed, value, envelope, savedEvidence:existing?.savedEvidence || [], base:options.baseRevision || existing?.base || indexed.head, sheet:options.sheet === true,
      localRevision, actionId:createSaveActionId(`document-${doc.id}`), resourceKey:doc.resource?.key || doc.resourceKey || null, scope, scopeObject,
    });
    try {
      const buffer = sourceEditBuffer(doc, envelope);
      if (buffer) {
        if (options.rebase) {
          const rebasedRevision = options.rebase.expectedRevision;
          if (!Number.isSafeInteger(Number(options.rebase.localRevision)) || !buffer.retryAtRevision(Number(options.rebase.localRevision), rebasedRevision)) {
            throw new Error('The reviewed version is no longer current; compare it again before saving over it.');
          }
        }
        buffer.apply(envelope, { origin:options.origin || 'local', history:options.history !== false, sheet:options.sheet === true });
        current.noteBuffers.delete(doc.id); current.noteBuffers.set(doc.id, buffer);
        Object.assign(indexed, cloneEnvelope(envelope));
        notifyDocumentBufferCreated(doc.id, buffer);
        buffers.scheduleDraft(buffer);
        if (!buffer.state().dirty) { current.noteDrafts.delete(doc.id); buffers.discardDraft(buffer); }
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
    doc.savePolicy = 'explicit';
    setLeafSaveState(doc.id, current.noteDrafts.has(doc.id) ? 'unsaved' : 'saved');
  }

  // Public source transaction seam for projections such as Mind.  It captures
  // the current draft envelope and its CAS base, then enters the same buffer
  // history/autosave path used by CodeMirror and Wiki textareas.
  function applyDocumentTransaction(doc, transform, { origin = 'projection', expectedLocalRevision = null, expectedSource = null } = {}) {
    if (!doc || typeof transform !== 'function') return { outcome:'failed', message:'A document and source transaction are required' };
    if (doc.readOnly === true || doc.builtin === true || doc.note_error || doc.rawPreserved === true) return { outcome:'failed', message:'This source is read-only until it is recovered or converted' };
    const snapshot = getDraftSnapshot(doc.id);
    const source = String(snapshot?.envelope?.text ?? doc.text ?? '');
    const currentRevision = context()?.noteBuffers?.get(doc.id)?.localRevision ?? snapshot?.localRevision ?? 0;
    if (expectedLocalRevision != null && Number(expectedLocalRevision) !== currentRevision || expectedSource != null && expectedSource !== source) return { outcome:'failed', message:'The document changed; retry this edit.' };
    const next = transform(source);
    if (typeof next !== 'string') return { outcome:'failed', message:'Source transaction must return text' };
    if (next === source) return { outcome:'unchanged', source, snapshot };
    queueSave(doc, next, { origin });
    doc.text = next;
    syncDocumentEditors(doc.id, next);
    setLeafSaveState(doc.id, 'unsaved');
    const transactionId = `notes-tx-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    return { outcome:'queued', source, content:next, snapshot, origin, transactionId };
  }

  function syncDocumentEditors(docId, value, source = null, changes = null, mirror = true) {
    if (mirror) onSourceChanged?.(docId, value, changes);
    for (const cache of context()?.noteLeafViews?.values() || []) {
      if (cache.docId !== docId || cache.editor === source) continue;
      if (changes && cache.editor?.applyChanges) cache.editor.applyChanges(changes);
      else cache.editor?.setValue(value);
      const liveDoc = workspaceDocuments().find((doc) => doc.id === docId) || null;
      if (cache.reading?.isConnected) {
        cache.reading.replaceChildren(renderMarkdown(value, new Set([docId]), liveDoc));
        applyCompletedVisibility(cache.reading);
        if (liveDoc) wireInteractiveTables(cache.reading, value, liveDoc, cache);
      }
      if (cache.preview && !cache.preview.hidden) {
        if (liveDoc) updatePreview(cache, liveDoc, value);
      }
    }
  }

  function receiveDocumentSource(docId, value, changes = null) {
    syncDocumentEditors(docId, value, null, changes, false);
    const current = context();
    if (current) current.noteShellCache = null;
    const buffer = current?.noteBuffers?.get(docId);
    setLeafSaveState(docId, buffer?.state()?.dirty || current?.noteDrafts?.has(docId) ? 'unsaved' : 'saved');
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

  function setLeafSaveState(docId, value, mirror = true) {
    if (mirror) onSaveStateChanged?.(docId, value);
    for (const cache of context()?.noteLeafViews?.values() || []) {
      if (cache.docId !== docId) continue;
      cache.saveState = value;
      const indicator = context()?.window?.root.querySelector(`.copal-note-tab[data-leaf-id="${CSS.escape(cache.leaf?.id || '')}"] .copal-note-tab-state`);
      if (indicator) {
        const label = value === 'saved' ? 'Saved' : value === 'saving' ? 'Saving…' : value === 'conflict' ? 'Conflict' : value === 'error' ? 'Save failed' : 'Unsaved changes';
        indicator.textContent = value === 'saved' ? '' : value === 'saving' ? '◌' : '●';
        indicator.title = label; indicator.setAttribute('aria-label', label);
      }
      updateLeafStatus(cache);
    }
  }

  async function offerRecovery(doc, resource, options, scope) {
    const current = context();
    if (!sameSaveScope(scope, bufferScope(current)) || current.noteDrafts?.has(doc.id)) return;
    const choice = await styledConfirm(`An unsaved draft of ${doc.name} is available. Restoring it does not save the document. Undo history from the previous browser session is unavailable.`, { title:'Recover unsaved draft', confirmText:'Restore draft', alternateText:'Discard draft', cancelText:'Later' });
    if (!sameSaveScope(scope, bufferScope(current)) || current.noteDrafts?.has(doc.id) || choice === false) return;
    const buffer = buffers.recoverDraft(resource, options);
    if (!buffer) return;
    if (choice === 'alternate') {
      if (!buffers.discardDraft(buffer)) { current.window?.setStatus('Recovery storage could not be cleared. Draft discard is pending; retry recovery.', true); current.noteRecoveryChoices.delete(doc.id); return; }
      buffer.resolveExternal(options.envelope, resource.revision, { force:true }); buffer.discard();
    } else {
      current.noteBuffers.set(doc.id, buffer);
      publishBufferDraft(doc.id, buffer);
      if (buffer.state().status === 'conflict') current.window?.setStatus('Recovered draft. The saved document changed; compare before saving.', true);
    }
    current.noteRenderVersion = Number(current.noteRenderVersion || 0) + 1;
    render();
  }

  async function saveHostBuffer(snapshot, options) {
    const receipt = await saveResource(snapshot, options);
    if (!receipt?.snapshot?.envelope) return receipt;
    const envelope = cloneEnvelope(receipt.snapshot.envelope);
    // Host receipts describe the provider representation alongside text and
    // byte metadata. Representation belongs to the ResourceHandle; it is not
    // an editable envelope field and must not manufacture a dirty save point.
    if (!Object.hasOwn(snapshot.envelope || {}, 'representation')) delete envelope.representation;
    return { ...receipt, snapshot:{ ...receipt.snapshot, envelope } };
  }

  function sourceEditBuffer(doc, fallbackEnvelope = null) {
    const current = context();
    if (!current) return null;
    bindSharedDocumentState(current);
    current.noteBuffers ||= new Map(); current.noteAcceptedEnvelopes ||= new Map();
    const existing = current.noteBuffers.get(doc.id);
    if (existing) return existing;
    const indexed = state.docs.find(item => item.id === doc.id) || doc;
    const scopeObject = bufferScope(current), scope = currentScope(current);
    const resource = doc.resource || (doc.resourceKey ? { key:doc.resourceKey, revision:{ kind:'copalHead', value:String(indexed.head || '0') }, locator:{ displayName:doc.name || doc.id, locationLabel:doc.name || doc.id }, representation:'nativeNote', capabilities:{ read:true, edit:doc.readOnly !== true } } : null);
    if (!resource?.key || !scopeObject) return null;
    // Acquiring a history/pending-edit owner is not a source edit. Include the
    // Host representation metadata in its accepted baseline so opening or
    // cancelling a rich comment cannot manufacture a dirty document.
    const accepted = cloneEnvelope(current.noteAcceptedEnvelopes.get(doc.id) || fallbackEnvelope || {
      text:String(doc.text ?? ''), properties:doc.properties, relations:doc.relations,
      ...(doc.extensions == null ? {} : { extensions:doc.extensions }),
    });
    if (doc.sourceMetadata && accepted.metadata == null) accepted.metadata = cloneEnvelope(doc.sourceMetadata);
    current.noteAcceptedEnvelopes.set(doc.id, cloneEnvelope(accepted));
    const buffer = buffers.acquire(resource, accepted, { actorId:`${scopeObject.accountId}:${scopeObject.workspace}`, epoch:scopeObject.epoch, scope:scopeObject, save:async (snapshot, flushOptions = {}) => {
      if (flushOptions.scope && !sameSaveScope(flushOptions.scope, scopeObject)) return false;
      if (scope !== currentScope(current)) return false;
      const target = state.docs.find(item => item.id === doc.id) || indexed;
      const targetResource = target.resource || (target.resourceKey ? { key:target.resourceKey } : null);
      if (snapshot.key?.provider === 'host' && typeof saveResource === 'function') return saveHostBuffer(snapshot, { resource:targetResource, document:target, scope:scopeObject });
      return saveDocument({ ...target, head:snapshot.expectedRevision.value }, snapshot.envelope?.text ?? '', false, 'notes', { snapshot, returnReceipt:true, scope:scopeObject, viaBuffer:true, sheet:flushOptions.sheet === true });
    } });
    current.noteBuffers.set(doc.id, buffer);
    notifyDocumentBufferCreated(doc.id, buffer);
    return buffer;
  }

  function registerPendingSourceEdit(doc, edit) {
    const buffer = sourceEditBuffer(doc);
    if (!buffer || !edit?.id) return false;
    buffer.pendingEdits.set(String(edit.id), edit); buffer.pendingEditRevision += 1; buffer.status = 'unsaved';
    buffers.scheduleDraft(buffer); setLeafSaveState(doc.id, 'unsaved');
    return true;
  }

  function removePendingSourceEdit(doc, id) {
    const buffer = context()?.noteBuffers?.get(doc.id);
    if (!buffer) return false;
    const removed = buffer.pendingEdits.delete(String(id)); buffer.pendingEditRevision += 1;
    if (buffer.state().dirty) buffers.scheduleDraft(buffer);
    else {
      // A same-source editor notification while a comment was staged may
      // have retained the legacy draft marker. Once the last staged edit is
      // cancelled and the resource equals its accepted envelope, both owners
      // must report the same clean checkpoint.
      context()?.noteDrafts?.delete(doc.id);
      buffer.pending = null; buffer.status = 'saved';
      if (!buffers.discardDraft(buffer)) context()?.window?.setStatus('Recovery cleanup failed. Retry before closing.', true);
    }
    setLeafSaveState(doc.id, buffer.state().dirty ? 'unsaved' : 'saved');
    return removed;
  }

  function flushPendingSourceEdits(docId) {
    const buffer = context()?.noteBuffers?.get(docId);
    for (const edit of [...(buffer?.pendingEdits?.values() || [])]) {
      let result;
      try { result = edit.flush?.(); } catch (error) { result = { outcome:'failed', message:error.message }; }
      if (!result || !['queued', 'unchanged'].includes(result.outcome)) {
        context()?.window?.setStatus(result?.message || 'A staged comment needs correction before saving. Reopen its rich editor or discard its draft.', true);
        return false;
      }
      buffer.pendingEdits.delete(String(edit.id)); buffer.pendingEditRevision += 1;
    }
    return true;
  }

  function transitionRevision(current, id) {
    const buffer = current?.noteBuffers?.get(id);
    return `${workbenchLocalRevision(current, id)}:${buffer?.pendingEditRevision || 0}`;
  }

  function pruneSessionHistory() {
    const current = context();
    if (!current?.noteBuffers) return;
    const active = new Set();
    for (const owner of state.windows?.values?.() || [current]) if (owner.window?.visible && owner.noteWorkspace) for (const leaf of workspaceLeaves(owner.noteWorkspace)) active.add(leaf.docId);
    const retained = [...current.noteBuffers].filter(([id, buffer]) => !active.has(id) && !buffer.state().dirty && !buffer.running && !buffer.drainPromise && !buffer.uncertainSave && !buffer.recoveryError);
    let bytes = retained.reduce((sum, [, buffer]) => sum + buffer.historyBytes + buffer.redoBytes, 0);
    while (retained.length > 32 || bytes > 64 * 1024 * 1024) {
      const [id, buffer] = retained.shift(); bytes -= buffer.historyBytes + buffer.redoBytes;
      buffers.release(buffer.key, { actorId:buffer.actorId, epoch:buffer.epoch }); current.noteBuffers.delete(id);
    }
  }

  function persistRecovery() {
    for (const buffer of context()?.noteBuffers?.values() || []) {
      if (buffer.state().dirty) buffers.persistDraft(buffer);
    }
  }

  function publishBufferDraft(docId, buffer) {
    const current = context(), doc = workspaceDocuments().find(item => item.id === docId);
    if (!current || !doc) return false;
    const snapshot = buffer.snapshot(), envelope = buffer.envelope;
    Object.assign(doc, cloneEnvelope(envelope));
    if (buffer.state().dirty) {
      current.noteDrafts.set(docId, { doc, value:String(envelope.text ?? ''), envelope:cloneEnvelope(envelope), base:buffer.handle.revision.value, localRevision:buffer.localRevision, actionId:snapshot.actionId, resourceKey:buffer.key, scope:currentScope(current), scopeObject:bufferScope(current) });
      buffers.scheduleDraft(buffer);
    } else {
      current.noteDrafts.delete(docId);
      if (!buffers.discardDraft(buffer)) current.window?.setStatus('The saved content is restored, but recovery cleanup failed. Retry before closing.', true);
    }
    current.noteRevisionCounters.set(docId, buffer.localRevision);
    syncDocumentEditors(docId, String(envelope.text ?? ''));
    setLeafSaveState(docId, buffer.state().status === 'conflict' ? 'conflict' : buffer.state().dirty ? 'unsaved' : 'saved');
    return true;
  }

  function undoDocument(docId, redo = false) {
    const buffer = context()?.noteBuffers?.get(docId);
    if (!buffer || !(redo ? buffer.redo() : buffer.undo())) return false;
    return publishBufferDraft(docId, buffer);
  }

  async function resolveDirtyDocuments(docIds, { closingLeafIds = null, closingWindow = false, force = false, title = 'Unsaved changes' } = {}) {
    const current = context(), scope = currentScope(current);
    if (!current) return false;
    const closing = new Set(closingLeafIds || []);
    const hasSurvivor = (id) => {
      if (force || !closingLeafIds && !closingWindow) return false;
      for (const owner of state.windows?.values?.() || [current]) {
        if (!owner.window?.visible || !owner.noteWorkspace) continue;
        if (workspaceLeaves(owner.noteWorkspace).some(leaf => leaf.docId === id && (owner !== current || !closingWindow && !closing.has(leaf.id)))) return true;
      }
      return false;
    };
    const ids = [...new Set(docIds)].filter(id => !hasSurvivor(id) && (current.noteDrafts?.has(id) || current.noteBuffers?.get(id)?.state().dirty || current.noteBuffers?.get(id)?.recoveryError));
    if (!ids.length) return true;
    const captured = new Map(ids.map(id => [id, transitionRevision(current, id)]));
    const stillCurrent = () => scope === currentScope(current) && ids.every(id => transitionRevision(current, id) === captured.get(id));
    const docs = ids.map(id => workspaceDocuments().find(doc => doc.id === id)).filter(Boolean);
    const choice = await styledConfirm(`Changes in ${docs.map(doc => doc.name).join(', ')}`, { title, confirmText:'Save', alternateText:'Discard changes', cancelText:'Cancel' });
    if (choice === false || !stillCurrent()) return false;
    if (choice !== 'alternate') {
      for (const id of ids) if (!flushPendingSourceEdits(id)) return false;
      for (const id of ids) captured.set(id, transitionRevision(current, id));
      const results = await Promise.all(ids.map(id => saveDraft(id)));
      return stillCurrent() && results.every(Boolean) && ids.every(id => !current.noteDrafts.has(id) && !current.noteBuffers.get(id)?.state().dirty && !current.noteBuffers.get(id)?.recoveryError);
    }
    // Settle submitted saves, then read authoritative bytes before resetting a
    // draft. A rejected/aborted request may already have committed remotely.
    await Promise.all(ids.map(id => current.noteSaveRuns?.get(id) || current.noteBuffers?.get(id)?.drainPromise || Promise.resolve()));
    if (!stillCurrent()) return false;
    const accepted = [];
    try {
      if (ids.some(id => current.noteBuffers?.get(id)?.uncertainSave)) throw new Error('A previous Save outcome is unknown. Discard is pending; retry the captured Save to reconcile it before closing.');
      for (const doc of docs) {
        const buffer = current.noteBuffers.get(doc.id);
        if (buffer?.running || buffer?.drainPromise) return false;
        if (isHostDocument(doc)) {
          const ref = doc.resource?.locator?.opaqueRef || doc.resourceRef;
          if (!ref) throw new Error('Reopen this authorized file before discarding its draft.');
          const opened = await filesFacadeClient.openResource(ref);
          const payload = opened.payload || {}, handle = normalizeResourceHandle(payload.resource);
          if (!sameResourceKey(handle.key, doc.resource.key)) throw new Error('The discard resource changed.');
          accepted.push({ doc, buffer, envelope:cloneEnvelope({ text:String(payload.text ?? payload.content ?? ''), ...(handle.metadata ? { metadata:handle.metadata } : {}) }), revision:handle.revision, handle });
        } else {
          const fresh = await api(`/documents/${encodeURIComponent(doc.id)}`);
          if (!fresh?.head || fresh.id !== doc.id) throw new Error('Saved document could not be verified for discard.');
          accepted.push({ doc, buffer, envelope:cloneEnvelope({ text:String(fresh.text ?? ''), properties:fresh.properties, relations:fresh.relations, ...(fresh.extensions == null ? {} : { extensions:fresh.extensions }) }), revision:{ kind:'copalHead', value:fresh.head }, fresh });
        }
      }
      if (!stillCurrent()) return false;
      for (const item of accepted) {
        if (item.buffer && !buffers.discardDraft(item.buffer)) {
          for (const prior of accepted) if (prior.buffer?.state().dirty) buffers.persistDraft(prior.buffer);
          throw new Error('Recovery storage could not be cleared. Discard is pending; retry before closing.');
        }
      }
      if (!stillCurrent()) return false;
      for (const item of accepted) {
        clearTimeout(state.saveTimers.get(item.doc.id)); state.saveTimers.delete(item.doc.id);
        if (item.fresh) Object.assign(item.doc, item.fresh);
        if (item.handle) item.doc.resource = item.handle;
        Object.assign(item.doc, cloneEnvelope(item.envelope));
        if (item.buffer) { item.buffer.resolveExternal(item.envelope, item.revision, { force:true }); item.buffer.discard(); }
        current.noteDrafts.delete(item.doc.id); current.noteAcceptedEnvelopes.set(item.doc.id, cloneEnvelope(item.envelope));
        syncDocumentEditors(item.doc.id, item.envelope.text); setLeafSaveState(item.doc.id, 'saved');
      }
      return true;
    } catch (error) { current.window?.setStatus(error.message || 'Discard failed; changes remain open.', true); return false; }
  }

  async function reloadDocument(docId) {
    const current = context(), scope = currentScope(current);
    const doc = workspaceDocuments().find(item => item.id === docId);
    if (!doc || !await resolveDirtyDocuments([docId], { force:true, title:'Reload saved document' })) return false;
    const revision = transitionRevision(current, docId);
    try {
      let envelope, providerRevision, fresh = null, handle = null;
      if (isHostDocument(doc)) {
        const ref = doc.resource?.locator?.opaqueRef || doc.resourceRef;
        const opened = await filesFacadeClient.openResource(ref), payload = opened.payload || {};
        handle = normalizeResourceHandle(payload.resource);
        if (!sameResourceKey(handle.key, doc.resource.key)) throw new Error('The reload resource changed.');
        envelope = cloneEnvelope({ text:String(payload.text ?? payload.content ?? ''), ...(handle.metadata ? { metadata:handle.metadata } : {}) }); providerRevision = handle.revision;
      } else {
        fresh = await api(`/documents/${encodeURIComponent(docId)}`);
        if (!fresh?.head || fresh.id !== docId) throw new Error('Saved document could not be verified.');
        envelope = cloneEnvelope({ text:String(fresh.text ?? ''), properties:fresh.properties, relations:fresh.relations, ...(fresh.extensions == null ? {} : { extensions:fresh.extensions }) }); providerRevision = { kind:'copalHead', value:fresh.head };
      }
      if (scope !== currentScope(current) || revision !== transitionRevision(current, docId) || current.noteDrafts.has(docId)) return false;
      const buffer = current.noteBuffers.get(docId);
      if (buffer?.state().dirty || buffer?.running || buffer?.drainPromise) return false;
      if (fresh) Object.assign(doc, fresh); if (handle) doc.resource = handle;
      Object.assign(doc, cloneEnvelope(envelope));
      buffer?.resolveExternal(envelope, providerRevision, { force:false });
      current.noteAcceptedEnvelopes.set(docId, cloneEnvelope(envelope)); syncDocumentEditors(docId, envelope.text); setLeafSaveState(docId, 'saved');
      current.window?.setStatus('Reloaded saved document.'); return true;
    } catch (error) { current.window?.setStatus(error.message || 'Reload failed; the document remains open.', true); return false; }
  }

  async function requestCloseLeaves(leaves, options = {}) {
    const current = context(), workspace = current?.noteWorkspace, scope = currentScope(current);
    const captured = leaves.filter(Boolean).map(leaf => ({ id:leaf.id, docId:leaf.docId, pinned:leaf.pinned === true }));
    const allowed = await resolveDirtyDocuments(captured.map(leaf => leaf.docId), { closingLeafIds:captured.map(leaf => leaf.id), ...options });
    return allowed && scope === currentScope(current) && current.noteWorkspace === workspace && captured.every(item => { const leaf = findWorkspaceLeaf(workspace, item.id); return leaf?.docId === item.docId && (leaf.pinned === true) === item.pinned; });
  }

  async function beforeWindowClose() {
    const current = context(), scope = currentScope(current);
    const layout = JSON.stringify(workspaceLeaves(current?.noteWorkspace).map(leaf => [leaf.id, leaf.docId]));
    const resources = () => JSON.stringify([...new Set([...(current?.noteDrafts?.keys() || []), ...(current?.noteBuffers?.keys() || [])])].sort());
    const resourceSet = resources();
    const allowed = await resolveDirtyDocuments([...(current?.noteDrafts?.keys() || []), ...(current?.noteBuffers?.keys() || [])], { closingWindow:true, title:'Close Editor' });
    return allowed && scope === currentScope(current) && resourceSet === resources() && layout === JSON.stringify(workspaceLeaves(current?.noteWorkspace).map(leaf => [leaf.id, leaf.docId]));
  }

  async function flushAll() {
    const ids = [...(context()?.noteDrafts?.keys() || [])];
    return Promise.all(ids.map(saveDraft));
  }

  function destroy() {
    persistRecovery();
    const current = context();
    if (current) {
      current.noteShellCache = null;
      disposeEditorFiles(current);
      current.noteWorkbenchMenu?.dispose(); current.noteWorkbenchMenu = null;
      current.noteWorkbenchMountToken = null;
      current.noteWorkbenchDispose?.(); current.noteWorkbenchDispose = null; current.noteWorkbenchHost = null;
    }
    for (const leafId of [...(current?.noteLeafViews?.keys() || [])]) disposeLeaf(leafId);
    if (current?.noteKeyHandler) current.window.root.removeEventListener('keydown', current.noteKeyHandler);
    if (current?.notePageHideHandler) window.removeEventListener('pagehide', current.notePageHideHandler);
    if (current?.noteBeforeUnloadHandler) window.removeEventListener('beforeunload', current.noteBeforeUnloadHandler);
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
    if (!getSharedDocumentState && globalThis.__openClankOpenResourceHandle === resourceOpener) globalThis.__openClankOpenResourceHandle = previousResourceOpener;
  }

  function getDraftSnapshot(docId) {
    const current = context();
    bindSharedDocumentState(current);
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
    bindSharedDocumentState(current);
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
    doc.savePolicy = 'explicit';
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
    return saveDraft(docId, { returnReceipt:true, snapshot:buffer.uncertainSave || buffer.pending });
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
    bindSharedDocumentState(current);
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
    bindSharedDocumentState(current);
    if (scope && !sameSaveScope(scope, bufferScope(current))) return source;
    current.noteAcceptedEnvelopes ||= new Map();
    const authoritative = cloneEnvelope({
      text:source.text, properties:source.properties, relations:source.relations,
      ...(source.extensions == null ? {} : { extensions:source.extensions }),
    });
    current.noteAcceptedEnvelopes.set(source.id, authoritative);
    const draft = current?.noteDrafts?.get(source.id);
    const buffer = current?.noteBuffers?.get(source.id);
    if (buffer && !buffer.state().dirty && !buffer.running && !buffer.drainPromise) {
      const head = String(source.head || '');
      const knownHead = String(buffer.handle?.revision?.value || '');
      if (head && knownHead !== head || JSON.stringify(buffer.envelope) !== JSON.stringify(authoritative)) {
        buffer.resolveExternal(authoritative, { kind:'copalHead', value:head || knownHead }, { force:false });
      }
    }
    const envelope = buffer && (buffer.state().dirty || buffer.running || buffer.drainPromise) ? buffer.envelope : draft?.envelope;
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
      const accountId = achievementOwner(), workspaceId = state.workspace;
      active.editor.insertText(rebaseTemplateLinks(expanded.text, template.name, activeDoc().name));
      if (expanded.text.trim()) {
        const draft = current.noteDrafts.get(target.docId);
        if (draft) (draft.savedEvidence ||= []).push({ type:'template.section.inserted', fields:{ documentId:target.docId, spanNonempty:true, sectionKey:template.id }, options:{ accountId, workspaceId, kind:'R' } });
      }
      current.window.setStatus(expanded.diagnostics.length ? expanded.diagnostics.join(' ') : `Inserted ${displayName(template)}.`);
    } });
  }

  function createTemplateFromCurrent() {
    const capturedWorkspace = ensureWorkspace();
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
          && target.scope === currentScope() && context() === current && current?.noteWorkspace === capturedWorkspace
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

  function editorCreationFolder(workspace, current = context()) {
    const explorer = current?.noteFilesExplorer;
    if (explorer?.ready && explorer.scope === currentScope(current)) return explorer.directoryReady ? explorer.displayedRoot : null;
    return current?.noteResourceRootReady === true ? workspace?.left?.resourceRoot || null : null;
  }

  function editorCreationReason(workspace, current = context()) {
    const explorer = current?.noteFilesExplorer;
    if (explorer?.directoryPending || explorer?.ready && !explorer.directoryReady) return 'The displayed folder is being authorized. Retry or choose a folder before creating files.';
    const folder = editorCreationFolder(workspace, current);
    if (workspace?.left?.folderWorkspaceRoot && folder?.provider !== 'host') return 'Browse to a Host folder to create files, or use Close Folder to return to Copal.';
    if (folder?.provider === 'host' && (isHostLocationsRoot(folder) || folder.capabilities?.write !== true)) return 'This folder does not support creating files.';
    return '';
  }

  function refreshEditorCreationFolder(workspace, folder) {
    const explorer = context()?.noteFilesExplorer;
    return explorer?.ready ? explorer.refresh()
      : loadEditorResourceFolder(workspace, folder, { query:workspace.left.resourceQuery || '', commitHistory:false });
  }

  function createNew(initial = null, { isContextCurrent = () => true } = {}) {
    const workspace = ensureWorkspace();
    const reason = editorCreationReason(workspace);
    if (reason) { context()?.window?.setStatus(reason, true); return false; }
    const folder = editorCreationFolder(workspace);
    if (folder?.provider === 'host') {
      const capturedCurrent = context();
      const capturedScope = currentScope();
      const ref = String(folder.ref || '');
      const capturedKey = canonicalEditorResourceKey(folder.resourceKey || ref, folder.provider || '');
      const capturedAnchor = canonicalEditorResourceKey(workspace.left.folderWorkspaceRoot?.resourceKey || workspace.left.folderWorkspaceRoot?.ref, workspace.left.folderWorkspaceRoot?.provider || '');
      const capturedEpoch = Number(capturedCurrent?.noteResourceRequestEpoch || 0);
      let expectedEpoch = capturedEpoch;
      const stillCurrent = () => isContextCurrent() && capturedCurrent === context() && capturedCurrent?.noteWorkspace === workspace && capturedScope === currentScope()
        && Number(capturedCurrent?.noteResourceRequestEpoch || 0) === expectedEpoch
        && canonicalEditorResourceKey(editorCreationFolder(workspace)?.resourceKey, editorCreationFolder(workspace)?.provider || '') === capturedKey
        && !editorCreationReason(workspace)
        && canonicalEditorResourceKey(workspace.left.folderWorkspaceRoot?.resourceKey || workspace.left.folderWorkspaceRoot?.ref, workspace.left.folderWorkspaceRoot?.provider || '') === capturedAnchor;
      showForm('New file', [['name', 'Name', initial || 'Untitled.md']], async ({ name }) => {
        const requested = String(name || '').trim();
        if (!ref || !requested || !stillCurrent() || folder.capabilities?.write !== true) throw new Error('The folder changed; choose New again.');
        const roots = await filesFacadeClient.roots({ copalWorkspace:state.workspace || 'default' });
        const generation = Number(roots?.policy_generation);
        if (!Number.isSafeInteger(generation) || generation < 0) throw new Error('Folder creation policy is unavailable.');
        if (!stillCurrent()) throw new Error('The folder changed; choose New again.');
        const operationId = 'copal-folder-create-' + (globalThis.crypto?.randomUUID?.() || Date.now());
        const created = await filesFacadeClient.createFile(ref, { name:requested, operationId, itemId:operationId, generation, collision:'fail' });
        if (!stillCurrent()) return;
        const item = created?.items?.find?.((entry) => entry?.item_id === operationId && ['committed', 'unchanged'].includes(entry?.outcome));
        const resourceRef = String(item?.resource_ref || '');
        if (!resourceRef) throw new Error('The new file was not committed.');
        const reloadEpoch = expectedEpoch + 1;
        const reloaded = await refreshEditorCreationFolder(workspace, folder);
        if (!reloaded || Number(capturedCurrent?.noteResourceRequestEpoch || 0) !== reloadEpoch) return;
        expectedEpoch = reloadEpoch;
        if (!stillCurrent()) return;
        await resourceOpener({ resourceRef, name:requested, parentResourceRef:ref });
      });
      return;
    }
    showForm('New Copal note', [['name', 'Name', initial || 'Untitled'], ['content', 'Starting text', '', 'textarea']], async ({ name, content }) => {
      if (!isContextCurrent()) throw new Error('The captured Editor target changed. Reopen New.');
      await createDatabaseNote(name, content);
    });
  }

  function createFolder({ isContextCurrent = () => true } = {}) {
    const workspace = ensureWorkspace();
    const reason = editorCreationReason(workspace);
    if (reason) { context()?.window?.setStatus(reason, true); return false; }
    const parent = editorCreationFolder(workspace);
    if (parent?.provider !== 'host') { createNew('New collection/Untitled', { isContextCurrent }); return; }
    const capturedCurrent = context();
    const capturedScope = currentScope();
    const ref = String(parent.ref || '');
    const capturedKey = canonicalEditorResourceKey(parent.resourceKey || ref, parent.provider || '');
    const capturedAnchor = canonicalEditorResourceKey(workspace.left.folderWorkspaceRoot?.resourceKey || workspace.left.folderWorkspaceRoot?.ref, workspace.left.folderWorkspaceRoot?.provider || '');
    const capturedEpoch = Number(capturedCurrent?.noteResourceRequestEpoch || 0);
    const stillCurrent = () => isContextCurrent() && capturedCurrent === context() && capturedCurrent?.noteWorkspace === workspace && capturedScope === currentScope()
      && Number(capturedCurrent?.noteResourceRequestEpoch || 0) === capturedEpoch
      && canonicalEditorResourceKey(editorCreationFolder(workspace)?.resourceKey, editorCreationFolder(workspace)?.provider || '') === capturedKey
      && !editorCreationReason(workspace)
      && canonicalEditorResourceKey(workspace.left.folderWorkspaceRoot?.resourceKey || workspace.left.folderWorkspaceRoot?.ref, workspace.left.folderWorkspaceRoot?.provider || '') === capturedAnchor;
    showForm('New folder', [['name', 'Name', 'Untitled folder']], async ({ name }) => {
      if (!ref || !String(name || '').trim() || !stillCurrent() || parent.capabilities?.write !== true) throw new Error('The folder changed; choose New again.');
      const roots = await filesFacadeClient.roots({ copalWorkspace:state.workspace || 'default' });
      const generation = Number(roots?.policy_generation);
      if (!Number.isSafeInteger(generation) || generation < 0 || !stillCurrent()) throw new Error('Folder creation policy is unavailable.');
      await filesFacadeClient.createDirectory(ref, {
        name:String(name).trim(),
        operationId:'copal-folder-create-' + (globalThis.crypto?.randomUUID?.() || Date.now()),
        generation,
        expectedRevision:parent.revision || null,
      });
      if (!stillCurrent()) return;
      await refreshEditorCreationFolder(workspace, parent);
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

  function createFromTemplate({ isContextCurrent = () => true } = {}) {
    const templates = templateDocuments();
    const folderMode = Boolean(ensureWorkspace()?.left?.folderWorkspaceRoot);
    const label = folderMode ? 'New Copal note from template' : 'New from template';
    if (!templates.length) {
      context().window.setStatus('No templates yet. Set a note’s type property to template.');
      return;
    }
    showChooser({ title:label, docs:templates, allowCreate:false, choose:(template) => {
      if (!isContextCurrent()) throw new Error('The captured Editor target changed. Reopen New from template.');
      const initial = `${displayName(template)} copy`;
      showForm(label, [['name', 'Name', initial]], async ({ name }) => {
        if (!isContextCurrent()) throw new Error('The captured Editor target changed. Reopen New from template.');
        const properties = copyTemplateProperties(template.properties);
        const expanded = expandTemplate(template.text, { title:name, now:new Date(), timeZone:Intl.DateTimeFormat().resolvedOptions().timeZone });
        const accountId = achievementOwner(), workspaceId = state.workspace;
        const created = await createDatabaseNote(name, expanded.text, properties);
        if (created?.id && created?.head) await recordPresentation('template.document.created', {
          templateId:template.id, documentId:created.id, revisionId:String(created.head), committed:true,
        }, { accountId, workspaceId, kind:'R', occurrenceId:`${created.id}:${created.head}` });
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

  function canMakeEditableWikiCopy(doc) {
    return typeof canCopyWikiArticle === 'function' && canCopyWikiArticle(doc)
      && typeof makeEditableWikiCopy === 'function';
  }

  function officialWikiCopyIdentity(doc) {
    const properties = doc?.properties && typeof doc.properties === 'object' && !Array.isArray(doc.properties)
      ? doc.properties : {};
    return JSON.stringify([
      String(doc?.id || ''), String(doc?.name || ''), String(doc?.kind || ''), String(doc?.head || ''),
      doc?.readOnly === true, doc?.builtin === true, String(doc?.product || ''),
      properties.builtin === true, String(properties.product || ''), String(properties.docId || ''),
      properties.seedVersion ?? null,
    ]);
  }

  function officialWikiCopySource(doc) {
    const source = { ...doc };
    const properties = doc?.properties && typeof doc.properties === 'object' && !Array.isArray(doc.properties)
      ? { ...doc.properties } : {};
    // A personal copy gets its own server document ID. Do not carry the
    // official seed identity into its note properties or a future provision
    // could mistake this editable copy for the canonical article.
    for (const key of ['builtin', 'product', 'docId', 'seedVersion']) delete properties[key];
    for (const key of ['builtin', 'product', 'docId', 'seedVersion', 'officialRef', 'officialDigest', 'officialVersion']) delete source[key];
    source.properties = properties;
    return source;
  }

  function makeEditableWikiCopyFrom(doc) {
    if (!canMakeEditableWikiCopy(doc)) throw new Error('This Wiki article is no longer available to copy.');
    return makeEditableWikiCopy(officialWikiCopySource(doc));
  }

  function startEditableWikiCopy(doc) {
    void Promise.resolve().then(() => makeEditableWikiCopyFrom(doc)).catch((error) => {
      context()?.window?.setStatus(error?.message || 'The editable Wiki copy could not be created.', true);
    });
  }

  function registerWikiArticleContextMenu(cache, leaf, doc, workspace, group) {
    cache.wikiArticleContextDispose?.(); cache.wikiArticleContextDispose = null;
    if (!canMakeEditableWikiCopy(doc)) return;
    const current = context();
    const captured = Object.freeze({
      current, workspace, scope:currentScope(current), leafId:leaf.id, groupId:group.id,
      docId:doc.id, name:doc.name, head:String(doc.head || ''), identity:officialWikiCopyIdentity(doc),
    });
    cache.wikiArticleContextDispose = registerAdapter(cache.root, {
      capture:() => captured,
      commands:(request) => request?.adapterContext === captured && cache.root.isConnected
        ? [{ id:'copal-wiki-make-editable-copy', label:'Make editable copy' }] : [],
      execute:async (command, request) => {
        if (command !== 'copal-wiki-make-editable-copy') return false;
        const currentContext = context();
        const currentWorkspace = currentContext?.noteWorkspace;
        const currentLeaf = findWorkspaceLeaf(currentWorkspace, captured.leafId);
        const currentGroup = currentLeaf && groupForLeaf(currentWorkspace, currentLeaf.id);
        const currentDoc = state.docs.find((item) => item.id === captured.docId);
        if (request?.adapterContext !== captured || captured.current !== currentContext
          || captured.workspace !== currentWorkspace || currentWorkspace !== workspace
          || captured.scope !== currentScope(currentContext) || currentLeaf?.docId !== captured.docId
          || currentGroup?.id !== captured.groupId || !cache.root.isConnected || !currentDoc
          || currentDoc.name !== captured.name || String(currentDoc.head || '') !== captured.head
          || officialWikiCopyIdentity(currentDoc) !== captured.identity || !canMakeEditableWikiCopy(currentDoc)) {
          throw new Error('This Wiki article changed. Reopen its menu and try again.');
        }
        await makeEditableWikiCopyFrom(currentDoc);
        return true;
      },
    });
  }

  function renameWithForm(doc) {
    if (!canManageCopalDocument(doc)) return;
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
      { category:'Tables', title:'Typed table (dates, currency, totals)', source:'<!-- clank-table v=1 id=tbl-demo\ncolumn id=col-when type=date format=locale\ncolumn id=col-item type=text format=auto\ncolumn id=col-amount type=currency format=locale\ncolumn id=col-qty type=number format=locale\n-->\n| When | Item | Amount | Qty |\n| :--- | :--- | ---: | ---: |\n| 2026-09-22 | Hosting | USD 12.50 | 2 |\n| 2026-10-01 | Stickers | USD 4.00 | 5 |\n| 2026-10-15 | Lunch | USD 9.25 | 1 |\n| | Total | =SUM(C1:C3) | =SUM(D1:D3) |' },
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
    const workbench = window.__openClankWorkbench;
    if (workbench?.openPalette) {
      ensureWorkbenchProvider();
      // Explicit root capture keeps header invocations bound to the active
      // leaf editor; it never treats the header button as a text destination.
      workbench.openPalette(workbench.capture(context().window.root));
      return;
    }
    const workspace = ensureWorkspace();
    const folderMode = Boolean(workspace?.left?.folderWorkspaceRoot);
    const doc = activeDoc(workspace);
    const actions = [
      [folderMode ? 'New File' : 'New note', 'Ctrl+N', () => createNew()],
      ['Open Folder', '', openFolderFromPicker],
      ...(workspace?.left?.folderWorkspaceRoot ? [['Close Folder / Back to Copal', '', () => closeFolderWorkspace(workspace)]] : []),
      ...(typeof createWikiArticle === 'function' ? [['New Wiki article', '', createWikiArticle]] : []),
      [folderMode ? 'Open Copal today’s note' : 'Open today’s note', '', openDailyNote],
      [folderMode ? 'New Copal note from template' : 'New from template', '', createFromTemplate],
      ['Insert template', '', insertTemplate],
      ['Create template from current document', '', createTemplateFromCurrent],
      ...(doc?.savePolicy === 'explicit' && !doc.readOnly ? [['Save file', 'Ctrl+S', () => void saveDraft(doc.id)]] : []),
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
      ['Toggle Editor sidebar', '', () => toggleSidebar('left')],
      ['Toggle linked sidebar', '', () => toggleSidebar('right')],
      ...(doc?.virtual ? [] : [['Toggle bookmark', '', () => doc && toggleBookmark(doc.id)]]),
      ...(doc ? [[presentationId === 'wiki' ? 'Open in Editor' : 'Open in Wiki', '', () => openOtherView?.(doc.id, presentationId === 'wiki' ? 'notes' : 'wiki')]] : []),
      ['Reopen closed note', '', reopenClosed],
      ...(doc?.virtual ? [] : [['History', '', () => doc && (isHostDocument(doc) ? void openEditorResourceHistory(doc) : showHistory(doc))]]),
      ['Open Trash', '', () => showTrash()],
      ...(canManageCopalDocument(doc) ? [['Move current note to Trash', '', () => deleteDocument(doc)]] : []),
      ['Import Markdown or Obsidian backup', '', importVault],
      ...(typeof importWikiMemes === 'function' ? [['Import native .memes', '', importWikiMemes]] : []),
      ['Syntax gallery', '', () => { const gallery = state.docs.find((d) => d.name.includes('Syntax Gallery') || d.name.includes('syntax-gallery')); if (gallery) open(gallery.id); else showSyntaxGallery(); }],
      ['Export Markdown backup', '', () => { window.location.href = `/api/copal/export/obsidian?workspace=${encodeURIComponent(state.workspace)}`; }],
      ...(typeof exportWikiMemes === 'function' ? [['Export native .memes', '', exportWikiMemes]] : []),
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
    const folderMode = Boolean(workspace?.left?.folderWorkspaceRoot);
    return h('section', { class:'copal-empty copal-notes-empty-workspace', tabindex:'-1', role:'region', 'aria-label':'No open documents' },
      h('h2', { text:'No open documents' }),
      h('p', { text:'The workspace is empty. Nothing was reopened for you — pick what to open next.' }),
      h('div', { class:'copal-empty-workspace-actions' },
        commandButton(folderMode ? 'New File' : 'New note', () => createNew(), { class:'copal-btn primary' }),
        commandButton('Quick switcher', () => showChooser()),
        commandButton('Reopen closed', reopenClosed, workspace.closed.length ? {} : { disabled:true }),
        commandButton('Open Timeline', () => open(TIMELINE_DOCUMENT.id)),
        commandButton('Import backup', importVault)));
  }

  function bindKeys(workspace, doc) {
    const current = context();
    if (current.noteKeyHandler) current.window.root.removeEventListener('keydown', current.noteKeyHandler);
    current.noteKeyHandler = (event) => {
      if (event.defaultPrevented || event.isComposing || !(event.ctrlKey || event.metaKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === 'o') { event.preventDefault(); event.stopPropagation(); showChooser(); }
      else if (key === 'p') { event.preventDefault(); event.stopPropagation(); showCommands(); }
      else if (key === 'f' && event.shiftKey) { event.preventDefault(); event.stopPropagation(); showSearch(); }
      else if (key === 'n') { event.preventDefault(); event.stopPropagation(); createNew(); }
      else if (key === 's' && doc) { event.preventDefault(); event.stopPropagation(); void saveDraft(doc.id); }
      else if (key === 'w' && doc) {
        event.preventDefault(); event.stopPropagation();
        const leaf = activeLeaf(workspace); if (!leaf || leaf.pinned) return;
        void requestCloseLeaves([leaf]).then(allowed => { if (allowed && closeWorkspaceLeaf(workspace, leaf.id)) { disposeLeaf(leaf.id); persist(true); render(); focusEmptyWorkspaceIfIdle(); } });
      } else if ((key === 'z' || key === 'y') && doc && !doc.readOnly && !event.target.closest?.('input, textarea, [contenteditable]')) {
        event.preventDefault(); event.stopPropagation(); undoDocument(doc.id, key === 'y' || event.shiftKey);
      }
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
      workspace.left.selected, workspace.settings?.showTutorialFolder, activeLeaf(workspace)?.docId || null,
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
    const tutorialInfo = (node) => {
      let total = node.docs.length;
      let tutorials = node.docs.filter((doc) => isOfficialDocument(doc)
        || (doc.owner === 'shared' && String(doc.name || '').startsWith('OpenClank/'))).length;
      for (const child of node.folders.values()) {
        const info = tutorialInfo(child);
        total += info.total; tutorials += info.tutorials;
      }
      return { total, tutorials };
    };
    const draw = (node, parent, path = []) => {
      for (const [name, folder] of [...node.folders].sort(([a], [b]) => a.localeCompare(b))) {
        const full = [...path, name].join('/'); const isOpen = expanded.has(full);
        const row = h('div', { class:'copal-folder-row', role:'treeitem', 'data-copal-context-object':'copal-folder', 'data-note-tree-key':`folder:${full}`, 'data-note-parent':path.join('/'), 'aria-expanded':String(isOpen), tabindex:'0' }, iconSpan(uiIcon(isOpen ? 'chevron-down' : 'chevron-right', 12), 'copal-tree-toggle'), iconSpan(fileIcon({ name, kind:'folder', open:isOpen }, 16), 'copal-file-kind'), h('span', { text:name }));
        const children = h('div', { class:'copal-tree-children', role:'group' }); children.hidden = !isOpen;
        const setFolderOpen = (open) => {
          const currentExpanded = new Set(workspace.left.expanded);
          if (open) currentExpanded.add(full); else currentExpanded.delete(full);
          workspace.left.expanded = [...currentExpanded];
          persist(true); render();
          if (!open) context()?.window.root.querySelector(`[data-note-tree-key="folder:${CSS.escape(full)}"]`)?.focus({ preventScroll:true });
        };
        const toggle = () => {
          // A focused descendant is removed when its folder closes; restore
          // keyboard focus after render rather than leaving it on a hidden row.
          setFolderOpen(!new Set(workspace.left.expanded).has(full));
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
        const info = tutorialInfo(folder);
        if (info.tutorials) {
          const actionLabel = info.tutorials < info.total
            ? 'Hide built-in tutorials in this folder'
            : 'Hide Open Clank tutorial folder';
          const folderMenu = wirePopover(h('details', { class:'copal-file-menu copal-folder-menu' },
            h('summary', { title:'Folder actions', 'aria-label':'Folder actions', text:'⋯' }),
            h('div', { class:'copal-popover-menu' },
              commandButton(actionLabel, () => updateSettings({ showTutorialFolder:false })))));
          folderMenu.addEventListener('click', (event) => event.stopPropagation());
          folderMenu.addEventListener('keydown', (event) => event.stopPropagation());
          row.append(folderMenu);
        }
        const folderContext = () => {
          const current = context();
          const descendants = documents().filter((doc) => doc.name.startsWith(`${full}/`));
          return Object.freeze({
            current, workspace, scope:currentScope(current), path:full,
            descendantSignature:JSON.stringify(descendants.map((doc) => [doc.id, doc.name, String(doc.head || '')]).sort((a, b) => a[0].localeCompare(b[0]))),
            hasTutorials:info.tutorials > 0,
            canCreateNote:!workspace.left.folderWorkspaceRoot && workspace.left.resourceRoot?.provider !== 'host',
          });
        };
        registerAdapter(row, {
          capture:folderContext,
          commands:(request) => {
            const captured = request?.adapterContext;
            if (!captured) return [];
            const commands = [{ id:'copal-folder-toggle', label:workspace.left.expanded.includes(full) ? 'Collapse folder' : 'Expand folder' }];
            if (captured.canCreateNote) commands.push({ id:'copal-folder-new-note', label:'New note in folder' });
            if (captured.hasTutorials) commands.push({ id:'copal-folder-hide-tutorials', label:info.tutorials < info.total ? 'Hide built-in tutorials in this folder' : 'Hide Open Clank tutorial folder' });
            return commands;
          },
          execute:async (command, request) => {
            const captured = request?.adapterContext;
            const current = context();
            const descendants = documents().filter((doc) => doc.name.startsWith(`${full}/`));
            const signature = JSON.stringify(descendants.map((doc) => [doc.id, doc.name, String(doc.head || '')]).sort((a, b) => a[0].localeCompare(b[0])));
            if (!captured || captured.current !== current || current?.noteWorkspace !== workspace || captured.workspace !== workspace
              || captured.scope !== currentScope(current) || captured.path !== full || captured.descendantSignature !== signature) {
              throw new Error('This Copal folder changed. Reopen its menu and try again.');
            }
            if (command === 'copal-folder-toggle') {
              setFolderOpen(!workspace.left.expanded.includes(full));
              return true;
            }
            if (command === 'copal-folder-new-note' && captured.canCreateNote) {
              showForm('New Copal note', [['name', 'Name', `${full}/Untitled`], ['content', 'Starting text', '', 'textarea']], async ({ name, content }) => {
                const liveContext = context();
                const liveDescendants = documents().filter((doc) => doc.name.startsWith(`${full}/`));
                const liveSignature = JSON.stringify(liveDescendants.map((doc) => [doc.id, doc.name, String(doc.head || '')]).sort((a, b) => a[0].localeCompare(b[0])));
                if (liveContext !== captured.current || liveContext?.noteWorkspace !== workspace || currentScope(liveContext) !== captured.scope
                  || liveSignature !== captured.descendantSignature || workspace.left.folderWorkspaceRoot || workspace.left.resourceRoot?.provider === 'host') {
                  throw new Error('This Copal folder changed. Reopen its menu and try again.');
                }
                await createDatabaseNote(name, content);
              });
              return true;
            }
            if (command === 'copal-folder-hide-tutorials' && captured.hasTutorials) {
              updateSettings({ showTutorialFolder:false });
              return true;
            }
            return false;
          },
        });
        parent.append(row, children); draw(folder, children, [...path, name]);
      }
      for (const doc of sorted(node.docs)) {
        const selected = activeLeaf(workspace)?.docId === doc.id;
        const chosen = selectedIds.has(doc.id);
        const row = h('div', { class:`copal-file-entry${selected ? ' active' : ''}${chosen ? ' selected' : ''}` });
        const openButton = h('button', { class:'copal-file-row', role:'treeitem', 'data-copal-context-object':'copal-document', 'data-document-id':doc.id, 'data-note-tree-key':`document:${doc.id}`, 'data-note-parent':path.join('/'), 'aria-selected':String(chosen || selected), draggable:canManageCopalDocument(doc) ? 'true' : false, title:doc.name, onclick:(event) => {
          if (canManageCopalDocument(doc) && (event.ctrlKey || event.metaKey)) {
            chosen ? selectedIds.delete(doc.id) : selectedIds.add(doc.id);
            workspace.left.selected = [...selectedIds]; persist(true); render(); return;
          }
          workspace.left.selected = []; open(doc.id);
        } },
          fileGlyph(doc), h('span', { text:displayName(doc) }), ...(doc.readOnly ? [h('small', { class:'copal-readonly-badge', text:'RO', title:'Read only', 'aria-label':'Read only' })] : []));
        if (canManageCopalDocument(doc)) openButton.addEventListener('dragstart', (event) => event.dataTransfer.setData('text/x-copal-document', doc.id));
        const menuItems = [
          ...(canManageCopalDocument(doc) ? [commandButton('Rename or move', () => renameWithForm(doc))] : []),
          commandButton('Open in new tab', () => open(doc.id, { intent:'newTab' })),
          commandButton('Open right', () => open(doc.id, { intent:'splitRight' })),
          commandButton('Open below', () => open(doc.id, { intent:'splitBelow' })),
          commandButton('Reveal path', () => { revealInExplorer(doc, workspace); persist(true); render(); }),
          ...(canManageCopalDocument(doc) ? [commandButton('Trash', () => deleteDocument(doc), { class:'copal-btn danger' })] : []),
        ];
        const menu = wirePopover(h('details', { class:'copal-file-menu' }, h('summary', { title:`Actions for ${doc.name}`, 'aria-label':`Actions for ${doc.name}`, text:'⋯' }),
          h('div', { class:'copal-popover-menu' }, menuItems)));
        registerAdapter(openButton, {
          capture:() => Object.freeze({
            current, workspace, scope:currentScope(current), docId:doc.id, name:doc.name,
            head:String(doc.head || ''), readOnly:Boolean(doc.readOnly || doc.virtual), managed:canManageCopalDocument(doc), kind:String(doc.kind || ''),
          }),
          commands:(request) => {
            const captured = request?.adapterContext;
            if (!captured) return [];
            return [
              { id:'copal-document-open', label:'Open' },
              { id:'copal-document-new-tab', label:'Open in new tab' },
              { id:'copal-document-split-right', label:'Open right' },
              { id:'copal-document-split-below', label:'Open below' },
              { id:'copal-document-reveal', label:'Reveal path' },
              ...(!captured.managed || captured.kind === 'timeline' ? [] : [
                { id:'copal-document-rename', label:'Rename or move' },
                { id:'copal-document-trash', label:'Trash' },
              ]),
            ];
          },
          execute:async (command, request) => {
            const captured = request?.adapterContext;
            const currentContext = context();
            const target = state.docs.find((item) => item.id === captured?.docId);
            if (!captured || captured.current !== currentContext || captured.workspace !== workspace || currentContext?.noteWorkspace !== workspace
              || captured.scope !== currentScope(currentContext) || !target || target.name !== captured.name
              || String(target.head || '') !== captured.head || Boolean(target.readOnly || target.virtual) !== captured.readOnly
              || canManageCopalDocument(target) !== captured.managed
              || String(target.kind || '') !== captured.kind) {
              throw new Error('This Copal document changed. Reopen its menu and try again.');
            }
            if (command === 'copal-document-open') { open(target.id); return true; }
            if (command === 'copal-document-new-tab') { open(target.id, { intent:'newTab' }); return true; }
            if (command === 'copal-document-split-right') { open(target.id, { intent:'splitRight' }); return true; }
            if (command === 'copal-document-split-below') { open(target.id, { intent:'splitBelow' }); return true; }
            if (command === 'copal-document-reveal') { revealInExplorer(target, workspace); persist(true); render(); return true; }
            if (command === 'copal-document-rename' && captured.managed && captured.kind !== 'timeline') {
              showForm(`Rename ${target.name}`, [['name', 'Path', target.name]], async ({ name }) => {
                const liveContext = context();
                const liveTarget = state.docs.find((item) => item.id === captured.docId);
                if (liveContext !== captured.current || liveContext?.noteWorkspace !== captured.workspace
                  || currentScope(liveContext) !== captured.scope || !liveTarget || liveTarget.name !== captured.name
                  || String(liveTarget.head || '') !== captured.head || !canManageCopalDocument(liveTarget)) {
                  throw new Error('This Copal document changed. Reopen its menu and try again.');
                }
                await renameNote(liveTarget, name);
                render();
              });
              return true;
            }
            if (command === 'copal-document-trash' && captured.managed && captured.kind !== 'timeline') {
              await deleteDocument(target);
              return true;
            }
            return false;
          },
        });
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
    const span = h('span', { class:'copal-file-kind', 'aria-hidden':'true' });
    const type = noteViewType(doc);
    span.innerHTML = ['canvas', 'base', 'timeline'].includes(type)
      ? uiIcon(({ canvas:'image', base:'database', timeline:'timeline' })[type], 15)
      : fileIcon({ name:doc?.name || 'Document', kind:'file', language:isHostDocument(doc) ? languageForPath(doc.name) : ['note','markdown','wiki'].includes(type) ? 'Markdown' : undefined }, 15);
    return span;
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

  function iconSpan(markup, className) {
    const span = h('span', { class:className, 'aria-hidden':'true' }); span.innerHTML = markup; return span;
  }

  function iconCommandButton(icon, run, attrs = {}) {
    const button = commandButton('', run, attrs);
    button.innerHTML = uiIcon(icon, 16);
    return button;
  }

  function disposeEditorFiles(current) {
    current.noteResourcePicker?.destroy(); current.noteResourcePicker = null;
    current.noteFilesExplorer?.dispose(); current.noteFilesExplorer = null;
    current.noteDocumentNavigation?.dispose(); current.noteDocumentNavigation = null;
    current.noteDocumentNavigationScope = null; current.noteDocumentNavigationKey = null;
  }

  function trackDocumentNavigation(current, workspace) {
    const scope = currentScope(current);
    if (!current.noteDocumentNavigation || current.noteDocumentNavigationScope !== scope) {
      current.noteDocumentNavigation?.dispose();
      current.noteDocumentNavigationScope = scope;
      current.noteDocumentNavigationKey = null;
      current.noteDocumentNavigation = createWindowNavigation({
        scope:{ account:state.accountId || '', workspace:state.workspace || '' },
        restore:entry => {
          if (current !== context() || scope !== currentScope(current) || !current.window.visible) return false;
          const model = current.noteWorkspace;
          const doc = workspaceDocuments().find(item => item.id === entry.docId);
          if (!doc || !findWorkspaceGroup(model, entry.groupId)) return false;
          current.noteDocumentNavigationRestoring = true;
          try {
            const leaf = findWorkspaceLeaf(model, entry.leafId);
            if (leaf?.docId === entry.docId) setActive(entry.groupId, entry.leafId);
            else open(entry.docId, { groupId:entry.groupId, intent:'current' });
            return true;
          } finally { current.noteDocumentNavigationRestoring = false; }
        },
      });
    }
    const leaf = activeLeaf(workspace);
    if (!leaf) return;
    const group = groupForLeaf(workspace, leaf.id);
    const key = `${leaf.docId}:${group?.id || ''}:${leaf.id}`;
    if (key === current.noteDocumentNavigationKey) return;
    current.noteDocumentNavigationKey = key;
    if (!current.noteDocumentNavigationRestoring) current.noteDocumentNavigation.commit({
      docId:leaf.docId, groupId:group?.id, leafId:leaf.id,
      scope:{ account:state.accountId || '', workspace:state.workspace || '' },
    });
  }

  function editorFilesExplorer(workspace) {
    const current = context();
    const scope = currentScope(current);
    let record = current.noteFilesExplorer;
    if (record?.scope !== scope) { record?.dispose(); record = null; }
    if (record) { record.sync(); record.update(); return record.element; }
    const controller = new AbortController();
    const browserHost = h('div', { class:'copal-editor-files-mount' });
    const controls = h('nav', { class:'copal-editor-files-navigation', 'aria-label':'Explorer folder navigation' });
    const location = h('div', { class:'copal-editor-files-location' });
    const status = h('div', { class:'copal-editor-files-status', role:'status' });
    const retry = commandButton('Retry', () => { void record.refresh(); }, { class:'copal-editor-files-retry', title:'Retry the current or chosen Editor folder', hidden:true });
    const element = h('section', { class:'copal-editor-files-explorer', 'aria-label':'Files places and folders', 'data-copal-context-object':'editor-resource' }, controls, location, browserHost, status, retry);
    record = { scope, element, browser:null, resources:new Map(), ready:false, syncing:false, syncedRef:null, syncedGeneration:null,
      requestEpoch:0, requestController:null, navigationPromise:null, activeNavigation:null, interactionEpoch:0,
      pendingSync:false, pendingReveal:null, pendingAction:null, failedSync:null,
      displayedRoot:null, directoryReady:false, directoryPending:false,
      filterQuery:workspace.left.resourceQuery || '',
      dispose:null, sync:null, update:null, refresh:null, reveal:null, navigate:null };
    current.noteFilesExplorer = record;
    const stillCurrent = () => !controller.signal.aborted && current === context() && current.noteFilesExplorer === record && scope === currentScope(current);
    const showError = error => { if (stillCurrent()) { status.textContent = error?.message || 'Folder unavailable.'; status.classList.add('error'); retry.hidden = false; } };
    const clearError = () => { status.textContent = ''; status.classList.remove('error'); retry.hidden = true; };
    const filesGeneration = () => Number(state.filesGeneration || state.contextEpoch || 0);
    const cache = row => {
      const resource = normalizeAuthorizedResource(filesBrowserResource(row), { purpose:'file', ...pickerScope() });
      if (resource.resourceId) record.resources.set(resource.resourceId, resource);
      return resource;
    };
    const navButtons = new Map();
    for (const [command, label] of [['back','Previous folder'], ['forward','Next folder'], ['up','Parent folder'], ['home','Files home'], ['refresh','Refresh folder']]) {
      const button = iconCommandButton(command, async () => {
        try {
          if (command === 'refresh') await record.refresh();
          else await record.navigate(() => runFilesNavigation(record.browser, command), `${label} could not be completed.`);
          record.update();
        } catch (error) { showError(error); }
      }, { title:label, 'aria-label':label });
      navButtons.set(command, button); controls.append(button);
    }
    const anchorButton = iconCommandButton('workspace', async () => {
      const anchor = current.noteWorkspace?.left?.folderWorkspaceRoot;
      if (!anchor || !record.browser || !stillCurrent()) return;
      try {
        const id = current.noteWorkspace.left.folderWorkspaceId;
        await record.navigate(() => id
          ? record.browser.revealWorkspaceResource(id, '')
          : record.browser.openDirectory(filesBrowserResource(anchor)), 'The chosen Editor folder could not be shown.');
      } catch (error) { showError(error); }
    }, { title:'Chosen Editor workspace folder', 'aria-label':'Chosen Editor workspace folder' });
    controls.append(anchorButton);
    record.update = () => {
      if (!stillCurrent()) return;
      const model = current.noteWorkspace;
      const folder = record.browser?.getCurrentDirectory();
      const anchor = model?.left?.folderWorkspaceRoot;
      location.textContent = [anchor ? `Workspace: ${anchor.name}` : '', folder?.name || 'Available places'].filter(Boolean).join(' · ');
      location.title = location.textContent;
      anchorButton.hidden = !anchor;
      const navigation = record.browser?.window?.navigation;
      navButtons.get('back').disabled = !navigation?.canGoBack?.();
      navButtons.get('forward').disabled = !navigation?.canGoForward?.();
      for (const name of ['up','home','refresh']) navButtons.get(name).disabled = !record.ready;
      retry.disabled = !record.ready || record.directoryPending || current.noteResourceLoading;
      if (current.noteResourceRootError && !record.directoryReady) showError(new Error(current.noteResourceRootError));
      const reason = editorCreationReason(model, current);
      for (const button of current.window.body.querySelectorAll('[data-editor-create]')) {
        button.disabled = !!reason;
        button.title = reason || (button.dataset.editorCreate === 'folder' ? 'New folder' : record.displayedRoot?.provider === 'host' ? 'New file' : 'New Copal note');
      }
    };
    const directoryChanged = async (row, { force = false, guard = stillCurrent } = {}) => {
      if (!stillCurrent() || !guard() || !record.ready
        || !force && record.syncing && record.activeNavigation?.interactionEpoch === record.interactionEpoch) return false;
      record.requestController?.abort();
      const request = record.requestController = new AbortController();
      const epoch = ++record.requestEpoch;
      const generation = filesGeneration();
      const model = current.noteWorkspace;
      const requestCurrent = () => stillCurrent() && guard() && !request.signal.aborted && epoch === record.requestEpoch
        && generation === filesGeneration() && model === current.noteWorkspace;
      record.directoryReady = false; record.directoryPending = !!row;
      record.displayedRoot = null;
      record.update();
      try {
        if (!row) {
          current.noteResourceRequestEpoch = Number(current.noteResourceRequestEpoch || 0) + 1;
          record.directoryReady = true;
          record.syncedRef = model.left.resourceRoot?.ref || null; record.syncedGeneration = generation;
          clearError(); return true;
        }
        const column = filesBrowserResource(row);
        const response = await filesFacadeClient.stat(column.ref, { signal:request.signal });
        if (!requestCurrent()) return false;
        let root = cache(response?.resource || response);
        const anchor = model.left.folderWorkspaceRoot;
        for (const child of row.entries || []) cache(child);
        // Browsing Places is independent of explicitly choosing Open Folder.
        // Once chosen, keep that anchor while visiting Favorites elsewhere.
        if (anchor && root.provider === 'host') {
          if (root.resourceKey === anchor.resourceKey) root = withHostRelativePath(root, '');
          else {
            const revealed = await filesFacadeClient.reveal(root.ref, { signal:request.signal });
            if (!requestCurrent()) return false;
            const ancestors = (revealed.ancestors || []).map(item => cache(item));
            const at = ancestors.findIndex(item => item.resourceKey === anchor.resourceKey);
            if (at >= 0) {
              const relative = [...ancestors.slice(at + 1).map(item => item.name), root.name].join('/');
              root = withHostRelativePath(root, relative);
            }
          }
          if (!requestCurrent()) return false;
          // Persist only locations the chosen Workspace can renew. Outside
          // Favorites are current display targets, not replacement anchors.
          if (savedHostRelativePath(root).present) {
            model.left.resourceRoot = root;
            model.left.resourceRows = (row.entries || []).map(item => withHostChildRelativePath(root, cache(item)));
            model.left.resourceCursor = row.nextCursor || null;
            model.left.resourceQuery = row.query || '';
            current.noteResourceRootReady = true; current.noteResourceRootError = null;
            current.noteResourceRootValidationKey = `${scope}:${generation}:${root.resourceKey}`;
            persist(true);
          }
        }
        current.noteResourceRequestEpoch = Number(current.noteResourceRequestEpoch || 0) + 1;
        record.displayedRoot = root; record.directoryReady = true;
        record.syncedRef = model.left.resourceRoot?.ref || null; record.syncedGeneration = generation;
        clearError(); return true;
      } catch (error) {
        if (requestCurrent() && error?.name !== 'AbortError') showError(error);
        return false;
      } finally {
        if (stillCurrent() && epoch === record.requestEpoch) { record.directoryPending = false; record.update(); }
      }
    };
    const captureNavigation = task => ({ ...task, model:current.noteWorkspace, root:current.noteWorkspace?.left?.resourceRoot,
      anchor:current.noteWorkspace?.left?.folderWorkspaceRoot, workspaceId:current.noteWorkspace?.left?.folderWorkspaceId,
      generation:filesGeneration(), interactionEpoch:record.interactionEpoch });
    const navigationCurrent = task => stillCurrent() && task.model === current.noteWorkspace
      && task.root === current.noteWorkspace?.left?.resourceRoot && task.anchor === current.noteWorkspace?.left?.folderWorkspaceRoot
      && task.workspaceId === current.noteWorkspace?.left?.folderWorkspaceId && task.generation === filesGeneration()
      && task.interactionEpoch === record.interactionEpoch;
    const folderReady = () => !current.noteWorkspace?.left?.resourceRoot?.ref
      || current.noteResourceRootReady === true && !current.noteResourceLoading;
    const drainNavigation = () => {
      if (!stillCurrent() || !record.ready) return Promise.resolve(false);
      if (record.navigationPromise) return record.navigationPromise;
      record.navigationPromise = Promise.resolve().then(async () => {
        let result = false;
        while (stillCurrent()) {
          if (!record.pendingAction && !folderReady()) break;
          const task = record.pendingAction
            ? record.pendingAction
            : record.pendingSync && current.noteWorkspace?.left?.resourceRoot?.ref
              ? captureNavigation({ kind:'sync' })
              : record.pendingReveal && record.pendingReveal.generation === filesGeneration()
                && record.pendingReveal.workspaceId === current.noteWorkspace?.left?.folderWorkspaceId
                ? captureNavigation({ kind:'reveal', ref:record.pendingReveal.ref }) : null;
          if (!task) { record.pendingSync = false; record.pendingReveal = null; break; }
          if (task.kind === 'action') record.pendingAction = null;
          else if (task.kind === 'sync') record.pendingSync = false;
          else record.pendingReveal = null;
          if (!navigationCurrent(task)) continue;
          record.activeNavigation = task; record.syncing = true;
          try {
            if (task.kind === 'sync') {
              const locator = savedHostRelativePath(task.root);
              result = task.workspaceId && locator.present
                ? await record.browser.revealWorkspaceResource(task.workspaceId, locator.path)
                : await record.browser.openDirectory(filesBrowserResource(task.root));
            } else result = await (task.kind === 'reveal' ? record.browser.revealResource(task.ref) : task.run());
            if (!navigationCurrent(task)) {
              // A normalized workspace/renewed ref can replace the captured
              // model while Files is awaiting its own roots or children.
              if (stillCurrent() && task.interactionEpoch === record.interactionEpoch) {
                record.pendingSync = true;
                if (task.kind === 'reveal' && task.generation === filesGeneration()
                  && task.workspaceId === current.noteWorkspace?.left?.folderWorkspaceId && !record.pendingReveal) record.pendingReveal = { ref:task.ref, generation:task.generation, workspaceId:task.workspaceId };
              }
              continue;
            }
            if (result === false) {
              if (task.kind === 'sync') record.failedSync = { ref:task.root.ref, generation:task.generation };
              if (task.kind !== 'action' || task.failure) {
                const failure = record.browser.element?.querySelector('.files-pane-status.error')?.textContent;
                showError(new Error(failure || task.failure || (task.kind === 'sync' ? 'The Editor folder could not be shown.' : 'The Editor file could not be shown.')));
              }
              continue;
            }
            const shown = record.browser.getCurrentDirectory();
            if (shown) cache(shown);
            for (const row of shown?.entries || []) cache(row);
            result = await directoryChanged(shown, { force:true, guard:() => navigationCurrent(task) });
            if (result && task.kind === 'sync') record.failedSync = null;
            else if (!result && task.kind === 'sync' && navigationCurrent(task)) record.failedSync = { ref:task.root.ref, generation:task.generation };
          } catch (error) {
            if (navigationCurrent(task) && error?.name !== 'AbortError') {
              if (task.kind === 'sync') record.failedSync = { ref:task.root.ref, generation:task.generation };
              showError(error);
            }
          } finally { record.activeNavigation = null; record.update(); }
        }
        return result;
      }).finally(() => {
        record.navigationPromise = null; record.syncing = false; record.update();
        if (stillCurrent() && (record.pendingAction || folderReady() && (record.pendingSync || record.pendingReveal))) void drainNavigation();
      });
      return record.navigationPromise;
    };
    record.sync = () => {
      if (!stillCurrent()) return Promise.resolve(false);
      if (record.pendingAction || record.activeNavigation?.kind === 'action') return drainNavigation();
      const root = current.noteWorkspace?.left?.resourceRoot;
      const generation = filesGeneration();
      if (root?.ref && (root.ref !== record.syncedRef || generation !== record.syncedGeneration)
        && !(record.failedSync?.ref === root.ref && record.failedSync.generation === generation)
        && !(record.activeNavigation?.kind === 'sync' && navigationCurrent(record.activeNavigation))) {
        record.pendingSync = true; record.directoryReady = false;
      }
      return drainNavigation();
    };
    record.navigate = (run, failure = '') => {
      if (!stillCurrent()) return Promise.resolve(false);
      record.browser?.cancelReveal(); record.requestController?.abort();
      record.interactionEpoch += 1; record.pendingSync = false; record.pendingReveal = null;
      record.directoryReady = false; record.update();
      record.pendingAction = captureNavigation({ kind:'action', run, failure });
      return drainNavigation();
    };
    record.refresh = () => {
      const displayed = record.directoryReady && record.displayedRoot;
      return record.navigate(async () => {
        const task = record.activeNavigation;
        const model = current.noteWorkspace;
        const root = model.left.resourceRoot || model.left.folderWorkspaceRoot;
        if (!displayed && root && (record.failedSync || current.noteResourceRootReady !== true)) {
          current.noteResourceRootValidationKey = null;
          const renewed = await revalidateSavedResourceRoot(model);
          if (!stillCurrent() || record.activeNavigation !== task || task.interactionEpoch !== record.interactionEpoch || current.noteWorkspace !== model) return false;
          if (renewed !== true) throw new Error(current.noteResourceRootError || 'The Editor folder could not be reauthorized.');
          // This owned renewal legitimately replaces the immutable saved refs.
          Object.assign(task, captureNavigation({}));
          const target = model.left.resourceRoot;
          const locator = savedHostRelativePath(target);
          const opened = model.left.folderWorkspaceId && locator.present
            ? await record.browser.revealWorkspaceResource(model.left.folderWorkspaceId, locator.path)
            : await record.browser.openDirectory(filesBrowserResource(target));
          if (opened) record.failedSync = null;
          return opened;
        }
        return record.browser.refresh();
      }, 'The Editor folder could not be refreshed.');
    };
    const supersedePresentation = () => {
      if (!stillCurrent()) return;
      const wasPresenting = record.activeNavigation?.interactionEpoch === record.interactionEpoch;
      record.browser?.cancelReveal(); record.requestController?.abort();
      record.interactionEpoch += 1; record.pendingSync = false; record.pendingReveal = null;
      current.noteResourceRequestEpoch = Number(current.noteResourceRequestEpoch || 0) + 1;
      if (wasPresenting && record.ready) void directoryChanged(record.browser.getCurrentDirectory());
    };
    // Actual interaction with shared sidebar controls also supersedes automatic
    // presentation, including Home/filter controls that select no tree row.
    browserHost.addEventListener('pointerdown', supersedePresentation, { capture:true, signal:controller.signal });
    browserHost.addEventListener('keydown', supersedePresentation, { capture:true, signal:controller.signal });
    const retainFolderQuery = event => {
      if (!stillCurrent() || !event.target.matches?.('.files-folder-search')) return;
      record.filterQuery = event.target.value;
      if (record.directoryReady && savedHostRelativePath(record.displayedRoot).present) {
        current.noteWorkspace.left.resourceQuery = record.filterQuery; persist(true);
      }
    };
    browserHost.addEventListener('input', retainFolderQuery, { signal:controller.signal });
    browserHost.addEventListener('search', retainFolderQuery, { signal:controller.signal });
    record.reveal = ref => {
      if (!ref || !stillCurrent()) return Promise.resolve(false);
      record.pendingReveal = { ref, generation:filesGeneration(), workspaceId:current.noteWorkspace?.left?.folderWorkspaceId };
      return record.sync();
    };
    const renameRetargetsEditor = resource => state.docs.some(doc =>
      ((doc.resourceRef || doc.resource?.locator?.opaqueRef) === resource.ref
        || canonicalEditorResourceKey(doc.resource?.key || doc.resourceKey, resource.provider) === resource.resourceKey
        || resource.provider === 'host' && resource.resourceId && resource.resourceKey === canonicalEditorResourceKey({
          accountId:resource.accountScope, workspaceId:resource.workspaceScope, provider:'host', resourceId:resource.resourceId,
        }) && canonicalEditorResourceKey(doc.resource?.key || doc.resourceKey, 'host') === canonicalEditorResourceKey({
          accountId:resource.accountScope, workspaceId:'host', provider:'host', resourceId:resource.resourceId,
        }))
      && (workspaceLeaves(current.noteWorkspace).some(leaf => leaf.docId === doc.id)
        || current.noteDrafts?.has(doc.id) || current.noteBuffers?.get(doc.id)?.state?.().dirty));
    const contextDispose = registerAdapter(element, {
      capture:node => {
        const id = node.closest?.('[data-tree-resource-id]')?.dataset.treeResourceId;
        const resource = record.resources.get(id);
        return resource ? Object.freeze({ resource, node, scope, generation:Number(state.filesGeneration || state.contextEpoch || 0), origin:capturePickerOrigin() }) : null;
      },
      commands:request => {
        const resource = request?.adapterContext?.resource;
        if (!resource) return [];
        return [
          { id:'editor-tree-open', label:resource.kind === 'folder' ? 'Open folder' : 'Open in Editor', disabled:resource.capabilities[resource.kind === 'folder' ? 'children' : 'open'] !== true },
          { id:'editor-tree-reveal', label:'Reveal in Files' },
          ...(resource.capabilities.rename ? [{ id:'editor-tree-rename', label:renameRetargetsEditor(resource) ? 'Rename (close in Editor first)' : 'Rename', disabled:renameRetargetsEditor(resource) }] : []),
        ];
      },
      execute:async (command, request) => {
        const target = request?.adapterContext;
        const assertCurrent = () => {
          if (!target || !stillCurrent() || !target.node.isConnected || !pickerOriginCurrent(target.origin)
            || target.generation !== Number(state.filesGeneration || state.contextEpoch || 0)) throw new Error('This Editor Files target changed. Reopen its menu.');
        };
        assertCurrent();
        const resource = target.resource;
        if (command === 'editor-tree-reveal') {
          if (!await showResourceInFiles(resource.ref)) throw new Error('This resource could not be shown in Files.');
          return true;
        }
        if (command === 'editor-tree-open') {
          if (resource.kind === 'folder') await record.navigate(() => record.browser.openDirectory(filesBrowserResource(resource)), 'The Editor folder could not be opened.');
          else await resourceOpener({ resourceRef:resource.ref, name:resource.name, origin:target.origin });
          return true;
        }
        if (command !== 'editor-tree-rename') return false;
        if (renameRetargetsEditor(resource)) throw new Error('Close this file in Editor before renaming it.');
        const name = await styledPrompt('Choose a new file name.', { title:'Rename file', defaultValue:resource.name });
        if (name == null) return true;
        assertCurrent();
        if (!name.trim() || /[\\/\0]/.test(name) || name === '.' || name === '..') throw new Error('Choose a file name without path separators.');
        const fresh = await filesFacadeClient.stat(resource.ref, { signal:controller.signal });
        assertCurrent();
        const authorized = cache(fresh.resource || fresh);
        if (authorized.resourceKey !== resource.resourceKey || !authorized.capabilities.rename || renameRetargetsEditor(authorized)) throw new Error('This file can no longer be renamed.');
        const response = await filesFacadeClient.action(authorized.ref, 'rename', { name:name.trim() }, {
          signal:controller.signal, actionId:`editor-rename-${globalThis.crypto?.randomUUID?.() || Date.now()}`,
        });
        assertCurrent();
        window.dispatchEvent(new CustomEvent('openclank-files-resource-mutated', { detail:{ resourceRef:authorized.ref, action:'rename', resource:response?.resource || null, history:response?.history || null } }));
        await record.refresh(); return true;
      },
    });
    record.dispose = () => {
      controller.abort(); record.requestController?.abort(); contextDispose(); record.browser?.cancelReveal(); record.browser?.dispose();
      record.pendingAction = null; record.pendingSync = false; record.pendingReveal = null;
      record.resources.clear(); element.remove();
    };
    record.update();
    // render attaches this retained element before the dynamic import resolves.
    const activeDocument = state.docs.find(doc => doc.id === activeLeaf(workspace)?.docId);
    if (isHostDocument(activeDocument)) {
      const ref = activeDocument.resourceRef || activeDocument.resource?.locator?.opaqueRef;
      if (ref) record.pendingReveal = { ref, generation:filesGeneration(), workspaceId:workspace.left.folderWorkspaceId };
    }
    const initialFolderQuery = { query:record.filterQuery, rootKey:workspace.left.resourceRoot?.resourceKey, interactionEpoch:record.interactionEpoch };
    void mountEditorFilesBrowser({ container:browserHost, parentWindow:current.window, navigationOnly:true, getLayoutScope:()=>({owner:state.accountId || state.username,workspace:state.workspace,surface:presentationId === "wiki" ? "wiki" : "editor"}),
      onDirectory:directoryChanged,
      onSelection:row => { if (stillCurrent() && row) {
        supersedePresentation();
        try {
          const resource = cache(row);
          if (resource.capabilities.children) { record.displayedRoot = null; record.directoryReady = false; retry.hidden = false; record.update(); }
          else if (!record.directoryReady) void directoryChanged(record.browser.getCurrentDirectory());
        } catch (error) { showError(error); }
      } },
      onConfirm:async (row, activation = {}) => {
        if (!stillCurrent()) return false;
        const origin = capturePickerOrigin();
        try {
          const resource = cache(row);
          if (resource.capabilities.open !== true) throw new Error('This resource cannot be opened in Editor.');
          await resourceOpener({ resourceRef:resource.ref, name:resource.name, origin, intent:activation.intent });
          return true;
        } catch (error) { showError(error); return false; }
      },
    }, stillCurrent).then(async browser => {
      if (!browser || !stillCurrent()) { browser?.dispose(); return; }
      record.browser = browser;
      await browser.open();
      if (!stillCurrent()) return;
      record.ready = true; await record.sync();
      if (!current.noteWorkspace?.left?.resourceRoot && stillCurrent()) await directoryChanged(browser.getCurrentDirectory());
      if (stillCurrent() && initialFolderQuery.query && record.directoryReady
        && initialFolderQuery.interactionEpoch === record.interactionEpoch
        && record.displayedRoot?.resourceKey === initialFolderQuery.rootKey) {
        await record.navigate(() => browser.search(initialFolderQuery.query), 'The Editor folder could not be searched.');
      }
      record.update();
    }).catch(showError);
    return element;
  }

  function explorerSectionLayout() {
    const current=context();
    if (!current.noteExplorerLayout) current.noteExplorerLayout=createExplorerLayout({
      getScope:()=>({owner:state.accountId || state.username,workspace:state.workspace,surface:`${presentationId === 'wiki' ? 'wiki' : 'editor'}-sections`}),
      onApplied:(records,share)=>{
        const body=current.noteExplorerSections;
        if (!body) return;
        const visible=records.filter(record=>!record.hidden && record.element?.()?.parentElement===body).sort((a,b)=>a.order-b.order);
        const expanded=visible.filter(record=>!record.collapsed);
        for (const record of visible) record.element().style.flex=record.collapsed ? '0 0 auto' : expanded.length===1 ? '1 1 0' : `${record.id==='places' ? share : 1-share} 1 0`;
        const divider=current.noteExplorerDivider;
        if(divider){divider.hidden=expanded.length!==2;if(expanded.length===2)body.insertBefore(divider,visible[1].element());divider.setAttribute('aria-valuenow',String(Math.round(share*100)));}
      },
    });
    return current.noteExplorerLayout;
  }
  function customizeExplorer() {
    return showExplorerCustomization([explorerSectionLayout(),context()?.noteFilesExplorer?.browser?.explorerLayout]);
  }
  function explorerSection(id,label,content) {
    const layout=explorerSectionLayout();
    const heading=h('header',{class:'copal-explorer-section-heading'});
    const toggle=h('button',{type:'button',text:label,'aria-label':`Collapse or expand ${label}`});heading.append(toggle);
    const section=h('section',{class:'copal-explorer-section'},heading,content);
    layout.register({id,label,group:'editor-sections',element:()=>section,content:()=>content,
      sync:record=>{toggle.setAttribute('aria-expanded',String(!record.collapsed));toggle.textContent=`${record.collapsed?'▸':'▾'} ${label}`;},
    });
    toggle.addEventListener('click',()=>layout.update(id,{collapsed:!layout.records().find(record=>record.id===id).collapsed}));
    heading.addEventListener('contextmenu',event=>{event.preventDefault();event.stopPropagation();customizeExplorer();});
    return section;
  }

  function leftSidebar(workspace, docs, shellState) {
    const folderMode = Boolean(workspace?.left?.folderWorkspaceRoot);
    const aside = h('aside', { id:`copal-${presentationId}-left-sidebar`, class:'copal-notes-explorer' });
    aside.style.setProperty('--copal-pane-width', `${workspace.left.width}px`);
    const closeMenu = (menu) => { if (menu?.open) menu.open = false; };
    const fileMenu = h('details', { class:'copal-editor-file-menu' });
    const summary = h('summary', { class:'copal-btn', 'aria-label':'Editor File menu', text:'File' });
    fileMenu.append(summary);
    const menuItems = [
      ['Open File', openFileFromPicker],
      ['Open Folder', openFolderFromPicker],
      ...(folderMode ? [['Close Folder', () => closeFolderWorkspace(workspace)]] : []),
      [folderMode ? 'New File' : 'New', () => createNew()],
      ['New Folder', createFolder],
      ...(typeof createWikiArticle === 'function' ? [['New Wiki article', createWikiArticle]] : []),
      [folderMode ? 'New Copal note from template' : 'New from template', createFromTemplate],
      ['Insert template', insertTemplate],
      ['Choose template folder', configureTemplateFolder],
      ...(typeof importWikiMemes === 'function' ? [['Import native .memes', importWikiMemes]] : []),
      ...(typeof exportWikiMemes === 'function' ? [['Export native .memes', exportWikiMemes]] : []),
    ];
    const menu = h('div', { class:'copal-editor-file-menu-items', role:'menu', 'aria-label':'Editor File actions' });
    for (const [label, action] of menuItems) {
      menu.append(h('button', { type:'button', role:'menuitem', text:label, onclick:() => { closeMenu(fileMenu); void action(); } }));
    }
    fileMenu.append(menu);
    const sideHead = h('header', { class:'copal-shell-side-header left' },
      ...(shellState.narrow ? [] : [shellState.controls.left]), h('strong', { text:'Explorer' }), commandButton('⚙',customizeExplorer,{'aria-label':'Customize Explorer',title:'Customize Explorer'}),
      // Fallback while this applet's local menu module is loading.
      ...(context()?.noteWorkbenchMenu ? [] : [fileMenu]),
      iconCommandButton('file-plus', () => createNew(), { 'data-editor-create':'file', disabled:!!editorCreationReason(workspace), title:editorCreationReason(workspace) || (editorCreationFolder(workspace)?.provider === 'host' ? 'New file' : 'New Copal note'), 'aria-label':'New file or Copal note' }),
      iconCommandButton('folder-plus', createFolder, { 'data-editor-create':'folder', disabled:!!editorCreationReason(workspace), title:editorCreationReason(workspace) || 'New folder', 'aria-label':'New folder' }),
      iconCommandButton(workspace.left.showDotFolders ? 'eye-off' : 'eye', () => { workspace.left.showDotFolders = !workspace.left.showDotFolders; persist(true); render(); }, { title:workspace.left.showDotFolders ? 'Hide hidden folders' : 'Show hidden folders', 'aria-label':workspace.left.showDotFolders ? 'Hide hidden folders' : 'Show hidden folders', 'aria-pressed':String(workspace.left.showDotFolders) }));
    const tabs = h('div', { class:'copal-side-tabs', role:'tablist', 'aria-label':'Editor navigation' });
    const panelIds = workspacePanelsForSide(workspace, 'left');
    for (const key of panelIds) {
      const def = NOTES_PANELS[key];
      if (!def) continue;
      tabs.append(h('button', { class:workspace.left.tab === key ? 'active' : '', role:'tab', 'aria-selected':String(workspace.left.tab === key), text:def.label, 'data-panel-id':key, onclick:() => { workspace.left.tab = key; persist(true); render(); } }));
    }
    addRovingFocus(tabs, panelIds, (id) => { workspace.left.tab = id; persist(true); render(); });
    const visibleDocs = workspace.left.folderWorkspaceRoot && getSettings().showCopalInFolderWorkspace !== true ? [] : explorerDocs();
    const body = h('div', { class:'copal-side-body', 'data-note-panel':workspace.left.tab === 'files' ? false : workspace.left.tab });
    if (workspace.left.tab === 'files') {
      const sort = h('select', { class:'copal-file-sort', 'aria-label':'Sort files' }, h('option', { value:'name', text:'Name' }), h('option', { value:'modified', text:'Modified' }));
      sort.value = workspace.left.sort;
      sort.addEventListener('change', () => { workspace.left.sort = sort.value === 'modified' ? 'modified' : 'name'; persist(true); render(); });
      const allFolders = [...new Set(visibleDocs.flatMap((doc) => {
        const parts = doc.name.split('/').slice(0, -1); return parts.map((_, index) => parts.slice(0, index + 1).join('/'));
      }))];
      const expand = () => { workspace.left.expanded = workspace.left.expanded.length === allFolders.length ? [] : allFolders; persist(true); render(); };
      if (!folderMode) body.append(h('header', { class:'copal-explorer-tools' }, sort,
        commandButton('↔', () => open(TIMELINE_DOCUMENT.id), { title:'Open Timeline', 'aria-label':'Open Timeline' }),
        commandButton(workspace.left.expanded.length === allFolders.length ? '−' : '+', expand, { title:'Expand or collapse all collections', 'aria-label':'Expand or collapse all collections' })));
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
      body.classList.add('copal-explorer-sections');
      const current=context(), layout=explorerSectionLayout();current.noteExplorerSections=body;
      const places=explorerSection('places','Files places',editorFilesExplorer(workspace));body.append(places);
      if (!folderMode || getSettings().showCopalInFolderWorkspace === true) {
        body.append(explorerSection('copal-documents','Copal documents',fileTree(visibleDocs,workspace)));
        if(!current.noteExplorerDivider)current.noteExplorerDivider=createExplorerDivider({getContainer:()=>current.noteExplorerSections,getShare:()=>layout.getShare(),setShare:value=>layout.setShare(value),getDirection:()=>current.noteExplorerSections?.querySelector('.copal-explorer-section')?.dataset.explorerCategory==='places'?1:-1});
        body.append(current.noteExplorerDivider);
      } else layout.remove('copal-documents');
      layout.apply();
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
      const tabDirty = Boolean(context()?.noteDrafts?.has(doc.id) || context()?.noteBuffers?.get(doc.id)?.state?.().dirty);
      const tab = h('div', { class:`copal-note-tab${leaf.id === group.activeLeafId ? ' active' : ''}${leaf.pinned ? ' pinned' : ''}`, role:'tab', 'aria-selected':String(leaf.id === group.activeLeafId), draggable:'true', 'data-leaf-id':leaf.id },
        h('button', { class:'copal-note-tab-label', title:doc.name, 'aria-label':`Open ${doc.name}${doc.readOnly ? ', read only' : ''}`, onclick:() => setActive(group.id, leaf.id) }, fileGlyph(doc), h('span', { text:`${displayName(doc)}${doc.readOnly ? ' · RO' : ''}` })),
        h('span', { class:'copal-note-tab-state', 'aria-label':tabDirty ? 'Unsaved changes' : 'Saved', title:tabDirty ? 'Unsaved changes' : 'Saved', text:tabDirty ? '●' : '' }),
        h('button', { class:'copal-note-tab-pin', text:leaf.pinned ? '●' : '○', title:leaf.pinned ? 'Unpin tab' : 'Pin tab', 'aria-pressed':String(leaf.pinned), onclick:() => { leaf.pinned = !leaf.pinned; persist(true); render(); } }),
        h('button', { class:'copal-note-tab-close', text:'×', disabled:leaf.pinned, 'aria-label':`Close ${doc.name}`, onclick:async () => {
          if (!await requestCloseLeaves([leaf])) return;
          const closed = closeWorkspaceLeaf(workspace, leaf.id); if (closed) disposeLeaf(leaf.id);
          syncSelectionToModel(workspace); persist(true); render(); focusEmptyWorkspaceIfIdle();
        } }));
      registerAdapter(tab, {
        capture:() => Object.freeze({
          current:context(), workspace, scope:currentScope(context()), leafId:leaf.id, docId:doc.id,
          groupId:group.id, name:doc.name, head:String(doc.head || ''), readOnly:doc.readOnly === true,
          pinned:leaf.pinned === true,
          otherLeaves:Object.freeze(group.tabs.filter((item) => item.id !== leaf.id).map((item) => Object.freeze({ id:item.id, docId:item.docId, pinned:item.pinned === true }))),
          groups:Object.freeze(workspaceGroups(workspace).filter((item) => item.id !== group.id).map((item) => String(item.id))),
        }),
        commands:(request) => {
          const captured = request?.adapterContext;
          if (!captured) return [];
          const closableOthers = captured.otherLeaves.filter((item) => !item.pinned);
          return [
            { id:'copal-tab-close', label:'Close tab', disabled:captured.pinned },
            { id:'copal-tab-close-others', label:'Close other tabs', disabled:closableOthers.length === 0 },
            { id:captured.pinned ? 'copal-tab-unpin' : 'copal-tab-pin', label:captured.pinned ? 'Unpin tab' : 'Pin tab' },
            { id:'copal-tab-split-right', label:'Split right' },
            { id:'copal-tab-split-below', label:'Split below' },
            ...captured.groups.map((groupId, index) => ({ id:`copal-tab-move-group:${encodeURIComponent(groupId)}`, label:`Move to group ${index + 1}` })),
          ];
        },
        execute:async (command, request) => {
          const captured = request?.adapterContext;
          const resolveTarget = ({ checkDom = true, checkRevision = true, checkPinned = true } = {}) => {
            const currentContext = context();
            const currentWorkspace = currentContext?.noteWorkspace;
            const currentLeaf = captured && findWorkspaceLeaf(currentWorkspace, captured.leafId);
            const currentGroup = currentLeaf && groupForLeaf(currentWorkspace, currentLeaf.id);
            const currentDoc = captured && state.docs.find((item) => item.id === captured.docId);
            if (!captured || captured.current !== currentContext || captured.workspace !== currentWorkspace || currentWorkspace !== workspace
              || captured.scope !== currentScope(currentContext) || !currentLeaf || currentLeaf.docId !== captured.docId
              || currentGroup?.id !== captured.groupId || !currentDoc
              || checkDom && !tab.isConnected
              || checkRevision && (currentDoc.name !== captured.name || String(currentDoc.head || '') !== captured.head || (currentDoc.readOnly === true) !== captured.readOnly)
              || checkPinned && (currentLeaf.pinned === true) !== captured.pinned) {
              throw new Error('This Copal tab changed. Reopen its menu and try again.');
            }
            return { workspace:currentWorkspace, leaf:currentLeaf, group:currentGroup, doc:currentDoc };
          };
          const target = resolveTarget();
          if (command === 'copal-tab-pin' || command === 'copal-tab-unpin') {
            if (command === 'copal-tab-pin' && target.leaf.pinned || command === 'copal-tab-unpin' && !target.leaf.pinned) throw new Error('This Copal tab changed. Reopen its menu and try again.');
            target.leaf.pinned = command === 'copal-tab-pin';
            persist(true); render();
            return true;
          }
          if (command === 'copal-tab-split-right' || command === 'copal-tab-split-below') {
            const orientation = command === 'copal-tab-split-right' ? 'horizontal' : 'vertical';
            if (splitWorkspaceGroup(target.workspace, target.group.id, target.doc, orientation)) { persist(true); render(); }
            return true;
          }
          if (command.startsWith('copal-tab-move-group:')) {
            const targetGroupId = decodeURIComponent(command.slice('copal-tab-move-group:'.length));
            if (!captured.groups.includes(targetGroupId) || !findWorkspaceGroup(target.workspace, targetGroupId)) throw new Error('The destination tab group changed. Reopen the menu and try again.');
            if (moveWorkspaceLeaf(target.workspace, target.leaf.id, targetGroupId)) { persist(true); render(); }
            return true;
          }
          if (command === 'copal-tab-close') {
            if (target.leaf.pinned) return true;
            if (!await requestCloseLeaves([target.leaf])) return true;
            const fresh = resolveTarget({ checkDom:false, checkRevision:false, checkPinned:false });
            if (fresh.leaf.pinned) return true;
            const closed = closeWorkspaceLeaf(fresh.workspace, fresh.leaf.id);
            if (closed) disposeLeaf(closed.id);
            syncSelectionToModel(fresh.workspace); persist(true); render(); focusEmptyWorkspaceIfIdle();
            return true;
          }
          if (command === 'copal-tab-close-others') {
            const closable = captured.otherLeaves.filter((item) => !item.pinned);
            const live = closable.map((item) => ({ captured:item, leaf:findWorkspaceLeaf(target.workspace, item.id) }));
            if (live.some(({ captured:item, leaf:currentLeaf }) => !currentLeaf || currentLeaf.docId !== item.docId || groupForLeaf(target.workspace, currentLeaf.id)?.id !== captured.groupId || currentLeaf.pinned)) {
              throw new Error('The other tabs changed. Reopen the menu and try again.');
            }
            if (!await requestCloseLeaves(live.map(({ leaf:other }) => other))) return true;
            const freshTarget = resolveTarget({ checkDom:false, checkRevision:false, checkPinned:false });
            for (const { captured:item } of live) {
              const other = findWorkspaceLeaf(freshTarget.workspace, item.id);
              if (!other || other.docId !== item.docId || groupForLeaf(freshTarget.workspace, other.id)?.id !== captured.groupId || other.pinned) {
                throw new Error('The other tabs changed. Reopen the menu and try again.');
              }
            }
            for (const { captured:item } of live) {
              const closed = closeWorkspaceLeaf(freshTarget.workspace, item.id);
              if (closed) disposeLeaf(closed.id);
            }
            syncSelectionToModel(freshTarget.workspace); persist(true); render();
            return true;
          }
          return false;
        },
      });
      tab.addEventListener('auxclick', async (event) => {
        if (event.button !== 1 || leaf.pinned) return;
        event.preventDefault(); if (!await requestCloseLeaves([leaf])) return;
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
        if (!await requestCloseLeaves(candidates)) return;
        for (const closed of closeWorkspaceOtherLeaves(workspace, activeId)) disposeLeaf(closed.id);
        syncSelectionToModel(workspace); persist(true); render();
      }),
      commandButton('Close tab group', async () => {
        const candidates = group.tabs.filter((leaf) => !leaf.pinned);
        if (!await requestCloseLeaves(candidates)) return;
        for (const closed of closeWorkspaceGroup(workspace, group.id)) disposeLeaf(closed.id);
        syncSelectionToModel(workspace); persist(true); render(); focusEmptyWorkspaceIfIdle();
      }))));
    const controls = h('div', { class:'copal-tab-group-controls' },
      commandButton('+', () => showChooser({ title:'Open note in this group', choose:(doc) => { openWorkspaceDocument(workspace, doc, { groupId:group.id, intent:'newTab' }); persist(true); render(); } }), { title:'Open note', 'aria-label':'Open note' }),
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
    breadcrumb.title = doc.name;
    breadcrumb.append(fileGlyph(doc));
    if (parts.length > 1) breadcrumb.append(h('span', { class:'copal-breadcrumb-parent', text:parts.slice(0, -1).join(' / ') }), h('span', { text:' / ', 'aria-hidden':'true' }));
    const resourceRef = String(doc.resourceRef || doc.resource?.locator?.opaqueRef || '').trim();
    const resourceReadOnly = Boolean(resourceRef);
    const hostFile = isHostDocument(doc);
    const historyResource = hostHistoryResourceForDocument(doc);
    const historyButton = historyResource
      ? commandButton('History', () => { void openEditorResourceHistory(doc); })
      : commandButton('History', () => showHistory(doc));
    if (doc.readOnly) {
      const menu = wirePopover(h('details', { class:'copal-leaf-menu' }, h('summary', { text:'⋯', title:'Knowledge note actions', 'aria-label':'Knowledge note actions' }), h('div', { class:'copal-popover-menu' },
        ...(historyResource || !resourceReadOnly ? [historyButton] : []),
        ...(canMakeEditableWikiCopy(doc) ? [commandButton('Make editable copy', () => startEditableWikiCopy(doc))] : []),
        ...(resourceReadOnly ? [commandButton('Show in Files', () => { void showResourceInFiles(resourceRef); })] : []),
        commandButton('Split right', () => { if (splitWorkspaceGroup(workspace, group.id, doc, 'horizontal')) { persist(true); render(); } }),
        commandButton('Split below', () => { if (splitWorkspaceGroup(workspace, group.id, doc, 'vertical')) { persist(true); render(); } }),
        ...workspaceGroups(workspace).filter((target) => target.id !== group.id).map((target, index) => commandButton(
          `Move to group ${index + 1}`,
          () => { if (moveWorkspaceLeaf(workspace, leaf.id, target.id)) { persist(true); render(); } },
        )),
        commandButton(workspace.bookmarks?.includes(doc.id) ? 'Remove bookmark' : 'Add bookmark', () => toggleBookmark(doc.id)),
        commandButton('Reveal in Editor', () => { workspace.left.open = true; workspace.left.tab = 'files'; revealInExplorer(doc, workspace); persist(true); render(); }))));
      breadcrumb.append(h('strong', { class:'copal-inline-title', text:parts.at(-1) }));
      cache.header.replaceChildren(breadcrumb, h('span', { class:'copal-leaf-mode', text:'Read only' }), menu);
      return;
    }
    const fileName = parts.at(-1); const extension = /\.[A-Za-z0-9]+$/.exec(fileName)?.[0] || '';
    const title = hostFile
      ? h('strong', { class:'copal-inline-title', text:fileName, title:doc.name, 'aria-label':'File name' })
      : h('input', { class:'copal-inline-title', value:displayName(doc), 'aria-label':'Note title' });
    if (!hostFile) title.addEventListener('change', async () => {
      const entered = title.value.trim();
      const nextFile = entered && /\.[A-Za-z0-9]+$/.test(entered) ? entered : `${entered}${extension}`;
      const name = [...parts.slice(0, -1), nextFile].filter(Boolean).join('/');
      if (!entered) { title.value = displayName(doc); return; }
      if (name !== doc.name) {
        try { await renameNote(doc, name); }
        catch (error) { title.value = displayName(doc); context().window.setStatus(error.message, true); }
      }
    });
    const menu = wirePopover(h('details', { class:'copal-leaf-menu' }, h('summary', { text:'⋯', title:hostFile ? 'File actions' : 'Note actions', 'aria-label':hostFile ? 'File actions' : 'Note actions' }), h('div', { class:'copal-popover-menu' },
      ...(doc.sourceKind === 'host' && doc.kind !== 'markdown'
        ? [commandButton('Source mode', () => { setWorkspaceLeafMode(workspace, leaf.id, 'source'); persist(true); render(); })]
        : [commandButton(doc.kind === 'note' ? 'Editing mode' : 'Live Preview', () => { setWorkspaceLeafMode(workspace, leaf.id, 'live'); persist(true); render(); }), ...(doc.kind === 'note' ? [] : [commandButton('Source mode', () => { setWorkspaceLeafMode(workspace, leaf.id, 'source'); persist(true); render(); })])]),
      commandButton('Reading mode', () => { setWorkspaceLeafMode(workspace, leaf.id, 'reading'); persist(true); render(); }),
      commandButton(workspace.settings.previewLayout === 'inline' ? 'Use side-by-side preview' : 'Use inline preview', () => setPreviewLayout(workspace.settings.previewLayout === 'inline' ? 'side-by-side' : 'inline')),
      commandButton('Find and replace', () => cache.editor && showFindReplace(cache.editor)),
      historyButton,
      commandButton('Split right', () => { if (splitWorkspaceGroup(workspace, group.id, doc, 'horizontal')) { persist(true); render(); } }),
      commandButton('Split below', () => { if (splitWorkspaceGroup(workspace, group.id, doc, 'vertical')) { persist(true); render(); } }),
      commandButton('Move tab left', () => { const index = group.tabs.findIndex((item) => item.id === leaf.id); if (index > 0 && moveWorkspaceLeaf(workspace, leaf.id, group.id, index - 1)) { persist(true); render(); } }),
      commandButton('Move tab right', () => { const index = group.tabs.findIndex((item) => item.id === leaf.id); if (index >= 0 && index < group.tabs.length - 1 && moveWorkspaceLeaf(workspace, leaf.id, group.id, index + 1)) { persist(true); render(); } }),
      ...workspaceGroups(workspace).filter((target) => target.id !== group.id).map((target, index) => commandButton(
        `Move to group ${index + 1}`,
        () => { if (moveWorkspaceLeaf(workspace, leaf.id, target.id)) { persist(true); render(); } },
      )),
      commandButton(workspace.bookmarks?.includes(doc.id) ? 'Remove bookmark' : 'Add bookmark', () => toggleBookmark(doc.id)),
      ...(resourceRef ? [commandButton('Show in Files', () => { void showResourceInFiles(resourceRef); })] : []),
      commandButton('Reveal in Editor', () => { workspace.left.open = true; workspace.left.tab = 'files'; revealInExplorer(doc, workspace); persist(true); render(); }),
      ...(leaf.view === 'canvas' || leaf.view === 'base' ? [commandButton(leaf.rawSource ? 'Back to typed view' : 'View raw source', () => { leaf.rawSource = !leaf.rawSource; persist(true); render(); })] : []),
      ...(canManageCopalDocument(doc) ? [commandButton('Rename', () => renameWithForm(doc)),
        commandButton('Move to trash', () => deleteDocument(doc), { class:'copal-btn danger' })] : []))));
    const actions = h('div', { class:'copal-note-header-actions' },
      ...(context()?.noteBuffers?.get(doc.id)?.uncertainSave ? [commandButton('Retry previous Save', () => void retryDocumentSave(doc.id), { title:'Reconcile the previously submitted version before saving newer changes' })] : []),
      ...(doc.savePolicy === 'explicit' ? [commandButton('Save', () => void saveDraft(doc.id), { title:'Save file (Ctrl/Cmd+S)', 'aria-label':`Save ${fileName}` })] : []),
      ...(uploadAttachment && !hostFile ? [commandButton('Attach file', () => chooseAttachment(cache, doc), { title:'Attach a file at the captured cursor', 'aria-label':'Attach file' })] : []),
      menu);
    const mode = doc.sourceKind === 'host' && doc.kind !== 'markdown' ? 'Source' : leaf.mode === 'live' ? doc.kind === 'note' ? 'Editing' : 'Live Preview' : leaf.mode === 'source' ? 'Source' : 'Reading';
    breadcrumb.append(title);
    cache.header.replaceChildren(breadcrumb, h('span', { class:'copal-leaf-mode', text:mode }), actions);
  }

  // S21 plan item 5: the Markdown Formatting Demo is the only page whose
  // rendered elements reveal their matching raw Markdown source. Ordinary
  // documentation pages stay rendered with no universal source toggle.
  const FORMATTING_DEMO_DOC_ID = 'openclank-docs-formatting-demo';

  function isFormattingDemoDocument(doc) {
    if (!doc) return false;
    const docId = doc.properties && doc.properties.docId;
    if (String(docId || '') === FORMATTING_DEMO_DOC_ID) return true;
    const name = String(doc.name || doc.path || '');
    return name === 'OpenClank/Markdown Formatting Demo'
      || name === 'Markdown Formatting Demo'
      || name.endsWith('/Markdown Formatting Demo');
  }

  function renderFormattingDemoBody(doc) {
    const source = sourceValue(doc);
    const sourceLines = String(source || '').split('\n');
    const rendered = renderMarkdown(source, new Set([doc.id]), doc);
    const shell = h('div', { class:'copal-md-source-reveal' });
    let inspector = null;
    const backToRendered = () => {
      if (inspector) { inspector.remove(); inspector = null; }
      rendered.style.display = '';
    };
    const revealSource = (node) => {
      const start = Number(node.getAttribute('data-md-start') || 0);
      const end = Number(node.getAttribute('data-md-end') || 0) || start;
      const from = Math.max(0, (start || 1) - 1);
      const to = Math.max(from + 1, end);
      const snippet = sourceLines.slice(from, to).join('\n');
      if (inspector) inspector.remove();
      rendered.style.display = 'none';
      inspector = h('div', {
        class:'copal-md-source-inspector',
        role:'region',
        'aria-label':'Markdown source inspector',
      },
        h('div', { class:'copal-md-source-inspector-bar' },
          h('span', { class:'copal-md-source-inspector-label', text:`Source · lines ${from + 1}–${to}` }),
          h('button', {
            class:'copal-btn',
            type:'button',
            text:'Back to rendered',
            onclick:() => backToRendered(),
          })),
        // Read-only inspector: selectable, copyable, never editable, never a save.
        h('pre', { class:'copal-md-source-inspector-body', tabindex:'0' }, h('code', { text:snippet })));
      // Defensive: reject paste/drop so the inspector cannot become an editor.
      inspector.addEventListener('paste', (event) => event.preventDefault());
      inspector.addEventListener('drop', (event) => event.preventDefault());
      inspector.addEventListener('dragover', (event) => event.preventDefault());
      shell.append(inspector);
      if (isOfficialDocument(doc) && String(doc.properties?.docId || '') === FORMATTING_DEMO_DOC_ID) {
        acknowledgeVisible(inspector, 'formatting.demo.toggled', { officialDemo:true, demoId:doc.id, fromView:'rendered', toView:'source' }, { workspaceId:state.workspace });
      }
    };
    for (const child of Array.from(rendered.children)) {
      if (!child || child.nodeType !== 1 || !child.hasAttribute('data-md-start')) continue;
      child.setAttribute('tabindex', '0');
      child.setAttribute('role', 'button');
      child.setAttribute('aria-label', 'Reveal Markdown source for this element');
      child.classList.add('copal-md-source-reveal-target');
      child.addEventListener('click', (event) => {
        // Keep real links, chips and controls doing their own work.
        const interactive = event.target && event.target.closest
          ? event.target.closest('a, button, input, select, textarea, .copal-chip, .copal-code-copy')
          : null;
        if (interactive && child.contains(interactive)) return;
        event.preventDefault();
        event.stopPropagation();
        revealSource(child);
      });
      child.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          event.stopPropagation();
          revealSource(child);
        }
      });
    }
    shell.append(rendered);
    return shell;
  }

  function renderLeaf(leaf, doc, workspace, group) {
    const current = context();
    let cache = current.noteLeafViews.get(leaf.id);
    if (!cache || cache.docId !== doc.id || cache.view !== leaf.view) {
      const languageMode = cache?.docId === doc.id ? cache.languageMode : [...current.noteLeafViews.values()].find(view => view.docId === doc.id)?.languageMode;
      if (cache) disposeLeaf(leaf.id);
      const root = h('article', { class:'copal-note-leaf', 'data-leaf-id':leaf.id, 'data-view-type':leaf.view });
      const header = h('header', { class:'copal-note-view-header' });
      const body = h('div', { class:'copal-note-leaf-content' });
      const propsFooter = h('div', { class:'copal-note-props-footer' });
      const status = h('footer', { class:'copal-note-status', role:'status', 'aria-live':'polite' });
      root.append(header, body, propsFooter, status);
      cache = { root, header, body, propsFooter, status, docId:doc.id, view:leaf.view, saveState:'saved', cursorLine:1, editor:null, languageMode };
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
    registerWikiArticleContextMenu(cache, leaf, doc, workspace, group);
    cache.body.classList.toggle('copal-rendered-article-scroll', doc.readOnly === true);
    if (doc.readOnly) {
      cache.body.replaceChildren(
        leaf.mode === 'source' ? h('pre', { class:'copal-readonly-source', tabindex:'0', 'aria-label':'Read-only article source' }, h('code', { text:sourceValue(doc) })) : isFormattingDemoDocument(doc)
          ? renderFormattingDemoBody(doc)
          : renderMarkdown(sourceValue(doc), new Set([doc.id]), doc),
      );
      if (leaf.view === 'wiki' && leaf.mode !== 'source') {
        cache.body.append(wikiArticleChrome(doc));
        if (canMakeEditableWikiCopy(doc)) {
          cache.body.append(h('div', { class:'copal-dialog-actions' },
            h('button', { class:'copal-btn primary', text:'Make editable copy', onclick:() => startEditableWikiCopy(doc) })));
        }
      }
      updateLeafStatus(cache);
      return cache.root;
    }
    if (leaf.view === 'wiki'
      && (doc.note_error || doc.rawPreserved
        || (doc.recoveryState && doc.recoveryState !== 'supported'))) {
      cache.body.replaceChildren(wikiRecoveryPanel(doc));
      cache.status.replaceChildren(h('span', { text:'Wiki · preserved source recovery' }));
      return cache.root;
    }
    if (doc.note_error) cache.body.replaceChildren(h('div', { class:'copal-inspector-error' },
      h('strong', { text:'This database note could not be decoded' }),
      h('p', { text:'Its stored record is preserved and has not been opened for editing.' }),
      h('p', { text:doc.note_error })));
    else if (leaf.view === 'markdown' || leaf.view === 'note' || leaf.view === 'wiki') updateMarkdownLeaf(cache, leaf, doc, workspace);
    else if (leaf.rawSource) updateSourceLeaf(cache, leaf, doc, workspace);
    else if (leaf.view === 'canvas') updateCanvasLeaf(cache, doc);
    else if (leaf.view === 'base') updateBaseLeaf(cache, doc);
    else updateAssetLeaf(cache, doc, leaf.view);
    // Properties footer for note/markdown/wiki views — inline editable card
    if ((leaf.view === 'markdown' || leaf.view === 'note' || leaf.view === 'wiki') && !doc.readOnly && !doc.note_error) {
      const props = buildInlineProps(doc);
      if (leaf.view === 'wiki') props.append(wikiArticleChrome(doc));
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

  function attachmentDialog(cache, doc, file, cursor, { clipboard = false } = {}) {
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
    const achievementAccount = achievementOwner();
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
        if (clipboard && /^image\//.test(mediaKind)) await recordPresentation('clipboard.image.inserted', {
          prepared:true, inserted:true, assetId:lifecycle.asset_id, documentId:doc.id, mediaLocation:lifecycle.asset_name,
        }, { accountId:achievementAccount, workspaceId:state.workspace, kind:'R', occurrenceId:actionId });
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
    if (authorized.kind === 'folder' || authorized.capabilities.open !== true) throw new Error('This Files resource cannot be opened in Editor.');
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
      attachmentDialog(cache, doc, file, attachmentCursor(cache), { clipboard:true });
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
          if (authorizedSource.kind === 'folder' || !['read', 'download', 'open', 'copy'].some(capability => authorizedSource.capabilities[capability] === true)) throw new Error('This Files item is unavailable for attachment.');
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
        historyOwner:{ undo:() => undoDocument(doc.id), redo:() => undoDocument(doc.id, true), state:() => context()?.noteBuffers?.get(doc.id)?.state() || { canUndo:false, canRedo:false } },
        getLocalRevision:() => sourceEditBuffer(doc)?.localRevision || 0,
        registerPendingSourceEdit:edit => registerPendingSourceEdit(doc, edit),
        getPendingSourceEdit:id => context()?.noteBuffers?.get(doc.id)?.pendingEdits.get(String(id)) || null,
        getPendingSourceEdits:() => [...(context()?.noteBuffers?.get(doc.id)?.pendingEdits.values() || [])],
        removePendingSourceEdit:id => removePendingSourceEdit(doc, id),
        applySourceTransaction:(transform, options = {}) => applyDocumentTransaction(doc, transform, options),
        onNotice:message => context()?.window?.setStatus(message, true),
        scrollTop:leaf.scrollTop,
        mode:doc.sourceKind === 'host' && doc.kind !== 'markdown' ? 'source' : leaf.mode === 'live' && workspace.settings.previewLayout === 'inline' ? 'live' : 'source',
        lineNumbers:workspace.settings.lineNumbers, readableLineWidth:workspace.settings.readableLineWidth,
        language:hostLanguage,
        languageOverride:cache.languageMode && cache.languageMode !== 'auto' ? cache.languageMode : undefined,
        languageDialect:hostDialect,
        languagePath:hostPath,
        richComments:true,
        // The live CodeMirror surface must use the same resolver and origin
        // semantics as the rendered Notes/Wiki view. Tests and alternate
        // hosts may still inject a specialized preview callback.
        renderPreview: renderPreview
          ? (source) => wireRichCommentTables(renderPreview(source, doc), source, doc, cache)
          : (source) => wireRichCommentTables(renderMarkdown(source, new Set([doc.id]), doc), source, doc, cache),
        renderComment:(body, editBody) => wireRichCommentTables(renderComment ? renderComment(body, doc) : renderMarkdown(body, new Set([doc.id]), doc), body, doc, cache, editBody),
        onSeeSource:(range) => {
          cache.editor?.revealCommentSource?.(range.from, range.to);
          context()?.window?.setStatus('Showing comment source.');
        },
        onSelection:(selection, update) => { if (update?.selectionSet && !update?.docChanged) { const buffer = context()?.noteBuffers?.get(doc.id); if (buffer) buffer.lastHistoryTime = 0; } leaf.selection = selection; cache.selectionGeneration = Number(cache.selectionGeneration || 0) + 1; cache.cursorLine = selection.line; persist(); updateLeafStatus(cache); },
        onScroll:(scrollTop) => { leaf.scrollTop = scrollTop; persist(); },
        onSyntaxStatus:() => updateLeafStatus(cache),
        onChange:(value, update, edit) => { const liveDoc = workspaceDocuments().find(item => item.id === doc.id) || doc; doc.text = value; liveDoc.text = value; queueSave(liveDoc, value, { origin:edit?.origin || 'transaction', history:true }); if (leaf.rawSource) publishRawBaseDefinition(liveDoc, value); syncDocumentEditors(doc.id, value, cache.editor, update?.changes); updatePreview(cache, liveDoc, value); updateLeafStatus(cache); },
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
    if (!cache.preview || cache.preview.hidden) return;
    cache.previewPending = { doc, value };
    if (cache.previewFrame != null) return;
    cache.previewFrame = requestAnimationFrame(() => {
      cache.previewFrame = null;
      const pending = cache.previewPending; cache.previewPending = null;
      if (!pending || !cache.preview?.isConnected || cache.preview.hidden) return;
      if (cache.previewValue === pending.value) return;
      cache.preview.replaceChildren(renderMarkdown(pending.value, new Set([pending.doc.id]), pending.doc));
      cache.previewValue = pending.value;
      applyCompletedVisibility(cache.preview);
      wireInteractiveTables(cache.preview, pending.value, pending.doc, cache);
    });
  }

  function tableEditable(doc) {
    return !(doc?.readOnly === true || doc?.builtin === true);
  }

  function wireRichCommentTables(root, commentSource, doc, cache, editBody) {
    if (!root || typeof root.querySelectorAll !== 'function') return root;
    // Table offsets belong to the stripped body, never the wrapper-bearing source.
    wireInteractiveTables(root, String(commentSource || ''), doc, cache, {
      editable: tableEditable(doc) && typeof editBody === 'function',
      liveText: () => String(commentSource || ''),
      onBodyEdit: editBody,
    });
    return root;
  }

  function collectTableBlocks(source) {
    // Table blocks include an optional leading `clank-table` metadata comment.
    const sourceLines = String(source || '').split('\n');
    const sourceTables = [];
    let i = 0;
    while (i < sourceLines.length) {
      const trimmed = sourceLines[i].trim();
      if (trimmed.includes('<!-- clank-table')) {
        // Consume the metadata comment (single- or multi-line).
        let j = i;
        if (!trimmed.includes('-->')) {
          j = i + 1;
          while (j < sourceLines.length && !sourceLines[j].includes('-->')) j++;
        }
        const commentEnd = j;
        let k = j + 1;
        while (k < sourceLines.length && !sourceLines[k].trim()) k++;
        if (k < sourceLines.length && sourceLines[k].includes('|') && k + 1 < sourceLines.length) {
          const sepLine = sourceLines[k + 1] || '';
          if (/^\|?\s*:?-{3,}/.test(sepLine.trim())) {
            const blockStart = i;
            const block = sourceLines.slice(blockStart, k + 1);
            let m = k + 2;
            while (m < sourceLines.length && sourceLines[m].includes('|') && sourceLines[m].trim()) {
              block.push(sourceLines[m]);
              m++;
            }
            sourceTables.push({ text: block.join('\n'), startLine: blockStart });
            i = m;
            continue;
          }
        }
        i = commentEnd + 1;
        continue;
      }
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
    return sourceTables;
  }

  function wireInteractiveTables(container, source, doc, cache = null, options = {}) {
    // Find all table blocks in the source (including adjacent metadata).
    const sourceTables = collectTableBlocks(source);
    const editable = options.editable ?? tableEditable(doc);
    const resolveBaseOffset = typeof options.baseOffset === 'function'
      ? options.baseOffset
      : () => options.baseOffset || 0;
    const liveText = options.liveText || (() => sourceValue(doc));
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
      const capturedHeader = model.rows[0]?.sourceText || '';
      const onEdit = (edit) => {
        // Protected docs allow selection/source inspection but no mutation.
        if (!tableEditable(doc)) {
          context()?.window?.setStatus('This document is read-only. Table structure and cells cannot be changed.', true);
          return;
        }
        // A stale/background editor selection must not mutate the wrong text.
        if (cache?.docId && cache.docId !== doc.id) {
          context()?.window?.setStatus('This table belongs to a background document. Activate it to edit.', true);
          return;
        }
        const live = String(liveText() || '');
        const liveLines = live.split('\n');
        const liveBlock = liveLines.slice(model.blockRange.from, model.blockRange.to + 1).join('\n');
        const liveModel = parseTable(liveBlock, model.blockRange.from);
        if (!liveModel.valid || (liveModel.rows[0]?.sourceText || '') !== capturedHeader) {
          context()?.window?.setStatus('This table changed in the source. The preview will refresh before editing.', true);
          updatePreview(cache, doc);
          return;
        }
        const result = applyTableEdit(live, liveModel, edit);
        if (result.changes.length) {
          if (typeof options.onBodyEdit === 'function') {options.onBodyEdit(result.newText);return;}
          // One logical operation = one change transaction = one undo step.
          // Rich-comment tables are relative to the comment body; offset them
          // into the document so wrappers/native envelopes stay valid.
          const base = resolveBaseOffset();
          const changes = result.changes.map((change) => ({
            from: change.from + base,
            to: change.to + base,
            insert: change.insert,
          }));
          // Try CodeMirror dispatch for atomic undo, fall back to setValue
          if (cache?.editor?.view?.dispatch) {
            cache.editor.view.dispatch({ changes });
          } else if (cache?.editor?.dispatch) {
            cache.editor.dispatch({ changes });
          } else if (cache?.editor?.setValue) {
            cache.editor.setValue(result.newText);
          }
        }
      };
      const widget = createTableWidget(model, onEdit, {
        editable,
        onStatus: (message, isError) => {
          if (message) context()?.window?.setStatus(message, !!isError);
        },
      });
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
      cache.reading ||= h('article', { class:'copal-note-reading', tabindex:'0', 'aria-label':'Article' });
      cache.reading.style.height = '100%';
      cache.reading.replaceChildren(renderMarkdown(sourceValue(doc), new Set([doc.id]), doc));
      applyCompletedVisibility(cache.reading);
      // Reading view exposes the same table operations as live preview.
      wireInteractiveTables(cache.reading, sourceValue(doc), doc, cache);
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

  function captureLanguageTarget(cache) {
    const current = context(), workspace = current?.noteWorkspace;
    if (!workspace) return null;
    const leaf = findWorkspaceLeaf(workspace, cache.leaf?.id);
    const group = leaf && groupForLeaf(workspace, leaf.id);
    if (!cache.root.isConnected || current?.noteLeafViews?.get(leaf?.id) !== cache || group?.activeLeafId !== leaf?.id) return null;
    // Status belongs to this exact visible leaf, even in an inactive split.
    // Activate its existing model without remounting a focused native selector.
    if (workspace.activeLeafId !== leaf.id) {
      activateWorkspaceLeaf(workspace, group.id, leaf.id);
      current.noteActivationSequence = Number(current.noteActivationSequence || 0) + 1;
      syncSelectionToModel(workspace); bindKeys(workspace, cache.doc); persistActiveContext(); persist(true);
      current.window.root.querySelectorAll('.copal-note-group').forEach(node => node.classList.toggle('active-group', node.dataset.groupId === group.id));
    }
    const target = captureNotesWorkbench();
    return target.editor === cache.editor && target.leafId === leaf.id ? target : null;
  }

  async function chooseLanguage(cache, id, target) {
    if (!target || target.editor !== cache.editor || !notesWorkbenchCurrent(target)) {
      context()?.window?.setStatus('The language target changed. Reopen its language selector.', true);
      updateLeafStatus(cache); return;
    }
    if (id !== 'auto' && !SELECTABLE_LANGUAGES.some(entry => entry.id === id)) {
      target.current.window?.setStatus('Choose a listed language mode or Auto.', true); updateLeafStatus(cache); return;
    }
    const generation = cache.languageChoiceGeneration = Number(cache.languageChoiceGeneration || 0) + 1;
    // Ephemeral presentation lives in the existing open-document view caches.
    // It is never part of the persisted layout, draft envelope or database.
    const peers = [...target.current.noteLeafViews.values()].filter(view => view.docId === target.docId);
    const jobs = [];
    try {
      for (const peer of peers) {
        peer.languageMode = id;
        if (typeof peer.editor?.setLanguage === 'function') jobs.push(peer.editor.setLanguage(id));
      }
      const results = await Promise.allSettled(jobs);
      if (cache.languageChoiceGeneration !== generation || !notesWorkbenchCurrent(target)) return;
      const failed = results.find(result => result.status === 'rejected');
      if (failed) throw failed.reason;
      const syntax = cache.editor.getSyntaxStatus?.();
      target.current.window?.setStatus(syntax?.message || 'Language mode updated.');
      updateLeafStatus(cache);
    } catch (error) {
      if (cache.languageChoiceGeneration === generation && notesWorkbenchCurrent(target)) {
        target.current.window?.setStatus(error?.message || 'Language mode could not be changed.', true);
        updateLeafStatus(cache);
      }
    }
  }

  function languageStatusControl(cache, editor, status, language) {
    const selectable = typeof editor?.setLanguage === 'function';
    if (!cache.languageControl || (cache.languageControl.tagName === 'SELECT') !== selectable) {
      cache.languageControl = selectable
        ? h('select', { class:'copal-status-language', 'aria-label':`Language mode for ${cache.doc.name}` },
          h('option', { value:'auto', text:'Auto (detect)' }),
          ...SELECTABLE_LANGUAGES.map(entry => h('option', { value:entry.id, text:entry.modeName || entry.displayName })))
        : h('span', { class:'copal-status-language' });
      if (selectable) {
        const remember = () => { cache.languageTarget = captureLanguageTarget(cache); };
        cache.languageControl.addEventListener('pointerdown', remember);
        cache.languageControl.addEventListener('focus', remember);
        cache.languageControl.addEventListener('change', () => {
          const id = cache.languageControl.value, target = cache.languageTarget || captureLanguageTarget(cache);
          cache.languageTarget = null; void chooseLanguage(cache, id, target);
        });
      }
      cache.statusDetails ||= h('div', { class:'copal-status-details' });
      cache.status.replaceChildren(cache.languageControl, cache.statusDetails);
    }
    if (selectable) {
      const mode = status?.language?.mode === 'override' ? status.language.override : 'auto';
      cache.languageControl.options[0].textContent = mode === 'auto' ? `Auto · ${language}` : 'Auto (detect)';
      cache.languageControl.value = mode;
      cache.languageControl.title = `${language} · ${mode === 'auto' ? 'automatic detection' : 'open buffer override'}`;
    } else { cache.languageControl.textContent = language; cache.languageControl.title = 'Detected language'; }
  }

  function updateLeafStatus(cache) {
    if (!cache?.status || !cache.doc) return;
    const buffer = context()?.noteBuffers?.get(cache.docId);
    const recoveryError = buffer?.state().recoveryError;
    const editor = cache.doc.readOnly || cache.leaf?.mode === 'reading' ? null : cache.editor;
    const status = editor?.getStatus?.();
    const syntax = status?.syntax || editor?.getSyntaxStatus?.();
    const selection = editor?.view?.state.selection;
    const primary = selection?.main;
    const line = primary ? editor.view.state.doc.lineAt(primary.head) : null;
    const count = selection?.ranges.length || 0;
    const dirty = Boolean(buffer?.state?.().dirty || context()?.noteDrafts?.has(cache.docId));
    const saveLabel = cache.doc.readOnly ? 'Read only' : buffer?.uncertainSave ? 'Save outcome unknown' : buffer?.pendingEdits?.size ? 'Staged comment edit' : cache.saveState === 'saving' ? 'Saving…' : cache.saveState === 'conflict' ? 'Conflict'
      : cache.saveState === 'error' ? 'Save failed' : dirty ? 'Unsaved' : 'Saved';
    const language = status?.language?.name || status?.language?.id
      || SELECTABLE_LANGUAGES.find(entry => entry.id === cache.languageMode)?.displayName
      || (isHostDocument(cache.doc) ? languageForPath(cache.doc.name) : ['note', 'markdown', 'wiki'].includes(cache.view) ? 'Markdown' : cache.view);
    const syntaxLabel = syntax ? ({ ready:'Syntax ready', loading:'Syntax loading…', pending:'Syntax loading…', degraded:'Syntax unavailable', error:'Syntax unavailable', failed:'Syntax unavailable', plain:'Plain text', unsupported:'Plain text' })[syntax.state] || `Syntax: ${syntax.state}` : null;
    languageStatusControl(cache, editor, status, language);
    cache.statusDetails.replaceChildren(
      ...(line ? [h('span', { class:'copal-status-position', text:`Ln ${line.number}, Col ${primary.head - line.from + 1}` })] : []),
      ...(count ? [h('span', { class:'copal-status-cursors', text:`${count} cursor${count === 1 ? '' : 's'}`, title:`${count} selection range${count === 1 ? '' : 's'}` })] : []),
      h('span', { class:`copal-save-state ${dirty ? 'unsaved' : cache.saveState}`, text:saveLabel }),
      ...(recoveryError ? [h('span', { class:'copal-save-state error', text:dirty ? 'Recovery unavailable' : 'Recovery cleanup failed', title:`${dirty ? 'Save before closing; restart recovery is unavailable.' : 'Saved; old recovery draft could not be cleared.'} ${String(recoveryError.message || recoveryError)}` })] : []),
      ...(buffer?.uncertainSave ? [commandButton('Retry previous Save', () => void retryDocumentSave(cache.docId))] : []),
      ...(buffer?.historyNotice ? [h('span', { text:'Undo history limit reached', title:buffer.historyNotice })] : []),
      ...(syntaxLabel ? [h('span', { class:'copal-syntax-status', text:syntaxLabel, title:syntax.message }), ...(syntax.retryable ? [h('button', { type:'button', class:'copal-btn', text:'Retry syntax', onclick:() => editor?.retrySyntax?.() })] : [])] : []));
  }

  function showFindReplace(editor, { isContextCurrent = () => true } = {}) {
    const dialog = h('dialog', { class:'copal-dialog copal-find-replace' }, h('h2', { text:'Find and replace' }));
    const find = h('input', { type:'text', placeholder:'Find', 'aria-label':'Find' });
    const replacement = h('input', { type:'text', placeholder:'Replace', 'aria-label':'Replace' });
    const feedback = h('span', { role:'status' });
    const valid = () => {
      if (isContextCurrent()) return true;
      feedback.textContent = 'The captured Editor target changed. Reopen Find and replace.';
      return false;
    };
    dialog.append(find, replacement, h('div', { class:'copal-dialog-actions' },
      commandButton('Find next', () => { if (!valid()) return; feedback.textContent = editor.find(find.value) ? '' : 'No match'; }),
      commandButton('Replace', () => { if (!valid()) return; feedback.textContent = editor.replace(find.value, replacement.value) ? 'Replaced' : 'No match'; }),
      commandButton('Replace all', () => { if (!valid()) return; feedback.textContent = `${editor.replace(find.value, replacement.value, true)} replaced`; }),
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
    const native = doc.kind === 'note' || doc.kind === 'wiki';
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
          const target = captureNotesWorkbench();
          if (target.docId !== doc.id) return;
          const newVal = await styledPrompt(`Edit ${key}.`, { title: 'Edit property', defaultValue: display, confirmText: 'Save', maxLength: 512 });
          if (newVal === null) return;
          if (!notesWorkbenchCurrent(target)) { context()?.window?.setStatus('The captured property target changed. Reopen the property editor.', true); return; }
          if (native) {
            try { const currentValue = doc.properties?.[key]; const type = currentValue && typeof currentValue === 'object' && !Array.isArray(currentValue) ? 'object' : propertyType(currentValue, key); commitNative(Object.entries(doc.properties || {}).map(([k, v]) => [k, k === key ? coercePropertyValue(newVal, type) : v])); } catch (e) { context().window.setStatus(e.message, true); }
          } else {
            try { applyDocumentSource(doc, setFrontmatterProperty(sourceValue(doc), key, newVal)); render(); } catch (e) { context().window.setStatus(e.message, true); }
          }
        });
        const removeBtn = h('button', { class:'copal-inline-props-remove', text:'×', title:`Remove ${key}`, 'aria-label':`Remove ${key}`, onclick:() => {
          if (native) commitNative(Object.entries(doc.properties || {}).filter(([k]) => k !== key));
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
        const target = captureNotesWorkbench();
        if (target.docId !== doc.id) return;
        const key = await styledPrompt('Property name', { title: 'Add property', confirmText: 'Next', maxLength: 96 });
        if (!key?.trim()) return;
        const val = await styledPrompt('Property value', { title: `Add ${key.trim()}`, confirmText: 'Add', maxLength: 1024 });
        if (val === null) return;
        if (!notesWorkbenchCurrent(target)) { context()?.window?.setStatus('The captured property target changed. Reopen Add property.', true); return; }
        if (native) {
          try { commitNative([...Object.entries(doc.properties || {}), [key.trim(), val]]); } catch (e) { context().window.setStatus(e.message, true); }
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
    const native = doc.kind === 'note' || doc.kind === 'wiki';
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

  /** Collapsible Wiki article chrome: links/backlinks and native Details. */
  function wikiArticleChrome(doc) {
    const chrome = h('details', { class:'copal-wiki-article-chrome' });
    chrome.append(h('summary', { text:'Article links and details' }));
    const outgoing = (doc.links || []).map((target) => ({ kind:'link', target }));
    const relations = doc.kind === 'note' || doc.kind === 'wiki' || doc.kind === 'wiki'
      ? (doc.relations || []).filter((relation) => ['link', 'embed'].includes(relation.kind))
      : outgoing;
    const links = h('div', { class:'copal-wiki-article-links' });
    links.append(h('strong', { text:'Links ' }));
    const seen = new Set();
    for (const relation of relations) {
      const targetName = relation.target || relation.targetDocumentId || '';
      if (!targetName || seen.has(targetName)) continue;
      seen.add(targetName);
      const target = state.docs.find((candidate) => candidate.id === relation.targetDocumentId) || resolveDocumentLink(state.docs, relation.target);
      links.append(h('button', {
        class:'copal-chip',
        text:`→ ${relation.target}`,
        disabled:!target,
        onclick:() => { if (target) open(target.id, { intent:'current' }); },
      }));
    }
    const incoming = linkedMentions(state.docs, doc);
    for (const mention of incoming) {
      links.append(h('button', {
        class:'copal-chip',
        text:`← ${displayName(mention.doc)}`,
        onclick:() => {
          const leaf = open(mention.doc.id, { intent:'current' });
          if (mention.line) requestAnimationFrame(() => context().noteLeafViews.get(leaf?.id)?.editor?.focusLine(mention.line));
        },
      }));
    }
    if (!seen.size && !incoming.length) links.append(h('span', { text:'None' }));
    chrome.append(links);
    const details = h('div', { class:'copal-wiki-article-details' });
    const props = doc.properties && typeof doc.properties === 'object' ? doc.properties : {};
    const propEntries = Object.entries(props);
    if (propEntries.length) {
      const fields = h('div', { class:'copal-meme-fields' });
      for (const [key, value] of propEntries) {
        const display = Array.isArray(value) ? value.join(', ') : String(value ?? '');
        fields.append(h('span', { class:'copal-chip', text:`${key}: ${display}` }));
      }
      details.append(fields);
    } else {
      details.append(h('p', { class:'copal-empty-inline', text:'No native properties.' }));
    }
    if (isOfficialDocument(doc)) details.append(h('p', { class:'copal-empty-inline', text:'Built-in article · read-only' }));
    chrome.append(details);
    return chrome;
  }

  /** Preserved-source recovery for malformed/future/legacy Wiki records. */
  function wikiRecoveryPanel(doc) {
    const legacyMarkdown = doc.recoveryState === 'legacy-import';
    const title = legacyMarkdown
      ? 'Imported Markdown needs explicit workspace conversion'
      : doc.recoveryState === 'unsupported-future'
        ? `This article uses unsupported Wiki schema version ${doc.sourceSchemaVersion || 'newer'}`
        : 'This article is preserved but cannot be decoded';
    const panel = h('div', { class:'copal-document-error copal-wiki-recovery', role:'alert' },
      h('h2', { text:title }),
      h('p', { text:String(doc.note_error || 'The stored record cannot be projected.') }),
      h('p', { text:doc.rawPreserved ? 'Its original bytes are preserved. Download the source and use the explicit workspace conversion tools before editing.' : 'Reload or restore a valid version before editing.' }),
      h('div', { class:'copal-dialog-actions' },
        doc.rawPreserved ? h('button', {
          class:'copal-btn',
          text:doc.recoveryState === 'unsupported-future' ? 'Download original' : 'Download preserved source',
          onclick:() => { window.location.href = `${state.api}/api/copal/documents/${encodeURIComponent(doc.id)}/download?workspace=${encodeURIComponent(state.workspace)}`; },
        }) : null,
      ));
    return panel;
  }

  function linksPane(doc) {
    const pane = h('div', { class:'copal-links-pane' });
    const filter = h('input', { class:'copal-links-filter', type:'search', placeholder:'Filter links…', 'aria-label':'Filter linked views' });
    const sort = h('select', { class:'copal-links-sort', 'aria-label':'Sort linked views' }, h('option', { value:'name', text:'Name' }), h('option', { value:'path', text:'Path' }));
    pane.append(h('div', { class:'copal-links-controls' }, filter, sort));
    const section = (title) => { const root = h('section', {}, h('h3', { text:title })); pane.append(root); return root; };
    const outgoing = section('Outgoing links');
    const relations = doc.kind === 'note' || doc.kind === 'wiki'
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
    const unique = [...new Set(bookmarks ? workspace.bookmarks : workspace.recent)].map(id => state.docs.find(doc => doc.id === id)).filter(doc=>doc && !isChatResource(doc));
    body.append(h('header', {}, h('strong', { text:bookmarks ? 'Bookmarks' : 'Recent documents' })), ...unique.map(button));
    if (!unique.length) body.append(h('p', { class:'copal-empty-inline', text:bookmarks ? 'Bookmark a document from its actions menu.' : 'No recent documents.' }));
  }

  function rightSidebar(workspace, shellState) {
    const doc = inspectorDoc(workspace);
    const aside = h('aside', { id:`copal-${presentationId}-right-sidebar`, class:'copal-notes-sidebar' });
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
    const folderMode = Boolean(workspace?.left?.folderWorkspaceRoot);
    return h('nav', { class:'copal-notes-ribbon', 'aria-label':'Editor actions' },
      h('button', { text:'＋', title:folderMode ? 'New File' : 'New note', 'aria-label':folderMode ? 'New File' : 'New note', onclick:() => createNew() }),
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
    ensureWorkbenchProvider(current);
    trackDocumentNavigation(current, workspace);
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
      current.notePageHideHandler = () => { persist(true); persistRecovery(); };
      window.addEventListener('pagehide', current.notePageHideHandler);
      current.noteBeforeUnloadHandler ||= event => { if (current.noteDrafts?.size || [...(current.noteBuffers?.values() || [])].some(buffer => buffer.state().dirty)) { persistRecovery(); event.preventDefault(); event.returnValue = ''; } };
      window.addEventListener('beforeunload', current.noteBeforeUnloadHandler);
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
      // Authorization/listing readiness lives outside the persisted layout.
      // Its transitions must replace stale rendered controls even when the
      // renewed ref and rows happen to be identical to the saved page.
      current.noteResourceRootReady === true,
      current.noteResourceLoading === true,
      current.noteResourceRootError || '',
      current.noteResourceRootValidationEpoch || 0,
      current.noteResourceRequestEpoch || 0,
    ]);
    const cached = current.noteShellCache;
    if (cached && cached.key === renderCacheKey && cached.docs === state.docs && cached.shell?.isConnected) {
      current.noteMetrics.renders += 1;
      current.noteMetrics.lastRenderMs = performance.now() - started;
      cached.shell.dataset.renderMs = current.noteMetrics.lastRenderMs.toFixed(2);
      afterRender?.(cached.shell);
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
    afterRender?.(shell);
    current.noteShellCache = { key:renderCacheKey, docs:state.docs, shell };
    current.noteMetrics.renders += 1;
    current.noteMetrics.lastRenderMs = performance.now() - started;
    shell.dataset.renderMs = current.noteMetrics.lastRenderMs.toFixed(2);
    shell.dataset.editorConstructions = String(current.noteMetrics.editorConstructions);
    const paneIds = [...shell.querySelectorAll('.copal-note-leaf')].filter(el => el.getClientRects().length)
      .map(el => findWorkspaceLeaf(workspace, el.dataset.leafId)?.docId).filter(Boolean);
    if (new Set(paneIds).size >= 2) acknowledgeVisible(shell, 'editor.split.presented', { mounted:true, documentIds:paneIds }, { workspaceId:state.workspace });
    if (doc && isOfficialDocument(doc) && String(doc.properties?.docId || '') !== FORMATTING_DEMO_DOC_ID) {
      acknowledgeVisible(shell, 'official.doc.opened', { resourceId:doc.id, provisionedResource:true }, { workspaceId:state.workspace });
    }
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

  function workbenchLocalRevision(current, docId) {
    return current?.noteBuffers?.get(docId)?.localRevision
      ?? current?.noteDrafts?.get(docId)?.localRevision
      ?? current?.noteRevisionCounters?.get(docId) ?? 0;
  }

  function captureNotesWorkbench() {
    const current = context();
    const workspace = ensureWorkspace();
    const leaf = activeLeaf(workspace);
    const group = leaf && groupForLeaf(workspace, leaf.id);
    const doc = activeDoc(workspace);
    const cache = current?.noteLeafViews?.get(leaf?.id);
    const editor = cache?.editor?.view?.dom?.isConnected && leaf?.mode !== 'reading' ? cache.editor : null;
    const selection = editor?.view.state.selection;
    return Object.freeze({
      current, workspace, scope:currentScope(current), generation:Number(state.filesGeneration || state.contextEpoch || 0),
      leafId:leaf?.id || null, groupId:group?.id || null, docId:doc?.id || null, doc,
      name:doc?.name || '', head:String(doc?.head || doc?.resource?.revision?.value || ''),
      readOnly:Boolean(doc?.readOnly || doc?.builtin || doc?.virtual || doc?.note_error || doc?.rawPreserved),
      pinned:leaf?.pinned === true, mode:leaf?.mode || '', view:leaf?.view || '',
      folder:editorCreationFolder(workspace, current), anchor:workspace?.left?.folderWorkspaceRoot || null,
      folderEpoch:current?.noteResourceRequestEpoch || 0,
      buffer:current?.noteBuffers?.get(doc?.id) || null, localRevision:workbenchLocalRevision(current, doc?.id),
      editor, documentIdentity:editor?.view.state.doc || null,
      ranges:selection ? selection.ranges.map(range => ({ anchor:range.anchor, head:range.head })) : [],
      mainIndex:selection?.mainIndex || 0, status:editor ? getEditorStatus() : null,
    });
  }

  function notesWorkbenchCurrent(target, { allowEditing = false, allowSaved = false, allowFolderRefresh = false } = {}) {
    const current = context();
    const workspace = current?.noteWorkspace;
    const leaf = activeLeaf(workspace);
    const doc = leaf ? workspaceDocuments().find(item => item.id === leaf.docId) || null : null;
    if (!target || current !== target.current || workspace !== target.workspace || target.scope !== currentScope(current)
      || target.generation !== Number(state.filesGeneration || state.contextEpoch || 0)
      || (leaf?.id || null) !== target.leafId || (doc?.id || null) !== target.docId
      || (leaf && groupForLeaf(workspace, leaf.id)?.id || null) !== target.groupId
      || (leaf?.pinned === true) !== target.pinned || (leaf?.mode || '') !== target.mode || (leaf?.view || '') !== target.view
      || (doc?.name || '') !== target.name
      || Boolean(doc?.readOnly || doc?.builtin || doc?.virtual || doc?.note_error || doc?.rawPreserved) !== target.readOnly
      || !allowFolderRefresh && (editorCreationFolder(workspace, current) !== target.folder || workspace?.left?.folderWorkspaceRoot !== target.anchor
        || (current?.noteResourceRequestEpoch || 0) !== target.folderEpoch)) return false;
    if (!allowEditing && workbenchLocalRevision(current, target.docId) !== target.localRevision) return false;
    if (!allowEditing && !allowSaved && (String(doc?.head || doc?.resource?.revision?.value || '') !== target.head
      || (current.noteBuffers?.get(target.docId) || null) !== target.buffer)) return false;
    if (target.editor) {
      if (current.noteLeafViews?.get(target.leafId)?.editor !== target.editor || !target.editor.view.dom.isConnected) return false;
      if (!allowEditing) {
        const selection = target.editor.view.state.selection;
        if (target.editor.view.state.doc !== target.documentIdentity || selection.mainIndex !== target.mainIndex
          || selection.ranges.length !== target.ranges.length
          || !selection.ranges.every((range, index) => range.anchor === target.ranges[index].anchor && range.head === target.ranges[index].head)) return false;
      }
    }
    return true;
  }

  function notesWorkbenchCommands(target) {
    const { workspace, doc, editor, current } = target;
    const hasDoc = doc && !doc.virtual;
    const missing = hasDoc ? '' : 'Open an Editor document first.';
    const editable = missing || (target.readOnly ? 'This document is read-only.' : '');
    const textView = ['note', 'markdown', 'wiki'].includes(target.view);
    const modeReason = editable || (!textView ? 'This document uses a typed view.' : '');
    const dirty = Boolean(current.noteDrafts?.has(target.docId) || target.buffer?.state?.().dirty);
    const creationReason = editorCreationReason(workspace, current);
    const assertTarget = (options) => { if (!notesWorkbenchCurrent(target, options)) throw new Error('The captured Editor target changed. Reopen the command.'); };
    const commands = [];
    const add = (id, label, menu, run, disabledReason = '') => commands.push({ id:`editor.${id}`, label, menu, disabledReason,
      run:() => { assertTarget(); return run(); } });
    add('new', target.folder?.provider === 'host' ? 'New file…' : 'New note…', 'File',
      () => createNew(null, { isContextCurrent:() => notesWorkbenchCurrent(target, { allowFolderRefresh:target.folder?.provider === 'host' }) }),
      creationReason);
    add('open-file', 'Open file…', 'File', () => openFileFromPicker({ isContextCurrent:() => notesWorkbenchCurrent(target) }));
    add('new-folder', target.folder?.provider === 'host' ? 'New folder…' : 'New collection…', 'File', () => createFolder({ isContextCurrent:() => notesWorkbenchCurrent(target) }), creationReason);
    add('template-folder', 'Choose template folder…', 'Tools', () => configureTemplateFolder({ isContextCurrent:() => notesWorkbenchCurrent(target) }));
    add('new-template', 'New Copal note from template…', 'File', () => createFromTemplate({ isContextCurrent:() => notesWorkbenchCurrent(target) }));
    if (typeof createWikiArticle === 'function') add('new-wiki', 'New Wiki article…', 'File', createWikiArticle);
    if (typeof importWikiMemes === 'function') add('import-memes', 'Import native .memes…', 'File', importWikiMemes);
    if (typeof exportWikiMemes === 'function') add('export-memes', 'Export native .memes…', 'File', exportWikiMemes);
    add('open-folder', 'Open folder…', 'File', () => openFolderFromPicker({ isContextCurrent:() => notesWorkbenchCurrent(target) }));
    add('close-folder', 'Close folder', 'File', () => closeFolderWorkspace(workspace), workspace.left.folderWorkspaceRoot ? '' : 'No folder workspace is open.');
    add('save', 'Save file', 'File', async () => { if (!await saveDraft(target.docId)) throw new Error('The file could not be saved. Review its save status.'); },
      editable || (doc?.savePolicy !== 'explicit' ? 'This document saves automatically.' : !dirty ? 'The file has no unsaved changes.' : ''));
    add('close-tab', 'Close editor tab', 'File', async () => {
      if (!await requestCloseLeaves([findWorkspaceLeaf(workspace, target.leafId)])) return;
      assertTarget({ allowEditing:true, allowSaved:true });
      const closed = closeWorkspaceLeaf(workspace, target.leafId); if (closed) disposeLeaf(closed.id);
      syncSelectionToModel(workspace); persist(true); render(); focusEmptyWorkspaceIfIdle();
    }, !target.leafId ? 'There is no active editor tab.' : target.pinned ? 'Unpin this tab before closing it.' : '');
    add('find', 'Find and replace…', 'Edit', () => showFindReplace(editor, { isContextCurrent:() => notesWorkbenchCurrent(target, { allowEditing:true }) }),
      editable || (!editor ? 'Switch to an editable text view first.' : ''));
    add('sidebar', 'Toggle Editor sidebar', 'View', () => toggleSidebar('left'));
    add('linked-sidebar', 'Toggle linked sidebar', 'View', () => toggleSidebar('right'));
    for (const [mode, label] of [['source', 'Source mode'], ['live', doc?.kind === 'note' ? 'Editing mode' : 'Live Preview mode'], ['reading', 'Reading mode']]) {
      const reason = mode === 'reading' ? missing || (!textView ? 'This document uses a typed view.' : '')
        : modeReason || (mode === 'source' && doc?.kind === 'note' ? 'Notes use Editing mode.' : '')
        || (mode === 'live' && isHostDocument(doc) && doc?.kind !== 'markdown' ? 'This file uses source mode.' : '');
      add(`mode-${mode}`, label, 'View', () => { setWorkspaceLeafMode(workspace, target.leafId, mode); persist(true); render(); }, reason);
    }
    const documentNavigation = current.noteDocumentNavigation;
    add('document-back', 'Previous document', 'Go', () => documentNavigation.back(), documentNavigation?.canGoBack() ? '' : 'No previous document.');
    add('document-forward', 'Next document', 'Go', () => documentNavigation.forward(), documentNavigation?.canGoForward() ? '' : 'No next document.');
    add('quick-open', 'Quick open…', 'Go', () => showChooser({ choose:(chosen) => { assertTarget(); open(chosen.id); } }));
    add('search', 'Search documents…', 'Go', showSearch);
    add('reload-document', 'Reload saved document', 'File', () => reloadDocument(target.docId), missing);
    add('undo-document', 'Undo document edit', 'Edit', () => undoDocument(target.docId), editable || (!target.buffer?.state().canUndo ? 'There is no document edit to undo.' : ''));
    add('redo-document', 'Redo document edit', 'Edit', () => undoDocument(target.docId, true), editable || (!target.buffer?.state().canRedo ? 'There is no document edit to redo.' : ''));
    add('reload-folder', 'Reload Explorer folder', 'Go', () => current.noteFilesExplorer?.refresh(),
      current.noteFilesExplorer ? '' : 'Open Explorer first.');
    add('history', 'Document revision history…', 'Go', () => isHostDocument(doc) ? openEditorResourceHistory(doc) : showHistory(doc), missing);
    add('customize-explorer','Customize Explorer…','View',customizeExplorer);
    add('switch-document-presentation',presentationId === 'wiki' ? 'Open in Editor' : 'Open in Wiki','View',() => target.docId && openOtherView?.(target.docId, presentationId === 'wiki' ? 'notes' : 'wiki'));
    add('settings', 'Editor settings…', 'Tools', showSettings);
    for (const [direction, orientation] of [['right', 'horizontal'], ['below', 'vertical']]) {
      add(`split-${direction}`, `Split editor ${direction}…`, 'Window', () => showChooser({
        title:`Split ${direction}`, allowCreate:false, choose:(chosen) => {
          assertTarget(); if (splitWorkspaceGroup(workspace, target.groupId, chosen, orientation)) { persist(true); render(); }
        },
      }), target.groupId ? '' : 'Open an editor tab group first.');
    }
    return commands;
  }

  function ensureWorkbenchProvider(current = context()) {
    const workbench = window.__openClankWorkbench;
    const host = current?.window?.root;
    if (!host) return;
    installEditorFilesStyles();
    if (workbench?.registerSurface && !(current.noteWorkbenchHost === host && current.noteWorkbenchDispose)) {
      current.noteWorkbenchDispose?.();
      current.noteWorkbenchHost = host;
      current.noteWorkbenchDispose = workbench.registerSurface(host, {
        kind:'editor', capture:captureNotesWorkbench, isCurrent:notesWorkbenchCurrent, commands:notesWorkbenchCommands,
        editingTarget:(target) => target.editor?.view.contentDOM || null,
        restore:(target) => { if (notesWorkbenchCurrent(target)) target.editor?.focus(); },
      });
    }
    if (current.noteWorkbenchMenu || current.noteWorkbenchMountToken) return;
    const token = current.noteWorkbenchMountToken = {};
    void import('../workbenchMenu.js').then(({ mountWorkbenchMenu }) => {
      if (current !== context() || current.noteWorkbenchMountToken !== token || !host.isConnected || !current.window.visible) {
        if (current.noteWorkbenchMountToken === token) current.noteWorkbenchMountToken = null;
        return;
      }
      // The outer .copal-modal-content sits directly beneath the title, while
      // Notes replaces only window.body. Never mount inside a rebuilt leaf.
      current.noteWorkbenchMenu = mountWorkbenchMenu({ host, container:current.window.content, before:current.window.body,
        id:current.window.id, kind:'editor', label:presentationId === 'wiki' ? 'Wiki' : 'Editor' });
      current.noteWorkbenchMountToken = null;
      ensureWorkbenchProvider(current);
      current.window.body.querySelectorAll('.copal-editor-file-menu').forEach(menu => menu.remove());
    }).catch(error => {
      if (current.noteWorkbenchMountToken === token) current.noteWorkbenchMountToken = null;
      current.window.setStatus(error?.message || 'Editor menu could not be loaded.', true);
    });
  }

  function runCommand(name) {
    const workspace = ensureWorkspace();
    const leaf = activeLeaf(workspace);
    const editor = context()?.noteLeafViews?.get(leaf?.id)?.editor;
    if (name === 'save') { const doc = activeDoc(workspace); if (!doc) return false; void saveDraft(doc.id); return true; }
    if (name === 'quick-open') { showChooser(); return true; }
    if (name === 'palette') { showCommands(); return true; }
    if (name === 'search') { showSearch(); return true; }
    if (name === 'retry-syntax') { void editor?.retrySyntax?.(); return Boolean(editor); }
    return editor?.runCommand?.(name) || false;
  }

  function getEditorStatus() {
    const leaf = activeLeaf();
    return context()?.noteLeafViews?.get(leaf?.id)?.editor?.getStatus?.() || null;
  }

  // Files uses this single exact-open seam for Open in Editor and Files row
  // actions.  The callback still reopens through Files-v1, preserving one
  // ResourceHandle/buffer identity and current CAS revision.
  if (registerGlobalOpener) globalThis.__openClankOpenResourceHandle = resourceOpener;

  return {
    receiveDocumentSource, receiveSaveState:(docId, value) => setLeafSaveState(docId, value, false),
    setMode:(mode) => { const doc = activeDoc(); const leaf = activeLeaf(); if (doc && leaf) open(doc.id, { leafId:leaf.id, mode }); },
    pinDocument:toggleBookmark,
    showDocumentPanel:(panel) => { const workspace = ensureWorkspace(); workspace.right.open = true; workspace.right.tab = panel; persist(true); render(); },
    insertAttachment:(target) => { if (!notesWorkbenchCurrent(target) || target.readOnly) return false; const cache = target.current.noteLeafViews.get(target.leafId); chooseAttachment(cache, target.doc); return true; },
    render, open, openResource, customizeExplorer, destroy, beforeWindowClose, resolveDirtyDocuments, persistRecovery, flushAll, queueSave, queueDocumentSave, suspendScope, loadSaved, showChooser, showCommands, showSearch, showSettings, insertTemplate, createTemplateFromCurrent, createFromTemplate, openDailyNote,
    acceptSavedDocument, getDraftSnapshot, getAuthoritativeSnapshot, applyDocumentTransaction, rotateResourceRef, retrySaveAtRevision, retryDocumentSave, documentRetryState, rebaseDocumentAtRevision, projectDocument, prepareDelete, getSettings, updateSettings, getNotesPanels, updateNotesPanel, focusSourceLine, flushDocument:saveDraft,
    getContext:context, openResourceRef:resourceOpener, captureEditorTarget:captureNotesWorkbench, editorTargetCurrent:notesWorkbenchCurrent,
    subscribeToDocumentBuffer, invalidateBaseLeaves, runCommand, getEditorStatus,
    mergeTemplateProperties,
    toggleLeft:() => toggleSidebar('left'),
    toggleRight:() => toggleSidebar('right'),
  };
}
