"""Current exact host-user sources for future memory consumers; no admission job."""
from __future__ import annotations

import re
import hashlib
import json
import asyncio
from dataclasses import asdict
from dataclasses import dataclass
from typing import Optional
from sqlalchemy import text

from src.openclank.chat_source_bridge import verify_current, verify_projection_drop, source_component_state
from src.openclank.conversation_archive import TrustedOriginPart
from src.openclank.workspace_policy_service import resolve_owned_workspace


async def _queued_owned_thread(fn, *args, **kwargs):
    """Do not release the caller's guard until its actual worker finishes."""
    task=asyncio.create_task(asyncio.to_thread(fn,*args,**kwargs));cancelled=False
    while not task.done():
        try: await asyncio.shield(task)
        except asyncio.CancelledError: cancelled=True
    result=task.result()
    if cancelled: raise asyncio.CancelledError
    return result


@dataclass(frozen=True)
class SourceRef:
    version: int
    kind: str
    account_id: str
    chat_id: str
    actor_id: str
    message_id: str
    part_id: str
    revision: int
    content_hash: str
    workspace_id: Optional[str]
    project_id: Optional[str]


@dataclass(frozen=True)
class AssistantInterpretationRef:
    """Private selector only: no source, role or capture authority by itself."""
    version: int
    root: SourceRef
    assistant_message_id: str
    assistant_part_id: str
    assistant_revision: int
    assistant_content_hash: str
    pair_sha256: str


def _assistant_ref_error(ref):
    if type(ref) is not AssistantInterpretationRef or type(ref.version) is not int or ref.version != 1 or _source_ref_error(ref.root):
        return 'invalid_assistant_ref'
    if (type(ref.assistant_message_id) is not str or not ref.assistant_message_id or len(ref.assistant_message_id.encode('utf-8'))>512
        or ref.assistant_message_id==ref.root.message_id or ref.assistant_part_id!='body'
        or type(ref.assistant_revision) is not int or ref.assistant_revision<1
        or type(ref.assistant_content_hash) is not str or not re.fullmatch(r'[0-9a-f]{64}',ref.assistant_content_hash)
        or type(ref.pair_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}',ref.pair_sha256)):
        return 'invalid_assistant_ref'
    return None


def _typed_assistant_component_matches(ref, component):
    from src.openclank.conversation_archive import TrustedAssistantComponent, TrustedOriginPart
    if _assistant_ref_error(ref) or type(component) is not TrustedAssistantComponent:
        return False
    root=ref.root
    return (component.owner == root.account_id and component.chat_id == root.chat_id
        and component.workspace_id == root.workspace_id
        and component.root == TrustedOriginPart(root.message_id,root.part_id,root.revision,root.content_hash)
        and component.assistant == TrustedOriginPart(ref.assistant_message_id,ref.assistant_part_id,ref.assistant_revision,ref.assistant_content_hash)
        and component.pair_sha256 == ref.pair_sha256)


def assistant_interpretation_ref(root, component):
    """Decode a page selector; subsequent guarded hydration supplies authority."""
    if _source_ref_error(root) or type(component) is not dict or set(component)!={'version','kind','owner','chat_id','root','assistant','pair_sha256','workspace_id'}:
        raise ValueError('assistant component unavailable')
    if (type(component.get('version')) is not int or component['version']!=1 or component['kind']!='host_assistant_body'
        or component['owner']!=root.account_id or component['chat_id']!=root.chat_id or component['workspace_id']!=root.workspace_id
        or component['root']!=[root.message_id,root.part_id,root.revision,root.content_hash]):
        raise ValueError('assistant component unavailable')
    assistant=component['assistant']
    if type(assistant) is not list or len(assistant)!=4:
        raise ValueError('assistant component unavailable')
    ref=AssistantInterpretationRef(1,root,assistant[0],assistant[1],assistant[2],assistant[3],component['pair_sha256'])
    if _assistant_ref_error(ref):
        raise ValueError('assistant component unavailable')
    return ref


@dataclass(frozen=True)
class AssistantInterpretationRetirementFact:
    source: AssistantInterpretationRef
    reason: str
    current_revision: Optional[int] = None
    current_hash: Optional[str] = None


@dataclass(frozen=True)
class SourceRetirementFact:
    """Content-free exact source withdrawal, valid only at its guarded read."""
    source: SourceRef
    reason: str
    current_revision: Optional[int] = None
    current_hash: Optional[str] = None


def _source_ref_error(ref):
    if type(ref) is not SourceRef or type(ref.version) is not int or ref.version not in (1, 2):
        return 'invalid_source_ref'
    if ref.kind != 'host_user_body' or ref.actor_id != 'main' or ref.part_id != 'body' or ref.project_id is not None:
        return 'unsupported_source'
    if (ref.version == 1 and (type(ref.workspace_id) is not str or not ref.workspace_id or ref.workspace_id != ref.workspace_id.strip() or len(ref.workspace_id) > 512)) or (ref.version == 2 and ref.workspace_id is not None):
        return 'invalid_source_ref'
    identifiers = (ref.account_id, ref.chat_id, ref.message_id)
    if any(type(value) is not str or not value or len(value) > 512 or value != value.strip() for value in identifiers) or type(ref.revision) is not int or ref.revision < 1 or type(ref.content_hash) is not str or not re.fullmatch(r'[0-9a-f]{64}', ref.content_hash):
        return 'invalid_source_ref'
    return None


class CurrentHostUserSource:
    """Trusted host supplies authorities at construction; callers supply only refs.

    Results permit immediate use only. Publication requires fresh authority and
    source checks at the actual mutation owner; no authorization is cached.
    """
    def __init__(self, *, owner, auth_manager, session_factory, archive, workspace_repository, policy_store, guarded_preparation=None):
        self._owner = owner
        self._auth = auth_manager
        self._session_factory = session_factory
        self._archive = archive
        self._workspaces = workspace_repository
        self._policy = policy_store
        self._guarded_preparation = guarded_preparation

    def _authority(self, db, ref, *, manual_review=False):
        account = self._auth.account_id(self._owner)
        if not account or str(account) != ref.account_id or self._archive.resolve_owner(self._owner) != ref.account_id:
            return False
        row = db.execute(text('SELECT s.owner,s.workspace_id FROM sessions s WHERE s.id=:chat'), {'chat': ref.chat_id}).first()
        if not row or self._auth.account_id(row[0]) != account or row[1] != ref.workspace_id:
            return False
        if ref.version == 2:
            from src.memery_database import GuardedStorePreparation
            if type(self._guarded_preparation) is not GuardedStorePreparation:
                return False
            try: self._guarded_preparation.assert_account_chat()
            except Exception: return False
        else:
            binding = resolve_owned_workspace(self._workspaces, workspace_id=ref.workspace_id, owner_username=self._owner,
                                              auth_manager=self._auth, purpose='app_folder', observation_only=True)
            if binding.workspace.id != ref.workspace_id:
                return False
        privileges = self._auth.get_privileges(self._owner)
        allowed = privileges.get('can_manage_memory', True) if isinstance(privileges, dict) else False
        if type(allowed) is not bool or not allowed:
            return False
        if manual_review:
            return True
        snapshot = self._policy.effective_snapshot(self._owner, read_privilege=allowed, write_privilege=allowed,
                                                   incognito=False, no_memory=False, compare_mode=False)
        return snapshot.get('eligibility_state') == 'available' and snapshot.get('capture_allowed') is True

    def hydrate(self, ref):
        return self._hydrate(ref, manual_review=False)

    def verify_feedback_origin(self, ref):
        """Check a committed live user body for observation, without returning it.

        This does not grant capture or publication authority. Callers must hold
        the host chat guard and recheck at the event's fixed-target commit.
        """
        if _source_ref_error(ref) or ref.revision != 1:
            return {'state': 'unavailable', 'reason': 'source_unavailable'}
        db = self._session_factory()
        try:
            if not self._authority(db, ref, manual_review=True):
                raise ValueError('authority unavailable')
            row = db.execute(text("SELECT origin_json,origin_state FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"),
                {'owner': ref.account_id, 'chat': ref.chat_id, 'message': ref.message_id}).first()
            if row and row[1] == 'vetoed':
                return {'state': 'denied', 'reason': 'privacy_veto'}
            if not row or row[1] not in ('pending', 'delivered', 'projection_user_dropped', 'projection_assistant_dropped', 'projection_both_dropped'):
                raise ValueError('origin unavailable')
            admission = json.loads(row[0])
            choices = json.loads(admission['choices_json'])
            if choices.get('incognito') is True or choices.get('noMemory') is True:
                return {'state': 'denied', 'reason': 'privacy_denied'}
            if (admission.get('owner') != ref.account_id or admission.get('chat_id') != ref.chat_id
                    or admission.get('root_operation_id') != ref.message_id
                    or choices.get('incognito') is not False or choices.get('noMemory') is not False):
                raise ValueError('origin privacy unavailable')
            part = TrustedOriginPart(ref.message_id, ref.part_id, ref.revision, ref.content_hash)
            if not verify_current(db, self._archive, ref.account_id, ref.chat_id, ref.actor_id, part):
                raise ValueError('current source unavailable')
            component = source_component_state(db, self._archive, owner=ref.account_id, chat_id=ref.chat_id,
                message_id=ref.message_id, content_hash=ref.content_hash)
            if component == 'projection_dropped':
                if not self._archive.verify_feedback_projection_drop(owner=ref.account_id, chat_id=ref.chat_id,
                    part=part, workspace_id=ref.workspace_id, project_id=ref.project_id,
                    verify_host_drop=lambda account, chat, actor, item: verify_projection_drop(db,self._archive,account,chat,actor,item)):
                    raise ValueError('projection source unavailable')
            elif component != 'current':
                raise ValueError('current source unavailable')
            from src.openclank.memory_policy import MemoryPolicyStore
            policy_read = self._policy.read_eligibility if type(self._policy) is MemoryPolicyStore else self._policy.effective_snapshot
            snapshot = policy_read(self._owner, read_privilege=True, write_privilege=False,
                incognito=False, no_memory=False, compare_mode=False)
            if snapshot.get('eligibility_state') != 'available' or snapshot.get('read_allowed') is not True:
                raise ValueError('read authority unavailable')
            if not self._authority(db, ref, manual_review=True):
                raise ValueError('authority changed')
            return {'state': 'available', 'source': ref}
        except Exception:
            return {'state': 'unavailable', 'reason': 'feedback_origin_unavailable'}
        finally:
            db.close()

    def hydrate_for_manual_review(self, ref):
        """Check exact source/privacy and current owner authority, not auto-capture settings."""
        result = self._hydrate(ref, manual_review=True)
        if result.get('state') != 'available' or ref.revision != 1:
            return {'state':'unavailable','reason':'source_unavailable'}
        from src.openclank.chat_source_bridge import verify_origin_assistant_component
        db = self._session_factory()
        try:
            if not verify_origin_assistant_component(db,self._archive,owner=ref.account_id,chat_id=ref.chat_id,message_id=ref.message_id,content_hash=ref.content_hash):
                return {'state':'unavailable','reason':'completion_unavailable'}
            if not self._authority(db,ref,manual_review=True):
                return {'state':'unavailable','reason':'authority_unavailable'}
            return result
        except Exception:
            return {'state':'unavailable','reason':'completion_unavailable'}
        finally:
            db.close()

    def _assistant_pair(self, root, *, lifecycle_owner, guard, expected=None, create=False, hydrate=False):
        """Internal original-root association under its already-held lifecycle guard."""
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner, assistant_component
        from src.openclank.conversation_archive import TrustedAssistantComponent
        if _source_ref_error(root) or type(lifecycle_owner) is not CompletedTurnLifecycleOwner or lifecycle_owner.source_authority is not self or not self._archive.assistant_component_profile_ready():
            raise ValueError('assistant component authority unavailable')
        if expected is not None and (_assistant_ref_error(expected) or expected.root != root):
            raise ValueError('assistant component unavailable')
        lifecycle=lifecycle_owner.for_source(source=root,guard=guard)
        def current():
            try:
                lifecycle.assert_current(source=root,provider=lifecycle_owner.provider,profile=lifecycle_owner.profile,
                    target_authority=lifecycle_owner.target_authority,guard=guard)
                privileges=self._auth.get_privileges(self._owner)
                allowed=privileges.get('can_manage_memory',True) if isinstance(privileges,dict) else False
                if type(allowed) is not bool or not allowed or self._auth.account_id(self._owner)!=root.account_id:
                    return False
                policy=self._policy.effective_snapshot(self._owner,read_privilege=allowed,write_privilege=allowed,
                    incognito=False,no_memory=False,compare_mode=False)
                return policy.get('eligibility_state')=='available' and policy.get('capture_allowed') is True
            except Exception:
                return False
        check_db=self._session_factory()
        try:
            if self._authority(check_db,root) is not True: raise ValueError('assistant authority unavailable')
        finally:
            check_db.close()
        # Provider callbacks hold the same preference authority before taking
        # the Host writer. Defer this optional association if it is busy.
        from routes.prefs_routes import PREFERENCE_LOCK
        if not PREFERENCE_LOCK.try_enter():
            raise ValueError('assistant component authority unavailable')
        try:
            db=self._session_factory()
            try:
                result=assistant_component(db,self._archive,owner=root.account_id,provider_owner=self._owner,
                    chat_id=root.chat_id,message_id=root.message_id,current_authority=current,create=create,hydrate=hydrate)
            finally:
                db.close()
        finally:
            PREFERENCE_LOCK.__exit__(None, None, None)
        component=result[0] if hydrate else result
        if type(component) is not TrustedAssistantComponent or component.root.message_id!=root.message_id or component.root.part_id!=root.part_id or component.root.revision!=root.revision or component.root.content_hash!=root.content_hash or component.workspace_id!=root.workspace_id:
            raise ValueError('assistant component changed')
        ref=AssistantInterpretationRef(1,root,component.assistant.message_id,component.assistant.part_id,
            component.assistant.revision,component.assistant.content_hash,component.pair_sha256)
        if _assistant_ref_error(ref) or (expected is not None and expected!=ref) or current() is not True:
            raise ValueError('assistant component changed')
        check_db=self._session_factory()
        try:
            if self._authority(check_db,root) is not True: raise ValueError('assistant authority changed')
        finally:
            check_db.close()
        if hydrate:
            return {'state':'available','source':ref,'user_context':result[1],'assistant_interpretation':result[2]}
        return ref

    def associate_assistant_component(self, root, *, lifecycle_owner, guard):
        """Reconcile only this retained root's original assistant delivery."""
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner, _valid_forward_pair, deliver
        if (_source_ref_error(root) or type(lifecycle_owner) is not CompletedTurnLifecycleOwner
                or lifecycle_owner.source_authority is not self or not self._archive.assistant_component_profile_ready()):
            raise ValueError('assistant component authority unavailable')
        lifecycle = lifecycle_owner.for_source(source=root, guard=guard)
        def current():
            lifecycle.assert_current(source=root, provider=lifecycle_owner.provider, profile=lifecycle_owner.profile,
                target_authority=lifecycle_owner.target_authority, guard=guard)
        current()
        db = self._session_factory()
        try:
            if self._authority(db, root) is not True:
                raise ValueError('assistant component authority unavailable')
            row = db.execute(text("SELECT origin_assistant_id, CASE WHEN length(CAST(execution_snapshot_json AS BLOB))<=16384 THEN execution_snapshot_json END FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:root AND part_id='body' AND revision=1 AND content_hash=:hash AND origin_completed=1 AND origin_state='delivered'"),
                {'owner':root.account_id,'chat':root.chat_id,'root':root.message_id,'hash':root.content_hash}).first()
            try:
                snapshot = json.loads(row[1]) if row else None
                pair = snapshot.get('nativePair') if type(snapshot) is dict and snapshot.get('version') == 3 else None
                valid = (_valid_forward_pair(pair) and pair['input']['hostMessageID'] == root.message_id
                    and pair['input']['hostHash'] == root.content_hash and pair['output']['hostMessageID'] == row[0])
            except (ValueError, TypeError, AttributeError):
                valid = False
            if not valid:
                raise ValueError('assistant delivery identity unavailable')
            answer = pair['output']
            # The delivery owner checks its exact pending intent, current Host
            # bytes and revision; no chat-wide retry or new delivery is created.
            current()
            if deliver(db, self._archive, owner=root.account_id, chat_id=root.chat_id,
                    message_id=answer['hostMessageID'], revision=answer['hostRevision']) != 'delivered':
                raise ValueError('assistant delivery unavailable')
            current()
        finally:
            db.close()
        return self._assistant_pair(root,lifecycle_owner=lifecycle_owner,guard=guard,create=True)

    def hydrate_assistant_interpretation(self, ref, *, lifecycle_owner, guard):
        if _assistant_ref_error(ref):
            return {'state':'unavailable','reason':'invalid_assistant_ref'}
        try:
            return self._assistant_pair(ref.root,lifecycle_owner=lifecycle_owner,guard=guard,expected=ref,hydrate=True)
        except Exception:
            return {'state':'unavailable','reason':'assistant_component_unavailable'}

    def hydrate_assistant_interpretation_for_manual_review(self, ref, *, source_guard, provider, current_human):
        """Read an already retained pair under the original current human guard."""
        from src.openclank.chat_source_bridge import _HostChatFenceCapability, assistant_component
        from src.openclank.conversation_archive import TrustedAssistantComponent
        if (_assistant_ref_error(ref) or type(source_guard) is not _HostChatFenceCapability
                or source_guard.provider is not provider or not callable(current_human)):
            return {'state':'unavailable','reason':'assistant_component_unavailable'}
        root = ref.root
        def current():
            try:
                source_guard.validate(provider=provider, owner=self._owner, chat_id=root.chat_id)
                if (current_human() is not True or self._auth.account_id(self._owner) != root.account_id
                        or self._auth.username_for_account_id(root.account_id) != self._owner):
                    return False
                privileges = self._auth.get_privileges(self._owner)
                allowed = privileges.get('can_manage_memory', True) if type(privileges) is dict else False
                if type(allowed) is not bool or not allowed:
                    return False
                policy = self._policy.effective_snapshot(self._owner, read_privilege=allowed, write_privilege=allowed,
                    incognito=False, no_memory=False, compare_mode=False)
                return policy.get('eligibility_state') == 'available' and policy.get('read_allowed') is True
            except Exception:
                return False
        try:
            if current() is not True or self.hydrate_for_manual_review(root).get('state') != 'available':
                raise ValueError('manual source unavailable')
            check_db = self._session_factory()
            try:
                if self._authority(check_db, root, manual_review=True) is not True:
                    raise ValueError('manual source authority unavailable')
            finally:
                check_db.close()
            from routes.prefs_routes import PREFERENCE_LOCK
            if not PREFERENCE_LOCK.try_enter():
                raise ValueError('manual source unavailable')
            try:
                db = self._session_factory()
                try:
                    result = assistant_component(db, self._archive, owner=root.account_id, provider_owner=self._owner,
                        chat_id=root.chat_id, message_id=root.message_id, current_authority=current, hydrate=True)
                finally:
                    db.close()
            finally:
                PREFERENCE_LOCK.__exit__(None, None, None)
            component, user_context, interpretation = result
            if (type(component) is not TrustedAssistantComponent
                    or not _typed_assistant_component_matches(ref, component) or current() is not True):
                raise ValueError('manual source changed')
            check_db = self._session_factory()
            try:
                if self._authority(check_db, root, manual_review=True) is not True or current() is not True:
                    raise ValueError('manual source authority changed')
            finally:
                check_db.close()
            return {'state':'available','source':ref,'user_context':user_context,'assistant_interpretation':interpretation}
        except Exception:
            return {'state':'unavailable','reason':'assistant_component_unavailable'}

    def assistant_interpretation_retirement(self, ref, *, lifecycle_owner, guard):
        """Return only positively proved root/assistant withdrawal, never inferred absence."""
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner, digest, origin_available
        from src.openclank.conversation_archive import TrustedAssistantComponent, TrustedOriginPart
        if _assistant_ref_error(ref) or type(lifecycle_owner) is not CompletedTurnLifecycleOwner or lifecycle_owner.source_authority is not self:
            return {'state':'unavailable','reason':'assistant_component_unavailable'}
        root=ref.root
        def current():
            try:
                lifecycle_owner._assert_owner_source(source=root,guard=guard)
                return self._auth.account_id(self._owner)==root.account_id and self._archive.resolve_owner(self._owner)==root.account_id
            except Exception:
                return False
        try:
            if current() is not True:
                return {'state':'unavailable','reason':'authority_unavailable'}
            component=TrustedAssistantComponent(root.account_id,root.chat_id,
                TrustedOriginPart(root.message_id,root.part_id,root.revision,root.content_hash),
                TrustedOriginPart(ref.assistant_message_id,ref.assistant_part_id,ref.assistant_revision,ref.assistant_content_hash),
                ref.pair_sha256,root.workspace_id)
            self._archive.recorded_assistant_component(component,authorize=lambda account,chat: account==root.account_id and chat==root.chat_id and current() is True)
            root_state=self.retirement(root)
            if root_state.get('state')=='retired' and root_state['fact'].reason!='completion_withdrawn':
                fact=root_state['fact']
                if current() is not True: raise ValueError('authority changed')
                return {'state':'retired','fact':AssistantInterpretationRetirementFact(ref,'root_'+fact.reason,fact.current_revision,fact.current_hash)}
            db=self._session_factory()
            try:
                if not origin_available(db): raise ValueError('origin unavailable')
                row=db.execute(text("SELECT i.origin_state,i.content_hash,i.origin_assistant_id,s.owner,s.workspace_id,a.role,CASE WHEN length(CAST(a.content AS BLOB))<=8192 THEN a.content END,a.source_body_revision,a.source_body_hash FROM host_source_delivery_intent i JOIN sessions s ON s.id=i.chat_id LEFT JOIN chat_messages a ON a.id=i.origin_assistant_id AND a.session_id=i.chat_id WHERE i.owner=:owner AND i.chat_id=:chat AND i.message_id=:root AND i.part_id='body' AND i.revision=1"),{'owner':root.account_id,'chat':root.chat_id,'root':root.message_id}).first()
                if (not row or row[1]!=root.content_hash or row[2]!=ref.assistant_message_id or self._auth.account_id(row[3])!=root.account_id or row[4]!=root.workspace_id):
                    raise ValueError('original association unavailable')
                if row[0]=='completion_withdrawn':
                    result={'state':'retired','fact':AssistantInterpretationRetirementFact(ref,'assistant_withdrawn')}
                elif root_state.get('state')=='current' and row[0]=='delivered' and row[5]=='assistant' and type(row[6]) is str and type(row[7]) is int and type(row[8]) is str and digest(row[6])==row[8]:
                    if row[7]==ref.assistant_revision and row[8]==ref.assistant_content_hash:
                        result={'state':'current'}
                    elif row[7]>ref.assistant_revision:
                        result={'state':'retired','fact':AssistantInterpretationRetirementFact(ref,'assistant_superseded',row[7],row[8])}
                    else:
                        raise ValueError('assistant identity unavailable')
                else:
                    raise ValueError('assistant identity unavailable')
            finally:
                db.close()
            if current() is not True:
                raise ValueError('authority changed')
            return result
        except Exception:
            return {'state':'unavailable','reason':'assistant_component_unavailable'}

    def _hydrate(self, ref, *, manual_review):
        error = _source_ref_error(ref)
        if error:
            return {'state': 'unavailable', 'reason': error}
        db = self._session_factory()
        try:
            if not self._authority(db, ref, manual_review=manual_review):
                return {'state': 'unavailable', 'reason': 'authority_unavailable'}
            part = TrustedOriginPart(ref.message_id, ref.part_id, ref.revision, ref.content_hash)
            source = self._archive.hydrate_origin_part(owner=ref.account_id, chat_id=ref.chat_id, actor_id=ref.actor_id, part=part,
                authorize=lambda account, chat: account == ref.account_id and chat == ref.chat_id,
                verify_host_source=lambda account, chat, actor, item: verify_current(db, self._archive, account, chat, actor, item),
                verify_projection_drop=lambda account, chat, actor, item: verify_projection_drop(db, self._archive, account, chat, actor, item))
            if (source.runtime_generation != '0' or source.role != 'user' or source.part_type != 'text' or
                source.event_workspace != ref.workspace_id or source.event_project != ref.project_id or
                not isinstance(source.content, str) or len(source.content.encode('utf-8')) > 65536):
                return {'state': 'unavailable', 'reason': 'source_unavailable'}
            if not self._authority(db, ref, manual_review=manual_review) or not verify_current(db, self._archive, ref.account_id, ref.chat_id, ref.actor_id, part):
                return {'state': 'unavailable', 'reason': 'authority_unavailable'}
            return {'state': 'available', 'content': source.content, 'source': ref}
        except Exception:
            return {'state': 'unavailable', 'reason': 'source_unavailable'}
        finally:
            db.close()

    def retirement(self, ref):
        """Read definitive withdrawal only; caller must hold the exact chat guard."""
        from src.openclank.chat_source_bridge import digest, origin_available
        error = _source_ref_error(ref)
        if error:
            return {'state':'unavailable', 'reason':error}
        try:
            if ref.version == 2:
                from src.memery_database import GuardedStorePreparation
                if type(self._guarded_preparation) is not GuardedStorePreparation:
                    return {'state':'unavailable', 'reason':'authority_unavailable'}
                self._guarded_preparation.assert_account_chat()
            if (self._auth.account_id(self._owner) != ref.account_id
                or self._auth.username_for_account_id(ref.account_id) != self._owner
                or self._archive.resolve_owner(self._owner) != ref.account_id):
                return {'state':'unavailable', 'reason':'authority_unavailable'}
            if self._archive.chat_erasure_started(owner=ref.account_id, chat_id=ref.chat_id):
                return {'state':'retired', 'fact':SourceRetirementFact(ref, 'chat_erasure_started')}
            db = self._session_factory()
            try:
                if not origin_available(db):
                    return {'state':'unavailable', 'reason':'source_proof_unavailable'}
                row = db.execute(text("SELECT i.content_hash,root.origin_state,m.content,m.source_body_revision,m.source_body_hash,s.owner,s.workspace_id FROM host_source_delivery_intent i JOIN sessions s ON s.id=i.chat_id LEFT JOIN host_source_delivery_intent root ON root.owner=i.owner AND root.chat_id=i.chat_id AND root.message_id=i.message_id AND root.part_id='body' AND root.revision=1 LEFT JOIN chat_messages m ON m.id=i.message_id AND m.session_id=i.chat_id WHERE i.owner=:owner AND i.chat_id=:chat AND i.message_id=:message AND i.part_id='body' AND i.revision=:revision"), {'owner':ref.account_id,'chat':ref.chat_id,'message':ref.message_id,'revision':ref.revision}).first()
                if (not row or row[0] != ref.content_hash or self._auth.account_id(row[5]) != ref.account_id or row[6] != ref.workspace_id):
                    return {'state':'unavailable', 'reason':'source_proof_unavailable'}
                reasons = {'vetoed':'privacy_veto', 'deleted':'deleted', 'completion_withdrawn':'completion_withdrawn', 'reset_retired':'reset_retired'}
                if row[1] in reasons:
                    return {'state':'retired', 'fact':SourceRetirementFact(ref, reasons[row[1]])}
                if (type(row[3]) is int and row[3] > ref.revision and type(row[2]) is str and
                    type(row[4]) is str and digest(row[2]) == row[4]):
                    return {'state':'retired', 'fact':SourceRetirementFact(ref, 'superseded', row[3], row[4])}
                if (row[3] == ref.revision and type(row[3]) is int and row[4] == ref.content_hash
                    and type(row[2]) is str and digest(row[2]) == ref.content_hash):
                    return {'state':'current'}
                if (ref.revision == 1 and source_component_state(db,self._archive,owner=ref.account_id,
                        chat_id=ref.chat_id,message_id=ref.message_id,content_hash=ref.content_hash) == 'projection_dropped'):
                    return {'state':'current'}
                return {'state':'unavailable', 'reason':'source_proof_unavailable'}
            finally:
                db.close()
        except Exception:
            return {'state':'unavailable', 'reason':'source_proof_unavailable'}

    def execution_receipt(self, ref):
        """Read only the exact current authorized host-origin execution receipt."""
        from src.openclank.chat_source_bridge import read_execution_snapshot
        current = self.hydrate(ref)
        if current.get('state') != 'available' or ref.revision != 1:
            return {'state': 'unavailable', 'reason': 'source_unavailable'}
        db = self._session_factory()
        try:
            part = TrustedOriginPart(ref.message_id, ref.part_id, ref.revision, ref.content_hash)
            if not self._authority(db, ref) or not verify_current(db, self._archive, ref.account_id, ref.chat_id, ref.actor_id, part):
                return {'state': 'unavailable', 'reason': 'authority_unavailable'}
            receipt = read_execution_snapshot(db, self._archive, owner=ref.account_id, chat_id=ref.chat_id, message_id=ref.message_id)
            if (not isinstance(receipt, dict) or receipt.get('rootOperationID') != ref.message_id
                or receipt.get('actorID') != ref.actor_id or not self._authority(db, ref)
                or not verify_current(db, self._archive, ref.account_id, ref.chat_id, ref.actor_id, part)):
                return {'state': 'unavailable', 'reason': 'execution_receipt_unavailable'}
            return {'state': 'available', 'receipt': receipt}
        except Exception:
            return {'state': 'unavailable', 'reason': 'execution_receipt_unavailable'}
        finally:
            db.close()

    def authority_evidence(self, ref, target):
        """Mint nonsecret current-source evidence; no model input is persisted."""
        from src.openclank.memory_job_admission import TargetBinding
        if type(ref) is not SourceRef or type(target) is not TargetBinding or target.source_account_id != ref.account_id:
            raise ValueError('source authority unavailable')
        if self.hydrate(ref).get('state') != 'available':
            raise ValueError('source authority unavailable')
        db = self._session_factory()
        try:
            part = TrustedOriginPart(ref.message_id, ref.part_id, ref.revision, ref.content_hash)
            if ref.revision != 1 or not self._authority(db, ref) or not verify_current(db, self._archive, ref.account_id, ref.chat_id, ref.actor_id, part):
                raise ValueError('source authority unavailable')
            row = db.execute(text("SELECT i.origin_json,i.origin_assistant_id FROM host_source_delivery_intent i JOIN chat_messages a ON a.id=i.origin_assistant_id AND a.session_id=i.chat_id AND a.role='assistant' WHERE i.owner=:owner AND i.chat_id=:chat AND i.message_id=:message AND i.part_id='body' AND i.revision=1 AND i.origin_completed=1 AND i.origin_state IN ('pending','delivered')"), {'owner':ref.account_id, 'chat':ref.chat_id, 'message':ref.message_id}).first()
            if not row or not row[0] or not row[1]:
                raise ValueError('source completion unavailable')
            origin = json.loads(row[0]); choices = json.loads(origin.get('choices_json') or '{}')
            if (origin.get('owner') != ref.account_id or origin.get('chat_id') != ref.chat_id or origin.get('root_operation_id') != ref.message_id
                or origin.get('capture_eligible') is not True or any(choices.get(k) is not False for k in ('incognito','noMemory','compareMode'))
                or not re.fullmatch(r'[0-9a-f]{64}', str(origin.get('policy_fingerprint') or ''))):
                raise ValueError('source privacy unavailable')
            archived = self._archive.read_origin_evidence(owner=ref.account_id, chat_id=ref.chat_id, actor_id=ref.actor_id,
                part=part, authorize=lambda account, chat: account == ref.account_id and chat == ref.chat_id,
                verify_host_source=lambda account, chat, actor, item: verify_current(db, self._archive, account, chat, actor, item),
                verify_projection_drop=lambda account, chat, actor, item: verify_projection_drop(db, self._archive, account, chat, actor, item))
            admission, completion = archived.admission, archived.completion
            if ((admission.owner, admission.chat_id, admission.actor_id, admission.admission_id, admission.root_operation_id)
                    != (ref.account_id, ref.chat_id, ref.actor_id, ref.message_id, ref.message_id)
                or admission.incognito is not False or admission.no_memory is not False or admission.compare_mode is not False
                or admission.capture_eligible is not True or admission.policy_source != 'trusted_fingerprint'
                or admission.policy_value != origin['policy_fingerprint']
                or (completion.owner, completion.chat_id, completion.actor_id, completion.admission_id, completion.outcome)
                    != (ref.account_id, ref.chat_id, ref.actor_id, ref.message_id, 'completed')
                or completion.parts != (part,) or archived.workspace_id != ref.workspace_id or archived.project_id != ref.project_id):
                raise ValueError('source origin evidence changed')
            workspace = None
            if ref.version == 1:
                workspace = resolve_owned_workspace(self._workspaces, workspace_id=ref.workspace_id, owner_username=self._owner,
                    auth_manager=self._auth, purpose='app_folder', observation_only=True).workspace
                if (workspace.id != ref.workspace_id or workspace.owner_subject_id != ref.account_id
                    or type(workspace.revision) is not int or type(workspace.generation) is not int):
                    raise ValueError('source workspace unavailable')
            privilege = self._auth.get_privileges(self._owner)
            allowed = privilege.get('can_manage_memory', True) if isinstance(privilege, dict) else False
            if allowed is not True or self._auth.account_id(self._owner) != ref.account_id:
                raise ValueError('source privilege unavailable')
            proof = {'version':ref.version, 'kind':'source_authority.v'+str(ref.version), 'source':asdict(ref), 'principal':self._owner,
                'admissionID':ref.message_id, 'assistantID':row[1], 'originPolicyFingerprint':origin['policy_fingerprint'],
                'originChoices':{'incognito':False,'noMemory':False,'compareMode':False}, 'captureEligible':True,
                'memoryPrivilege':True, 'workspaceRevision':workspace.revision if workspace else None, 'workspaceGeneration':workspace.generation if workspace else None,
                'target':asdict(target)}
            current_workspace = (resolve_owned_workspace(self._workspaces, workspace_id=ref.workspace_id, owner_username=self._owner,
                auth_manager=self._auth, purpose='app_folder', observation_only=True).workspace if ref.version == 1 else None)
            if ((ref.version == 1 and (current_workspace.id,current_workspace.owner_subject_id,current_workspace.revision,current_workspace.generation)
                    != (workspace.id,workspace.owner_subject_id,workspace.revision,workspace.generation))
                or not self._authority(db, ref) or not verify_current(db, self._archive, ref.account_id, ref.chat_id, ref.actor_id, part)):
                raise ValueError('source authority changed')
            encoded = json.dumps(proof, sort_keys=True, separators=(',',':'), ensure_ascii=False, allow_nan=False).encode()
            return {'evidence':proof, 'fingerprint':hashlib.sha256(encoded).hexdigest()}
        finally:
            db.close()


class CurrentQueuedSource:
    """Exact guarded queued-origin withdrawal; no capture or publication grant."""
    def __init__(self, *, owner, auth_manager, session_factory, archive, workspace_repository,
                 mapping, target_authority, provider, bridge, worker):
        from src.openclank.session_map import OwnerSessionMap
        from src.openclank.memory_job_admission import TargetAuthority
        if type(mapping) is not OwnerSessionMap or type(target_authority) is not TargetAuthority:
            raise ValueError('queued source authority unavailable')
        self._owner=owner; self._auth=auth_manager; self._session_factory=session_factory
        self._archive=archive; self._workspaces=workspace_repository; self._mapping=mapping
        self._target=target_authority; self._provider=provider; self._bridge=bridge; self._worker=worker

    def _current_source_verifier(self, source, *, guard, preimage, producer_epoch, reset_lease=None):
        """Build the shared exact current-source check under the held chat guard."""
        from src.openclank.queued_source import QueuedSourceRef, match_queued_boundary
        from src.openclank.managed_protocol import validate_engine_method_result
        if type(source) is not QueuedSourceRef or type(preimage) is not dict or not callable(producer_epoch):
            raise ValueError('queued source unavailable')
        fields={'version','owner','accountID','stableChatID','sessionID','requestID','messageID','partID',
                'receiptSHA256','messageSHA256','partSHA256','enrollmentSHA256'}
        if (set(preimage)!=fields or type(preimage['version']) is not int or preimage['version']!=1
            or any(type(preimage[k]) is not str or re.fullmatch(r'[0-9a-f]{64}',preimage[k]) is None
                   for k in ('receiptSHA256','messageSHA256','partSHA256','enrollmentSHA256'))
            or any(preimage[k]!=v for k,v in {'owner':self._owner,'accountID':source.account_id,'stableChatID':source.chat_id,
                'sessionID':source.native_session_id,'requestID':source.request_id,'messageID':source.message_id,'partID':source.part_id}.items())):
            raise ValueError('queued preimage unavailable')
        self._mapping.validate_chat_guard(source.chat_id,guard)
        def resolve():
            return (self._target.resolve(source.account_id) if reset_lease is None else
                self._target._resolve_queued_reset(source.account_id,lease=reset_lease,claim=reset_lease.claim))
        target=resolve()
        holder=getattr(self._bridge,'_managed_provider_generation',None)
        generation=getattr(self._worker,'installed_generation',None);epoch=producer_epoch()
        if (not isinstance(holder,str) or not holder or type(generation) is not int or generation<0 or type(epoch) is not int or epoch<0
            or getattr(self._worker,'_bridge',None) is not self._bridge
            or getattr(getattr(self._worker,'_managed_callbacks',None),'holder_id',None)!=holder):
            raise ValueError('queued native holder unavailable')
        producer={'version':1,'holderSHA256':hashlib.sha256(holder.encode('utf-8')).hexdigest(),'generation':generation,'ownerEpoch':epoch}
        def current():
            try:
                if reset_lease is not None: self._target._supervisor.assert_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                self._mapping.validate_chat_guard(source.chat_id,guard)
                if (self._auth.account_id(self._owner)!=source.account_id
                    or self._auth.username_for_account_id(source.account_id)!=self._owner
                    or self._archive.resolve_owner(self._owner)!=source.account_id
                    or resolve()!=target
                    or getattr(self._worker,'_bridge',None) is not self._bridge
                    or getattr(self._bridge,'_managed_provider_generation',None)!=holder
                    or getattr(self._worker,'installed_generation',None)!=generation or producer_epoch()!=epoch
                    or getattr(getattr(self._worker,'_managed_callbacks',None),'holder_id',None)!=holder): return False
                db=self._session_factory()
                try:
                    row=db.execute(text('SELECT s.owner,s.workspace_id FROM sessions s WHERE s.id=:chat'),{'chat':source.chat_id}).first()
                    if not row or self._auth.account_id(row[0])!=source.account_id or row[1]!=source.workspace_id: return False
                    binding=resolve_owned_workspace(self._workspaces,workspace_id=source.workspace_id,owner_username=self._owner,
                        auth_manager=self._auth,purpose='app_folder',observation_only=True)
                    return binding.workspace.id==source.workspace_id
                finally: db.close()
            except Exception: return False
        async def verify():
            async def check():
                if reset_lease is None: return current()
                pool=self._target._supervisor
                await pool.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                valid=await _queued_owned_thread(current)
                await pool.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                return valid
            async def archive_read():
                if reset_lease is None: return self._archive.verify_current_queued_source(source,current=current)
                await self._target._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                value=await _queued_owned_thread(self._archive.verify_current_queued_source,source,current=current)
                await self._target._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                return value
            if not await check(): raise ValueError('queued source authority changed')
            archived=await archive_read()
            if archived.get('producer')!=producer:raise ValueError('queued source producer unavailable')
            binding=await self._bridge._handle_session_binding_read({'sessionID':source.native_session_id})
            if any(binding.get(k)!=v for k,v in archived['binding'].items()) or not await check(): raise ValueError('queued binding changed')
            method='_openclank/session/v1/queued-source/read'
            value=await self._worker.managed_engine_call(method,{'owner':self._owner,'stableChatID':source.chat_id,
                'sessionID':source.native_session_id,'requestID':source.request_id})
            if not await check(): raise ValueError('queued source authority changed')
            value=validate_engine_method_result(method,value)
            proof=match_queued_boundary(value.get('proof'),owner=self._owner,account_id=source.account_id,
                chat_id=source.chat_id,session_id=source.native_session_id,actor_id='main',workspace_id=source.workspace_id,receipt=value)
            if (not proof or value.get('messageID')!=source.message_id or value.get('partID')!=source.part_id
                or value.get('payloadHash')!=archived['payload_hash'] or value.get('mutationPreimage')!=preimage
                or hashlib.sha256(value['proof'].encode('utf-8')).hexdigest()!=source.ingress_sha256
                or await self._bridge._handle_session_binding_read({'sessionID':source.native_session_id})!=binding
                or not await check()): raise ValueError('queued native source changed')
            await archive_read()
        return verify

    async def fence_for_mutation(self, source, *, guard, preimage, producer_epoch, reset_lease=None):
        """Fence only after the same current-source verifier succeeds on both sides."""
        from dataclasses import asdict
        verify=self._current_source_verifier(source,guard=guard,preimage=preimage,producer_epoch=producer_epoch,reset_lease=reset_lease)
        await verify()
        options={'mapping':self._mapping,'guard':guard,'target_authority':self._target}
        if reset_lease is not None: options['reset_lease']=reset_lease
        receipt=await self._provider._host_queued_origin_fence(source,**options)
        await verify()
        return {'version':1,'source':asdict(source),'preimage':dict(preimage),'fence':receipt}

    def _terminal_source_verifier(self, source, fence, *, guard, evidence, lease_current, reset_lease=None):
        """Build the read-only exact terminal-source and first-stage verifier."""
        from src.openclank.queued_source import QueuedSourceRef, match_queued_boundary
        from src.openclank.managed_protocol import validate_engine_method_result
        if (type(source) is not QueuedSourceRef or type(fence) is not dict or type(evidence) is not tuple
            or len(evidence)!=3 or not callable(lease_current)):
            raise ValueError('queued retirement unavailable')
        self._mapping.validate_chat_guard(source.chat_id,guard)
        def resolve():
            return (self._target.resolve(source.account_id) if reset_lease is None else
                self._target._resolve_queued_reset(source.account_id,lease=reset_lease,claim=reset_lease.claim))
        target=resolve()
        holder=getattr(self._bridge,'_managed_provider_generation',None)
        def current():
            try:
                if reset_lease is not None: self._target._supervisor.assert_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                self._mapping.validate_chat_guard(source.chat_id,guard)
                if (lease_current() is not True or self._auth.account_id(self._owner)!=source.account_id
                    or self._auth.username_for_account_id(source.account_id)!=self._owner
                    or self._archive.resolve_owner(self._owner)!=source.account_id
                    or resolve()!=target
                    or getattr(self._worker,'_bridge',None) is not self._bridge
                    or getattr(self._bridge,'_managed_provider_generation',None)!=holder
                    or getattr(getattr(self._worker,'_managed_callbacks',None),'holder_id',None)!=holder): return False
                db=self._session_factory()
                try:
                    row=db.execute(text('SELECT s.owner,s.workspace_id FROM sessions s WHERE s.id=:chat'),{'chat':source.chat_id}).first()
                    if not row or self._auth.account_id(row[0])!=source.account_id or row[1]!=source.workspace_id: return False
                    binding=resolve_owned_workspace(self._workspaces,workspace_id=source.workspace_id,owner_username=self._owner,
                        auth_manager=self._auth,purpose='app_folder',observation_only=True)
                    return binding.workspace.id==source.workspace_id
                finally: db.close()
            except Exception: return False
        async def verify():
            async def check():
                if reset_lease is None: return current()
                pool=self._target._supervisor
                await pool.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                valid=await _queued_owned_thread(current)
                await pool.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                return valid
            async def archive_read():
                if reset_lease is None: return self._archive.verify_current_queued_source(source,current=current)
                await self._target._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                value=await _queued_owned_thread(self._archive.verify_current_queued_source,source,current=current)
                await self._target._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                return value
            if not await check(): raise ValueError('queued retirement authority changed')
            archived=await archive_read()
            options={'holder_sha256':evidence[0],'generation':evidence[1],'epoch':evidence[2],'current':current}
            if reset_lease is None:
                self._archive.verify_queued_retirement_stage(source,fence,**options)
            else:
                await self._target._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
                await _queued_owned_thread(self._archive.verify_queued_retirement_stage,source,fence,**options)
                await self._target._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
            binding=await self._bridge._handle_session_binding_read({'sessionID':source.native_session_id})
            if any(binding.get(k)!=v for k,v in archived['binding'].items()) or not await check(): raise ValueError('queued binding changed')
            method='_openclank/session/v1/queued-source/read'
            value=validate_engine_method_result(method,await self._worker.managed_engine_call(method,{
                'owner':self._owner,'stableChatID':source.chat_id,'sessionID':source.native_session_id,'requestID':source.request_id}))
            if not await check(): raise ValueError('queued retirement authority changed')
            proof=match_queued_boundary(value.get('proof'),owner=self._owner,account_id=source.account_id,chat_id=source.chat_id,
                session_id=source.native_session_id,actor_id='main',workspace_id=source.workspace_id,receipt=value)
            if (not proof or proof['bodySHA256']!=source.text_sha256 or value.get('messageID')!=source.message_id
                or value.get('partID')!=source.part_id or value.get('payloadHash')!=archived['payload_hash']
                or hashlib.sha256(value['proof'].encode('utf-8')).hexdigest()!=source.ingress_sha256
                or await self._bridge._handle_session_binding_read({'sessionID':source.native_session_id})!=binding
                or not await check()): raise ValueError('queued native source changed')
            await archive_read()
        return verify,current

    async def complete_retirement(self, source, fence, *, guard, evidence, lease_current, reset_lease=None):
        """Complete an already staged, current terminal source."""
        verify,current=self._terminal_source_verifier(source,fence,guard=guard,evidence=evidence,
            lease_current=lease_current,reset_lease=reset_lease)
        await verify()
        options={'mapping':self._mapping,'guard':guard,'target_authority':self._target}
        if reset_lease is not None: options['reset_lease']=reset_lease
        fresh=await self._provider._host_queued_origin_fence(source,**options)
        if any(fresh.get(k)!=fence.get(k) for k in ('version','source_id','origin_id','source','visibility','covered')):
            raise ValueError('queued retirement fence changed')
        if fresh.get('state') not in ('fenced','already_fenced'): raise ValueError('queued retirement fence unavailable')
        await verify()
        if reset_lease is None:
            self._archive.complete_queued_retirement(source,fence,holder_sha256=evidence[0],generation=evidence[1],epoch=evidence[2],current=current)
            if current() is not True: raise ValueError('queued retirement authority changed')
        else:
            await self._target._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
            await _queued_owned_thread(self._archive.complete_queued_retirement,source,fence,holder_sha256=evidence[0],generation=evidence[1],epoch=evidence[2],current=current)
            await self._target._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
