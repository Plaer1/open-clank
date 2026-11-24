"""Server-owned plan draft/approval state.

The browser may display a plan, but it must not be able to make arbitrary
markdown executable.  Approval is a compare-and-swap over the plan revision
and digest persisted in the canonical session's ``mimo_state``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from src.openclank.transcript_projection import get_mimo_state, mutate_plan_state
from src.clanker_paths import ClankerPathError, canonical_plan_path, canonical_robonote_path


def _state_plan(state: dict[str, Any]) -> dict[str, Any]:
    value = state.get("plan_state")
    return dict(value) if isinstance(value, dict) else {}


def approved_plan_state(session_id: str, owner: str) -> dict[str, Any]:
    """Return the current plan only when its exact revision is approved."""
    state = get_mimo_state(session_id, owner=owner)
    plan = _state_plan(state)
    revision = int(plan.get("revision") or 0)
    approved_revision = plan.get("approved_revision")
    digest = str(plan.get("digest") or "")
    approved_digest = str(plan.get("approved_digest") or "")
    if plan.get("slice_coverage") == "unavailable" or not revision or not digest or approved_revision != revision or approved_digest != digest:
        return {}
    if str(plan.get("status") or "approved") in {"cleared", "draft", "revised"}:
        return {}
    return plan


def approve_plan(session_id: str, owner: str, *, revision: int, digest: str, current_authority=None) -> dict[str, Any]:
    if current_authority is None:
        raise ValueError("original plan account unavailable")
    def apply(plan):
        if (plan.get("slice_coverage") == "unavailable" or plan.get("status") == "cleared"
                or int(plan.get("revision") or 0) != int(revision) or not digest
                or plan.get("digest") != digest):
            raise ValueError("plan revision is stale or unavailable")
        plan.update(approved_revision=int(revision), approved_digest=digest, status="approved")
        return plan
    return mutate_plan_state(session_id, owner, apply, current_authority=current_authority)


def save_plan_draft(session_id: str, owner: str, plan_text: str, *, current_authority=None) -> dict[str, Any]:
    if current_authority is None:
        raise ValueError("original plan account unavailable")
    plan_text = str(plan_text or "").strip()[:8192]
    if not plan_text:
        raise ValueError("plan text is required")
    def apply(previous):
        evidence = {"plan": plan_text}
        if previous.get("slice_digests"):
            evidence["slice_digests"] = previous["slice_digests"]
        if previous.get("slice_coverage"):
            evidence["slice_coverage"] = previous["slice_coverage"]
            evidence["slice_coverage_reason"] = previous.get("slice_coverage_reason")
        digest = hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        revision = int(previous.get("revision") or 0) + (previous.get("digest") != digest or previous.get("status") == "cleared")
        plan = dict(previous)
        if revision != int(previous.get("revision") or 0):
            # Existing Files association and any original operation selector must
            # be preserved for the qualified workflow rebind/reconciliation.
            if previous.get("artifact_association") is not None or previous.get("artifact_set") is not None:
                raise ValueError("changed plan requires the admitted workflow writer")
        plan.update(plan=plan_text, digest=digest, revision=revision, approved_revision=None, approved_digest=None, status="draft")
        return plan
    return mutate_plan_state(session_id, owner, apply, current_authority=current_authority)



def create_initial_workflow_draft(session_id: str, owner: str, plan_text: str, *, current_authority=None) -> dict[str, Any]:
    """Atomically create only an absent draft; an existing revision wins."""
    if not callable(current_authority) or not isinstance(plan_text, str) or not plan_text.strip() or len(plan_text) > 8192:
        raise ValueError("initial workflow draft unavailable")
    text = plan_text.strip()
    digest = hashlib.sha256(json.dumps({"plan": text}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    def apply(previous):
        if previous:
            raise ValueError("plan already exists")
        return {"plan": text, "digest": digest, "revision": 1, "approved_revision": None,
                "approved_digest": None, "status": "draft"}
    return mutate_plan_state(session_id, owner, apply, current_authority=current_authority)



def transition_workflow_plan(session_id: str, owner: str, *, expected_plan: dict[str, Any], text: str,
                             operation_id: str, verified_members: dict[str, Any],
                             writer_authority=None, current_authority=None) -> dict[str, Any]:
    """Atomically retire an old association and reserve the exact new draft write.

    The private Host caller freshly reauthorizes every member before passing
    `verified_members`; neither those identities nor lineage are grants.
    """
    if (not callable(current_authority) or not callable(writer_authority) or type(expected_plan) is not dict or
            type(verified_members) is not dict or not isinstance(text,str) or not text.strip() or len(text)>8192 or
            not isinstance(operation_id,str) or not re.fullmatch(r"workflowop_[0-9a-f]{64}",operation_id)):
        raise ValueError("plan transition unavailable")
    original=json.loads(json.dumps(expected_plan)); old_assoc=original.get('artifact_association');old_set=original.get('artifact_set')
    if (not isinstance(old_assoc,dict) or not isinstance(old_set,dict) or old_assoc.get('members')!=verified_members or
            old_assoc.get('artifact_set_id')!=old_set.get('artifact_set_id') or old_assoc.get('subject')!=old_set.get('subject') or
            old_assoc.get('plan_revision')!=original.get('revision') or old_assoc.get('plan_digest')!=original.get('digest') or
            old_set.get('plan_revision')!=original.get('revision') or old_set.get('plan_digest')!=original.get('digest') or
            old_assoc.get('account_id')!=old_set.get('account_id') or old_assoc.get('workspace_id')!=old_set.get('workspace_id') or
            not isinstance(verified_members.get('metaplan'),dict)):
        raise ValueError("plan transition association unavailable")
    intents=old_assoc.get('create_intents') or {}
    if not isinstance(intents,dict) or any(key not in verified_members for key in intents) or any(
            not isinstance(member,dict) or member.get('pending_write') is not None for member in verified_members.values()):
        raise ValueError("original workflow outcome requires reconciliation")
    text=text.strip();evidence={'plan':text}
    if original.get('slice_digests'): evidence['slice_digests']=original['slice_digests']
    if original.get('slice_coverage'):
        evidence['slice_coverage']=original['slice_coverage'];evidence['slice_coverage_reason']=original.get('slice_coverage_reason')
    digest=hashlib.sha256(json.dumps(evidence,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    if digest==original.get('digest'): raise ValueError("plan transition is unchanged")
    revision=int(original.get('revision') or 0)+1;subject=old_set['subject']
    material=['openclank.workflow.artifact-set.v1',old_set['account_id'],old_set['workspace_id'],session_id,
              subject['kind'],subject['ref'],revision,digest]
    set_id='artifact-set-'+hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    result=[]
    def apply(plan):
        if plan!=original or writer_authority() is not True: raise ValueError("plan transition changed")
        lineage=plan.get('artifact_history',[])
        if not isinstance(lineage,list) or len(lineage)>=32: raise ValueError("plan history is unavailable")
        prior={'plan_revision':original['revision'],'plan_digest':original['digest'],'artifact_set':old_set,'association':old_assoc}
        reserved=dict(old_set);reserved.update(artifact_set_id=set_id,plan_revision=revision,plan_digest=digest)
        members=json.loads(json.dumps(verified_members));meta=dict(members['metaplan']);meta['pending_write']={'operation_id':operation_id};members['metaplan']=meta
        association=dict(old_assoc);association.update(artifact_set_id=set_id,plan_revision=revision,plan_digest=digest,members=members)
        plan.update(plan=text,digest=digest,revision=revision,approved_revision=None,approved_digest=None,status='draft',
                    artifact_history=[*lineage,prior],artifact_set=reserved,artifact_association=association)
        result.append(json.loads(json.dumps(plan)));return plan
    mutate_plan_state(session_id,owner,apply,current_authority=current_authority)
    return result[0]


def bind_artifact_path(session_id: str, owner: str, relative_path: str, *, expected_revision=None, expected_digest=None, current_authority=None) -> str:
    if current_authority is None:
        raise ValueError("original plan account unavailable")
    try:
        value = canonical_plan_path(str(relative_path or ""), allow_legacy=True)
    except ClankerPathError as exc:
        raise ValueError("invalid plan artifact path") from exc
    result = []
    def apply(plan):
        if expected_revision is not None and (plan.get("revision") != expected_revision or plan.get("digest") != expected_digest or plan.get("approved_revision") != expected_revision or plan.get("approved_digest") != expected_digest or plan.get("status") != "approved"):
            raise ValueError("approved plan changed")
        raw = str(plan.get("artifact_relpath") or "").strip()
        try:
            existing = canonical_plan_path(raw, allow_legacy=True) if raw else value
        except ClankerPathError as exc:
            raise ValueError("invalid previously bound plan artifact path") from exc
        if raw and raw != existing:
            rev = int(plan.get("artifact_path_revision") or 0) + 1
            events = list(plan.get("artifact_path_events") or [])
            events.append({"kind": "legacy_path_migration", "from": raw, "to": existing, "revision": rev})
            plan.update(artifact_path_revision=rev, artifact_path_events=events[-32:])
        elif not raw and value != str(relative_path or "").strip():
            plan.update(artifact_path_revision=1, artifact_path_events=[{"kind": "legacy_path_migration", "from": str(relative_path or "").strip(), "to": value, "revision": 1}])
        plan["artifact_relpath"] = existing
        result.append(existing)
        return plan
    mutate_plan_state(session_id, owner, apply, current_authority=current_authority)
    return result[0]




def reserve_artifact_set(session_id: str, owner: str, *, account_id: str, workspace_id: str,
                         subject: dict[str, str], expected_revision: int, expected_digest: str,
                         association_authority=None, current_authority=None) -> dict[str, Any]:
    """Reserve only a plan identity; it carries no Files resource authority."""
    if (current_authority is None or not callable(association_authority) or type(subject) is not dict
            or set(subject) != {"kind", "ref"} or subject.get("kind") not in {"task", "root_operation"}
            or type(expected_revision) is not int or expected_revision < 1
            or not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)):
        raise ValueError("plan artifact set unavailable")
    for value in (account_id, workspace_id, session_id, subject.get("ref")):
        if (not isinstance(value, str) or not value or value != value.strip() or len(value.encode("utf-8")) > 256
                or any(ord(ch) < 32 for ch in value)):
            raise ValueError("plan artifact set unavailable")
    material = ["openclank.workflow.artifact-set.v1", account_id, workspace_id, session_id,
                subject["kind"], subject["ref"], expected_revision, expected_digest]
    identity = "artifact-set-" + hashlib.sha256(json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    reservation = {"version":1, "artifact_set_id":identity, "account_id":account_id, "workspace_id":workspace_id,
                   "subject":dict(subject), "plan_revision":expected_revision, "plan_digest":expected_digest}
    result = []
    def apply(plan):
        if (plan.get("status") == "cleared" or plan.get("revision") != expected_revision
                or plan.get("digest") != expected_digest or association_authority() is not True):
            raise ValueError("plan artifact set changed")
        previous = plan.get("artifact_set")
        if previous is not None:
            if not isinstance(previous,dict) or {key:value for key,value in previous.items() if key != "metaplan_create"} != reservation:
                raise ValueError("plan artifact set already bound")
            if "metaplan_create" in previous: reservation["metaplan_create"] = dict(previous["metaplan_create"])
        plan["artifact_set"] = reservation
        result.append(dict(reservation, subject=dict(subject)))
        return plan
    mutate_plan_state(session_id, owner, apply, current_authority=current_authority)
    return result[0]


def bind_artifact_association(session_id: str, owner: str, association: dict[str, Any], *,
                              expected_revision: int, expected_digest: str,
                              association_authority=None, current_authority=None) -> dict[str, Any]:
    """CAS-bind an admitted task/root and Files identity to the current plan.

    Neither opaque identifiers nor this stored association are authorization.
    The caller must independently prove the original association and current
    writer authority, including before each later Files operation.
    """
    fields = {"version", "account_id", "workspace_id", "subject", "metaplan_resource_id"}
    if (current_authority is None or not callable(association_authority)
            or type(association) is not dict or set(association) != fields
            or type(association.get("version")) is not int or association["version"] != 1
            or type(expected_revision) is not int or expected_revision < 1
            or not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)):
        raise ValueError("plan artifact association unavailable")
    for key in ("account_id", "workspace_id"):
        value = association[key]
        if not isinstance(value, str) or not value or value != value.strip() or len(value.encode("utf-8")) > 256 or any(ord(ch) < 32 for ch in value):
            raise ValueError("plan artifact association unavailable")
    subject = association["subject"]
    if (type(subject) is not dict or set(subject) != {"kind", "ref"}
            or subject.get("kind") not in {"task", "root_operation"}
            or not isinstance(subject.get("ref"), str) or not subject["ref"]
            or subject["ref"] != subject["ref"].strip() or len(subject["ref"].encode("utf-8")) > 256
            or any(ord(ch) < 32 for ch in subject["ref"])
            or not isinstance(association["metaplan_resource_id"], str)
            or not re.fullmatch(r"resource-[0-9a-f]{64}", association["metaplan_resource_id"])):
        raise ValueError("plan artifact association unavailable")
    bound = {**association, "subject": dict(subject), "plan_revision": expected_revision, "plan_digest": expected_digest}
    result = []
    def apply(plan):
        if (plan.get("status") == "cleared" or plan.get("revision") != expected_revision
                or plan.get("digest") != expected_digest or association_authority() is not True):
            raise ValueError("plan artifact association changed")
        reserved = plan.get("artifact_set")
        if (not isinstance(reserved, dict) or reserved.get("account_id") != association["account_id"]
                or reserved.get("workspace_id") != association["workspace_id"] or reserved.get("subject") != subject
                or reserved.get("plan_revision") != expected_revision or reserved.get("plan_digest") != expected_digest
                or not re.fullmatch(r"artifact-set-[0-9a-f]{64}", str(reserved.get("artifact_set_id") or ""))):
            raise ValueError("plan artifact set unavailable")
        bound["artifact_set_id"] = reserved["artifact_set_id"]
        existing = plan.get("artifact_association")
        if existing is not None:
            if not isinstance(existing, dict) or {key:value for key,value in existing.items() if key not in {"members", "create_intents"}} != bound:
                raise ValueError("plan artifact association already bound")
            bound["members"] = dict(existing.get("members") or {})
            bound["create_intents"] = dict(existing.get("create_intents") or {})
        plan["artifact_association"] = bound
        result.append(dict(bound, subject=dict(subject)))
        return plan
    mutate_plan_state(session_id, owner, apply, current_authority=current_authority)
    return result[0]



def join_artifact_association(session_id: str, owner: str, *, artifact_set_id: str, account_id: str,
                              workspace_id: str, expected_revision: int, expected_digest: str,
                              writer_authority=None, current_authority=None) -> dict[str, Any]:
    """Join a live writer to an existing exact plan; historical subject is not a grant."""
    if (current_authority is None or not callable(writer_authority) or not isinstance(artifact_set_id, str)
            or not re.fullmatch(r"artifact-set-[0-9a-f]{64}", artifact_set_id)
            or type(expected_revision) is not int or expected_revision < 1
            or not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)):
        raise ValueError("plan artifact association unavailable")
    result = []
    def apply(plan):
        association = plan.get("artifact_association")
        reserved = plan.get("artifact_set")
        if (plan.get("status") == "cleared" or plan.get("revision") != expected_revision
                or plan.get("digest") != expected_digest or not isinstance(association, dict)
                or not isinstance(reserved, dict) or association.get("artifact_set_id") != artifact_set_id
                or reserved.get("artifact_set_id") != artifact_set_id
                or association.get("account_id") != account_id or reserved.get("account_id") != account_id
                or association.get("workspace_id") != workspace_id or reserved.get("workspace_id") != workspace_id
                or association.get("plan_revision") != expected_revision or association.get("plan_digest") != expected_digest
                or reserved.get("plan_revision") != expected_revision or reserved.get("plan_digest") != expected_digest
                or association.get("subject") != reserved.get("subject") or writer_authority() is not True):
            raise ValueError("plan artifact association changed")
        result.append(json.loads(json.dumps(association)))
        return plan
    mutate_plan_state(session_id, owner, apply, current_authority=current_authority)
    return result[0]





def record_metaplan_create(session_id: str, owner: str, *, artifact_set_id: str, operation_id: str, writer_session_id: str,
                           relative_path: str, expected_revision: int, expected_digest: str,
                           writer_authority=None, current_authority=None) -> dict[str, str]:
    """Freeze original create selector before issue; it grants no resource access."""
    if (current_authority is None or not callable(writer_authority) or not isinstance(artifact_set_id,str)
            or not re.fullmatch(r"artifact-set-[0-9a-f]{64}",artifact_set_id) or not isinstance(operation_id,str)
            or not re.fullmatch(r"workflowop_[0-9a-f]{64}",operation_id) or not isinstance(writer_session_id,str)
            or not writer_session_id or len(writer_session_id.encode('utf-8'))>256):
        raise ValueError("workflow create intent unavailable")
    path=workflow_member_path(relative_path,"metaplan",None)
    intent={"operation_id":operation_id,"relative_path":path,"writer_session_id":writer_session_id}; result=[]
    def apply(plan):
        reserved=plan.get("artifact_set")
        if (plan.get("status")=="cleared" or plan.get("revision")!=expected_revision or plan.get("digest")!=expected_digest
                or not isinstance(reserved,dict) or reserved.get("artifact_set_id")!=artifact_set_id
                or reserved.get("plan_revision")!=expected_revision or reserved.get("plan_digest")!=expected_digest
                or writer_authority() is not True): raise ValueError("workflow create intent changed")
        old=reserved.get("metaplan_create")
        if old is not None and old != intent: raise ValueError("original workflow create must be reconciled")
        reserved=dict(reserved);reserved["metaplan_create"]=intent;plan["artifact_set"]=reserved
        result.append(dict(intent));return plan
    mutate_plan_state(session_id,owner,apply,current_authority=current_authority)
    return result[0]


def workflow_member_path(metaplan_path: str, role: str, name: str | None) -> str:
    """Return the only canonical role path beneath this metaplan topic."""
    match = re.fullmatch(r"\.clanker/futures/([a-z0-9]+(?:-[a-z0-9]+)*)\.md", str(metaplan_path or ""))
    if not match or len(match.group(1)) > 60: raise ValueError("workflow metaplan path unavailable")
    topic = match.group(1)
    if role == "metaplan":
        if name is not None: raise ValueError("workflow artifact name unavailable")
        return metaplan_path
    if role == "progress":
        if not isinstance(name,str) or not re.fullmatch(r"task-[a-f0-9]{64}",name):
            raise ValueError("workflow artifact name unavailable")
    elif role in {"concern", "slice"}:
        if not isinstance(name,str) or len(name)>60 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*",name):
            raise ValueError("workflow artifact name unavailable")
    elif role not in {"ledger", "checkpoint"} or name is not None:
        raise ValueError("workflow artifact name unavailable")
    paths = {"ledger":f".clanker/robonotes/{topic}/00-index.md", "checkpoint":f".clanker/robonotes/{topic}/checkpoint.md",
             "concern":f".clanker/robonotes/{topic}/{name}.md", "progress":f".clanker/robonotes/{topic}/tasks/{name}.md",
             "slice":f".clanker/futures/{topic}/{name}.md"}
    return paths[role]



def record_member_create(session_id: str, owner: str, *, artifact_set_id: str, role: str, name: str | None,
                         operation_id: str, writer_session_id: str, relative_path: str, expected_revision: int, expected_digest: str,
                         writer_authority=None, current_authority=None, task_ref=None) -> dict[str, str]:
    """Freeze a non-metaplan member's original create before any issue."""
    if (current_authority is None or not callable(writer_authority) or role == "metaplan"
            or not isinstance(operation_id,str) or not re.fullmatch(r"workflowop_[0-9a-f]{64}",operation_id)
            or not isinstance(writer_session_id,str) or not writer_session_id or len(writer_session_id.encode('utf-8'))>256):
        raise ValueError("workflow create intent unavailable")
    if role=="progress":
        if (type(task_ref) is not dict or set(task_ref)!={"kind","sessionID","taskID","assignmentEventID"} or task_ref.get("kind")!="native-task"
                or not isinstance(task_ref.get("sessionID"),str) or not task_ref["sessionID"] or not isinstance(task_ref.get("taskID"),str)
                or not re.fullmatch(r"T[0-9]+(?:\.[0-9]+)*",task_ref["taskID"]) or type(task_ref.get("assignmentEventID")) is not int
                or not 0<task_ref["assignmentEventID"]<=9007199254740991): raise ValueError("workflow task reference unavailable")
        material=["openclank.workflow.task-ref.v1",task_ref["sessionID"],task_ref["taskID"],task_ref["assignmentEventID"]]
        if name!="task-"+hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(",", ":")).encode()).hexdigest():
            raise ValueError("workflow task reference changed")
    elif task_ref is not None: raise ValueError("workflow task reference unavailable")
    key=role if name is None else role+":"+name
    intent={"operation_id":operation_id,"relative_path":relative_path,"writer_session_id":writer_session_id}
    if task_ref is not None: intent["task_ref"]=dict(task_ref)
    result=[]
    def apply(plan):
        assoc=plan.get("artifact_association"); meta=(assoc.get("members") or {}).get("metaplan") if isinstance(assoc,dict) else None
        if (plan.get("status")=="cleared" or plan.get("revision")!=expected_revision or plan.get("digest")!=expected_digest
                or not isinstance(assoc,dict) or assoc.get("artifact_set_id")!=artifact_set_id or not isinstance(meta,dict)
                or workflow_member_path(meta.get("relative_path"),role,name)!=relative_path or writer_authority() is not True):
            raise ValueError("workflow create intent changed")
        intents=dict(assoc.get("create_intents") or {}); old=intents.get(key)
        if old is not None and old!=intent: raise ValueError("original workflow create must be reconciled")
        intents[key]=intent;assoc=dict(assoc);assoc["create_intents"]=intents;plan["artifact_association"]=assoc
        result.append(dict(intent));return plan
    mutate_plan_state(session_id,owner,apply,current_authority=current_authority)
    return result[0]



def _workflow_revision_valid(value):
    return (type(value) is dict and set(value)=={"kind","value"} and value.get("kind")=="hostFingerprint"
            and isinstance(value.get("value"),str) and re.fullmatch(r"sha256:[0-9a-f]{64}:(?:0|[1-9][0-9]*)",value["value"]) is not None)


def _workflow_object_tag_valid(value):
    return isinstance(value,str) and re.fullmatch(r"[0-9a-f]{64}",value) is not None


def pending_artifact_write(session_id: str, owner: str, *, artifact_set_id: str, role: str, name: str | None,
                           operation_id: str, expected_revision: int, expected_digest: str,
                           writer_authority=None, current_authority=None, settle: bool = False, committed_revision=None, committed_object_tag=None) -> dict[str, Any] | None:
    """Freeze or explicitly settle one original member update under plan CAS.

    `settle` is private to a Host caller that independently verified the exact
    original Files terminal outcome; operation selectors never authorize I/O.
    """
    if (current_authority is None or not callable(writer_authority) or type(settle) is not bool
            or not isinstance(operation_id,str) or not re.fullmatch(r"workflowop_[0-9a-f]{64}",operation_id)
            or (settle and (not _workflow_revision_valid(committed_revision) or not _workflow_object_tag_valid(committed_object_tag)))
            or (not settle and (committed_revision is not None or committed_object_tag is not None))):
        raise ValueError("workflow update intent unavailable")
    key=role if name is None else role+":"+name;result=[]
    def apply(plan):
        assoc=plan.get("artifact_association");members=assoc.get("members") if isinstance(assoc,dict) else None
        member=members.get(key) if isinstance(members,dict) else None
        if (plan.get("status")=="cleared" or plan.get("revision")!=expected_revision or plan.get("digest")!=expected_digest
                or not isinstance(assoc,dict) or assoc.get("artifact_set_id")!=artifact_set_id or not isinstance(member,dict)
                or member.get("role")!=role or member.get("name")!=name or writer_authority() is not True):
            raise ValueError("workflow update intent changed")
        member=dict(member); old=member.get("pending_write")
        if settle:
            if old!={"operation_id":operation_id}: raise ValueError("original workflow update changed")
            member.pop("pending_write",None);member["revision"]=dict(committed_revision);member["object_tag"]=committed_object_tag;result.append(None)
        else:
            intent={"operation_id":operation_id}
            if old is not None and old!=intent: raise ValueError("original workflow update must be reconciled")
            member["pending_write"]=intent;result.append(dict(intent))
        members=dict(members);members[key]=member;assoc=dict(assoc);assoc["members"]=members;plan["artifact_association"]=assoc
        return plan
    mutate_plan_state(session_id,owner,apply,current_authority=current_authority)
    return result[0]


def bind_artifact_member(session_id: str, owner: str, *, artifact_set_id: str, role: str, name: str | None,
                         relative_path: str, resource_id: str, history_resource_id: str, revision: dict[str, Any], object_tag: str,
                         expected_revision: int, expected_digest: str, writer_authority=None, current_authority=None, task_ref=None) -> dict[str, Any]:
    """Record a proven Files/History resource inside an existing task-bound set."""
    if (current_authority is None or not callable(writer_authority) or role not in {"metaplan", "ledger", "concern", "checkpoint", "progress", "slice"}
            or not isinstance(artifact_set_id, str) or not re.fullmatch(r"artifact-set-[0-9a-f]{64}", artifact_set_id)
            or not isinstance(resource_id, str) or not re.fullmatch(r"resource-[0-9a-f]{64}", resource_id)
            or not _workflow_revision_valid(revision) or not _workflow_object_tag_valid(object_tag)
            or not isinstance(history_resource_id, str) or not history_resource_id.startswith("file:")
            or len(history_resource_id.encode("utf-8")) > 512 or any(ord(ch) < 32 for ch in history_resource_id)
            or type(expected_revision) is not int or expected_revision < 1
            or not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)):
        raise ValueError("workflow artifact member unavailable")
    if role == "progress":
        if not isinstance(name,str) or not re.fullmatch(r"task-[a-f0-9]{64}",name):
            raise ValueError("workflow artifact member unavailable")
    elif role in {"concern", "slice"}:
        if not isinstance(name, str) or len(name) > 60 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError("workflow artifact member unavailable")
    elif name is not None:
        raise ValueError("workflow artifact member unavailable")
    try:
        path = (canonical_plan_path(relative_path, allow_legacy=False) if role in {"metaplan", "slice"}
                else canonical_robonote_path(relative_path, allow_legacy=False))
    except ClankerPathError as exc:
        raise ValueError("workflow artifact member unavailable") from exc
    if not path.endswith(".md"):
        raise ValueError("workflow artifact member unavailable")
    if role == "progress":
        if (type(task_ref) is not dict or set(task_ref)!={"kind","sessionID","taskID","assignmentEventID"}
                or task_ref.get("kind")!="native-task" or not isinstance(task_ref.get("sessionID"),str) or not task_ref["sessionID"]
                or not isinstance(task_ref.get("taskID"),str) or not re.fullmatch(r"T[0-9]+(?:\.[0-9]+)*",task_ref["taskID"])
                or type(task_ref.get("assignmentEventID")) is not int or not 0<task_ref["assignmentEventID"]<=9007199254740991):
            raise ValueError("workflow task reference unavailable")
        material=["openclank.workflow.task-ref.v1",task_ref["sessionID"],task_ref["taskID"],task_ref["assignmentEventID"]]
        if name != "task-"+hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(",", ":")).encode()).hexdigest():
            raise ValueError("workflow task reference changed")
    elif task_ref is not None: raise ValueError("workflow task reference unavailable")
    key = role if name is None else role + ":" + name
    member = {"role":role,"name":name,"relative_path":path,"resource_id":resource_id,"history_resource_id":history_resource_id,"revision":dict(revision),"object_tag":object_tag}
    if task_ref is not None: member["task_ref"]=dict(task_ref)
    result = []
    def apply(plan):
        assoc = plan.get("artifact_association")
        if (plan.get("status") == "cleared" or plan.get("revision") != expected_revision or plan.get("digest") != expected_digest
                or not isinstance(assoc,dict) or assoc.get("artifact_set_id") != artifact_set_id
                or assoc.get("plan_revision") != expected_revision or assoc.get("plan_digest") != expected_digest
                or (role == "metaplan" and assoc.get("metaplan_resource_id") != resource_id) or writer_authority() is not True):
            raise ValueError("workflow artifact association changed")
        members = dict(assoc.get("members") or {})
        if role == "metaplan":
            match = re.fullmatch(r"\.clanker/futures/([a-z0-9]+(?:-[a-z0-9]+)*)\.md", path)
            if not match or len(match.group(1)) > 60 or (plan.get("artifact_relpath") and plan["artifact_relpath"] != path):
                raise ValueError("workflow metaplan path unavailable")
        else:
            meta = members.get("metaplan")
            match = re.fullmatch(r"\.clanker/futures/([a-z0-9]+(?:-[a-z0-9]+)*)\.md", str(meta.get("relative_path") or "")) if isinstance(meta, dict) else None
            if not match: raise ValueError("workflow metaplan identity unavailable")
            if path != workflow_member_path(str(meta["relative_path"]), role, name):
                raise ValueError("workflow artifact path unavailable")
        if key in members and members[key] != member:
            raise ValueError("workflow artifact member identity changed")
        if any(other != key and (value.get("relative_path") == path or value.get("resource_id") == resource_id
                or value.get("history_resource_id") == history_resource_id) for other,value in members.items() if isinstance(value,dict)):
            raise ValueError("workflow artifact member identity conflict")
        members[key] = member
        assoc = dict(assoc); assoc["members"] = members; plan["artifact_association"] = assoc
        result.append(dict(member)); return plan
    mutate_plan_state(session_id, owner, apply, current_authority=current_authority)
    return result[0]


def clear_plan(session_id: str, owner: str, *, current_authority=None) -> dict[str, Any]:
    if current_authority is None:
        raise ValueError("original plan account unavailable")
    def apply(plan):
        plan.update(plan="", todos=[], revision=int(plan.get("revision") or 0)+1, digest="", approved_revision=None, approved_digest=None, status="cleared")
        plan.pop("artifact_association", None)
        plan.pop("artifact_set", None)
        return plan
    return mutate_plan_state(session_id, owner, apply, current_authority=current_authority)
