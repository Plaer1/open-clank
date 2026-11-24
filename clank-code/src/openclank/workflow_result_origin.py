"""Receipt-level identity for an already admitted workflow writer.

A selector and this digest grant no access. The original FilePolicy receipt,
current plan association and current Human Files authorization remain owners.
"""
from __future__ import annotations
import hashlib
import json
import re

_FIELDS = frozenset({'version','account','chat','sessionID','rootOperationID','actorID',
                     'executionRevision','innerTurnID','toolCallID','operationID',
                     'artifactSetID','planRevision','planDigest','role','name'})

def origin_binding(request_digest, origin):
    if not isinstance(origin,dict) or set(origin) != _FIELDS or type(origin.get('version')) is not int or origin['version'] != 1:
        raise ValueError('workflow result origin unavailable')
    for field in ('account','chat','sessionID','rootOperationID','actorID','innerTurnID','toolCallID'):
        value=origin[field]
        if not isinstance(value,str) or not value or len(value.encode('utf-8','strict'))>256 or any(ord(ch)<32 for ch in value):
            raise ValueError('workflow result origin unavailable')
    if (type(origin['executionRevision']) is not int or not 0<=origin['executionRevision']<=9007199254740991
        or type(origin['planRevision']) is not int or not 1<=origin['planRevision']<=9007199254740991
        or not isinstance(origin['planDigest'],str) or not re.fullmatch(r'[0-9a-f]{64}',origin['planDigest'])
        or not isinstance(origin['artifactSetID'],str) or not re.fullmatch(r'artifact-set-[0-9a-f]{64}',origin['artifactSetID'])
        or not isinstance(origin['operationID'],str) or not re.fullmatch(r'workflowop_[0-9a-f]{64}',origin['operationID'])
        or origin['role'] not in ('metaplan','ledger','concern','checkpoint','progress','slice')
        or (origin['name'] is not None and (not isinstance(origin['name'],str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',origin['name']) or len(origin['name'])>80))
        or not isinstance(request_digest,str) or not re.fullmatch(r'[0-9a-f]{64}',request_digest)):
        raise ValueError('workflow result origin unavailable')
    operation_material=['openclank.workflow.operation.v1',origin['account'],origin['sessionID'],origin['rootOperationID'],origin['actorID'],origin['executionRevision'],origin['innerTurnID'],origin['toolCallID']]
    operation='workflowop_'+hashlib.sha256(json.dumps(operation_material,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
    if operation != origin['operationID']: raise ValueError('workflow result origin unavailable')
    data=json.dumps(['openclank.workflow-result-origin.v1',request_digest,origin],sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    return {'version':1,'origin':json.loads(json.dumps(origin,ensure_ascii=False)), 'digest':hashlib.sha256(data).hexdigest()}
