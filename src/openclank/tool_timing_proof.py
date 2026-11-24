"""Validate private tool evidence; failure never prevents ordinary history."""
import base64
import hashlib
import json
import struct
from dataclasses import dataclass, field

_SEAL = object()

@dataclass(frozen=True)
class QualifiedToolTiming:
    account: str
    chat: str
    holder: str
    evidence_json: str
    _seal: object = field(repr=False)

    def projection(self):
        if self._seal is not _SEAL: raise ValueError("invalid timing seal")
        return {"version": 1, "account": self.account, "chat": self.chat, "holder": self.holder, "evidence": json.loads(self.evidence_json)}

def seal_tool_timing(account, chat, holder, evidence):
    return QualifiedToolTiming(account, chat, holder, json.dumps(evidence,sort_keys=True,separators=(",",":")), _SEAL)

_FIELDS = ('version','algorithm','clockID','sessionID','rootOperationID','actorID','executionRevision','innerTurnID','messageID','partID','callID','phase','startUS','endUS','wallStartMS','wallEndMS','contentSHA256')

@dataclass(frozen=True)
class QualifiedToolActivity:
    account: str
    chat: str
    holder: str
    evidence_json: str
    _seal: object = field(repr=False)
    def projection(self):
        if self._seal is not _SEAL: raise ValueError("invalid activity seal")
        return {"version": 2, "account": self.account, "chat": self.chat, "holder": self.holder, "evidence": json.loads(self.evidence_json)}

def seal_tool_activity(account, chat, holder, evidence):
    return QualifiedToolActivity(account, chat, holder, json.dumps(evidence,sort_keys=True,separators=(",",":")), _SEAL)

_ACTIVITY_FIELDS = ('version','algorithm','clockID','sessionID','rootOperationID','actorID','executionRevision','innerTurnID','messageID','partID','callID','phase','sequence','observedUS','wallObservedMS','contentSHA256')

def verify_tool_activity(client, item, session_id):
    try:
        import re
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        proof=item.get('toolTiming'); record=client.tool_timing_verifier()
        if not record or type(proof) is not dict or set(proof)!=set(_ACTIVITY_FIELDS)|{'signature'}: return None
        if type(proof['version']) is not int or proof['version']!=2 or proof['algorithm']!='ed25519' or proof['phase'] not in ('started','progress') or proof['clockID']!=record['clockID'] or not re.fullmatch(r'[0-9a-f]{32}',proof['clockID']): return None
        if any(type(proof[k]) is not int or not 0<=proof[k]<=9007199254740991 for k in ('executionRevision','observedUS','wallObservedMS')) or type(proof['sequence']) is not int or not 1<=proof['sequence']<=9007199254740991: return None
        if any(type(proof[k]) is not str or not proof[k] or len(proof[k])>(128 if k=='actorID' else 256) for k in ('sessionID','rootOperationID','actorID','innerTurnID','messageID','partID','callID')): return None
        if type(proof['contentSHA256']) is not str or not re.fullmatch(r'[0-9a-f]{64}',proof['contentSHA256']) or type(proof['signature']) is not str or len(proof['signature'])!=88:return None
        content=item.get('content')
        if type(content) is not str or hashlib.sha256(content.encode('utf-8')).hexdigest()!=proof['contentSHA256']: return None
        part=json.loads(content)
        if (type(part) is not dict or item.get('partType')!='tool' or part.get('type')!='tool' or proof['sessionID']!=session_id
                or proof['messageID']!=item.get('messageID') or proof['partID']!=item.get('partID') or proof['actorID']!=item.get('actorID')
                or part.get('sessionID')!=session_id or part.get('messageID')!=proof['messageID'] or part.get('id')!=proof['partID']
                or part.get('callID')!=proof['callID'] or part.get('execution')!='started'
                or type(part.get('state')) is not dict or part['state'].get('status')!='running'):return None
        if (proof['phase']=='started') != (proof['sequence']==1):return None
        values=['openclank.tool-activity.v2']+[str(proof[k]) for k in _ACTIVITY_FIELDS]
        payload=b''.join(struct.pack('>I',len(v.encode('utf-8')))+v.encode('utf-8') for v in values)
        signature=base64.b64decode(proof['signature'],validate=True)
        load_der_public_key(base64.b64decode(record['publicKey'],validate=True)).verify(signature,payload)
        return {k:proof[k] for k in _ACTIVITY_FIELDS}
    except Exception:return None


def verify_tool_timing(client, item, session_id):
    try:
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        proof=item.get('toolTiming'); record=client.tool_timing_verifier()
        if not record or type(proof) is not dict or set(proof)!=set(_FIELDS)|{'signature'}: return None
        if proof['version'] != 1 or proof['algorithm'] != 'ed25519' or proof['phase'] != 'terminal' or proof['clockID'] != record['clockID']: return None
        if any(type(proof[k]) is not int or proof[k]<0 or proof[k]>9007199254740991 for k in ('executionRevision','startUS','endUS','wallStartMS','wallEndMS')) or proof['endUS']<proof['startUS']: return None
        if any(type(proof[k]) is not str or not proof[k] or len(proof[k])>256 for k in ('sessionID','rootOperationID','actorID','innerTurnID','messageID','partID','callID')): return None
        content=item.get('content')
        if type(content) is not str or hashlib.sha256(content.encode('utf-8')).hexdigest()!=proof['contentSHA256']: return None
        part=json.loads(content)
        if (not isinstance(part,dict) or item.get('partType')!='tool' or part.get('type')!='tool' or proof['sessionID']!=session_id or proof['messageID']!=item.get('messageID') or proof['partID']!=item.get('partID') or proof['actorID']!=item.get('actorID') or part.get('sessionID')!=session_id or part.get('messageID')!=proof['messageID'] or part.get('id')!=proof['partID'] or part.get('callID')!=proof['callID']): return None
        state=part.get('state'); times=state.get('time') if isinstance(state,dict) else None
        if not isinstance(times,dict) or state.get('status') not in ('completed','error') or times.get('start')!=proof['wallStartMS'] or times.get('end')!=proof['wallEndMS']: return None
        values=['openclank.tool-interval.v1']+[str(proof[k]) for k in _FIELDS]
        payload=b''.join(struct.pack('>I',len(v.encode('utf-8')))+v.encode('utf-8') for v in values)
        signature=base64.b64decode(proof['signature'],validate=True)
        load_der_public_key(base64.b64decode(record['publicKey'],validate=True)).verify(signature,payload)
        return {k:proof[k] for k in _FIELDS}
    except Exception:
        return None
