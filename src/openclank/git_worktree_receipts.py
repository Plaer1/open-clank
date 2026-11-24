"""Narrow protected update of the original worktree custody receipt."""
import json
from src.secret_storage import decrypt,encrypt

def record_cleanup(repository,utility,generation,operation,outcome):
    if outcome not in ('released','unknown'):return False
    with repository._connect() as db:
        repository._begin(db)
        row=db.execute('SELECT request_digest,generation,receipt_ciphertext,lifecycle_phase FROM file_operations WHERE owner_subject_id=? AND operation_id=?',(utility.account,utility.idempotency_key)).fetchone()
        if (row is None or row['request_digest']!=utility.request_hash or row['generation']!=generation
                or row['lifecycle_phase']!=operation+'_settled'):db.rollback();return False
        old=row['receipt_ciphertext']
        try:receipt=json.loads(decrypt(str(old)))
        except Exception:db.rollback();return False
        if type(receipt) is not dict or receipt.get('operation')!=operation or receipt.get('state')!='applied' or receipt.get('settled') is not True:db.rollback();return False
        if receipt.get('cleanup') not in (None,'pending'):
            same=receipt['cleanup']==outcome;db.rollback();return same
        receipt['cleanup']=outcome;encoded=encrypt(json.dumps(receipt,sort_keys=True,separators=(',',':'),ensure_ascii=False))
        changed=db.execute('UPDATE file_operations SET receipt_ciphertext=? WHERE owner_subject_id=? AND operation_id=? AND request_digest=? AND generation=? AND lifecycle_phase=? AND receipt_ciphertext=?',
            (encoded,utility.account,utility.idempotency_key,utility.request_hash,generation,operation+'_settled',old))
        if changed.rowcount!=1:db.rollback();return False
        db.commit();return True
