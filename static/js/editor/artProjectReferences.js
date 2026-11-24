/** Explicit Gallery selection; returned identities describe, and never grant, access. */
import { openFilesImageBrowser } from '../filesImageBrowser.js'
import { filesFacadeClient } from '../filesFacadeClient.js'

const encoder = new TextEncoder()
const valid = (value, maximum) => typeof value === 'string' && value.length > 0 &&
  value.isWellFormed() && encoder.encode(value).length <= maximum && !/[\u0000-\u001f\u007f-\u009f]/u.test(value)
const sameRevision = (a, b) => !!a && !!b && a.kind === b.kind && a.value === b.value

function assertCurrent(accountId, current, signal) {
  if (!valid(accountId, 256) || typeof current !== 'function' || signal?.aborted || current() !== true)
    throw new Error('The original image project is unavailable or changed.')
}

/** Reopen the authorized picker and return only a stable key and observed revision. */
export async function selectArtProjectReference({ accountId, current, signal, picker = openFilesImageBrowser, client = filesFacadeClient }) {
  assertCurrent(accountId, current, signal)
  if (typeof picker !== 'function' || !client || typeof client.openResource !== 'function' || typeof client.stat !== 'function') throw new Error('Files selection is unavailable.')
  const selected = await new Promise((resolve, reject) => {
    try {
      Promise.resolve(picker({ signal, title: 'Choose a reference image', onPick: resolve, onCancel: () => resolve(null) })).catch(reject)
    } catch (error) { reject(error) }
  })
  assertCurrent(accountId, current, signal)
  if (selected === null) return null
  const revision = selected?.revision
  if (selected?.provider !== 'files' || !valid(selected?.id, 512) || !valid(selected?.ref, 16384) ||
      !revision || Object.keys(revision).sort().join(',') !== 'kind,value' ||
      revision.kind !== 'filesImageRevision' || !valid(revision.value, 2048))
    throw new Error('This reference is unavailable or unsupported.')
  const selectedId = selected.id, selectedRef = selected.ref
  const observedRevision = { kind: revision.kind, value: revision.value }
  const opened = await client.openResource(selectedRef, { expectedRevision: observedRevision, signal })
  assertCurrent(accountId, current, signal)
  if (opened?.target?.app !== 'imps' || opened?.resource?.provider !== 'files' ||
      opened.resource.id !== selectedId || !sameRevision(opened.resource.revision, observedRevision) ||
      opened?.payload?.provider !== 'files' || typeof opened.payload.resource_id !== 'string' ||
      !/^image:[A-Za-z0-9._-]{1,240}$/.test(opened.payload.resource_id))
    throw new Error('This reference is unavailable or changed.')
  const fresh = await client.stat(selectedRef, { signal })
  assertCurrent(accountId, current, signal)
  if (fresh?.provider !== 'files' || fresh.id !== selectedId || !sameRevision(fresh.revision, observedRevision))
    throw new Error('This reference is unavailable or changed.')
  return { resourceKey: { provider: 'files', account_id: accountId, workspace_id: null, resource_id: opened.payload.resource_id },
    revision: { kind: 'files-revision', value: observedRevision.value } }
}

/** Check a previously described reference only against a fresh explicit selection. */
export async function checkArtProjectReference(reference, options) {
  const key = reference?.resourceKey, revision = reference?.revision
  if (!key || Object.keys(key).sort().join(',') !== 'account_id,provider,resource_id,workspace_id' ||
      key.provider !== 'files' || key.account_id !== options?.accountId || key.workspace_id !== null ||
      typeof key.resource_id !== 'string' || !/^image:[A-Za-z0-9._-]{1,240}$/.test(key.resource_id) ||
      !revision || Object.keys(revision).sort().join(',') !== 'kind,value' || revision.kind !== 'files-revision' ||
      !valid(revision.value, 2048)) throw new Error('This reference is unavailable or unsupported.')
  const selected = await selectArtProjectReference(options)
  assertCurrent(options.accountId, options.current, options.signal)
  return !!selected && selected.resourceKey.provider === key.provider && selected.resourceKey.account_id === key.account_id &&
    selected.resourceKey.workspace_id === key.workspace_id && selected.resourceKey.resource_id === key.resource_id &&
    sameRevision(selected.revision, revision)
}
