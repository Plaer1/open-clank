import hashlib, json, sys, tempfile, os
from datetime import datetime
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.database import Base, Session as DbSession, ChatMessage
from core.stats_models import StatsEvent, StatsPriceSchedule
from routes.stats_routes import setup_stats_routes
from routes.stats_activity_routes import setup_stats_activity_routes
from routes.stats_analysis_routes import setup_stats_analysis_routes, DatabaseStatsAnalysisLoader
from services.stats.privacy import identity_handle

def main(path):
    fd, db_path = tempfile.mkstemp(prefix='stats-route-', suffix='.db'); os.close(fd); engine = create_engine('sqlite:///' + db_path); Base.metadata.create_all(engine); factory=sessionmaker(bind=engine)
    with factory() as db:
        for owner, sid, model, workspace in (
            ("local-installation", "session-a", "model-a", "workspace-a"),
            ("local-installation", "session-b", "model-b", "workspace-b"),
            ("bob", "session-secret", "model-secret", "workspace-secret"),
        ):
            db.add(DbSession(id=sid, name=sid, endpoint_url="fixture://provider", model=model,
                             owner=owner, workspace_id=workspace))
        db.flush()
        db.add_all([
            ChatMessage(id="fixture-message-a1", session_id="session-a", role="user", content="load bearing seam", timestamp=datetime(2026, 9, 20, 10)),
            ChatMessage(id="fixture-message-a2", session_id="session-a", role="assistant", content="load bearing", timestamp=datetime(2026, 9, 20, 10, 1)),
            ChatMessage(id="fixture-message-b1", session_id="session-b", role="user", content="fixture", timestamp=datetime(2026, 9, 21, 11)),
        ])
        for i,(owner,model,workspace,session,inp,out,state,metadata) in enumerate((("local-installation","model-a","workspace-a","session-a",100,50,'reported',{"billing_lane":"fixture-lane","normalization_profile":"managed-sdk-inclusive-v1"}),("local-installation","model-b","workspace-b","session-b",20,25,'estimated',{"billing_lane":"fixture-lane","normalization_profile":"managed-sdk-inclusive-v1"}),("bob","model-secret","workspace-secret","session-secret",900,900,'reported',{}),("bob",None,"workspace-secret","session-secret",20,None,'unavailable',{}))):
            db.add(StatsEvent(id=f'route-{i}', replay_key=f'route-{i}', owner=owner, session_id=session, workspace_id=workspace, event_kind='response', event_time=datetime(2026,9,20+i), source='fixture', producer_revision='route-v1', observation_scope='message', status='complete', actual_model=model, requested_model=model, provider_id='safe-provider', input_tokens=inp, input_tokens_state=state, output_tokens=out, output_tokens_state=state, cache_read_tokens=0 if model else None, cache_read_tokens_state=state if model else 'unavailable', cache_write_tokens=0 if model else None, cache_write_tokens_state=state if model else 'unavailable', reasoning_tokens=0 if model else None, reasoning_tokens_state=state if model else 'unavailable', event_metadata=metadata))
        for model, category, rate in (("model-a", "input_tokens", 1), ("model-a", "output_tokens", 1), ("model-b", "input_tokens", 1), ("model-b", "output_tokens", 100)):
            db.add(StatsPriceSchedule(id=f"fixture-{model}-{category}", provider_id="safe-provider", billing_lane="fixture-lane", model_identity=model, route_id=None, token_category=category, currency="USD", rate_numerator=rate, rate_denominator=1, rate_unit="currency_major_per_token", effective_start=datetime(2026,9,1), effective_end=datetime(2026,10,1), source_url="fixture://price", source_hash=f"fixture-hash-{model}-{category}", admission_revision="fixture-price-v1", active=True))
        for model in ("model-a", "model-b"):
            for category in ("cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "web_search_credits"):
                db.add(StatsPriceSchedule(id=f"fixture-{model}-{category}", provider_id="safe-provider", billing_lane="fixture-lane", model_identity=model, route_id=None, token_category=category, currency="USD", rate_numerator=0, rate_denominator=1, rate_unit="currency_major_per_token", effective_start=datetime(2026,9,1), effective_end=datetime(2026,10,1), source_url="fixture://price", source_hash=f"fixture-hash-{model}-{category}", admission_revision="fixture-price-v1", active=True))
        db.commit()
    app=FastAPI(); app.include_router(setup_stats_routes(session_factory=factory)); app.include_router(setup_stats_activity_routes(session_factory=factory)); app.state.stats_analysis_loader = DatabaseStatsAnalysisLoader(factory); app.include_router(setup_stats_analysis_routes(allow_test_owner_state=True)); c=TestClient(app)
    params={'period':'custom','start':'2026-09-01','end':'2026-09-30','timezone':'UTC','resolution':'day'}
    result={}
    activity=c.get('/api/stats/v1/activity',params={'period':'30d','timezone':'UTC','resolution':'day'}); result['activity']=activity.json()
    quality=c.get('/api/stats/v1/analysis/quality',params={'period':'30d','timezone':'UTC'}); result['quality']=quality.json()
    trends=c.post('/api/stats/v1/analysis/trends/query',json={'content_opt_in':True,'resolution':'day'}); result['trends']=trends.json()
    for name, extra in [('summary',{}),('buckets',{}),('groups',{'field':'actual_model'}),('cost',{}),('cache',{})]:
        r=c.get('/api/stats/v1/'+name,params={**params,**extra}); result[name]=r.json() if r.status_code==200 else {'coverage':{'state':'unavailable'},'status':r.status_code}
    handles=[identity_handle("local-installation", "actual_model", model) for model in ('model-a','model-b')]
    compare=c.post('/api/stats/v1/compare',json={'handles':handles,'dimension':'actual_model'}); result['compare']=compare.json() if compare.status_code==200 else {'comparison':{'state':'unavailable'},'status':compare.status_code}
    sessions=c.get('/api/stats/v1/sessions/top',params={'period':'30d','timezone':'UTC','rank':'tokens','limit':10}); result['sessions']=sessions.json() if sessions.status_code == 200 else {'status': sessions.status_code, 'body': sessions.text}
    cost_sessions=c.get('/api/stats/v1/sessions/top',params={'period':'30d','timezone':'UTC','rank':'cost','limit':10}); result['sessions_cost']=cost_sessions.json()
    if 'sessions' in result['sessions']:
        result['opened_by_handle']={}
        for row in result['sessions']['sessions'] + result['sessions_cost']['sessions']:
            opened=c.post('/api/stats/v1/sessions/open',json={'handle':row['handle']}); result['opened_by_handle'][row['handle']]=opened.json()
        result['opened']=result['opened_by_handle'][result['sessions']['sessions'][0]['handle']]
    result['safe_choices']=result['sessions'].get('choices',{})
    with open(path,'w') as f: json.dump(result,f)
    engine.dispose(); os.unlink(db_path)
if __name__=='__main__': main(sys.argv[1])
