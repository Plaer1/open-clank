import hashlib
import json
import sys
from email.message import Message
from pathlib import Path

import pytest

from scripts.qualify_treehouse import qualify_storage_activation, verify_public_resources, qualify_global_response


def test_packaged_child_flow_contract_on_source_interpreter(tmp_path):
    # Source rehearsal of the same child protocol; installed proof remains release-owned.
    proof=qualify_storage_activation(Path.cwd(),Path(sys.executable),tmp_path)
    assert proof['proof']=='offline-packaged-child'
    assert all(proof[k] for k in ('missingAllFourBeforeActivation','fiveEditedCoursesAndAllOldSchemaRowsUnchanged','replay','fourRootReadAndDraft','incompatibleRefused'))


def test_registered_resource_bytes_and_mime_are_verified(tmp_path):
    manifest={'resources':[]};responses={}
    for i in range(17):
        path='static/treehouse/fixtures/fixture'+str(i)+'.json';p=tmp_path/path;p.parent.mkdir(parents=True,exist_ok=True);body=json.dumps({'fixture':i}).encode();p.write_bytes(body)
        row={'id':str(i),'path':path,'mime':'application/json','bytes':len(body),'digest':'sha256:'+hashlib.sha256(body).hexdigest()};manifest['resources'].append(row)
        headers=Message();headers['Content-Type']='application/json';responses['/'+path]=(200,headers,body)
    class Journey:
        def raw(self,path):return responses[path]
    assert verify_public_resources(Journey(),tmp_path,manifest)['count']==17
    key=next(iter(responses));original=responses[key]
    for bad in [(200,original[1],b'tampered'),(200,Message(),original[2])]:
        responses[key]=bad
        with pytest.raises(RuntimeError,match='descriptor differs'):verify_public_resources(Journey(),tmp_path,manifest)


def test_current_global_response_probe_uses_normal_auth_without_private_defaults(tmp_path):
    from fastapi.testclient import TestClient
    from scripts.treehouse_candidate import build_candidate
    app=build_candidate(tmp_path);owner=TestClient(app);learner=TestClient(app)
    password='Synthetic-qualification-only-password'
    assert owner.post('/api/auth/setup',json={'username':'fixtureowner','password':password}).status_code==200
    assert owner.post('/api/auth/login',json={'username':'fixtureowner','password':password}).status_code==200
    assert owner.post('/api/auth/users',json={'username':'fixturelearner','password':password,'is_admin':False}).status_code==200
    assert learner.post('/api/auth/login',json={'username':'fixturelearner','password':password}).status_code==200
    class Journey:
        def __init__(self):self.learner=learner
        def request(self,path,body=None,opener=None):
            client=opener or owner;response=client.post(path,json=body) if body is not None else client.get(path)
            assert response.status_code==200,response.text
            return response.json()
        def denied(self,path,body=None,*,status,opener=None):
            response=(opener or owner).post(path,json=body)
            assert response.status_code==status,response.text
    assert owner.get('/api/copal/treehouse').json()['state']['courses']=={}
    proof=qualify_global_response(Journey())
    assert proof['evidence']=='submitted-self-report' and proof['reviewedOrAutomaticMastery'] is False
    assert proof['readingEarnedNoWork'] and proof['clickCompletionDenied'] and proof['accountIsolation'] and proof['exactReplay']
    assert owner.get('/api/copal/treehouse').json()['state']['courses']=={}
