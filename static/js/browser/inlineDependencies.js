import { filesFacadeClient } from '../filesFacadeClient.js';
import { readBoundedTextResponse } from '../editor/previewModel.js';
import { acquirePreviewDependencies } from './dependencyManifest.js';

const equalRevision = (a, b) => !!a && !!b && a.kind === b.kind && a.value === b.value;
const stale = () => new DOMException('Files source or scope changed.', 'AbortError');

/** Acquire only declared resources beneath the authorized source's actual parent. */
export async function acquireInlineDependencies({ target, signal, isCurrent, client = filesFacadeClient, fetchContent = fetch } = {}) {
  const current = () => !signal?.aborted && isCurrent?.() === true;
  const check = () => { if (!current()) throw stale(); };
  const readText = async (ref, requestSignal, revision) => {
    check();
    const response = await fetchContent(client.contentUrl(ref, {purpose:'preview', expectedRevision:revision}),
      {credentials:'same-origin', cache:'no-store', signal:requestSignal});
    if (!response.ok) throw new Error('Files preview content is unavailable.');
    const result = await readBoundedTextResponse(response, {limit:512 * 1024, signal:requestSignal});
    check();
    if (result.truncated) throw new Error('A linked source exceeds the dependency scan limit.');
    return result.text;
  };
  check();
  let revealed;
  try { revealed = await client.reveal(target.resourceRef, {signal}); }
  catch (error) {
    check();
    if (error?.name === 'AbortError') throw error;
    return {dependencies:[], warnings:['The authorized source folder is unavailable; linked resources were not acquired.']};
  }
  check();
  const source = revealed?.resource, parent = revealed?.parent;
  if (!source?.id || source.id !== target.resourceID || !equalRevision(source.revision,target.revision)) throw stale();
  const sourceRef = source.ref || source.resource_ref, parentRef = parent?.ref || parent?.resource_ref;
  if (!sourceRef || !parentRef || !parent?.id || parent.id === source.id || !parent.capabilities?.includes('children')
      || !source.capabilities?.includes('preview') || !source.capabilities?.includes('stat')) {
    return {dependencies:[], warnings:['The authorized source folder cannot be traversed; linked resources were not acquired.']};
  }
  try {
    const html = await readText(sourceRef, signal, source.revision);
    check();
    return await acquirePreviewDependencies({html, rootRef:parentRef, client, readText, signal, isCurrent:current});
  } catch (error) {
    check();
    if (error?.name === 'AbortError') throw error;
    return {dependencies:[], warnings:['Linked resources could not be acquired through Files; only self-contained content may work.']};
  }
}
