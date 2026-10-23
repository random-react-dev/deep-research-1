import hashlib
import json
import os
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
SOURCE=Path(os.environ.get('RESEARCH_SERVER_SOURCE',str(Path(__file__).resolve().parents[3]/'production/server')))
if not SOURCE.is_dir():pytest.skip('Set RESEARCH_SERVER_SOURCE for integration tests',allow_module_level=True)
sys.path.insert(0,str(SOURCE));sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from app import create_app
from store import Store

@pytest.fixture
def system(tmp_path,monkeypatch):
    monkeypatch.setenv('PUBLIC_DASHBOARD','false')
    store=Store(tmp_path/'state.db','a'*40,'b'*40)
    app=create_app(store);client=TestClient(app)
    def enroll(label):
        result=store.redeem(store.enroll()['code'],label,'nonce-123456789012345',label,'Darwin')
        return result['device_id'],{'Authorization':'Bearer '+result['device_token']}
    first,h=enroll('first-device-12345678');other,oh=enroll('other-device-12345678')
    return store,app,client,first,h,other,oh

def submit(c,h,key='research-key-12345',question='What is SQLite WAL?'):
    return c.post('/api/device/research/tasks',headers=h,json={'question':question,'provider':'codex','idempotency_key':key})

def test_submission_lost_ack_replay_and_readonly_lookup(system):
    store,app,c,device,h,other,oh=system
    missing=c.get('/api/device/research/tasks/lookup?idempotency_key=research-key-12345',headers=h)
    assert missing.status_code==404
    one=submit(c,h);assert one.status_code==200,one.text
    two=submit(c,h);assert two.json()==one.json()
    task=one.json()['task_id']
    found=c.get('/api/device/research/tasks/lookup?idempotency_key=research-key-12345',headers=h)
    assert found.status_code==200 and found.json()['id']==task
    data=c.get('/api/device/research/tasks/'+task,headers=h).json()
    assert data['device_id']==device and 'Compare at least three sources' in data['prompt']
    assert 'Save report.md' in data['prompt'] and 'untrusted research data' in data['prompt']
    assert submit(c,h,question='Changed question').status_code==409
    with store.db() as db:assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
    # Read-only lookup of someone else's key cannot reveal a task or create one.
    assert c.get('/api/device/research/tasks/lookup?idempotency_key=research-key-12345',headers=oh).status_code==404
    assert c.get('/api/device/research/tasks/'+task,headers=oh).status_code==404
    assert submit(c,oh).json()['task_id']!=task

@pytest.mark.parametrize('question',['','   ','é'*1025])
def test_question_byte_limit(system,question):
    store,app,c,device,h,other,oh=system
    assert submit(c,h,question=question).status_code==422
    with store.db() as db:assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==0

def test_device_bound_submit_and_auth_all_routes(system):
    store,app,c,device,h,other,oh=system
    body={'question':'Question','provider':'codex','idempotency_key':'key-12345678','device_id':other}
    assert c.post('/api/device/research/tasks',headers=h,json=body).status_code==422
    assert submit(c,{}).status_code==401
    for route in ('/api/device/research/tasks/lookup?idempotency_key=key-12345678','/api/device/research/tasks/taskid','/api/device/files/fileid','/api/device/uploads/'+'a'*32+'/download'):
        assert c.get(route).status_code==401

def test_pending_bootstrap_gates_execution_but_not_submission(system):
    store,app,c,device,h,other,oh=system
    with store.db(True) as db:db.execute('INSERT INTO helper_bootstrap_devices(device_id,gate,ready) VALUES (?,1,0)',(device,))
    task=submit(c,h).json()['task_id']
    assert not store.work(device)['can_start']
    assert store.claim(device,'worker')['task'] is None
    with store.db(True) as db:db.execute('UPDATE helper_bootstrap_devices SET ready=1 WHERE device_id=?',(device,))
    claimed=store.claim(device,'worker')['task']
    assert claimed['id']==task and 'First-time setup' not in claimed['prompt']

def test_small_and_large_downloads_are_owned(system):
    store,app,c,device,h,other,oh=system
    task=submit(c,h).json()['task_id']
    claimed=store.claim(device,'worker')['task']
    data=b'# Report\nSources: https://sqlite.org/wal.html\n'
    item=store.save_output(device,task,claimed['attempt_id'],claimed['lease_token'],'report.md','text/markdown',data)
    route='/api/device/files/'+item['id']
    assert c.get(route,headers=oh).status_code==404
    response=c.get(route,headers=h)
    assert response.content==data and response.headers['X-Content-SHA256']==hashlib.sha256(data).hexdigest()
    uid='a'*32;uploads=app.state.disk_uploads
    uploads.create(device,uid,'report.zip',len(data),hashlib.sha256(data).hexdigest(),task)
    route='/api/device/uploads/'+uid+'/download'
    assert c.get(route,headers=h).status_code==409
    assert c.get(route,headers=oh).status_code==404
    uploads.append(device,uid,0,hashlib.sha256(data).hexdigest(),data)
    response=c.get(route,headers=h)
    assert response.content==data and response.headers['X-Content-SHA256']==hashlib.sha256(data).hexdigest()
    assert c.get('/api/device/uploads/'+uid,headers=h).json()['state']=='ready'
    assert c.get('/api/device/uploads/'+uid,headers=oh).status_code==404
