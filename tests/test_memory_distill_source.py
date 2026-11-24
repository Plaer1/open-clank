import hashlib
import json

from src.openclank.conversation_archive import ConversationArchive, SourcePart
from src.openclank.tool_timing_proof import seal_tool_timing


def test_qualified_pattern_is_current_scoped_and_excludes_argument_values(tmp_path):
    archive=ConversationArchive(str(tmp_path/'archive.db'))
    def add(chat, message, part_id, secret):
        part={'type':'tool','id':part_id,'sessionID':'engine-'+chat,'messageID':message,'callID':'call-'+chat,
              'tool':'read','state':{'status':'completed','input':{'path':secret,'private-'+secret:True},
              'output':'PRIVATE-OUTPUT','metadata':{'truncated':False}}}
        content=json.dumps(part,separators=(',',':')); digest=hashlib.sha256(content.encode()).hexdigest()
        evidence={'sessionID':'engine-'+chat,'rootOperationID':'root-'+chat,'actorID':'main','executionRevision':1,
                  'innerTurnID':'turn-'+chat,'messageID':message,'partID':part_id,'callID':'call-'+chat,'contentSHA256':digest}
        source=SourcePart(owner='account',chat_id=chat,actor_id='main',message_id=message,part_id=part_id,role='assistant',part_type='tool',content=content,content_hash=digest)
        archive.append_managed_parts([source],_qualified_tool=seal_tool_timing('account',chat,'synthetic-holder',evidence))
        return dict(account_id='account',chat_id=chat,actor_id='main',message_id=message,part_id=part_id,revision=1,content_hash=digest)
    a=add('chat-a','message-a','part-a','SECRET-A');b=add('chat-b','message-b','part-b','SECRET-B')
    first=archive.get_qualified_tool_pattern(**a); second=archive.get_qualified_tool_pattern(**b)
    assert first and second and first['rootOperationID']=='root-chat-a'
    assert first['toolName']=='read' and first['argumentShapeSHA256']==second['argumentShapeSHA256']
    assert 'SECRET' not in json.dumps([first,second]) and 'PRIVATE-OUTPUT' not in json.dumps([first,second])
    assert archive.get_qualified_tool_pattern(**{**a,'account_id':'other'}) is None
    assert archive.get_qualified_tool_pattern(**{**a,'content_hash':'0'*64}) is None


def test_distill_rechecks_current_sources_before_draft(tmp_path, monkeypatch):
    import asyncio
    import time
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import src.memery_external_sources as sources
    import src.openclank.conversation_archive as archive_module
    import src.openclank.memory_distill_work as work
    import src.teacher_escalation as teacher
    import src.tool_implementations as tools

    archive=ConversationArchive(str(tmp_path/'archive.db'))
    now=int(time.time()*1000); occurrences=[]
    for chat in ('a','b'):
        parts=[]
        for n in range(2):
            message='message-'+chat+str(n); part_id='part-'+chat+str(n)
            part={'type':'tool','id':part_id,'sessionID':'session-'+chat,'messageID':message,'callID':'call-'+chat+str(n),
                  'tool':'read','state':{'status':'completed','input':{'path':'private-'+chat},
                  'output':'private output','metadata':{'truncated':False}}}
            content=json.dumps(part,separators=(',',':')); digest=hashlib.sha256(content.encode()).hexdigest()
            evidence={'sessionID':'session-'+chat,'rootOperationID':'root-'+chat,'actorID':'main','executionRevision':1,
                      'innerTurnID':'turn-'+chat,'messageID':message,'partID':part_id,'callID':'call-'+chat+str(n),
                      'contentSHA256':digest,'wallEndMS':now,'clockID':'clock-'+chat,'startUS':n*100+1,'endUS':n*100+50}
            source=SourcePart(owner='account',chat_id=chat,actor_id='main',message_id=message,part_id=part_id,role='assistant',part_type='tool',content=content,content_hash=digest,event_sequence=n+1)
            archive.append_managed_parts([source],_qualified_tool=seal_tool_timing('account',chat,'synthetic-holder',evidence))
            parts.append({'messageID':message,'partID':part_id,'revision':1,'contentHash':digest})
        occurrences.append({'source':{'version':1,'kind':'host_user_body','account_id':'account','chat_id':chat,'actor_id':'main',
            'message_id':'root-'+chat,'part_id':'body','revision':1,'content_hash':'a'*64,'workspace_id':'source-workspace','project_id':None},'parts':parts})
    class Authority:
        def __init__(self,**kwargs): pass
        def hydrate(self,root): return {'state':'available','content':'A bounded user request'}
        def execution_receipt(self,root):
            return {'state':'available','receipt':{'sessionID':'session-'+root.chat_id,'rootOperationID':root.message_id,'actorID':'main','executionRevision':1}}
    live={'value':True}; saves=[]; prompts=[]
    @asynccontextmanager
    async def guard(**kwargs):
        async def validate(): return live['value']
        yield None,validate
    async def call(spec,prompt,**kwargs):
        prompts.append(prompt);return '```json\n{"name":"draft","procedure":["review"]}\n```'
    async def save(payload,owner):
        saves.append(json.loads(payload));return {'results':'created draft'}
    monkeypatch.setattr(sources,'CurrentHostUserSource',Authority)
    monkeypatch.setattr(archive_module,'get_conversation_archive',lambda:archive)
    monkeypatch.setattr(work,'held_s08_human_review_sources',guard)
    monkeypatch.setattr(teacher,'_teacher_setting',lambda key,owner,default: True if key=='teacher_enabled' else 'selected-model')
    monkeypatch.setattr(teacher,'_call_teacher',call)
    monkeypatch.setattr(tools,'do_manage_skills',save)
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(auth_manager=object(),files_policy_repository=object())))
    context={'user':'user','account':'account','workspace':'memory-workspace','current_write':lambda:live['value']}
    provider=SimpleNamespace(_guarded_preparation=object())
    result=asyncio.run(work.distill_selected(request=request,context=context,provider=provider,occurrences=occurrences))
    assert result['state']=='processed' and saves[0]['status']=='draft' and saves[0]['action']=='add'
    assert 'private-' not in prompts[0] and 'private output' in prompts[0]
    assert saves[0]['source_uri'].startswith('data:application/vnd.openclank.conversation-selection+json;base64,') and len(saves[0]['source_revision'])==64
    reordered=[dict(entry) for entry in occurrences]
    reordered[0]={'source':occurrences[0]['source'],'parts':list(reversed(occurrences[0]['parts']))}
    assert asyncio.run(work.distill_selected(request=request,context=context,provider=provider,occurrences=reordered))['state']=='unavailable'
    assert len(prompts)==1
    async def revoke(*args,**kwargs):
        live['value']=False;return '```json\n{"name":"late","procedure":["review"]}\n```'
    monkeypatch.setattr(teacher,'_call_teacher',revoke)
    assert asyncio.run(work.distill_selected(request=request,context=context,provider=provider,occurrences=occurrences))['state']=='unavailable'
    assert len(saves)==1


def test_distill_discovery_refuses_roots_from_other_workspaces(monkeypatch):
    from types import SimpleNamespace
    from src.openclank.memory_distill_work import _discover_distill_groups
    import core.database
    class DB:
        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(all=lambda: [('message', 'a'*64, 'other-workspace')])
        def close(self): pass
    monkeypatch.setattr(core.database, 'SessionLocal', lambda: DB())
    archive=SimpleNamespace(list_qualified_tool_candidates=lambda **_: [])
    authority=SimpleNamespace(execution_receipt=lambda *_: (_ for _ in ()).throw(AssertionError('wrong workspace was inspected')))
    result=_discover_distill_groups(chat_ids=['chat'],account='account',user='user',workspace='workspace',archive=archive,authority=authority,since=0)
    assert result == [[]]
