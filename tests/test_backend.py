import hashlib
import os
import json
import sys
import time
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
SOURCE=Path(os.environ.get('RESEARCH_SERVER_SOURCE',str(Path(__file__).resolve().parents[3]/'production/server')))
if not SOURCE.is_dir():pytest.skip('Set RESEARCH_SERVER_SOURCE for integration tests',allow_module_level=True)
sys.path.insert(0,str(SOURCE))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from store import Store
from app import create_app

@pytest.fixture
def system(tmp_path,monkeypatch):
    release=tmp_path/'release';release.mkdir();data=b'fixture zip bytes';sha=hashlib.sha256(data).hexdigest()
    (release/(sha+'.zip')).write_bytes(data)
    (release/'current.json').write_text(json.dumps({'sha256':sha,'version':'test1'}))
    monkeypatch.setenv('HELPER_BOOTSTRAP_RELEASES',str(release))
    store=Store(tmp_path/'state.db','a'*40,'b'*40)
    app=create_app(store);client=TestClient(app)
    auth=store.redeem(store.enroll()['code'],'test-device-123456789','nonce-123456789012345','Test','Darwin')
    headers={'Authorization':'Bearer '+auth['device_token']}
    body={'provider':'codex','idempotency_key':'key-12345678','installation_root':'/Users/test/Library/Application Support/HelperRunner','label':'com.helper-runner.test','python':'/usr/bin/python3','cli_paths':{'codex':'/opt/homebrew/bin/codex'},'credential_file':'/Users/test/private/device.json'}
    return store,client,auth,headers,body,release

def create(system):
    store,client,auth,h,b,r=system
    result=client.post('/api/device/helper-bootstrap/sessions',headers=h,json=b)
    assert result.status_code==200,result.text
    return result.json()

def test_idempotent_pinned_instructions(system):
    store,c,a,h,b,r=system;s=create(system);assert create(system)['id']==s['id']
    instructions=c.get(s['instructions_url'],headers=h).json()['instructions']
    assert 'curl --fail' in instructions and 'shasum -a 256' in instructions
    assert 'uploader_binary' in instructions and 'launchctl bootstrap' in instructions
    assert 'User-Agent: helper-runner/1.0' in instructions
    assert 'instead of blindly looping' in instructions
    assert 'setup_step.py' not in instructions and 'install.py' not in instructions
    assert a['device_token'] not in instructions
    (r/'current.json').write_text('{}')
    assert create(system)['id']==s['id']

def test_readiness_requires_same_instance_and_elapsed_time(system,monkeypatch):
    store,c,a,h,b,r=system;s=create(system);sid=s['id'];device=a['device_id']
    store.create_task(device,'Test','Reply hello','codex','research','task-12345678')
    assert store.work(device)['can_start'] is False
    assert store.claim(device,'worker')['task'] is None
    now=time.time()+1
    monkeypatch.setattr(time,'time',lambda:now)
    store.heartbeat(device,{'source':'helper','instance_id':'one'})
    now+=14;store.heartbeat(device,{'source':'helper','instance_id':'one'})
    assert c.get('/api/device/helper-bootstrap/sessions/'+sid,headers=h).json()['status']=='pending'
    now+=1;store.heartbeat(device,{'source':'helper','instance_id':'two'})
    assert not store.work(device)['can_start']
    now+=15;store.heartbeat(device,{'source':'helper','instance_id':'two'})
    assert c.get('/api/device/helper-bootstrap/sessions/'+sid,headers=h).json()['status']=='ready'
    assert store.work(device)['can_start']
    now+=1000
    claimed=store.claim(device,'worker')['task']
    assert claimed['prompt']=='Reply hello' # no legacy setup even after idle
    assert 'setup' not in store.work(device)

def test_attempt_credentials_expiry_and_bounds(system,monkeypatch):
    store,c,a,h,b,r=system;s=create(system);url='/api/device/helper-bootstrap/sessions/'+s['id']+'/attempt'
    assert c.post(url,headers=h,json={'previous_runner_stopped':False}).status_code==409
    one=c.post(url,headers=h,json={'previous_runner_stopped':True}).json()
    assert store.authorize_model(one['model_token'],'codex')['scope']=='helper-bootstrap'
    with pytest.raises(HTTPException):store.authorize_model(one['model_token'],'claude')
    two=c.post(url,headers=h,json={'previous_runner_stopped':True}).json()
    with pytest.raises(HTTPException):store.authorize_model(one['model_token'],'codex')
    assert c.post(url,headers=h,json={'previous_runner_stopped':True}).status_code==409
    monkeypatch.setattr(time,'time',lambda:two['deadline']+1)
    with pytest.raises(HTTPException):store.authorize_model(two['model_token'],'codex')

def test_fake_completion_and_cross_device(system):
    store,c,a,h,b,r=system;s=create(system);base='/api/device/helper-bootstrap/sessions/'+s['id']
    c.post(base+'/attempt',headers=h,json={'previous_runner_stopped':True})
    assert c.post(base+'/events',headers=h,json={'attempt':1,'event':'completed','message':'done'}).status_code==422
    response=c.post(base+'/events',headers=h,json={'attempt':1,'event':'exit','message':'done','exit_code':0})
    assert response.json()['status']=='failed'
    other=store.redeem(store.enroll()['code'],'other-device-123456789','nonce-123456789012345','Other','Darwin')
    assert c.get(base,headers={'Authorization':'Bearer '+other['device_token']}).status_code==404
    assert c.get(base).status_code==401

def test_existing_helper_ungated_and_verify_only(system):
    store,c,a,h,b,r=system
    store.heartbeat(a['device_id'],{'source':'helper','instance_id':'old'})
    b['reuse_existing']=True;s=create(system)
    instructions=c.get(s['instructions_url'],headers=h).json()['instructions']
    assert 'Do not overwrite' in instructions and 'curl --fail' not in instructions
    store.create_task(a['device_id'],'Test','Hello','codex','research','task-12345678')
    assert store.work(a['device_id'])['can_start']

def test_release_integrity_and_traversal(system):
    store,c,a,h,b,r=system
    assert c.get('/api/public/helper-bootstrap/releases/current').status_code==200
    sha=json.loads((r/'current.json').read_text())['sha256']
    assert c.get('/api/public/helper-bootstrap/artifacts/'+sha+'.zip').content==b'fixture zip bytes'
    assert c.get('/api/public/helper-bootstrap/artifacts/bad.zip').status_code==404
    (r/(sha+'.zip')).write_bytes(b'corrupt')
    assert c.get('/api/public/helper-bootstrap/releases/current').status_code==503

def test_instruction_update_pins_per_new_session(system,monkeypatch,tmp_path):
    store,c,a,h,b,r=system
    text=tmp_path/'instructions.txt';text.write_text('Marker: first')
    monkeypatch.setenv('HELPER_BOOTSTRAP_INSTRUCTIONS',str(text))
    one=create(system)
    text.write_text('Marker: second')
    assert 'Marker: first' in c.get(one['instructions_url'],headers=h).json()['instructions']
    c.post('/api/device/helper-bootstrap/sessions/'+one['id']+'/attempt',headers=h,json={'previous_runner_stopped':True})
    c.post('/api/device/helper-bootstrap/sessions/'+one['id']+'/events',headers=h,json={'attempt':1,'event':'failed','message':'failed'})
    b['idempotency_key']='second-key-12345';two=create(system)
    assert 'Marker: second' in c.get(two['instructions_url'],headers=h).json()['instructions']

def test_logs_redacted_and_ready_cannot_be_claimed_by_exit(system):
    store,c,a,h,b,r=system;s=create(system);base='/api/device/helper-bootstrap/sessions/'+s['id']
    c.post(base+'/attempt',headers=h,json={'previous_runner_stopped':True})
    result=c.post(base+'/events',headers=h,json={'attempt':1,'event':'failed','message':'Bearer testsecret token=othersecret'})
    assert 'testsecret' not in result.text and 'othersecret' not in result.text
    with store.db() as db:
        assert 'testsecret' not in db.execute('SELECT message FROM helper_bootstrap_events').fetchone()[0]
    assert store.state(True)['devices'][0]['helper_bootstrap']['status']=='failed'

def test_late_heartbeat_resets_window_and_timeout_is_visible(system,monkeypatch):
    store,c,a,h,b,r=system;s=create(system);base='/api/device/helper-bootstrap/sessions/'+s['id']
    attempt=c.post(base+'/attempt',headers=h,json={'previous_runner_stopped':True}).json()
    now=time.time()+1;monkeypatch.setattr(time,'time',lambda:now)
    store.heartbeat(a['device_id'],{'source':'helper','instance_id':'same'})
    now+=76;store.heartbeat(a['device_id'],{'source':'helper','instance_id':'same'})
    assert c.get(base,headers=h).json()['heartbeats']==1
    now=attempt['deadline']+1
    state=c.get(base,headers=h).json()
    assert state['status']=='failed' and 'expired' in state['last_error']
    assert not state['helper_fresh']

def test_model_and_state_root_are_pinned_for_new_flow(system,monkeypatch):
    store,c,a,h,b,r=system
    monkeypatch.setenv('HELPER_BOOTSTRAP_MODEL','abliterated-model-large-flash')
    s=create(system);base='/api/device/helper-bootstrap/sessions/'+s['id']
    instructions=c.get(s['instructions_url'],headers=h).json()['instructions']
    assert "'state_dir':str(root)" in instructions
    assert "'state_dir':str(root/'state')" not in instructions
    assert "'model':'abliterated-model-large-flash'" in instructions
    monkeypatch.setenv('HELPER_BOOTSTRAP_MODEL','other-later-model')
    result=c.post(base+'/attempt',headers=h,json={'previous_runner_stopped':True}).json()
    assert result['model']=='abliterated-model-large-flash'

@pytest.mark.parametrize('terminal',['ready','failed','expired'])
def test_delete_bootstrap_children_and_preserve_other_device(system,monkeypatch,terminal):
    from device_delete import delete_device
    store,c,a,h,b,r=system;s=create(system);device=a['device_id'];base='/api/device/helper-bootstrap/sessions/'+s['id']
    c.post(base+'/attempt',headers=h,json={'previous_runner_stopped':True})
    c.post(base+'/events',headers=h,json={'attempt':1,'event':'progress','message':'installing'})
    other=store.redeem(store.enroll()['code'],'keep-device-123456789','nonce-123456789012345','Keep','Darwin')
    uploads=c.app.state.disk_uploads
    with pytest.raises(HTTPException) as error:delete_device(store,uploads,device)
    assert error.value.status_code==409
    assert store.device(a['device_token'])==device
    with store.db(True) as db:
        if terminal=='expired':db.execute('UPDATE helper_bootstrap_sessions SET deadline=? WHERE id=?',(time.time()-1,s['id']))
        else:db.execute('UPDATE helper_bootstrap_sessions SET status=? WHERE id=?',(terminal,s['id']))
    assert delete_device(store,uploads,device)['deleted']
    assert store.device(other['device_token'])==other['device_id']
    with store.db() as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
        for table in ('helper_bootstrap_tokens','helper_bootstrap_events','helper_bootstrap_sessions','helper_bootstrap_devices'):
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
    assert delete_device(store,uploads,device)['deleted']
