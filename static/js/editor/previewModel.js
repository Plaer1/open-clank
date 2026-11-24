// Preview presentation helpers shared by Files and future provider adapters.
// This module is deliberately authority-neutral: server-supplied preview
// descriptors are authoritative, while MIME/extension classification is only a
// UI hint for deciding which control to offer.

import { isTextPath } from './entryModel.js';

// Presentation hints only: server authorization and adapter validation remain authoritative.
const METADATA_FORMATS = Object.freeze({
  safetensors: {suffixes:['.safetensors'], render:safetensorsDetails, label:'Model header'},
  gguf: {suffixes:['.gguf'], render:safetensorsDetails, label:'Model header'},
  asar: {suffixes:['.asar'], render:asarDetails, label:'Archive header'},
  fastfile: {suffixes:['.ff'], render:fastfileDetails, label:'FastFile candidates'},
  wasm: {suffixes:['.wasm'], render:wasmDetails, label:'WebAssembly sections'},
  elf: {suffixes:['.elf','.so','.o','.axf'], render:elfDetails, label:'ELF headers'},
  pe: {suffixes:['.exe','.dll','.sys','.efi'], render:peDetails, label:'PE headers'},
  macho: {suffixes:['.dylib','.bundle','.macho'], render:machoDetails, label:'Mach-O framing'},
  sqlite: {suffixes:['.sqlite','.sqlite3','.db'], render:sqliteDetails, label:'SQLite schema'},
});
export function metadataFormatId(value) {
  return typeof value === 'string' && Object.hasOwn(METADATA_FORMATS, value) ? value : null;
}
export function metadataFormatForName(name) {
  const lower = String(name || '').toLowerCase();
  return Object.keys(METADATA_FORMATS).find(id => METADATA_FORMATS[id].suffixes.some(suffix => lower.endsWith(suffix))) || null;
}
export function isMetadataInspection(format) {
  return format?.version === 1 && metadataFormatId(format.formatId) !== null && format.operations?.inspect?.available === true
    && Array.isArray(format.outputKinds) && format.outputKinds.includes('metadata');
}
export function metadataDetails(result, ref, revision, generation, formatId) {
  const item = METADATA_FORMATS[metadataFormatId(formatId)];
  if (!item) throw new Error('Inspection format is unavailable.');
  return item.render(result, ref, revision, generation);
}
export function metadataLabel(formatId) {
  return METADATA_FORMATS[metadataFormatId(formatId)]?.label || 'Metadata';
}

const IMAGE_EXTENSIONS = /\.(png|jpe?g|gif|webp|avif|bmp|tiff?)$/i;
const AUDIO_EXTENSIONS = /\.(mp3|wav|flac|ogg|opus|m4a|aac|wma)$/i;

export function previewMime(entry = {}) {
  return String(entry?.mime_type || entry?.mimeType || entry?.media_type || entry?.mime || '').trim().toLowerCase();
}

export function previewDescriptor(entry = {}) {
  const descriptor = entry.preview || entry.preview_descriptor || entry.previewDescriptor;
  return descriptor && typeof descriptor === 'object' && !Array.isArray(descriptor) ? descriptor : null;
}

export function previewKind(entry, path = '') {
  const descriptor = previewDescriptor(entry);
  const explicit = String(descriptor?.kind || entry.preview_kind || '').toLowerCase();
  if (descriptor?.allowed === false || entry.previewable === false) return null;
  if (entry?.format != null) {
    const format = entry.format;
    if (typeof format !== 'object' || Array.isArray(format) || format.version !== 1 || format.operations?.preview?.available !== true) return null;
    const refs = [entry?.ref, entry?.resource_ref].filter(value => typeof value === 'string' && value);
    if (refs.some(ref => ref !== format.resourceRef)) return null;
    if (entry?.policy_generation != null && entry.policy_generation !== format.policyGeneration) return null;
    if (Object.hasOwn(entry, 'revision')) {
      const a = entry.revision, b = format.providerRevision;
      if ((a == null) !== (b == null) || (a != null && (a.kind !== b?.kind || a.value !== b?.value))) return null;
    }
    const kind = String(format.formatId || '').toLowerCase();
    if (kind === 'iwi' && Array.isArray(format.outputKinds) && format.outputKinds.includes('image')) return 'image';
    return ['image', 'audio', 'video', 'text'].includes(kind) && Array.isArray(format.outputKinds) && format.outputKinds.includes(kind) ? kind : null;
  }
  if (explicit === 'image' || explicit === 'audio' || explicit === 'text') return explicit;
  const mime = previewMime(entry);
  const name = String(entry?.name || path || '').toLowerCase();
  if (mime.startsWith('image/') || IMAGE_EXTENSIONS.test(name)) return 'image';
  if (mime.startsWith('audio/') || AUDIO_EXTENSIONS.test(name)) return 'audio';
  if (mime==='video/webm' && /\.webm$/i.test(name)) return 'video';
  if (mime.startsWith('text/') || isTextPath(name)) return 'text';
  return null;
}

// This is a presentation hint; WB06 reauthorizes and verifies the bytes.
export function hasBrowserPreview(entry) {
  const format = entry?.format;
  const ref = entry?.ref || entry?.resource_ref;
  const revision = entry?.revision;
  return Boolean(format?.version === 1 && format.operations?.browserPreview?.available === true
    && typeof ref === 'string' && ref && format.resourceRef === ref
    && revision && typeof revision.kind === 'string' && revision.kind
    && typeof revision.value === 'string' && revision.value
    && format.providerRevision?.kind === revision.kind && format.providerRevision?.value === revision.value
    && entry.policy_generation === format.policyGeneration);
}

export function isPreviewable(entry, path = '') {
  return previewKind(entry, path) !== null;
}

export function boundedTextPreview(text, metadata = {}, limit = 320_000) {
  const value = String(text || '');
  if (value.length <= limit) return { text: value, truncated: false };
  const head = Math.floor(limit * 0.72);
  const tail = Math.max(0, limit - head);
  const size = Number(metadata.size || value.length);
  const formatted = Number.isFinite(size) && size >= 1024
    ? `${(size / (1024 ** Math.floor(Math.log(size) / Math.log(1024)))).toFixed(1)} ${['B', 'KB', 'MB', 'GB', 'TB'][Math.min(4, Math.floor(Math.log(size) / Math.log(1024)))]}`
    : `${size || 0} B`;
  return {
    text: `${value.slice(0, head)}\n\n… [preview truncated; ${formatted} total] …\n\n${value.slice(-tail)}`,
    truncated: true,
  };
}

function abortError() {
  return new DOMException('The operation was aborted.', 'AbortError');
}

function managedTextEncoding(bytes, contentType = '') {
  if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) return 'utf-8';
  if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) return 'utf-16le';
  if (bytes.length >= 2 && bytes[0] === 0xfe && bytes[1] === 0xff) return 'utf-16be';
  const charset = /charset\s*=\s*["']?([^;"'\s]+)/i.exec(String(contentType || ''))?.[1];
  return charset || 'utf-8';
}

function sampledTextIsBinary(text) {
  for (const character of String(text || '')) {
    const code = character.codePointAt(0);
    if (code === 0) return true;
    if (code < 0x20 && ![0x09, 0x0a, 0x0c, 0x0d, 0x1b].includes(code)) return true;
    if (code === 0x7f) return true;
  }
  return false;
}

/**
 * Consume a managed-provider text response without ever retaining an
 * unbounded Blob/ArrayBuffer in the renderer. A response without a readable
 * stream is refused so the caller can offer the normal download path.
 */
export async function readBoundedTextResponse(response, { limit = 320_000, signal = null } = {}) {
  const byteLimit = Math.max(1, Math.min(512 * 1024, Number(limit) || 320_000));
  if (signal?.aborted) throw abortError();
  if (!response?.body?.getReader) {
    throw new Error('Streaming preview is unavailable; download the file to view it safely.');
  }
  const reader = response.body.getReader();
  const retained = new Uint8Array(byteLimit);
  let retainedBytes = 0;
  let streamDone = false;
  let truncated = false;
  const contentLengthValue = Number(response.headers?.get?.('content-length'));
  const contentEncoding = String(response.headers?.get?.('content-encoding') || '').trim().toLowerCase();
  const declaredSize = (!contentEncoding || contentEncoding === 'identity')
    && Number.isSafeInteger(contentLengthValue) && contentLengthValue >= 0
    ? contentLengthValue
    : null;
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal?.addEventListener('abort', abort, { once: true });
  try {
    while (!streamDone) {
      const chunk = await reader.read();
      if (signal?.aborted) throw abortError();
      streamDone = Boolean(chunk.done);
      if (streamDone) break;
      const bytes = chunk.value instanceof Uint8Array ? chunk.value : new Uint8Array(chunk.value || 0);
      const remaining = byteLimit - retainedBytes;
      if (bytes.byteLength > remaining) {
        retained.set(bytes.subarray(0, remaining), retainedBytes);
        retainedBytes += remaining;
        truncated = true;
        await reader.cancel('text preview byte limit reached').catch(() => {});
        break;
      }
      retained.set(bytes, retainedBytes);
      retainedBytes += bytes.byteLength;
      if (retainedBytes === byteLimit && declaredSize !== null && declaredSize > byteLimit) {
        truncated = true;
        await reader.cancel('text preview byte limit reached').catch(() => {});
        break;
      }
      if (retainedBytes === byteLimit) {
        // With no trustworthy length, one additional stream observation is
        // required to distinguish an exact-limit response from truncation.
        const probe = await reader.read();
        if (signal?.aborted) throw abortError();
        streamDone = Boolean(probe.done);
        truncated = !streamDone;
        if (truncated) await reader.cancel('text preview byte limit reached').catch(() => {});
      }
    }
  } finally {
    signal?.removeEventListener('abort', abort);
  }

  const bytes = retained.subarray(0, retainedBytes);
  const encoding = managedTextEncoding(bytes, response.headers?.get?.('content-type'));
  let text;
  try {
    // A truncated stream may end partway through a code point. `stream: true`
    // preserves the valid prefix without replacing an incomplete suffix;
    // invalid bytes in the retained sample still fail because `fatal` is set.
    text = new TextDecoder(encoding, { fatal: true }).decode(bytes, { stream: truncated });
  } catch {
    throw new Error('This managed file is not supported as text; download it instead.');
  }
  if (sampledTextIsBinary(text)) {
    throw new Error('This managed file appears to be binary; download it instead.');
  }
  return {
    text,
    encoding,
    truncated,
    tailIncluded: false,
    bytesRead: retainedBytes,
    size: truncated ? declaredSize : retainedBytes,
  };
}

/** Validate and retain one bounded IWI-derived PNG against the captured descriptor. */
export async function readIwiPreview(response, { descriptor, revision, signal = null } = {}) {
  if (signal?.aborted) throw abortError();
  const format = descriptor && typeof descriptor === 'object' ? descriptor : null;
  if (format?.version !== 1 || format.formatId !== 'iwi' || format.operations?.preview?.available !== true
      || !revision || revision.kind !== format.providerRevision?.kind || revision.value !== format.providerRevision?.value) {
    throw new Error('The captured IWI preview identity is unavailable.');
  }
  if (!response?.ok || !response.body?.getReader || !String(response.headers.get('content-type') || '').toLowerCase().startsWith('image/png')) {
    throw new Error('The IWI preview is unavailable.');
  }
  const sha = response.headers.get('X-OpenClank-Source-SHA256') || '';
  const decoder = response.headers.get('X-OpenClank-Decoder') || '';
  const variant = response.headers.get('X-OpenClank-Variant') || '';
  if (!/^[a-f0-9]{64}$/.test(sha) || decoder !== format.decoderGeneration || !['DXT1', 'DXT3', 'DXT5'].includes(variant)
      || response.headers.get('X-OpenClank-Revision-Kind') !== revision.kind
      || response.headers.get('X-OpenClank-Revision-Value') !== revision.value) {
    throw new Error('The IWI preview no longer matches its captured source.');
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  const limit = 64 * 1024 * 1024;
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal?.addEventListener('abort', abort, { once:true });
  try {
    while (true) {
      const part = await reader.read();
      if (signal?.aborted) throw abortError();
      if (part.done) break;
      const bytes = part.value instanceof Uint8Array ? part.value : new Uint8Array(part.value || 0);
      size += bytes.byteLength;
      if (size > limit) { await reader.cancel('preview limit').catch(() => {}); throw new Error('The IWI preview exceeds its display limit.'); }
      chunks.push(bytes);
    }
  } finally {
    signal?.removeEventListener('abort', abort);
    try { reader.releaseLock(); } catch (_) {}
  }
  if (!size) throw new Error('The IWI preview is empty.');
  const prefix = new Uint8Array(await new Blob(chunks).slice(0, 8).arrayBuffer());
  if (prefix.length !== 8 || ![137,80,78,71,13,10,26,10].every((value, index) => prefix[index] === value)) {
    throw new Error('The IWI preview is not a PNG.');
  }
  if (signal?.aborted) throw abortError();
  return { blob:new Blob(chunks, { type:'image/png' }), evidence:{ sourceSHA256:sha, decoder, variant, revision:{...revision} } };
}

export function safetensorsDetails(result, ref, revision, generation) {
  if (result?.version !== 1 || !['safetensors-header-1','gguf-v3-header-1'].includes(result.profile) || result.resourceRef !== ref
      || result.providerRevision?.kind !== revision?.kind || result.providerRevision?.value !== revision?.value
      || result.policyGeneration !== generation || !/^[a-f0-9]{64}$/.test(result.headerSHA256 || '')
      || result.payloadIntegrity !== 'not_checked' || !Array.isArray(result.tensors) || result.tensors.length > 256
      || !Number.isSafeInteger(result.tensorCount) || !Number.isSafeInteger(result.omittedTensors)) throw new Error('Inspection receipt is stale or unavailable.');
  const rows = result.tensors.map(t => {
    if (typeof t?.name !== 'string' || !Array.isArray(t.shape)) throw new Error('Invalid tensor declaration.');
    if (result.profile === 'gguf-v3-header-1') return `${t.name} · ${t.typeName || `unknown type ${t.typeCode}`} · [${t.shape.join(', ')}] · offset ${t.dataOffset} · ${t.declaredBytes == null ? 'size unknown' : `${t.declaredBytes} declared bytes`}`;
    if (typeof t.dtype !== 'string' || !Array.isArray(t.dataOffsets)) throw new Error('Invalid tensor declaration.');
    return `${t.name} · ${t.dtype} · [${t.shape.join(', ')}] · data offsets ${t.dataOffsets.join('–')}`;
  });
  return [`Declared tensors: ${result.tensorCount}; omitted: ${result.omittedTensors}.`,
    `Header SHA-256 (bytes ${result.headerRange.offset}–${result.headerRange.offset + result.headerRange.length - 1} only): ${result.headerSHA256}`,
    result.profile === 'gguf-v3-header-1' ? `Unknown tensor sizes: ${result.unknownTensorSizes}. Tensor payload integrity was not checked.` : `Declared data bytes: ${result.dataBytes}. Tensor payload integrity was not checked.`, ...rows].join('\n');
}

export function asarDetails(result, ref, revision, generation) {
  if (result?.version !== 1 || result.profile !== 'asar-header-1' || result.resourceRef !== ref
      || result.providerRevision?.kind !== revision?.kind || result.providerRevision?.value !== revision?.value
      || result.policyGeneration !== generation || !/^[a-f0-9]{64}$/.test(result.headerSHA256 || '')
      || result.payloadIntegrity !== 'not_checked' || !Array.isArray(result.entries) || result.entries.length > 256
      || !Number.isSafeInteger(result.entryCount) || !Number.isSafeInteger(result.omittedEntries)
      || !Number.isSafeInteger(result.headerRange?.length)) throw new Error('Inspection receipt is stale or unavailable.');
  const rows = result.entries.map(entry => {
    if (typeof entry?.path !== 'string' || !['file','directory','link'].includes(entry.kind) || typeof entry.unpacked !== 'boolean') throw new Error('Invalid archive declaration.');
    if (entry.kind === 'file' && (!Number.isSafeInteger(entry.size) || (entry.offset != null && !Number.isSafeInteger(entry.offset)))) throw new Error('Invalid archive range.');
    if (entry.kind === 'link' && typeof entry.link !== 'string') throw new Error('Invalid archive link.');
    return `${entry.path} · ${entry.kind}${entry.unpacked ? ' · unpacked declaration' : ''}${entry.kind === 'file' ? ` · ${entry.size} bytes${entry.offset != null ? ` · packed offset ${entry.offset}` : ''}` : ''}${entry.kind === 'link' ? ` → ${entry.link}` : ''}`;
  });
  return [`Declared entries: ${result.entryCount}; omitted: ${result.omittedEntries}.`,
    `Header SHA-256 (first ${result.headerRange.length} bytes only): ${result.headerSHA256}`,
    'Payload integrity was not checked; links and unpacked entries were not followed.', ...rows].join('\n');
}

export function sqliteDetails(result, ref, revision, generation) {
  if (result?.version !== 1 || result.profile !== 'sqlite-main-schema-1' || result.resourceRef !== ref
      || result.providerRevision?.kind !== revision?.kind || result.providerRevision?.value !== revision?.value
      || result.policyGeneration !== generation || result.contentSHA256 !== null || result.payloadIntegrity !== 'not_checked'
      || result.snapshotScope !== 'complete_main_file' || result.transactionalCompleteness !== 'unknown' || result.neighborState !== 'not_inspected'
      || !/^[0-9a-f]{64}$/.test(result.snapshotSHA256 || '') || !Array.isArray(result.rows) || result.rows.length > 256
      || result.displayedRows !== result.rows.length || typeof result.additionalRows !== 'boolean')
    throw new Error('Inspection receipt is stale or unavailable.');
  return [`Captured main-file SHA-256: ${result.snapshotSHA256}.`,
    'Main file only: WAL and rollback journal were not inspected; transactional completeness is unknown.',
    `Schema declarations displayed: ${result.displayedRows}${result.additionalRows ? '; further declarations exist' : ''}.`,
    ...result.rows.map(row => `${JSON.stringify(row.type)} ${JSON.stringify(row.name)} · table ${JSON.stringify(row.tableName)}\n${row.sql === null ? '(no SQL declaration)' : row.sql}`)].join('\n');
}

export function machoDetails(result, ref, revision, generation) {
  if (result?.version !== 1 || result.profile !== 'macho-framing-1' || result.resourceRef !== ref
      || result.providerRevision?.kind !== revision?.kind || result.providerRevision?.value !== revision?.value
      || result.policyGeneration !== generation || result.contentSHA256 !== null || result.payloadIntegrity !== 'not_checked'
      || ![32,64].includes(result.bits) || !['thin','fat'].includes(result.container))
    throw new Error('Inspection receipt is stale or unavailable.');
  if (result.container === 'thin') {
    if (!Array.isArray(result.commands) || result.commands.length > 256 || result.commandCount - result.commands.length !== result.omittedCommands) throw new Error('Invalid Mach-O framing receipt.');
    return [`Thin Mach-O ${result.bits}-bit · ${result.byteOrder} endian · CPU ${result.cpuType}/${result.cpuSubtype}`,
      `Declared commands: ${result.commandCount}; not displayed: ${result.omittedCommands}.`,
      'Command framing only; command bodies, executable validity and payload integrity were not checked.',
      ...result.commands.map(row => `Command ${row.command} · file offset ${row.offset} · size ${row.size}`)].join('\n');
  }
  if (!Array.isArray(result.slices) || result.slices.length > 64 || result.slices.length !== result.sliceCount || result.sliceContents !== 'not_inspected') throw new Error('Invalid Mach-O framing receipt.');
  return [`Universal Mach-O ${result.bits}-bit slice table · ${result.sliceCount} entries.`,
    'Slice contents, CPU matches and payload integrity were not checked.',
    ...result.slices.map(row => `CPU ${row.cpuType}/${row.cpuSubtype} · file offset ${row.fileOffset} · file size ${row.fileSize} · alignment exponent ${row.alignmentExponent}`)].join('\n');
}

export function peDetails(result, ref, revision, generation) {
  if (result?.version !== 1 || result.profile !== 'pe-image-metadata-1' || result.resourceRef !== ref
      || result.providerRevision?.kind !== revision?.kind || result.providerRevision?.value !== revision?.value
      || result.policyGeneration !== generation || result.contentSHA256 !== null || result.payloadIntegrity !== 'not_checked'
      || ![32,64].includes(result.bits) || !Array.isArray(result.sections) || result.sections.length > 96
      || result.sectionCount !== result.sections.length || result.dataDirectories !== 'not_inspected')
    throw new Error('Inspection receipt is stale or unavailable.');
  return [`PE${result.bits === 64 ? '32+' : '32'} · machine ${result.machine} · subsystem ${result.subsystem}`,
    `Entry point RVA ${result.entryPointRVA} · image base ${result.imageBase} · sections ${result.sectionCount}.`,
    'Header and section declarations only; executable validity and payload integrity were not checked.',
    ...result.sections.map(row => `Section ${JSON.stringify(row.name)} · RVA ${row.virtualAddressRVA} · virtual size ${row.virtualSize} · ${row.rawSize ? `file offset ${row.rawOffset}, raw size ${row.rawSize}` : 'no file-backed bytes'}`)].join('\n');
}

export function elfDetails(result, ref, revision, generation) {
  if (result?.version !== 1 || result.profile !== 'elf-v1-metadata-1' || result.resourceRef !== ref
      || result.providerRevision?.kind !== revision?.kind || result.providerRevision?.value !== revision?.value
      || result.policyGeneration !== generation || result.contentSHA256 !== null || result.payloadIntegrity !== 'not_checked'
      || ![32,64].includes(result.bits) || !['little','big'].includes(result.byteOrder)
      || !Array.isArray(result.sections) || !Array.isArray(result.programs) || result.sections.length > 256 || result.programs.length > 256)
    throw new Error('Inspection receipt is stale or unavailable.');
  return [`ELF${result.bits} · ${result.byteOrder} endian · type ${result.objectType} · machine ${result.machine}`,
    `Declared sections: ${result.sectionCount}; omitted: ${result.omittedSections}. Programs: ${result.programCount}; omitted: ${result.omittedPrograms}.`,
    'Header and table declarations only; executable validity and payload integrity were not checked.',
    ...result.sections.map(row => `Section ${row.name === null ? '(no name)' : JSON.stringify(row.name)} · type ${row.type} · file offset ${row.offset} · declared size ${row.size}`),
    ...result.programs.map(row => `Program type ${row.type} · file offset ${row.offset} · file size ${row.fileSize} · memory size ${row.memorySize}`)].join('\n');
}

export function wasmDetails(result, ref, revision, generation) {
  if (result?.version !== 1 || result.profile !== 'wasm-core-v1-framing-1' || result.resourceRef !== ref
      || result.providerRevision?.kind !== revision?.kind || result.providerRevision?.value !== revision?.value
      || result.policyGeneration !== generation || result.contentSHA256 !== null || result.payloadIntegrity !== 'not_checked'
      || !['framing_only','partial_budget'].includes(result.coverage) || !Array.isArray(result.sections) || result.sections.length > 256
      || !Number.isSafeInteger(result.fileBytes) || result.fileBytes < 8 || result.fileBytes > 268435456
      || !Number.isSafeInteger(result.scannedSections) || result.scannedSections < result.sections.length || result.scannedSections > 1024
      || result.omittedScannedSections !== result.scannedSections - result.sections.length)
    throw new Error('Inspection receipt is stale or unavailable.');
  const rows = result.sections.map(row => {
    if (!Number.isSafeInteger(row?.id) || row.id < 0 || row.id > 255
        || !Number.isSafeInteger(row.sectionOffset) || !Number.isSafeInteger(row.payloadOffset)
        || !Number.isSafeInteger(row.payloadLength) || row.sectionOffset < 8 || row.payloadOffset <= row.sectionOffset
        || row.payloadLength < 0 || row.payloadOffset + row.payloadLength > result.fileBytes
        || (row.id === 0 ? typeof row.customName !== 'string' || new TextEncoder().encode(row.customName).length > 4096 : row.customName !== null))
      throw new Error('Invalid WebAssembly framing receipt.');
    return `Section ${row.id} · offset ${row.sectionOffset} · payload ${row.payloadOffset} + ${row.payloadLength} bytes${row.id === 0 ? ` · name ${JSON.stringify(row.customName)}` : ''}`;
  });
  return [`Scanned sections: ${result.scannedSections}; scanned entries not shown: ${result.omittedScannedSections}.`,
    result.coverage === 'partial_budget' ? 'Partial scan: further sections were not counted.' : 'Section framing only; module validity was not checked.',
    'Payload integrity and instructions were not checked.', ...rows].join('\n');
}

export function fastfileDetails(result, ref, revision, generation) {
  const supported = (result?.profile === 'fastfile-cod4-v5-1' && result?.variant === 'unsigned-cod4-v5')
    || (result?.profile === 'fastfile-waw-v387-1' && result?.variant === 'unsigned-waw-v387');
  if (result?.version !== 1 || !supported || result.resourceRef !== ref
      || result.providerRevision?.kind !== revision?.kind || result.providerRevision?.value !== revision?.value
      || result.policyGeneration !== generation || result.coverage !== 'heuristic_non_exhaustive'
      || result.payloadIntegrity !== 'not_checked' || !/^[a-f0-9]{64}$/.test(result.contentSHA256 || '')
      || !Array.isArray(result.candidates) || result.candidates.length > 2048
      || result.candidateCount !== result.candidates.length || !Number.isSafeInteger(result.expandedBytes))
    throw new Error('Inspection receipt is stale or unavailable.');
  const rows = result.candidates.map(row => {
    if (typeof row?.name !== 'string' || row.name.length > 256 || !Number.isSafeInteger(row.decompressedOffset)
        || !Number.isSafeInteger(row.declaredLength) || row.decompressedOffset < 0 || row.declaredLength < 1
        || row.decompressedOffset + row.declaredLength > result.expandedBytes)
      throw new Error('Invalid FastFile candidate.');
    return `${row.name} · declared ${row.declaredLength} bytes · decompressed-stream offset ${row.decompressedOffset}`;
  });
  const label = result.variant === 'unsigned-waw-v387' ? 'Unsigned WaW v387' : 'Unsigned CoD4 v5';
  return [`${label} · ${result.candidateCount} heuristic candidates; this scan is not exhaustive.`,
    `Compressed source SHA-256: ${result.contentSHA256}`,
    'Candidates are not verified assets. No match does not prove absence; payload integrity was not checked.', ...rows].join('\n');
}
