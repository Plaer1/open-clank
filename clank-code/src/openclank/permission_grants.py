"""Legacy GrantStore API backed exclusively by canonical operation bindings."""
from __future__ import annotations
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Optional
import os
from src.openclank.file_policy import FilePolicyRepository

def derive_pattern(raw_input: Optional[dict]) -> str:
    """Grant pattern for a request: file dir for file requests, else '*'."""
    if isinstance(raw_input, dict):
        filepath = raw_input.get("filepath")
        if isinstance(filepath, str) and filepath:
            return os.path.dirname(filepath) or filepath
    return "*"


def grant_scope_for_lifetime(
    lifetime: str,
    *,
    session_id: str = "",
    workspace: str = "",
    workspace_id: str = "",
) -> tuple[str, str, str]:
    """Map the human-facing lifetime to durable matcher dimensions.

    ``once`` is intentionally not persisted. Chat grants bind to one stable
    session, Workspace grants bind across chats to the selected workspace, and
    Always grants have neither dimension. This keeps the labels honest instead
    of storing the old session+workspace combination under “Always”.
    """
    lifetime = str(lifetime or "").strip().lower()
    if lifetime == "chat":
        # A chat-scoped approval without a stable chat identity cannot be
        # safely persisted; callers treat the empty pair as Once.
        if not session_id:
            return "", "", ""
        return str(session_id), "", str(workspace_id or "")
    if lifetime == "workspace":
        # A filesystem path is mutable and can be rebound to a different
        # Location. New Workspace approvals therefore require the stable,
        # owner-scoped policy identity and degrade to Once without it.
        if not workspace_id:
            return "", "", ""
        return "", "", str(workspace_id)
    if lifetime == "always":
        return "", "", ""
    return "", "", ""


class GrantStore:
    """Compatibility adapter. The old permission_grants table is never queried."""
    def __init__(self, db_path: str):
        self._db_path = db_path
        self.repository = FilePolicyRepository(db_path)

    def _subject(self, owner):
        return self.repository.subject_for_username(owner)

    def add(self, permission_type, pattern, *, owner='', session_id='', workspace='', workspace_id='', resource='', expires_at=None):
        from src.openclank.operation_approvals import record_operation_approval
        if workspace:
            raise ValueError('Raw workspace approvals require explicit immutable Workspace resolution')
        lifetime='chat' if session_id else 'workspace' if workspace_id else 'always'
        if not record_operation_approval(owner=owner,permission_type=permission_type,pattern=pattern,resource=resource,
            lifetime=lifetime,session_id=session_id,workspace_id=workspace_id,target_path=pattern if pattern!='*' else '',
            expires_at=expires_at,repository=self.repository):
            raise ValueError('Approval cannot be persisted in current file access scope')

    def match(self, permission_type, filepath=None, *, owner='', session_id='', workspace='', workspace_id='', resource=''):
        from src.openclank.operation_approvals import match_operation_approval
        return match_operation_approval(owner=owner,permission_type=permission_type,filepath=filepath or '',resource=resource,
            session_id=session_id,workspace_id=workspace_id,workspace_path=workspace,repository=self.repository)

    def list_records(self, *, owner):
        sid=self._subject(owner)
        if not sid:
            return []
        records=[]
        for b in self.repository.list_bindings(subject_id=sid,binding_class='operation'):
            detail=self.repository.approval_details(b.id) or {'permission_type':b.operation,'pattern':'[scoped operation]','resource':''}
            records.append(dict(detail,id=b.id,owner=owner,session_id=b.chat_id or '',workspace='',workspace_id=b.workspace_id or '',
                created_at='',expires_at=datetime.fromtimestamp(b.expires_unix_ms/1000,timezone.utc).isoformat() if b.expires_unix_ms else None))
        return records

    def list(self, *, owner=None):
        names=[owner] if owner is not None else {self.repository.username_for_subject(b.subject_id) for b in self.repository.list_bindings(binding_class='operation')}
        return [(r['permission_type'],r['pattern'],r['created_at']) for name in names if name is not None for r in self.list_records(owner=name)]

    def revoke(self, grant_id, *, owner):
        sid=self._subject(owner)
        identifier=str(grant_id)
        if identifier.isdigit():
            from src.openclank.file_policy import deterministic_legacy_id
            identifier=deterministic_legacy_id('binding','unified:grant:'+identifier)
        try:
            b=self.repository.get_binding(identifier)
        except ValueError:
            return False
        if not sid or b.subject_id!=sid or b.binding_class!='operation' or b.status!='active':
            return False
        self.repository.revoke_binding(b.id,actor_subject_id=sid,reason_code='approval_revoked')
        return True

    def remove(self, permission_type, pattern, *, owner=''):
        changed=False
        for record in self.list_records(owner=owner):
            if record['permission_type']==permission_type and record['pattern']==pattern:
                changed=self.revoke(record['id'],owner=owner) or changed
        return changed

    def revoke_scope(self, *, owner, session_id='', workspace='', workspace_id=''):
        if workspace and not workspace_id:
            raise ValueError('Use a stable Workspace ID')
        if not session_id and not workspace_id:
            raise ValueError('A chat or Workspace is required')
        sid=self._subject(owner)
        if not sid:
            return 0
        if workspace_id:
            workspace_id=self.repository.legacy_target('workspace',sid+':'+workspace_id, fallback=workspace_id)
            ws=self.repository.get_workspace(workspace_id)
            if ws.owner_subject_id!=sid:
                raise ValueError('Workspace belongs to another account')
        with self.repository._connect() as conn:
            self.repository._begin(conn)
            condition="lifetime='chat' AND chat_id=?" if session_id else 'workspace_id=?'
            count=conn.execute("SELECT COUNT(*) FROM file_policy_bindings WHERE binding_class='operation' AND subject_id=? AND status='active' AND "+condition,(sid,session_id or workspace_id)).fetchone()[0]
            if not count:
                conn.rollback(); return 0
            generation=self.repository._bump(conn)
            conn.execute("UPDATE file_policy_bindings SET status='revoked',generation=?,revision=revision+1,updated_unix_ms=? WHERE binding_class='operation' AND subject_id=? AND status='active' AND "+condition,(generation,int(time.time()*1000),sid,session_id or workspace_id))
            self.repository._audit(conn,generation=generation,event='reset',actor_subject_id=sid,target_type='approvals',target_id=session_id or workspace_id,reason_code='approval_reset',details={'count':count})
            conn.commit()
        return count

    def preview_agent_reset(self, *, owner, scope, chat_id='', workspace_id='', legacy_workspace='', location_workspace_ids=(), legacy_location_path=''):
        sid=self._subject(owner)
        if not sid:
            return 0
        return self.repository.preview_agent_reset(subject_id=sid,scope=scope,chat_id=chat_id or None,workspace_id=workspace_id or None)['matched']

    def reset_agent_permissions(self, *, owner, scope, chat_id='', workspace_id='', legacy_workspace='', location_workspace_ids=(), legacy_location_path=''):
        sid=self._subject(owner)
        if not sid:
            return 0
        return self.repository.reset_agent_permissions(actor_subject_id=sid,subject_id=sid,scope=scope,chat_id=chat_id or None,workspace_id=workspace_id or None)['matched']

    @staticmethod
    def _inventory_matches(actual, expected):
        return all(actual.get(k)==expected.get(k) for k in ('count','active','revoked','fingerprint'))

    def owner_inventory(self, owner):
        sid=self._subject(owner)
        rows=self.repository.list_bindings(subject_id=sid,binding_class='operation',include_inactive=True) if sid else []
        material=[(b.id,b.status,b.revision,b.location_id,b.workspace_id,b.chat_id,b.resource_ref,b.operation,b.capabilities,b.expires_unix_ms) for b in rows]
        review = [item for item in self.repository.migration_state(subject_id=sid, include_inactive=True)['items'] if item['kind']=='grant' and item['status']=='unresolved'] if sid else []
        material.extend((item['id'],item['status'],item['reason'],item['source']) for item in review)
        active=sum(b.status=='active' for b in rows)
        return dict(schema_version=1,owner=str(owner).strip().lower(),subject_id=sid,count=len(rows)+len(review),active=active,revoked=len(rows)+len(review)-active,
            fingerprint='sha256:'+hashlib.sha256(json.dumps(material,separators=(',',':')).encode()).hexdigest(),content_included=False)

    def preview_owner_rename(self, old_owner, new_owner):
        source=self.owner_inventory(old_owner); target=self.owner_inventory(new_owner)
        if target.get('subject_id') or target['count']:
            raise RuntimeError('Target already contains permission state')
        return dict(schema_version=1,source=source,target=target,content_included=False)

    def reconcile_owner_rename(self, old_owner, new_owner, manifest):
        expected=manifest['source']; source=self.owner_inventory(old_owner); target=self.owner_inventory(new_owner)
        expected_sid=expected.get('subject_id') or source.get('subject_id') or target.get('subject_id')
        at_source=self._inventory_matches(source,expected) and not target['count']
        at_target=not source['count'] and self._inventory_matches(target,expected)
        if not at_source and not at_target:
            raise RuntimeError('Permission owner changed after preflight')
        state=self.repository.move_subject_alias(old_owner,new_owner,expected_sid) if expected_sid else 'empty'
        return dict(schema_version=1,state=state,source=self.owner_inventory(old_owner),target=self.owner_inventory(new_owner),content_included=False)

    def compensate_owner_rename(self, old_owner, new_owner, manifest):
        return self.reconcile_owner_rename(new_owner,old_owner,manifest)

    def purge_owner_lifecycle(self, owner, *, expected=None):
        before=self.owner_inventory(owner)
        if expected and before['count'] and not self._inventory_matches(before,expected):
            raise RuntimeError('Permission owner changed before purge')
        sid=self._subject(owner)
        if sid:
            with self.repository._connect() as conn:
                self.repository._begin(conn)
                conn.execute("DELETE FROM file_policy_bindings WHERE subject_id=? AND binding_class='operation'",(sid,))
                conn.execute("UPDATE file_policy_import_items SET status='revoked',reason='account_deleted' WHERE subject_id=? AND kind='grant'",(sid,))
                self.repository._bump(conn); conn.commit()
        return dict(schema_version=1,state='applied',before=before,after=self.owner_inventory(owner),content_included=False)

    def rename_owner(self, old_owner, new_owner):
        self.reconcile_owner_rename(old_owner,new_owner,self.preview_owner_rename(old_owner,new_owner))

    def purge_owner(self, owner):
        self.purge_owner_lifecycle(owner,expected=self.owner_inventory(owner))
