"""One explicit kernel PNG through existing account custody and Files import."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io

from src.openclank.artifacts import account_artifact_owner
from src.openclank.files_facade import FilesFacadeError

MAX_ARTIFACT_BYTES = 1024 * 1024


def _retain_png(store, account, encoded):
    if not isinstance(encoded, str) or len(encoded) > ((MAX_ARTIFACT_BYTES + 2) // 3) * 4:
        raise ValueError('kernel artifact unavailable')
    data = base64.b64decode(encoded, validate=True)
    if not 0 < len(data) <= MAX_ARTIFACT_BYTES or base64.b64encode(data).decode('ascii') != encoded:
        raise ValueError('kernel artifact unavailable')
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'PNG' or not 1 <= image.width <= 4096 or not 1 <= image.height <= 4096 or image.width * image.height > 16_777_216:
            raise ValueError('kernel PNG unsupported')
        image.verify()
    digest = hashlib.sha256(data).hexdigest()
    owner = account_artifact_owner(account)
    row = store.put(owner=owner, chunks=[data], media_type='image/png')
    retained = b''.join(store.read_chunks(owner=owner, artifact_id=row.id))
    if row.content_sha256 != digest or row.size_bytes != len(data) or retained != data:
        raise ValueError('kernel artifact custody changed')
    return retained, digest


async def publish_kernel_png(*, encoded, store, account, facade, context, destination, name, operation_id, current):
    """Never retry an uncertain import; its existing Files ledger is the authority."""
    dispatched = False
    try:
        if not await current(): return {'artifact_status':'unavailable', 'artifacts':[]}
        data, digest = await asyncio.to_thread(_retain_png, store, account, encoded)
        if not await current(): return {'artifact_status':'unavailable', 'artifacts':[]}
        class Upload:
            filename = name
            content_type = 'image/png'
            def __init__(self): self.offset = 0
            async def read(self, size):
                chunk = data[self.offset:self.offset + size]; self.offset += len(chunk); return chunk
        dispatched = True
        receipt = await facade.import_file(context, upload=Upload(), before_commit=current,
            expected_destination_revision=destination['destination_revision'],
            source_evidence={'kernel_operation_id':operation_id, 'artifact_sha256':digest},
            metadata={'operation_id':operation_id, 'item_id':'output', 'generation':context.policy_generation,
                      'destination_ref':destination['destination_ref'], 'name':name, 'collision':'fail'})
        items = receipt.get('items') if isinstance(receipt, dict) else None
        if (not isinstance(receipt, dict) or receipt.get('state') != 'complete' or not isinstance(items,list) or len(items) != 1
            or items[0].get('item_id') != 'output' or items[0].get('outcome') != 'committed' or not isinstance(items[0].get('resource_ref'),str)):
            return {'artifact_status':'unknown', 'artifacts':[]}
        if not await current(): return {'artifact_status':'unknown', 'artifacts':[]}
        return {'artifact_status':'committed', 'artifacts':[{'resource_ref':items[0]['resource_ref'],
                'sha256':digest, 'mime_type':'image/png', 'size':len(data)}]}
    except FilesFacadeError as error:
        return {'artifact_status':'unknown' if error.code == 'mutation_outcome_unknown' else 'unavailable', 'artifacts':[]}
    except Exception:
        # Import exceptions may follow dispatch; never reissue automatically.
        return {'artifact_status':'unknown' if dispatched else 'unavailable', 'artifacts':[]}
