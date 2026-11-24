import { isTextPath, EDITOR_BINARY_SUFFIX_PATTERN } from './editor/languageRegistry.js';
import { isMetadataInspection } from './editor/previewModel.js';
import { graphDisabledReason } from './copal/resourceGraphCommands.js';

const EDITOR_BINARY_SUFFIX = new RegExp(EDITOR_BINARY_SUFFIX_PATTERN, 'i');

function hasCapability(resource, name) {
  const capabilities = resource?.capabilities;
  return Array.isArray(capabilities) ? capabilities.includes(name) : capabilities?.[name] === true;
}

function resourceRef(resource) {
  return resource?.resource_ref || resource?.resourceRef || resource?.ref;
}

export function fileResourceIsDirectory(resource) {
  return hasCapability(resource, 'children') ||
    ['directory', 'recursive_directory', 'folder', 'virtual_folder', 'provider_root', 'album'].includes(String(resource?.kind || '').toLowerCase());
}

export function fileResourceIsTextual(resource) {
  const name = String(resource?.name || '');
  if (isMetadataInspection(resource?.format)) return true;
  if (EDITOR_BINARY_SUFFIX.test(name)) return false;
  if (['open_editor', 'edit', 'text'].some(name => hasCapability(resource, name))) return true;
  if (resource?.open_target?.app === 'editor' || resource?.format?.formatId === 'iwi') return true;
  const mime = String(resource?.mime_type || resource?.media_type || '').toLowerCase();
  return mime.startsWith('text/') || /(?:json|javascript|typescript|xml|yaml|toml|sql|markdown)/.test(mime) || isTextPath(name);
}

export function fileResourceCanOpen(resource) {
  return Boolean(!resource?.unavailable && resourceRef(resource) &&
    hasCapability(resource, fileResourceIsDirectory(resource) ? 'children' : 'open'));
}

export function fileResourceCanOpenInEditor(resource) {
  return fileResourceCanOpen(resource) && !fileResourceIsDirectory(resource) && fileResourceIsTextual(resource);
}

export function fileResourceCanOpenCodeGraph(resource) {
  return !resource?.unavailable && !graphDisabledReason([resource]) &&
    (fileResourceIsDirectory(resource) || (fileResourceIsTextual(resource) && !isMetadataInspection(resource.format)));
}

export function fileResourceCanShare(resource) {
  return Boolean(!resource?.unavailable && resource?.provider === 'host' && resource.kind === 'file' &&
    resourceRef(resource) && resource.revision && hasCapability(resource, 'stat') && hasCapability(resource, 'download'));
}

export function fileResourceCanBrowseHostFolder(resource) {
  return Boolean(!resource?.unavailable && resource?.provider === 'host' && resourceRef(resource) && hasCapability(resource, 'children'));
}

export function fileResourceCanReveal(resource) {
  return Boolean(!resource?.unavailable && resourceRef(resource) && hasCapability(resource, 'stat'));
}

export function fileResourceCanCopyPath(resource, clipboard = globalThis.navigator?.clipboard) {
  return Boolean(!resource?.unavailable && typeof resource?.path === 'string' && resource.path.trim() && clipboard?.writeText);
}

export function fileResourceCopyPath(resource, clipboard = globalThis.navigator?.clipboard) {
  return fileResourceCanCopyPath(resource, clipboard) ? clipboard.writeText(resource.path) : false;
}
