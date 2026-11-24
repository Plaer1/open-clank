"""Inactive S12a policy contract over the existing preferences authority.

Consumers must opt in after owner acceptance and validate capabilities again at
use. This module does not change capture, scheduling, recall, or forgetting.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.memory_gate import capture_allowed, capture_mode, memory_mode, write_allowed
from src.owner_identity import LOCAL_INSTALLATION_OWNER

KEY = "memory_policy"
SCHEMA = "openclank.memory-policy.v1"
SCHEMA_V2 = "openclank.memory-policy.v2"
SCHEMA_V3 = "openclank.memory-policy.v3"
WORK_KINDS = ("interpretation", "synthesis", "skill_processing")
AXES = ("importance", "connections", "merge")
EFFECTS = ("found", "admitted", "helped")


class MemoryPolicyError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def explicit_human_review_allowed(snapshot: Any) -> bool:
    """Evaluate a fresh effective snapshot; identity and review authority are separate."""
    if type(snapshot) is not dict or snapshot.get("source") != "legacy_compatibility":
        return False
    choices, privileges, legacy = (snapshot.get("choices"), snapshot.get("privileges"), snapshot.get("legacy_inputs"))
    if (type(choices) is not dict or set(choices) != {"incognito", "no_memory", "compare_mode"}
        or any(choices[name] is not False for name in choices)
        or type(privileges) is not dict or set(privileges) != {"read", "write"}
        or privileges["read"] is not True or privileges["write"] is not True
        or type(legacy) is not dict or set(legacy) != {"memory_mode", "auto_memory", "memory_enabled", "graph"}
        or legacy["memory_enabled"] is not True
        or (legacy["auto_memory"] is not None and type(legacy["auto_memory"]) is not bool)
        or snapshot.get("eligibility_state") != "available" or snapshot.get("eligibility_reason") is not None
        or snapshot.get("unsupported_inputs") != [] or snapshot.get("read_allowed") is not True
        or type(snapshot.get("policy_state")) is not str
        or snapshot["policy_state"] not in {"accepted", "legacy_compatibility"}):
        return False
    raw_mode = legacy["memory_mode"]
    if raw_mode is None:
        mode = memory_mode({})
    elif type(raw_mode) is str and raw_mode.strip().lower() in {"off", "manual", "automatic"}:
        mode = memory_mode({"memory_mode": raw_mode})
    else:
        return False
    return mode in {"manual", "automatic"}


def _inactive_capabilities() -> dict[str, Any]:
    # Ratings use the optional host schema, independently of memory consumers.
    rating_ready = False
    try:
        from core.database import SessionLocal
        from src.openclank.chat_ratings import schema_state
        with SessionLocal() as db:
            rating_ready = schema_state(db) == "ready"
    except Exception:
        pass
    return {"version":"s12a.capabilities.v1", "axes":{axis:[] for axis in AXES},
            "consumers":{"scheduler":False, "automatic_context_recall":False, "feedback":False, "ratings":rating_ready}}


# Only the real app lifespan may attach its exact production reader.  The
# public function identity stays stable for existing source-currentness proofs.
_runtime_capability_context = None

def _install_runtime_capability_reader(state, reader):
    import sys
    global _runtime_capability_context
    from pathlib import Path
    from types import FunctionType
    name = getattr(reader, '__module__', None)
    module = sys.modules.get(name) if name in ('app', '__main__') else None
    expected = Path(__file__).resolve().parents[2] / 'app.py'
    if (type(reader) is not FunctionType or module is None
            or not getattr(module, '__file__', None) or Path(module.__file__).resolve() != expected
            or reader.__globals__ is not module.__dict__ or Path(reader.__code__.co_filename).resolve() != expected
            or getattr(getattr(module, 'app', None), 'state', None) is not state
            or getattr(module, '_live_memory_policy_capabilities', None) is not reader
            or STORE.capability_provider is not capabilities or _runtime_capability_context is not None
            or getattr(state, 's05_profile', None) is None):
        raise ValueError('runtime capability owner unavailable')
    token = (state, reader, state.s05_profile, module, name)
    _runtime_capability_context = token
    return token

def _remove_runtime_capability_reader(token):
    global _runtime_capability_context
    if _runtime_capability_context is not token:
        raise ValueError('runtime capability owner changed')
    _runtime_capability_context = None

def _current_runtime_capability_token():
    import sys
    token = _runtime_capability_context
    if token is None:
        return None
    module = token[3]
    if (sys.modules.get(token[4]) is not module
            or getattr(getattr(module, 'app', None), 'state', None) is not token[0]
            or getattr(module, '_live_memory_policy_capabilities', None) is not token[1]
            or getattr(token[0], 's05_profile', None) is not token[2]
            or STORE.capability_provider is not capabilities):
        return None
    return token

def capabilities() -> dict[str, Any]:
    token = _current_runtime_capability_token()
    if token is None:
        return _inactive_capabilities()
    try:
        return token[1](token[0])
    except Exception:
        return _inactive_capabilities()


def defaults() -> dict[str, Any]:
    return {"axes":{axis:"rules" for axis in AXES},
        "schedule":{"mode":"idle", "quiet_seconds":5, "timezone":"local", "interval_seconds":900, "force_after_timeout":False},
        "budget":{"daily_seconds":60, "daily_tokens":16384, "daily_calls":20, "job_seconds":30,
                  "job_output_tokens":4096, "owner_queue":100, "global_queue":500, "concurrency":1},
        "recall":{"explicit_pull":True, "digest":True, "automatic_context_recall":False,
                  "candidates":20, "admitted_items":3, "admitted_characters":1200, "time_weight":0},
        "effects":{name:{"enabled":False, "weight":0} for name in EFFECTS},
        "rank_learning":{"enabled":False,"salience":False,"age_decay":False,"retrieval_boost":False},
        "ratings":{"visible":True, "collection_enabled":True, "explicit_submission_only":True},
        "automatic_admission":"review_candidates", "optional_token_saving":False}


def _keys(value: Any, expected: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) != expected:
        raise MemoryPolicyError("invalid_policy", f"{label} must contain exactly the supported fields")
    return value


def _integer(value: Any, low: int, high: int, label: str) -> None:
    if type(value) is not int or not low <= value <= high:
        raise MemoryPolicyError("invalid_policy", f"{label} is outside its integer bounds")


def _boolean(value: Any, label: str) -> None:
    if type(value) is not bool:
        raise MemoryPolicyError("invalid_policy", f"{label} must be boolean")


def _weight(value: Any, label: str) -> None:
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)) or not 0 <= value <= 1:
        raise MemoryPolicyError("invalid_policy", f"{label} must be finite and between 0 and 1")
    number = Decimal(str(value))
    if number < 0 or number > 1 or number % Decimal("0.05") != 0:
        raise MemoryPolicyError("invalid_policy", f"{label} must be 0–1 in 0.05 steps")


def validate_policy(value: Any, capability_snapshot: dict[str, Any]) -> dict[str, Any]:
    # Old, fingerprinted accepted policies retain their exact stored bytes;
    # the absent additive group has the historic off behavior.
    if type(value) is dict and "rank_learning" not in value:
        value = {**value, "rank_learning":copy.deepcopy(defaults()["rank_learning"])}
    root = _keys(value, set(defaults()), "policy")
    axes = _keys(root["axes"], set(AXES), "axes")
    cap_axes = capability_snapshot.get("axes") if type(capability_snapshot) is dict else None
    if type(cap_axes) is not dict or set(cap_axes) != set(AXES):
        raise MemoryPolicyError("invalid_capabilities", "Exact axis capability snapshot required")
    for axis in AXES:
        modes = cap_axes[axis]
        if type(modes) is not list or any(type(mode) is not str or mode not in {"rules", "model", "hybrid"} for mode in modes) or len(set(modes)) != len(modes):
            raise MemoryPolicyError("invalid_capabilities", "Invalid axis capabilities")
        if type(axes[axis]) is not str or axes[axis] not in {"rules", "model", "hybrid"} or axes[axis] not in modes:
            raise MemoryPolicyError("unsupported_mode", f"{axis} mode is unavailable")
    schedule = _keys(root["schedule"], {"mode","quiet_seconds","timezone","interval_seconds","force_after_timeout"}, "schedule")
    if type(schedule["mode"]) is not str or schedule["mode"] not in {"idle", "interval"}:
        raise MemoryPolicyError("invalid_policy", "unsupported schedule")
    if schedule["force_after_timeout"] is not False:
        raise MemoryPolicyError("invalid_policy", "idle timeout cannot force execution")
    _integer(schedule["quiet_seconds"],5,3600,"quiet_seconds")
    _integer(schedule["interval_seconds"],900,604800,"interval_seconds")
    if type(schedule["timezone"]) is not str or not schedule["timezone"]:
        raise MemoryPolicyError("invalid_policy", "timezone is required")
    if schedule["timezone"] != "local":
        try: ZoneInfo(schedule["timezone"])
        except (ZoneInfoNotFoundError, ValueError): raise MemoryPolicyError("invalid_policy", "invalid timezone") from None
    budget = _keys(root["budget"], set(defaults()["budget"]), "budget")
    for field, low, high in (("daily_seconds",0,86400),("daily_tokens",0,1000000),("daily_calls",0,1000),
                             ("job_seconds",1,300),("job_output_tokens",1,32768),("owner_queue",1,500)):
        _integer(budget[field],low,high,field)
    if type(budget["global_queue"]) is not int or budget["global_queue"] != 500 or type(budget["concurrency"]) is not int or budget["concurrency"] != 1:
        raise MemoryPolicyError("invalid_policy", "global queue and concurrency are fixed")
    for daily, job in (("daily_seconds","job_seconds"),("daily_tokens","job_output_tokens")):
        if budget[daily] and budget[job] > budget[daily]:
            raise MemoryPolicyError("invalid_policy", f"{job} exceeds {daily}")
    recall = _keys(root["recall"], set(defaults()["recall"]), "recall")
    for field in ("explicit_pull","digest","automatic_context_recall"): _boolean(recall[field],field)
    if not recall["explicit_pull"] or not recall["digest"]:
        raise MemoryPolicyError("invalid_policy", "explicit pull and digest remain enabled")
    for field, low, high in (("candidates",1,100),("admitted_items",0,3),("admitted_characters",0,1200)):
        _integer(recall[field],low,high,field)
    _weight(recall["time_weight"],"time_weight")
    effects = _keys(root["effects"], set(EFFECTS), "effects")
    for name in EFFECTS:
        effect = _keys(effects[name], {"enabled","weight"}, name)
        _boolean(effect["enabled"],name); _weight(effect["weight"],name)
    ranking = _keys(root["rank_learning"], {"enabled","salience","age_decay","retrieval_boost"}, "rank_learning")
    for name, value in ranking.items(): _boolean(value, "rank_learning."+name)
    ratings = _keys(root["ratings"], {"visible","collection_enabled","explicit_submission_only"}, "ratings")
    for name,value in ratings.items(): _boolean(value,name)
    if not ratings["explicit_submission_only"]:
        raise MemoryPolicyError("invalid_policy", "ratings require explicit submission")
    if type(root["automatic_admission"]) is not str or root["automatic_admission"] not in {"review_candidates", "admit_untrusted_summaries"}:
        raise MemoryPolicyError("invalid_policy", "unsupported automatic admission")
    _boolean(root["optional_token_saving"],"optional_token_saving")
    consumers = capability_snapshot.get("consumers", {})
    if type(consumers) is not dict:
        raise MemoryPolicyError("invalid_capabilities", "Invalid consumer capabilities")
    if ranking["enabled"] and any(ranking[k] for k in ("salience","age_decay","retrieval_boost")) and consumers.get("rank_learning") is not True:
        raise MemoryPolicyError("unsupported_consumer", "ranking effects are unavailable")
    if recall["automatic_context_recall"] and consumers.get("automatic_context_recall") is not True:
        raise MemoryPolicyError("unsupported_consumer", "automatic recall is unavailable")
    if any(effect["enabled"] for effect in effects.values()) and consumers.get("feedback") is not True:
        raise MemoryPolicyError("unsupported_consumer", "feedback effects are unavailable")
    if root["optional_token_saving"] and consumers.get("optional_token_saving") is not True:
        raise MemoryPolicyError("unsupported_consumer", "optional token saving is unavailable")
    if root["automatic_admission"] != "review_candidates" and consumers.get("automatic_admission") is not True:
        raise MemoryPolicyError("unsupported_consumer", "automatic admission is unavailable")
    return copy.deepcopy(root)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _selection_shape(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    required = {"version", "modelRouteID", "connectionID", "modelID", "grantID", "optionRouteID", "catalogRevision", "options"}
    if (type(value) is not dict or set(value) != required or type(value["version"]) is not int or value["version"] != 1
        or type(value["catalogRevision"]) is not int or not 1 <= value["catalogRevision"] <= 9007199254740991
        or any(type(value[k]) is not str or not value[k].strip() or len(value[k]) > 256 or value[k] != value[k].strip() for k in ("modelRouteID", "connectionID", "modelID", "optionRouteID"))
        or (value["grantID"] is not None and (type(value["grantID"]) is not str or not value["grantID"].strip() or len(value["grantID"]) > 256 or value["grantID"] != value["grantID"].strip()))
        or type(value["options"]) is not dict or set(value["options"]) - {"reasoning_effort"}
        or any(type(v) is not str or not v or len(v) > 32 or v in {"default", "inherit"} for v in value["options"].values())):
        raise MemoryPolicyError("invalid_assignment", "Invalid work-kind assignment")
    return copy.deepcopy(value)


def _empty_assignments() -> dict[str, Any]:
    return {kind: {"revision": 0, "selection": None} for kind in WORK_KINDS}


def _validate_assignment_route(owner: str, value: Any) -> dict[str, Any] | None:
    selected = _selection_shape(value)
    if selected is None:
        return None
    from src.openclank.chat_routing import resolve_chat_route, shared_endpoint_id
    from src.openclank.provider_request_options import project_request_options, validate_request_option_selection
    try:
        route = resolve_chat_route(owner=owner, model_route_id=selected["modelRouteID"], model_id=selected["modelID"],
            endpoint_id=shared_endpoint_id(selected["grantID"]) if selected["grantID"] else selected["connectionID"])
        if (route.connection_id != selected["connectionID"] or route.provider_grant_id != selected["grantID"]
            or "chat.complete" not in route.operations):
            raise ValueError("route unavailable")
        options = validate_request_option_selection(route_id=route.option_route_id, catalog_revision=route.catalog_revision,
            descriptors=project_request_options(route.capabilities, catalog_revision=route.catalog_revision, source="engine-catalog"),
            selection={"route_id":selected["optionRouteID"], "catalog_revision":selected["catalogRevision"], "options":selected["options"]})
        if options != selected["options"]:
            raise ValueError("selection is not normalized")
    except Exception as exc:
        raise MemoryPolicyError("invalid_assignment", "Work-kind route or options are unavailable or stale") from exc
    return selected


def _public_routes(owner: str, allowed_models: frozenset[str] | None = None) -> list[Any]:
    from src.openclank.chat_routing import list_chat_routes
    own, shared = list_chat_routes(owner)
    return [route for route in (*own, *shared) if "chat.complete" in route.operations
            and (allowed_models is None or route.provider_model_id in allowed_models or route.model_route_id in allowed_models)]


def normalize_public_assignment(owner: str, selection: Any, *, allowed_models: frozenset[str] | None = None) -> dict[str, Any] | None:
    """Resolve recipient-visible selector to the existing private frozen route."""
    if selection is None:
        return None
    keys = {"version", "endpointID", "modelID", "optionRouteID", "catalogRevision", "options"}
    if (type(selection) is not dict or set(selection) != keys or type(selection.get("version")) is not int
        or selection["version"] != 1 or type(selection.get("catalogRevision")) is not int
        or not 1 <= selection["catalogRevision"] <= 9007199254740991
        or any(type(selection.get(key)) is not str or not selection[key] or len(selection[key]) > 256 for key in ("endpointID", "modelID", "optionRouteID"))
        or type(selection.get("options")) is not dict):
        raise MemoryPolicyError("invalid_assignment", "Invalid public work-kind selection")
    try:
        matches = [route for route in _public_routes(owner, allowed_models) if route.public_endpoint_id == selection["endpointID"]
                   and route.provider_model_id == selection["modelID"] and route.option_route_id == selection["optionRouteID"]
                   and route.catalog_revision == selection["catalogRevision"]]
        if len(matches) != 1:
            raise ValueError("route unavailable or ambiguous")
        route = matches[0]
        return _validate_assignment_route(owner, {"version":1, "modelRouteID":route.model_route_id,
            "connectionID":route.connection_id, "modelID":route.provider_model_id, "grantID":route.provider_grant_id,
            "optionRouteID":route.option_route_id, "catalogRevision":route.catalog_revision, "options":selection["options"]})
    except Exception as exc:
        raise MemoryPolicyError("invalid_assignment", "Work-kind route or options are unavailable or stale") from exc


def public_policy_response(owner: str, value: Any, *, allowed_models: frozenset[str] | None = None) -> Any:
    """Remove internal assignment route/connection identities from all responses."""
    result = copy.deepcopy(value)
    if type(result) is not dict:
        return result
    document = result.get("document") if "document" in result else result
    if type(document) is not dict or document.get("schema") not in (SCHEMA_V2, SCHEMA_V3):
        return result
    try:
        routes = _public_routes(owner, allowed_models)
    except Exception:
        routes = []
    for entry in document.get("workKindAssignments", {}).values():
        selection = entry.get("selection") if type(entry) is dict else None
        if selection is None:
            continue
        # Retain only recipient-visible opaque option identity and user choice
        # when the original grant or descriptor is no longer available.
        safe = {"state":"unavailable", "optionRouteID":selection["optionRouteID"],
                "catalogRevision":selection["catalogRevision"], "options":copy.deepcopy(selection["options"])}
        matches = [route for route in routes if route.model_route_id == selection["modelRouteID"]
                   and route.connection_id == selection["connectionID"] and route.provider_model_id == selection["modelID"]
                   and route.provider_grant_id == selection["grantID"] and route.option_route_id == selection["optionRouteID"]
                   and route.catalog_revision == selection["catalogRevision"]]
        if len(matches) == 1:
            try:
                _validate_assignment_route(owner, selection)
                route = matches[0]
                safe = {"state":"available", "version":1, "endpointID":route.public_endpoint_id,
                        "modelID":route.provider_model_id, "optionRouteID":route.option_route_id,
                        "catalogRevision":route.catalog_revision, "options":copy.deepcopy(selection["options"])}
            except MemoryPolicyError:
                pass
        entry["selection"] = safe
    return result


def _validated_document(document: Any) -> dict[str, Any]:
    if type(document) is dict and document.get("schema") in (SCHEMA_V2, SCHEMA_V3):
        required={"schema", "revision", "accepted_policy", "workKindAssignments", "fingerprint"}
        if document.get("schema")==SCHEMA_V3:required.add("ratings_override")
        if (set(document) != required
            or type(document.get("revision")) is not int or document["revision"] < 1
            or type(document.get("fingerprint")) is not str or type(document.get("workKindAssignments")) is not dict
            or set(document["workKindAssignments"]) != set(WORK_KINDS)):
            raise MemoryPolicyError("invalid_stored_policy", "Stored memory policy requires recovery")
        try:
            if _fingerprint({key:value for key,value in document.items() if key != "fingerprint"}) != document["fingerprint"]:
                raise ValueError("fingerprint mismatch")
            if document.get("schema")==SCHEMA_V3:_validate_ratings(document["ratings_override"])
            accepted = document["accepted_policy"]
            if accepted is not None and (type(accepted) is not dict or accepted.get("schema") != SCHEMA):
                raise ValueError("invalid accepted policy")
            if accepted is not None:
                _validated_document(accepted)
            for entry in document["workKindAssignments"].values():
                if type(entry) is not dict or set(entry) != {"revision", "selection"} or type(entry["revision"]) is not int or entry["revision"] < 0:
                    raise ValueError("invalid work-kind revision")
                _selection_shape(entry["selection"])
        except (MemoryPolicyError, TypeError, ValueError, OverflowError):
            raise MemoryPolicyError("invalid_stored_policy", "Stored memory policy requires recovery") from None
        return copy.deepcopy(document)
    if (type(document) is not dict or set(document) != {"schema","revision","policy","capabilities","legacy_at_acceptance","fingerprint"} or
        document.get("schema") != SCHEMA or type(document.get("revision")) is not int or document["revision"] < 1 or
        type(document.get("fingerprint")) is not str):
        raise MemoryPolicyError("invalid_stored_policy", "Stored memory policy requires recovery")
    body = {key:value for key,value in document.items() if key != "fingerprint"}
    try:
        valid_hash = _fingerprint(body) == document["fingerprint"]
        validate_policy(document["policy"], document["capabilities"])
    except (MemoryPolicyError, TypeError, ValueError, OverflowError):
        raise MemoryPolicyError("invalid_stored_policy", "Stored memory policy requires recovery") from None
    if not valid_hash:
        raise MemoryPolicyError("invalid_stored_policy", "Stored memory policy requires recovery")
    return copy.deepcopy(document)


def _validate_ratings(value):
    ratings = _keys(value, {"visible", "collection_enabled", "explicit_submission_only"}, "ratings")
    for key, item in ratings.items():_boolean(item,key)
    if not ratings['explicit_submission_only']:raise MemoryPolicyError('invalid_policy','ratings require explicit submission')
    return copy.deepcopy(ratings)


def policy_view(document):
    """One validated view of legacy receipts and the ratings-only wrapper."""
    if document is None:return {'document':None,'accepted':None,'assignments':_empty_assignments(),'ratings':defaults()['ratings'],'wrapped':False}
    document=_validated_document(document)
    wrapped=document.get('schema') in (SCHEMA_V2,SCHEMA_V3)
    accepted=document['accepted_policy'] if wrapped else document
    ratings=(document['ratings_override'] if document.get('schema')==SCHEMA_V3 else accepted['policy']['ratings'] if accepted else defaults()['ratings'])
    return {'document':document,'accepted':accepted,'assignments':copy.deepcopy(document['workKindAssignments']) if wrapped else _empty_assignments(),
            'ratings':copy.deepcopy(ratings),'wrapped':wrapped}


def _graph_projection(graph: Any) -> dict[str, Any]:
    """Project only graph controls actually consumed by graph_extractor."""
    if not isinstance(graph, dict) or graph.get("state") != "available":
        return {"state":"unavailable", "reason":"graph_settings_unavailable"}
    raw = graph.get("value")
    if raw is None:
        raw = {}  # The existing graph reader treats a non-dict as its empty defaults.
    if not isinstance(raw, dict):
        return {"state":"unsupported", "reason":"graph_settings_not_object", "unknown_fields_present":True}
    known = {"enabled", "every_n_turns", "min_interval_seconds"}
    values, unsupported = {}, []
    for key in known:
        if key not in raw:
            continue
        value = raw[key]
        valid = (type(value) is bool if key == "enabled" else
                 type(value) is int if key == "every_n_turns" else
                 type(value) in (int, float) and (type(value) is int or math.isfinite(value)))
        if valid:
            values[key] = value
        else:
            unsupported.append(key)
    unknown = [key for key in raw if key not in known]
    visible_names = sorted(key for key in unknown if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key)
                           and not any(word in key.lower() for word in ("secret", "token", "password", "prompt", "path", "key", "credential")))[:64]
    return {"state":"unsupported" if unsupported else "available", "values":values,
            "unsupported_fields":sorted(unsupported), "unknown_fields":visible_names,
            "unknown_fields_present":bool(unknown), "redacted_unknown_fields":max(0, len(unknown) - len(visible_names))}


def compatibility_preview(prefs: Any, graph: Any) -> dict[str, Any]:
    prefs = prefs if isinstance(prefs, dict) else {}
    raw_mode = prefs.get("memory_mode")
    valid_mode = type(raw_mode) is str and raw_mode.strip().lower() in {"off","manual","automatic"}
    unsupported = []
    if "memory_mode" in prefs and not valid_mode:
        unsupported.append("memory_mode")
    if "auto_memory" in prefs and type(prefs["auto_memory"]) is not bool:
        unsupported.append("auto_memory")
    if "memory_enabled" in prefs and type(prefs["memory_enabled"]) is not bool:
        unsupported.append("memory_enabled")
    result = {"label":"legacy_compatibility", "memory_mode_raw":raw_mode if valid_mode else None,
        "auto_memory_raw":prefs.get("auto_memory") if type(prefs.get("auto_memory")) is bool else None,
        "memory_enabled_raw":prefs.get("memory_enabled", True) if type(prefs.get("memory_enabled", True)) is bool else None,
        "capture_allowed":bool(capture_allowed(prefs)), "capture_mode":capture_mode(prefs),
        "manual_write_allowed":write_allowed(prefs)[0], "memory_mode_effective":memory_mode(prefs),
        "graph":_graph_projection(graph), "unsupported":unsupported}
    if "memory_assistant_interpretation" in prefs:
        value=prefs["memory_assistant_interpretation"]
        if type(value) is not bool: unsupported.append("memory_assistant_interpretation")
        result["memory_assistant_interpretation"] = value if type(value) is bool else None
    for key in ('memory_policy_importance_enabled', 'memory_policy_connections_enabled', 'memory_policy_consolidation_enabled'):
        if key in prefs:
            value=prefs[key]
            if type(value) is not bool: unsupported.append(key)
            result[key]=value if type(value) is bool else None
    if prefs.get('memory_policy_consolidation_interval_days') is not None:
        value=prefs['memory_policy_consolidation_interval_days']
        if type(value) is not int or not 1<=value<=365: unsupported.append('memory_policy_consolidation_interval_days')
        result['memory_policy_consolidation_interval_days']=value if type(value) is int and 1<=value<=365 else None
    if 'memory_policy_connections_predicate' in prefs:
        value=prefs['memory_policy_connections_predicate']
        valid=(value is None or (type(value) is dict and set(value)=={'predicate','version','sha256'}
            and type(value['predicate']) is str and bool(value['predicate']) and value['predicate']==value['predicate'].strip()
            and len(value['predicate'])<=512 and type(value['version']) is int and value['version']>0
            and type(value['sha256']) is str and len(value['sha256'])==64 and all(c in '0123456789abcdef' for c in value['sha256'])))
        if not valid: unsupported.append('memory_policy_connections_predicate')
        result['memory_policy_connections_predicate']=copy.deepcopy(value) if valid else None
    return result


class MemoryPolicyStore:
    def __init__(self, *, capability_provider=capabilities, graph_provider=None):
        self.capability_provider = capability_provider
        self.graph_provider = graph_provider or self._graph

    @staticmethod
    def _graph():
        try:
            from src.settings import get_setting
            value = get_setting("memory_graph")
            return {"state":"available", "value":copy.deepcopy(value)}
        except Exception:
            return {"state":"unavailable"}

    @staticmethod
    def _owner(owner: str) -> str:
        if type(owner) is not str or not owner.strip():
            raise MemoryPolicyError("owner_required", "Trusted owner required")
        return owner.strip().lower()

    def snapshot(self, owner: str) -> dict[str, Any]:
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        owner = self._owner(owner)
        with PREFERENCE_LOCK:
            prefs = _load_for_user(None if owner == LOCAL_INSTALLATION_OWNER else owner)
            capability = copy.deepcopy(self.capability_provider())
            legacy = compatibility_preview(prefs, self.graph_provider())
            document = copy.deepcopy(prefs.get(KEY))
            if document is not None:
                document = _validated_document(document)
            view = policy_view(document)
            accepted = view["accepted"]
            result = {"owner":owner, "state":"accepted" if accepted is not None else "legacy_compatibility",
                      "revision":document.get("revision", 0) if isinstance(document, dict) else 0,
                      "document":document, "legacy":legacy, "capabilities":capability,
                      "consumer_activation":"inactive"}
            if document and document.get("schema")==SCHEMA_V3:result["ratings"] = view["ratings"]
            result["fingerprint"] = _fingerprint(result)
            return result

    def effective_snapshot(self, owner: str, *, read_privilege: bool, write_privilege: bool,
                           incognito: bool, no_memory: bool, compare_mode: bool) -> dict[str, Any]:
        """Trusted current inputs; capture again at use, never treat a snapshot as a lasting grant."""
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        owner = self._owner(owner)
        choices = {"incognito":incognito, "no_memory":no_memory, "compare_mode":compare_mode}
        privileges = {"read":read_privilege, "write":write_privilege}
        if any(type(value) is not bool for value in (*choices.values(), *privileges.values())):
            raise MemoryPolicyError("invalid_context", "trusted current choices and privileges must be booleans")
        with PREFERENCE_LOCK:
            prefs = _load_for_user(None if owner == LOCAL_INSTALLATION_OWNER else owner)
            policy = self.snapshot(owner)
            read = read_privilege and not incognito and not no_memory and bool(prefs.get("memory_enabled", True)) and memory_mode(prefs) != "off"
            capture = write_privilege and not no_memory and bool(capture_allowed(prefs, incognito=incognito, compare_mode=compare_mode))
            direct_write = write_privilege and not incognito and not no_memory and write_allowed(prefs)[0]
            unsupported = policy["legacy"]["unsupported"]
            available = not unsupported
            result = {"owner":owner, "policy_revision":policy["revision"], "policy_fingerprint":policy["fingerprint"],
                      "source":"legacy_compatibility", "policy_state":policy["state"], "choices":choices, "privileges":privileges,
                      "legacy_inputs":{"memory_mode":policy["legacy"]["memory_mode_raw"], "auto_memory":policy["legacy"]["auto_memory_raw"],
                                       "memory_enabled":policy["legacy"]["memory_enabled_raw"], "graph":policy["legacy"]["graph"]},
                      "eligibility_state":"available" if available else "unavailable",
                      "eligibility_reason":None if available else "unsupported_legacy_preferences", "unsupported_inputs":unsupported,
                      "read_allowed":read if available else False, "capture_allowed":capture if available else False,
                      "capture_mode":capture_mode(prefs) if available else None,
                      "direct_write_allowed":direct_write if available else False, "new_consumers":"inactive"}
            if 'ratings' in policy:result['ratings']=copy.deepcopy(policy['ratings'])
            result["fingerprint"] = _fingerprint(result)
            return result

    def read_eligibility(self, owner: str, *, read_privilege: bool, write_privilege: bool,
                         incognito: bool, no_memory: bool, compare_mode: bool) -> dict[str, Any]:
        """Fresh read-only policy decision; does not grant source or Native access."""
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        owner = self._owner(owner)
        if any(type(value) is not bool for value in (read_privilege,write_privilege,incognito,no_memory,compare_mode)):
            raise MemoryPolicyError("invalid_context", "trusted current choices and privileges must be booleans")
        with PREFERENCE_LOCK:
            prefs = _load_for_user(None if owner == LOCAL_INSTALLATION_OWNER else owner)
            # Validate the entire accepted/wrapped document exactly as the full view does.
            policy_view(prefs.get(KEY))
            unsupported = compatibility_preview(prefs, {"state":"unavailable"})['unsupported']
            available = not unsupported
            read = read_privilege and not incognito and not no_memory and bool(prefs.get("memory_enabled", True)) and memory_mode(prefs) != "off"
            return {"eligibility_state":"available" if available else "unavailable",
                    "read_allowed":bool(read) if available else False}

    def _capture_policy_projection(self, owner: str, *, read_privilege: bool, write_privilege: bool,
                                   incognito: bool, no_memory: bool, compare_mode: bool) -> dict[str, Any]:
        """Fresh, locked capture inputs; this is not an authority grant."""
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        owner = self._owner(owner)
        if any(type(value) is not bool for value in (read_privilege,write_privilege,incognito,no_memory,compare_mode)):
            raise MemoryPolicyError("invalid_context", "trusted current choices and privileges must be booleans")
        with PREFERENCE_LOCK:
            prefs = _load_for_user(None if owner == LOCAL_INSTALLATION_OWNER else owner)
            document = copy.deepcopy(policy_view(prefs.get(KEY))["document"])
            legacy = compatibility_preview(prefs, {"state":"unavailable"})
            legacy.pop("graph")
            available = not legacy["unsupported"]
            capture = write_privilege and not no_memory and bool(capture_allowed(prefs, incognito=incognito, compare_mode=compare_mode))
            return {"version":1, "owner":owner, "document":document, "legacy":legacy,
                    "choices":{"incognito":incognito, "no_memory":no_memory, "compare_mode":compare_mode},
                    "privileges":{"read":read_privilege, "write":write_privilege},
                    "eligibility_state":"available" if available else "unavailable",
                    "capture_allowed":bool(capture) if available else False}

    def capture_eligibility(self, owner: str, *, read_privilege: bool, write_privilege: bool,
                            incognito: bool, no_memory: bool, compare_mode: bool) -> dict[str, Any]:
        """Fresh capture decision only; no presentation or durable authority."""
        projection = self._capture_policy_projection(owner, read_privilege=read_privilege, write_privilege=write_privilege,
                                                     incognito=incognito, no_memory=no_memory, compare_mode=compare_mode)
        return {"eligibility_state":projection["eligibility_state"], "capture_allowed":projection["capture_allowed"]}

    def capture_policy_snapshot(self, owner: str, *, read_privilege: bool, write_privilege: bool,
                                incognito: bool, no_memory: bool, compare_mode: bool) -> dict[str, Any]:
        """Fresh capture decision and input fingerprint; not a lasting grant."""
        projection = self._capture_policy_projection(owner, read_privilege=read_privilege, write_privilege=write_privilege,
                                                     incognito=incognito, no_memory=no_memory, compare_mode=compare_mode)
        return {"eligibility_state":projection["eligibility_state"], "capture_allowed":projection["capture_allowed"],
                "fingerprint":_fingerprint(projection)}

    def accept(self, owner: str, policy: Any, *, expected_revision: Any) -> dict[str, Any]:
        from routes.prefs_routes import _mutate_user
        owner = self._owner(owner)
        if type(expected_revision) is not int or expected_revision < 0:
            raise MemoryPolicyError("invalid_policy", "expected_revision must be a nonnegative integer")
        user = None if owner == LOCAL_INSTALLATION_OWNER else owner
        def prepare(prefs):
            existing = prefs.get(KEY)
            if existing is not None:
                existing = _validated_document(existing)
            revision = existing["revision"] if existing else 0
            if revision != expected_revision:
                raise MemoryPolicyError("revision_conflict", "Memory policy changed; reload settings")
            capability = copy.deepcopy(self.capability_provider())
            checked = validate_policy(policy, capability)
            legacy = compatibility_preview(prefs, self.graph_provider())
            previous = policy_view(existing)["accepted"]
            accepted_revision = previous["revision"] + 1 if previous else 1
            document = {"schema":SCHEMA, "revision":accepted_revision, "policy":checked,
                        "capabilities":capability, "legacy_at_acceptance":legacy}
            document["fingerprint"] = _fingerprint(document)
            if existing and existing.get("schema") in (SCHEMA_V2,SCHEMA_V3):
                wrapper = {"schema":existing["schema"], "revision":revision+1, "accepted_policy":document,
                           "workKindAssignments":copy.deepcopy(existing["workKindAssignments"])}
                if existing["schema"]==SCHEMA_V3:wrapper["ratings_override"]=copy.deepcopy(existing["ratings_override"])
                wrapper["fingerprint"] = _fingerprint(wrapper)
                prefs[KEY] = wrapper
            else:
                prefs[KEY] = document
            return prefs, (bool(existing and existing.get("schema") in (SCHEMA_V2,SCHEMA_V3)), copy.deepcopy(document))
        _, (wrapped, document) = _mutate_user(user, prepare, allow_memory_policy=True)
        return self.snapshot(owner) if wrapped else document

    def set_ratings(self, owner: str, ratings: Any, *, expected_revision: Any) -> dict[str, Any]:
        from routes.prefs_routes import _mutate_user
        owner=self._owner(owner);checked=_validate_ratings(ratings)
        if type(expected_revision) is not int or expected_revision<0:raise MemoryPolicyError('invalid_policy','expected_revision must be a nonnegative integer')
        user=None if owner==LOCAL_INSTALLATION_OWNER else owner
        def prepare(prefs):
            view=policy_view(prefs.get(KEY));existing=view['document'];revision=existing['revision'] if existing else 0
            if revision!=expected_revision:raise MemoryPolicyError('revision_conflict','Memory policy changed; reload settings')
            if view['ratings']==checked:return prefs,None
            wrapper={'schema':SCHEMA_V3,'revision':revision+1,'accepted_policy':copy.deepcopy(view['accepted']),
                     'workKindAssignments':copy.deepcopy(view['assignments']),'ratings_override':checked}
            wrapper['fingerprint']=_fingerprint(wrapper);prefs[KEY]=wrapper
            return prefs,None
        _mutate_user(user,prepare,allow_memory_policy=True)
        return self.snapshot(owner)

    def s05_base_policy_snapshot(self, owner: str, *, read_privilege: bool, write_privilege: bool) -> dict[str, Any]:
        """Fingerprint effective non-assignment inputs; never an execution grant."""
        from routes.prefs_routes import PREFERENCE_LOCK
        with PREFERENCE_LOCK:
            effective = self.effective_snapshot(owner, read_privilege=read_privilege, write_privilege=write_privilege,
                incognito=False, no_memory=False, compare_mode=False)
            current = self.snapshot(owner)
            document = current['document']
            accepted = policy_view(document)['accepted']
            inputs = {key: copy.deepcopy(effective[key]) for key in (
                'owner','source','policy_state','choices','privileges','legacy_inputs','eligibility_state',
                'eligibility_reason','unsupported_inputs','read_allowed','capture_allowed','capture_mode','direct_write_allowed')}
            inputs['accepted_policy'] = copy.deepcopy(accepted['policy']) if accepted else None
            if document and document.get('schema')==SCHEMA_V3:inputs['ratings_override']=policy_view(document)['ratings']
            result = {'version':1, 'inputs':inputs}
            result['fingerprint'] = _fingerprint(result)
            return result

    def work_kind_snapshot(self, owner: str, kind: str) -> dict[str, Any]:
        if kind not in WORK_KINDS:
            raise MemoryPolicyError("invalid_assignment", "Invalid work-kind")
        snapshot = self.snapshot(owner)
        document = snapshot["document"]
        entry = (document["workKindAssignments"][kind] if document and document.get("schema") in (SCHEMA_V2,SCHEMA_V3)
                 else _empty_assignments()[kind])
        result = {"version": 1, "kind": kind, "revision": entry["revision"], "selection": copy.deepcopy(entry["selection"])}
        result["fingerprint"] = _fingerprint(result)
        return result

    def set_work_kind(self, owner: str, kind: str, selection: Any, *, expected_revision: Any) -> dict[str, Any]:
        """Store an owner choice without accepting or activating an engine policy."""
        from routes.prefs_routes import _mutate_user
        owner = self._owner(owner)
        if kind not in WORK_KINDS or type(expected_revision) is not int or expected_revision < 0:
            raise MemoryPolicyError("invalid_assignment", "Invalid work-kind or revision")
        user = None if owner == LOCAL_INSTALLATION_OWNER else owner
        def prepare(prefs):
            existing = _validated_document(prefs[KEY]) if KEY in prefs else None
            revision = existing["revision"] if existing else 0
            if revision != expected_revision:
                raise MemoryPolicyError("revision_conflict", "Memory policy changed; reload settings")
            selected = _validate_assignment_route(owner, selection)
            assignments = policy_view(existing)["assignments"]
            if assignments[kind]["selection"] == selected:
                return prefs, None
            assignments[kind] = {"revision": assignments[kind]["revision"] + 1, "selection": selected}
            accepted = policy_view(existing)["accepted"]
            wrapper = {"schema":SCHEMA_V3 if existing and existing.get("schema")==SCHEMA_V3 else SCHEMA_V2, "revision":revision+1, "accepted_policy":accepted, "workKindAssignments":assignments}
            if wrapper["schema"]==SCHEMA_V3:wrapper["ratings_override"]=copy.deepcopy(existing["ratings_override"])
            wrapper["fingerprint"] = _fingerprint(wrapper)
            prefs[KEY] = wrapper
            return prefs, None
        _mutate_user(user, prepare, allow_memory_policy=True)
        return self.snapshot(owner)


STORE = MemoryPolicyStore()
