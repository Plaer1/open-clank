"""Verify retained staging moves against existing account-operation receipts.

This is factual evidence only; a caller still holds original current account,
privacy and file guards and validates the exact staged file before access.
"""
import re
import time
from src.openclank.account_lifecycle import AccountOwnerLifecycle
from src.openclank.owner_custody_facts import _verified_owner_custody


def _staging_receipt(value):
    return (type(value) is dict and set(value)=={'count','bytes','fingerprint','complete','already_applied'}
        and type(value['count']) is int and value['count']>=0
        and type(value['bytes']) is int and value['bytes']>=0
        and type(value['fingerprint']) is str and re.fullmatch(r'sha256:[0-9a-f]{64}',value['fingerprint']) is not None
        and value['complete'] is True and type(value['already_applied']) is bool)


def _frozen_move(operation, name):
    value=operation['manifest'].get(name)
    if type(value) is not dict or set(value)!={'source','target'}:
        raise ValueError('staging move manifest unavailable')
    for side in ('source','target'):
        entry=value[side]
        if (type(entry) is not dict or set(entry)!={'count','bytes','fingerprint'}
            or type(entry['count']) is not int or entry['count']<0
            or type(entry['bytes']) is not int or entry['bytes']<0
            or type(entry['fingerprint']) is not str or re.fullmatch(r'sha256:[0-9a-f]{64}',entry['fingerprint']) is None):
            raise ValueError('staging move manifest unavailable')
    # A nonempty pre-existing target cannot prove which account supplied bytes.
    if value['target']['count'] != 0 or value['target']['bytes'] != 0:
        raise ValueError('staging move target unavailable')
    return value['source']


def verified_staging_relocations(db, *, database_id, account_id, current_owner, lifecycle, current,
                                 delete_capability=None, delete_phase=None):
    """Check each committed custody move against its same-account Host step.

    Must be called once per guarded operation, inside the original SQLite read
    transaction; an account barrier/current guard must exclude owner changes.
    Empty output says only that no custody moves were recorded.
    """
    if not callable(current) or current() is not True:
        raise ValueError('staging account authority unavailable')
    if delete_phase is not None:
        from src.openclank.chat_source_bridge import HostAccountSourceTransition
        if (delete_phase not in ('before_move','before_purge') or type(delete_capability) is not HostAccountSourceTransition
            or delete_capability.lifecycle is not lifecycle):
            raise ValueError('staging delete authority unavailable')
    elif delete_capability is not None:
        raise ValueError('staging delete authority unavailable')
    deadline=time.monotonic()+5.0
    owners,_has_head,descriptors,_members=_verified_owner_custody(db,database_id=database_id,account_id=account_id,current_owner=current_owner)
    if descriptors and type(lifecycle) is not AccountOwnerLifecycle:
        raise ValueError('staging lifecycle unavailable')
    result=[]; reversed_staging={}
    for link in descriptors:
        if time.monotonic()>=deadline or current() is not True: raise ValueError('staging relocation unavailable')
        operation=lifecycle.get_operation(link['operation_id'])
        if (type(operation) is not dict or operation.get('subject_id')!=account_id
            or type(operation.get('steps')) is not dict or type(operation.get('manifest')) is not dict
            or ((operation['manifest'].get('memory_participants') or {}).get('fm') or {}).get('database_id')!=database_id):
            raise ValueError('staging relocation unavailable')
        steps=operation['steps'];action=link['action'];kind=operation.get('kind');pending_stage=False;frozen_source=None
        if kind=='rename' and action=='forward' and link['step']=='memory_fm':
            if (operation.get('state')!='complete' or operation.get('source_owner')!=link['owner']
                or operation.get('target_owner')!=link['new_owner']): raise ValueError('staging relocation unavailable')
            frozen_source=_frozen_move(operation,'import_staging')
            native=steps.get('memory_fm'); stage=steps.get('import_staging')
            receipt=stage.get('receipt') if type(stage) is dict and stage.get('state')=='applied' else None
        elif kind=='delete' and action=='reverse' and link['step']=='memory_fm_tombstoned':
            if operation.get('tombstone_owner')!=link['owner'] or operation.get('source_owner')!=link['new_owner']:
                raise ValueError('staging relocation unavailable')
            frozen_source=_frozen_move(operation,'import_staging_tombstoned')
            native=steps.get('memory_fm_reverse_'+str(link['generation']))
            completed=steps.get('memory_compensation_complete_'+str(link['generation']))
            proof=completed.get('receipt') if type(completed) is dict and completed.get('state')=='applied' else None
            receipt=((proof.get('results') or {}).get('import_staging_tombstoned')
                     if type(proof) is dict and proof.get('state')=='complete' else None)
        elif (kind=='delete' and action=='forward' and link['step']=='memory_fm_tombstoned'
              and delete_phase is not None and delete_capability.operation_id==link['operation_id']
              and link==descriptors[0] and type(operation['manifest'].get('memory_fm_tombstoned')) is dict
              and type(operation['manifest']['memory_fm_tombstoned'].get('generation')) is int
              and operation['manifest']['memory_fm_tombstoned']['generation']==link['generation']):
            active=operation['manifest'].get('memory_fm_tombstoned')
            if (operation.get('source_owner')!=link['owner'] or operation.get('tombstone_owner')!=link['new_owner']
                or link != descriptors[0] or type(active) is not dict
                or type(active.get('generation')) is not int or active['generation']!=link['generation']):
                raise ValueError('staging relocation unavailable')
            if delete_phase=='before_move':
                delete_capability.assert_current(provider=delete_capability.provider,action='rename',owner=link['owner'],new_owner=link['new_owner'])
            else:
                delete_capability.assert_current(provider=delete_capability.provider,action='purge',owner=link['new_owner'])
            native=steps.get('memory_fm_forward_'+str(link['generation']))
            stage=steps.get('import_staging_tombstoned')
            pending_stage=(delete_phase=='before_move' and type(stage) is dict and stage.get('state')=='applying')
            frozen_source=_frozen_move(operation,'import_staging_tombstoned')
            receipt=stage.get('receipt') if type(stage) is dict and stage.get('state')=='applied' else None
        elif kind=='delete' and action=='forward' and link['step']=='memory_fm_tombstoned':
            # A completed reverse already established the actual current root.
            # Its own typed receipt and saved staging result were checked first.
            if (operation.get('source_owner')!=link['owner'] or operation.get('tombstone_owner')!=link['new_owner']
                or (link['operation_id'],link['generation']) not in reversed_staging):
                raise ValueError('staging relocation unavailable')
            frozen_source=_frozen_move(operation,'import_staging_tombstoned')
            native=steps.get('memory_fm_forward_'+str(link['generation']))
            receipt=reversed_staging[(link['operation_id'],link['generation'])]
        else:
            # Incomplete/tombstone/legacy phase is never a normal staged read.
            raise ValueError('staging relocation unavailable')
        native_receipt=native.get('receipt') if type(native) is dict and native.get('state')=='applied' else None
        if (type(native_receipt) is not dict or native_receipt.get('state')!='applied'
            or native_receipt.get('receipt_id')!=link['receipt_id'] or native_receipt.get('receipt_hash')!=link['receipt_sha256']
            or (not pending_stage and (not _staging_receipt(receipt) or
                any(receipt[k]!=frozen_source[k] for k in ('count','bytes','fingerprint'))))
            or current() is not True):
            raise ValueError('staging relocation unavailable')
        if kind=='delete' and action=='reverse':
            reversed_staging[(link['operation_id'],link['generation'])]=receipt
        result.append((link['operation_id'],link['generation'],action,link['owner'],link['new_owner']))
    if time.monotonic()>=deadline or current() is not True: raise ValueError('staging relocation unavailable')
    return owners,tuple(result)
