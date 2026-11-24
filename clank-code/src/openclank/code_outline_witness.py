"""Closed selected-file outline evidence checks; no source authority."""
import re

_HEX = re.compile(r"[0-9a-f]{64}\Z")

def verify_outline_completeness(result, *, receipt, file_id, rel_path, source_hash):
    if not isinstance(result, dict) or result.get("operation") != "outline" or result.get("receipt") != receipt or result.get("error") is not None or result.get("degraded") is not False or result.get("next_cursor") is not None:
        return False
    witness=result.get("outline_completeness")
    if not isinstance(witness,dict) or set(witness)!={"version","file_id","rel_path","source_hash","declaration_count","scope"}:
        return False
    count=witness["declaration_count"]
    if (type(witness["version"]) is not int or witness["version"]!=1 or witness["scope"]!="supported_indexed_file_declarations" or witness["file_id"]!=file_id or witness["rel_path"]!=rel_path or witness["source_hash"]!=source_hash or not isinstance(file_id,str) or not file_id.startswith("file:") or not _HEX.fullmatch(file_id[5:]) or not isinstance(source_hash,str) or not _HEX.fullmatch(source_hash) or type(count) is not int or not 0<=count<=200):
        return False
    hits=result.get("hits")
    if not isinstance(hits,list) or len(hits)!=count: return False
    ids=set()
    for hit in hits:
        if not isinstance(hit,dict) or hit.get("kind")!="declaration": return False
        ident=hit.get("id"); source=hit.get("source"); props=hit.get("properties")
        if not isinstance(ident,str) or not ident.startswith("decl:") or not _HEX.fullmatch(ident[5:]) or ident in ids: return False
        if not isinstance(source,dict) or source.get("file_id")!=file_id[5:] or source.get("rel_path")!=rel_path or source.get("source_hash")!=source_hash or not isinstance(props,dict) or props.get("identity_state")=="ambiguous_snapshot_local" or hit.get("evidence_state") in ("ambiguous","stale","unsupported"): return False
        ids.add(ident)
    return True
