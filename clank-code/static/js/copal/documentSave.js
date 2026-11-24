import { cloneEnvelope, normalizeRevision, sameResourceKey } from './resourceModel.js';

function identityError(message) {
  const error = new Error(message);
  error.code = 'receipt_identity';
  error.retryable = false;
  return error;
}

export function createSaveActionId(prefix = 'save') {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${String(prefix).replace(/[^a-z0-9_-]+/gi, '-').slice(0, 32)}-${random}`;
}

/** Freeze the request before yielding, including native fields and its base. */
export function prepareDocumentSave(doc, content, { snapshot, relationsFor = () => [], scope = null, actionId = null } = {}) {
  if (snapshot?.key && doc.resource?.key && !sameResourceKey(snapshot.key, doc.resource.key)) {
    throw identityError('The draft belongs to another resource');
  }
  const revision = normalizeRevision(snapshot?.expectedRevision || { kind:'copalHead', value:doc.head });
  if (revision.kind !== 'copalHead') throw new Error('A Copal head is required to save this document');
  const envelope = cloneEnvelope(snapshot?.envelope || { text:content, properties:doc.properties, relations:doc.relations });
  const immutableActionId = String(snapshot?.actionId || actionId || createSaveActionId(`document-${doc.id}`));
  const payload = { actionId:immutableActionId, content:String(envelope.text ?? content), base:revision.value };
  if (doc.kind === 'note' || doc.kind === 'wiki') {
    payload.properties = envelope.properties || {};
    payload.relations = [
      ...(envelope.relations || []).filter((relation) => relation.origin === 'explicit'),
      ...relationsFor(payload.content),
    ];
  }
  return { id:doc.id, payload, envelope, revision, localRevision:snapshot?.localRevision, actionId:immutableActionId, resourceKey:doc.resource?.key || null, scope:snapshot?.scope || scope || null };
}

/** Only the write response acknowledges a save; refreshing a view cannot undo it. */
export async function commitDocumentSave(prepared, write) {
  const result = await write(prepared.id, prepared.payload);
  if (result?.outcome === 'stale' || result?.outcome === 'conflict') {
    return { outcome:'conflict', remote:result.doc, result };
  }
  if (!result?.doc?.head || !['committed', 'unchanged'].includes(result.outcome)) {
    throw new Error('The save response did not confirm a document revision');
  }
  const returnedActionId = result.actionId || result.action_id;
  if (!returnedActionId || String(returnedActionId) !== String(prepared.actionId)) throw identityError('The save response action identity does not match the submitted operation');
  const returnedDocumentId = result.doc.id ?? result.doc.documentId;
  if (!returnedDocumentId || String(returnedDocumentId) !== String(prepared.id)) throw identityError('The save response document identity does not match the submitted document');
  if (result.doc.resource?.key && prepared.resourceKey && !sameResourceKey(result.doc.resource.key, prepared.resourceKey)) throw identityError('The save response resource identity does not match the submitted resource');
  return {
    outcome:'applied', revision:normalizeRevision({ kind:'copalHead', value:result.doc.head }),
    acknowledgedLocalRevision:prepared.localRevision, doc:result.doc, result,
    actionId:returnedActionId,
    projections:result.projections || null,
  };
}

export function sameSaveScope(left, right) {
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  return ['workspace', 'storageNamespace', 'accountId', 'epoch'].every((key) => left[key] === right[key]);
}
