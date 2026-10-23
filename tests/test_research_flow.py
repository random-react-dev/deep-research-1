"""Process-level ASK UX against a device-authenticated mock research service."""
import concurrent.futures
import hashlib
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/research/scripts'
sys.path.insert(0, str(SCRIPTS))
import ask


@pytest.fixture
def flow(tmp_path):
    state, workspace = tmp_path/'state', tmp_path/'workspace'
    workspace.mkdir()
    connection = state/'connection'
    (connection/'runtime').mkdir(parents=True)
    (connection/'runtime/common.py').write_text('def preflight_research(): pass\n')
    report = b'# Fixture research\nSources: https://example.com/source\n'
    evidence = {'tasks':{}, 'posts':0, 'lost_ack':False, 'interrupt':False, 'bad_checksum':False,
                'downloads':0, 'delay':.3, 'stuck':False, 'request_paths':[]}
    guard = threading.Lock()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def reply(self,value,status=200):
            data = value if isinstance(value,bytes) else json.dumps(value).encode()
            self.send_response(status); self.send_header('Content-Length',str(len(data))); self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError): pass
        def do_POST(self):
            assert self.headers['Authorization']=='Bearer fixture'
            assert self.path=='/api/device/research/tasks'
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with guard:
                evidence['posts']+=1; evidence['request_paths'].append(self.path)
                key=body['idempotency_key']
                if key not in evidence['tasks']:
                    evidence['tasks'][key]={'id':'task-'+str(len(evidence['tasks'])+1),'payload':body,'created':time.time()}
                task=evidence['tasks'][key]
                assert task['payload']==body
                lose=evidence['lost_ack']; evidence['lost_ack']=False
            if lose: self.connection.shutdown(2); self.connection.close(); return
            self.reply({'task_id':task['id'],'state':'pending'})
        def do_GET(self):
            assert self.headers['Authorization']=='Bearer fixture'
            evidence['request_paths'].append(self.path)
            if self.path.startswith('/api/device/research/tasks/'):
                with guard:
                    if '/lookup?' in self.path:
                        key=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)['idempotency_key'][0]
                        task=evidence['tasks'].get(key)
                    else: task=next((t for t in evidence['tasks'].values() if self.path.endswith(t['id'])),None)
                    if task is None: self.reply({},404); return
                    ready=not evidence['stuck'] and time.time()-task['created']>=evidence['delay']
                self.reply({'id':task['id'],'device_id':'fixture-device','state':'completed' if ready else 'running',
                            'result':{'outputs':[{'id':'report','path':'report.md','size':len(report),'sha256':hashlib.sha256(report).hexdigest()}]}})
            elif self.path=='/api/device/files/report':
                evidence['downloads']+=1
                if evidence['interrupt']:
                    evidence['interrupt']=False
                    self.send_response(200);self.send_header('Content-Length',str(len(report)));self.end_headers()
                    self.wfile.write(report[:10]); self.connection.shutdown(2);self.connection.close()
                else: self.reply(b'X'+report[1:] if evidence['bad_checksum'] else report)
            else: raise AssertionError('Unexpected route: '+self.path)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    service='http://127.0.0.1:'+str(server.server_port)
    config={'device_id':'fixture-device','device_token':'fixture','server_url':service,'state_dir':str(connection),
            'model':'abliterated-model-large-flash','research_gateway_enabled':True}
    (connection/'device.json').write_text(json.dumps(config))
    (connection/'receipt.json').write_text('{"status":"ready"}')
    (connection/'session.json').write_text(json.dumps({'credential_file':str(connection/'device.json')}))
    def call(*args):
        started=time.monotonic()
        result=subprocess.run([sys.executable,str(SCRIPTS/'ask.py'),'--state-dir',str(state),'--server-url',service,*args],cwd=workspace,capture_output=True,text=True,timeout=3)
        assert result.stdout,result.stderr
        return json.loads(result.stdout),result.returncode,time.monotonic()-started
    def receipt(job): return json.loads((state/'jobs'/job/'receipt.json').read_text())
    def wait(job,status='ready',timeout=14):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            value=receipt(job)
            if value['status']==status: return value
            if value['status']=='failed' and status!='failed': pytest.fail(str(value))
            time.sleep(.05)
        pytest.fail('Timeout: '+str(receipt(job)))
    yield evidence,state,call,wait,receipt
    for beat in (state/'jobs').glob('*/heartbeat.json'):
        try: os.kill(json.loads(beat.read_text())['pid'],signal.SIGTERM)
        except ProcessLookupError: pass
    server.shutdown();server.server_close();thread.join()


def test_fast_start_dedupe_and_report_reuse(flow):
    evidence,state,call,wait,receipt=flow
    first,code,elapsed=call('test question')
    assert code==0 and elapsed<1.9
    job=first['job_id']
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        repeats=list(pool.map(lambda _:call('test question'),range(3)))
    assert all(item[0]['job_id']==job for item in repeats)
    ready=wait(job)
    assert len(evidence['tasks'])==1
    result,code,_=call('--result',job)
    assert code==0 and '# Fixture research' in result['report']
    downloads=evidence['downloads']
    assert call('--result',job)[1]==0 and evidence['downloads']==downloads
    assert (state/'jobs'/job/'receipt.json').stat().st_mode & 0o777==0o600
    assert ready['completed_attempt'] is None


def test_killed_coordinator_resumes_same_task(flow):
    evidence,state,call,wait,receipt=flow
    evidence['stuck']=True
    job=call('recover me')[0]['job_id']
    until=time.time()+5
    while not receipt(job).get('task_id') and time.time()<until: time.sleep(.05)
    os.kill(json.loads((state/'jobs'/job/'heartbeat.json').read_text())['pid'],signal.SIGKILL)
    time.sleep(.1); evidence['stuck']=False
    call('--resume',job);wait(job)
    assert len(evidence['tasks'])==1


def test_lost_ack_and_interrupted_download(flow):
    evidence,state,call,wait,receipt=flow
    evidence.update(lost_ack=True,interrupt=True)
    job=call('recover transport')[0]['job_id']
    wait(job)
    assert len(evidence['tasks'])==1 and evidence['posts']>=2 and evidence['downloads']>=2
    assert not list((state/'jobs'/job/'results').glob('.download-*'))


def test_deadline_readonly_lookup_recovers_lost_ack(flow):
    evidence,state,call,wait,receipt=flow
    evidence.update(lost_ack=True,stuck=True)
    job=call('--timeout-seconds','1','deadline me')[0]['job_id']
    time.sleep(2.3)
    status=call('--status',job)[0]
    assert status['status']=='failed' and '--result' in status['next_command']
    posts=evidence['posts']; evidence['stuck']=False
    call('--result',job); wait(job)
    result,code,_=call('--result',job)
    assert code==0 and result['report']
    assert evidence['posts']==posts
    assert any('/lookup?' in path for path in evidence['request_paths'])


def test_bad_checksum_does_not_publish_report(flow):
    evidence,state,call,wait,receipt=flow
    evidence['bad_checksum']=True
    job=call('bad checksum')[0]['job_id']
    value=wait(job,'failed')
    assert 'checksum' in value['error']
    assert not (state/'jobs'/job/'results/report.md').exists()


def test_no_global_promotion_and_no_old_state_default():
    assert not hasattr(ask,'promote_global')
    assert ask.DEFAULT_STATE.name=='ResearchRunner'
    assert 'materialize(' not in (SCRIPTS/'ask.py').read_text()


def test_background_report_waits_without_publishing(tmp_path, monkeypatch):
    monkeypatch.setattr(ask,'STATE_DIR',tmp_path)
    value={'job_id':'ask_'+'c'*32,'status':'running'}
    ask.private_dir(ask.job_path(value['job_id']))
    data={'result':{'outputs':[{'id':'upload','path':'report.md','size':5,'sha256':hashlib.sha256(b'hello').hexdigest(),'delivery':'background'}]}}
    monkeypatch.setattr(ask.launch,'api',lambda *args,**kwargs:{'state':'uploading'})
    monkeypatch.setattr(ask,'download',lambda *args,**kwargs:pytest.fail('Must wait before download'))
    ask.retrieve(value,{},data)
    assert value['status']=='running'
    assert not (ask.job_path(value['job_id'])/'results/report.md').exists()


def test_unsafe_and_generated_report_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(ask,'STATE_DIR',tmp_path)
    value={'job_id':'ask_'+'d'*32,'status':'running'}
    ask.private_dir(ask.job_path(value['job_id']))
    output={'id':'file','path':'report.md','size':5,'sha256':hashlib.sha256(b'hello').hexdigest()}
    with pytest.raises(ask.ClientError,match='did not provide'):
        ask.retrieve(value,{}, {'result':{'outputs':[{**output,'generated_response':True}]}})
    with pytest.raises(ask.ClientError,match='unsafe'):
        ask.retrieve(value,{}, {'result':{'outputs':[{**output,'path':'../outside'},output]}})
    assert not (tmp_path/'outside').exists()


@pytest.mark.parametrize('failure', ['Install the selected CLI: codex', 'This Python installation has broken setup modules'])
def test_new_job_preflight_failure_does_not_create_job_or_connection(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(ask,'STATE_DIR',tmp_path)
    monkeypatch.setattr(ask,'CONNECTION_DIR',tmp_path/'connection')
    def unavailable(provider): raise RuntimeError(failure)
    monkeypatch.setattr(ask.launch,'prerequisites',unavailable)
    monkeypatch.setattr(ask,'spawn_coordinator',lambda *a,**k:pytest.fail('Must not start coordination'))
    with pytest.raises(RuntimeError,match=failure): ask.start('test preflight')
    assert not list((tmp_path/'jobs').glob('ask_*'))
    assert not (tmp_path/'connection').exists()


def test_status_and_result_checks_do_not_require_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ask,'STATE_DIR',tmp_path)
    identifier='ask_'+'e'*32
    ask.private_dir(ask.job_path(identifier))
    ask.write_json(ask.job_path(identifier)/'receipt.json', {'job_id':identifier,'status':'preparing','created_at':time.time()})
    monkeypatch.setattr(ask.launch,'prerequisites',lambda *a:pytest.fail('Read-only checks must not require CLI'))
    for operation in ('--status','--result'):
        monkeypatch.setattr(sys,'argv',['ask.py','--state-dir',str(tmp_path),operation,identifier])
        assert ask.main()==(0 if operation=='--status' else 2)
        capsys.readouterr()
