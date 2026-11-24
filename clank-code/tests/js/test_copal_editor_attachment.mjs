import assert from 'node:assert/strict';
import test from 'node:test';

import { serializeAttachmentInsertion, validateAttachmentPreparationRecovery } from '../../static/js/copal/notesFeature.js';

test('Editor attachment serializer consumes only the strict S01 markdown descriptor', () => {
  assert.equal(
    serializeAttachmentInsertion({ format:'markdown', link_target:'copal://resource/asset-1', label:'A [photo]', media_kind:'image' }),
    '[A \\[photo\\]](<copal://resource/asset-1>)',
  );
  assert.equal(
    serializeAttachmentInsertion({ format:'markdown', link_target:'copal://resource/asset-1', label:'Photo', media_kind:'image' }, { mode:'embed' }),
    '![Photo](<copal://resource/asset-1>)',
  );
  assert.throws(() => serializeAttachmentInsertion({ insertion:{ text:'legacy' } }), /format/);
  assert.throws(() => serializeAttachmentInsertion({ format:'markdown', link_target:'javascript:alert(1)', label:'bad', media_kind:'image' }), /unsafe/);
});

const preparation = {
  operation_id:'editor-attachment-gesture-1', generation:7,
  preparation_receipt_id:'prep-1',
  source_revision:{ kind:'hostFingerprint', value:'source-1' },
  source_identity:{ resource_key:'acct|work|host:source-1', resource_ref:'sealed-source-1', item_id:'item-1' },
  target_identity:{ resource_ref:'sealed-target-1', kind:'copal_document' },
  target_revision:{ kind:'copalHead', value:'head-1' },
  insertion:{ format:'markdown', link_target:'asset-1', label:'source.txt', media_kind:'text/plain' },
  action_receipt:{ action_id:'attachment-editor-attachment-gesture-1', status:'complete', phase:'complete' },
};
const recoveryArgs = {
  operationId:'editor-attachment-gesture-1', generation:7,
  sourceKey:'acct|work|host:source-1', sourceRef:'sealed-source-1', sourceItemId:'item-1', sourceRevision:preparation.source_revision,
  targetKey:'acct|work|copal:doc-1', targetRef:'sealed-target-1', targetRevision:preparation.target_revision, mode:'link',
};

test('Editor recovery unwraps only complete typed preparation bound to the gesture', () => {
  const recovered = validateAttachmentPreparationRecovery({ operation_id:recoveryArgs.operationId, generation:7, state:'complete', preparation }, recoveryArgs);
  assert.equal(recovered.preparation_receipt_id, 'prep-1');
  for (const [label, value] of [
    ['wrong operation', { operation_id:'other', generation:7, state:'complete', preparation }],
    ['wrong generation', { operation_id:recoveryArgs.operationId, generation:8, state:'complete', preparation }],
    ['pending', { operation_id:recoveryArgs.operationId, generation:7, state:'pending', preparation:null }],
    ['redacted', { operation_id:recoveryArgs.operationId, generation:7, state:'complete', preparation:null }],
  ]) assert.throws(() => validateAttachmentPreparationRecovery(value, recoveryArgs), /recovery|operation/ , label);
  assert.throws(() => validateAttachmentPreparationRecovery({ operation_id:recoveryArgs.operationId, generation:7, state:'complete', preparation:{ ...preparation, source_identity:{ ...preparation.source_identity, item_id:'other-item' } } }, recoveryArgs), /source item/);
  assert.throws(() => validateAttachmentPreparationRecovery({ operation_id:recoveryArgs.operationId, generation:7, state:'complete', preparation:{ ...preparation, source_identity:{ ...preparation.source_identity, resource_key:'other-key' } } }, recoveryArgs), /source identity/);
  assert.throws(() => validateAttachmentPreparationRecovery({ operation_id:recoveryArgs.operationId, generation:7, state:'complete', preparation:{ ...preparation, source_identity:{ ...preparation.source_identity, resource_ref:'other-ref' } } }, recoveryArgs), /source/);
  assert.throws(() => validateAttachmentPreparationRecovery({ operation_id:recoveryArgs.operationId, generation:7, state:'complete', preparation:{ ...preparation, target_identity:{ resource_ref:'other-target', kind:'copal_document' } } }, recoveryArgs), /target/);
  assert.throws(() => validateAttachmentPreparationRecovery({ operation_id:recoveryArgs.operationId, generation:7, state:'complete', preparation:{ ...preparation, target_revision:{ kind:'copalHead', value:'other-head' } } }, recoveryArgs), /target revision/);
});

test('Editor recovery rejects hostile typed receipts before insertion', () => {
  const bound = { ...recoveryArgs, accountId:'account-owner', workspace:'default', policyGeneration:7 };
  const envelope = patch => ({
    operation_id:bound.operationId, generation:7, state:'complete',
    ...patch,
    preparation:Object.hasOwn(patch || {}, 'preparation') ? patch.preparation : { ...preparation },
  });
  for (const [label, value, pattern] of [
    ['stale state', envelope({ state:'stale', preparation:null }), /recovery|operation/],
    ['denied state', envelope({ state:'failed', preparation:null }), /recovery|operation/],
    ['malformed descriptor', envelope({ preparation:{ ...preparation, insertion:{ format:'legacy', link_target:'asset-1', label:'source.txt', media_kind:'text/plain' } } }), /format|insertion/],
    ['wrong mode', envelope({ preparation:{ ...preparation, mode:'embed' } }), /mode/],
    ['wrong source item', envelope({ preparation:{ ...preparation, source_identity:{ resource_ref:'sealed-source-1', resource_key:'acct|work|host:source-1', item_id:'other-item' } } }), /source item/],
    ['wrong descriptor target', envelope({ preparation:{ ...preparation, target_identity:{ resource_ref:'sealed-target-2', kind:'copal_document' } } }), /target/],
    ['wrong policy generation', envelope({ policy_generation:8 }), /generation/],
    ['wrong account', envelope({ account_id:'other-account' }), /account/],
    ['wrong workspace', envelope({ workspace_id:'other-workspace' }), /workspace/],
  ]) assert.throws(() => validateAttachmentPreparationRecovery(value, { ...bound, mode:'link' }), pattern, label);
  assert.throws(() => validateAttachmentPreparationRecovery({ operation_id:bound.operationId, generation:7, state:'complete', preparation:{ ...preparation, insertion:{ format:'markdown', link_target:'asset-1', label:'source.txt', media_kind:'text/plain', text:'legacy' } } }, bound), /insertion/);
});
