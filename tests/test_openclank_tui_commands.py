"""Actual TUI command loop against a scoped fake host; no network or terminal."""
import io
from types import SimpleNamespace
from src.openclank.tui_app import OpenClankTui
from src.openclank.tui_client import TuiClientError


class Host:
    profile = SimpleNamespace(name='test')
    def __init__(self): self.calls = []; self.resumes = 0
    def bootstrap(self): return {'principal':{'owner':'owner'}, 'sessions':[{'id':'chat','name':'test'}]}
    def active_turn(self, session):
        self.calls.append(('active', session)); return {'session_id':session,'run_id':'run-a','status':'running'}
    def stream_active_turn(self, session, *, run_id, after):
        self.calls.append(('stream',session,run_id,after))
        if after:
            self.resumes += 1
            if self.resumes == 1:
                yield {'id':4,'event':'message','data':{'delta':'unavailable gap'}}
                return
            raise TuiClientError('run changed', status_code=409)
        yield {'id':1,'event':'message','data':{'delta':'hello'}}
        yield {'id':2,'event':'message','data':{'type':'permission_request'}}
    def stop_active_turn(self, session, *, run_id):
        self.calls.append(('stop',session,run_id)); return {'session_id':session,'run_id':'run-b','stopped':False}
    def actors(self, session):
        self.calls.append(('actors',session)); return {'items':[{'actor_id':'a','agent':'worker','status':'done'}]}


def test_existing_run_commands():
    host=Host(); out=io.StringIO()
    commands=iter(['active 1','resume 1','resume 1','resume 1','stop 1','actors 1','quit'])
    tui=OpenClankTui(host,input_fn=lambda _prompt:next(commands),output=out)
    assert tui.run()==0
    assert host.calls==[('active','chat'),('stream','chat','run-a',0),('stream','chat','run-a',2),('stream','chat','run-a',2),('stop','chat','run-a'),('actors','chat')]
    text=' '.join(out.getvalue().split())
    assert 'Human action required' in text and 'no replacement was attached' in text.lower()
    assert 'no cancellation was requested' in text and 'Persisted actor history' in text
    assert 'gap or was evicted' in text and 'unavailable gap' not in text
    assert 'run-b' not in text
