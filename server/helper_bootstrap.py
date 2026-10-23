"""Additive server-controlled setup: the runner executes instructions; heartbeats prove readiness."""
import hashlib
import base64
import binascii
import math
import json
import os
import re
import secrets
import shlex
import time
import uuid
from pathlib import Path
from typing import Literal
from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
class Create(Body):
    provider: Literal['codex','claude']
    idempotency_key: str = Field(min_length=8,max_length=128)
    installation_root: str = Field(min_length=1,max_length=4096)
    label: str = Field(pattern=r'^[a-zA-Z0-9.-]{1,100}$')
    python: str = Field(min_length=1,max_length=4096)
    cli_paths: dict[str,str] = Field(default_factory=dict)
    credential_file: str = Field(min_length=1,max_length=4096)
    reuse_existing: bool = False

    @field_validator('installation_root','python','credential_file')
    @classmethod
    def absolute_path(cls,value):
        if not Path(value).is_absolute() or any(ord(char)<32 or ord(char)==127 for char in value):
            raise ValueError('Absolute local paths without control characters required')
        return value

    @field_validator('cli_paths')
    @classmethod
    def cli_path_map(cls,value):
        if set(value)-{'codex','claude'}:
            raise ValueError('Only codex and claude CLI paths are supported')
        for path in value.values():
            if len(path)>4096:
                raise ValueError('CLI path too long')
            cls.absolute_path(path)
        return value

    @field_validator('idempotency_key')
    @classmethod
    def plain_key(cls,value):
        if any(ord(char)<32 or ord(char)==127 for char in value):
            raise ValueError('Control characters are not allowed')
        return value
class Attempt(Body):
    previous_runner_stopped: bool
class Event(Body):
    attempt: int = Field(ge=0,le=2)
    event: Literal['progress','exit','failed']
    message: str = Field(default='',max_length=4096)
    exit_code: int | None = None

    @model_validator(mode='after')
    def preflight_failure_only(self):
        if self.attempt==0 and self.event!='failed':
            raise ValueError('Attempt zero only reports preflight failure')
        return self

def hashed(token):
    return hashlib.sha256(token.encode()).hexdigest()

def redact(text):
    return re.sub(r'(?i)(bearer\s+|(?:token|secret|password|api_key)\s*[:=]\s*)[^\s,;]+',r'\1[REDACTED]',text)[:4096]

class Bootstrap:
    def __init__(self, store):
        self.store=store
        with store.db() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS helper_bootstrap_devices(device_id TEXT PRIMARY KEY REFERENCES devices(id),gate INTEGER NOT NULL,ready INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS helper_bootstrap_sessions(id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES devices(id),idempotency_key TEXT NOT NULL,provider TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,document TEXT NOT NULL,instructions TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,deadline REAL NOT NULL DEFAULT 0,instance TEXT,first_seen REAL,last_seen REAL,heartbeats INTEGER NOT NULL DEFAULT 0,last_error TEXT,UNIQUE(device_id,idempotency_key));
CREATE TABLE IF NOT EXISTS helper_bootstrap_tokens(hash TEXT PRIMARY KEY,session_id TEXT NOT NULL,attempt INTEGER NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS helper_bootstrap_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,attempt INTEGER NOT NULL,event TEXT NOT NULL,message TEXT NOT NULL,created REAL NOT NULL);
''')
    def release(self):
        root=Path(os.environ.get('HELPER_BOOTSTRAP_RELEASES','/var/lib/research-workers/helper-bootstrap-releases'))
        try:
            release=json.loads((root/'current.json').read_text())
            sha=release['sha256']
            if not re.fullmatch('[0-9a-f]{64}',sha): raise ValueError()
            path=root/(sha+'.zip')
            with path.open('rb') as archive:
                if hashlib.file_digest(archive,'sha256').hexdigest()!=sha: raise ValueError()
        except (OSError,ValueError,KeyError,TypeError):
            raise HTTPException(503,'Helper release is unavailable') from None
        return release
    def signed_setup(self,release,model):
        """Pin the deployment envelope; the launcher verifies its offline public-key signature."""
        location=os.environ.get('HELPER_BOOTSTRAP_SIGNED_SETUP')
        if location is None:
            return None
        try:
            with Path(location).open('rb') as source:
                raw=source.read(1024*1024+1)
            if len(raw)>1024*1024:
                raise ValueError('Envelope too large')
            envelope=json.loads(raw)
            if not isinstance(envelope,dict) or set(envelope)!={'payload','signature'}:
                raise ValueError('Invalid envelope')
            payload_raw=base64.b64decode(envelope['payload'],validate=True)
            signature=base64.b64decode(envelope['signature'],validate=True)
            if not signature or len(signature)>16384:
                raise ValueError('Invalid signature encoding')
            payload=json.loads(payload_raw.decode('utf-8'))
            required={'schema','revision','issued_at','expires_at','model','release','model_catalog_sha256','install_instructions','reuse_instructions'}
            if not isinstance(payload,dict) or not required.issubset(payload):
                raise ValueError('Incomplete payload')
            if type(payload['schema']) is not int or payload['schema']!=1:
                raise ValueError('Unsupported schema')
            revision=payload['revision']
            if not ((type(revision) is int and revision>0) or (isinstance(revision,str) and 0<len(revision)<=128)):
                raise ValueError('Invalid revision')
            for key in ('issued_at','expires_at'):
                if type(payload[key]) not in (int,float) or not math.isfinite(payload[key]):
                    raise ValueError('Invalid timestamp')
            if payload['expires_at']<=payload['issued_at'] or payload['expires_at']<=time.time() or payload['issued_at']>time.time()+300:
                raise ValueError('Expired or future envelope')
            if payload['release']!=release or payload['model']!=model:
                raise ValueError('Deployment and envelope disagree')
            if not isinstance(payload['model_catalog_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',payload['model_catalog_sha256']):
                raise ValueError('Invalid catalog digest')
            for key in ('install_instructions','reuse_instructions'):
                if not isinstance(payload[key],str) or not payload[key].strip():
                    raise ValueError('Instructions missing')
            return envelope
        except (OSError,ValueError,TypeError,KeyError,UnicodeError,binascii.Error):
            raise HTTPException(503,'Signed helper setup is unavailable or inconsistent') from None

    def row(self,db,device,sid):
        row=db.execute('SELECT * FROM helper_bootstrap_sessions WHERE id=? AND device_id=?',(sid,device)).fetchone()
        if row is None: raise HTTPException(404,'Setup session not found')
        if row['status']=='running' and row['deadline']<time.time():
            db.execute("UPDATE helper_bootstrap_sessions SET status='failed',last_error='Runner attempt deadline expired',updated=? WHERE id=?",(time.time(),sid))
            db.execute('DELETE FROM helper_bootstrap_tokens WHERE session_id=?',(sid,))
            row=db.execute('SELECT * FROM helper_bootstrap_sessions WHERE id=?',(sid,)).fetchone()
        return row
    def view(self,row):
        doc=json.loads(row['document'])
        with self.store.db() as db:
            seen=db.execute('SELECT helper_seen FROM devices WHERE id=?',(row['device_id'],)).fetchone()[0]
        fresh=seen is not None and seen>=time.time()-60
        return {'helper_fresh':fresh, **{k:row[k] for k in ('id','status','attempt','provider','heartbeats','first_seen','last_seen','last_error','deadline')}} | {'release':doc['release'],'instructions_url':doc['origin']+'/api/device/helper-bootstrap/sessions/'+row['id']+'/instructions'}
    def create(self,device,data,origin):
        for key in ('installation_root','python','credential_file'):
            if not Path(data[key]).is_absolute() or any(ord(char)<32 or ord(char)==127 for char in data[key]): raise HTTPException(422,'Absolute local paths required')
        with self.store.db() as db:
            previous=db.execute('SELECT * FROM helper_bootstrap_sessions WHERE device_id=? AND idempotency_key=?',(device,data['idempotency_key'])).fetchone()
            if previous:return self.view(previous)
        release=self.release()
        sid=str(uuid.uuid4()); now=time.time()
        doc=dict(data,origin=origin,release=release,model=os.environ.get("HELPER_BOOTSTRAP_MODEL","abliterated-model-large-flash"))
        doc['signed_setup']=self.signed_setup(release,doc['model'])
        instructions=self.instructions(sid,doc)
        with self.store.db(True) as db:
            previous=db.execute('SELECT * FROM helper_bootstrap_sessions WHERE device_id=? AND idempotency_key=?',(device,data['idempotency_key'])).fetchone()
            if previous:return self.view(previous)
            active=db.execute("SELECT * FROM helper_bootstrap_sessions WHERE device_id=? AND status IN ('pending','running')",(device,)).fetchone()
            if active:return self.view(active)
            d=db.execute('SELECT helper_seen FROM devices WHERE id=?',(device,)).fetchone()
            existing=d['helper_seen'] is not None
            if data['reuse_existing'] and (not existing or d['helper_seen']<now-90):raise HTTPException(409,'Existing helper has no fresh heartbeat')
            db.execute('INSERT OR IGNORE INTO helper_bootstrap_devices(device_id,gate) VALUES (?,?)',(device,int(not existing)))
            db.execute('INSERT INTO helper_bootstrap_sessions(id,device_id,idempotency_key,provider,status,created,updated,document,instructions) VALUES (?,?,?,?,?,?,?,?,?)',(sid,device,data['idempotency_key'],data['provider'],'pending',now,now,json.dumps(doc),instructions))
            return self.view(self.row(db,device,sid))
    def attempt(self,device,sid,stopped):
        if not stopped:raise HTTPException(409,'Confirm the previous runner stopped')
        now=time.time();token=secrets.token_urlsafe(32)
        with self.store.db(True) as db:
            row=self.row(db,device,sid)
            if row['status']=='ready':raise HTTPException(409,'Helper is already verified')
            if row['attempt']>=2:raise HTTPException(409,'Two setup attempts exhausted')
            if db.execute('SELECT paused FROM devices WHERE id=?',(device,)).fetchone()[0]:raise HTTPException(409,'Device paused')
            attempt=row['attempt']+1
            db.execute('DELETE FROM helper_bootstrap_tokens WHERE session_id=?',(sid,))
            db.execute('INSERT INTO helper_bootstrap_tokens VALUES (?,?,?,?)',(hashed(token),sid,attempt,now+900))
            db.execute("UPDATE helper_bootstrap_sessions SET attempt=?,deadline=?,status='running',updated=? WHERE id=?",(attempt,now+600,now,sid))
            document=json.loads(row['document'])
            origin=document['origin']
        result={'attempt':attempt,'model_token':token,'expires_at':now+900,'deadline':now+600,'model':document.get('model',os.environ.get('HELPER_BOOTSTRAP_MODEL','abliterated-model-large-flash')),'base_url':origin+('/api/model' if row['provider']=='codex' else '/api/model/anthropic')}
        models=os.environ.get('HELPER_BOOTSTRAP_MODELS')
        if models:result['model_catalog']=json.loads(Path(models).read_text())
        return result
    def authorize(self,token,provider):
        with self.store.db() as db:
            row=db.execute("SELECT s.device_id,s.id,s.attempt FROM helper_bootstrap_tokens t JOIN helper_bootstrap_sessions s ON s.id=t.session_id JOIN devices d ON d.id=s.device_id WHERE t.hash=? AND t.expires>? AND s.deadline>? AND t.attempt=s.attempt AND s.provider=? AND s.status IN ('running','ready') AND d.paused=0",(hashed(token),time.time(),time.time(),provider)).fetchone()
        return dict(device_id=row['device_id'],task_id=row['id'],attempt_id=str(row['attempt']),provider=provider,scope='helper-bootstrap') if row else None
    def heartbeat(self,db,device,data,now):
        if data['source']!='helper':return
        db.execute("UPDATE helper_bootstrap_sessions SET status='failed',last_error='Runner attempt deadline expired',updated=? WHERE device_id=? AND status='running' AND deadline<?",(now,device,now))
        rows=db.execute("SELECT * FROM helper_bootstrap_sessions WHERE device_id=? AND status IN ('pending','running')",(device,)).fetchall()
        for row in rows:
            if now<row['created']:continue
            same=row['instance']==data['instance_id'] and row['last_seen'] is not None and now-row['last_seen']<=75
            first=row['first_seen'] if same else now
            count=row['heartbeats']+1 if same else 1
            ready=count>=2 and now-first>=15
            db.execute('UPDATE helper_bootstrap_sessions SET instance=?,first_seen=?,last_seen=?,heartbeats=?,status=?,updated=? WHERE id=?',(data['instance_id'],first,now,count,'ready' if ready else row['status'],now,row['id']))
            if ready:
                db.execute('UPDATE helper_bootstrap_devices SET ready=1 WHERE device_id=?',(device,))
                if db.execute('SELECT gate FROM helper_bootstrap_devices WHERE device_id=?',(device,)).fetchone()[0]:
                    db.execute("INSERT INTO setup VALUES (?,'runner','completed','Independent helper heartbeats verified',?) ON CONFLICT(device_id,stage) DO UPDATE SET status='completed',detail=excluded.detail,updated=excluded.updated",(device,now))
    def event(self,device,sid,data):
        with self.store.db(True) as db:
            row=self.row(db,device,sid)
            if row['attempt']!=data['attempt']:raise HTTPException(409,'Stale attempt')
            if data['attempt']==0 and (data['event']!='failed' or row['status']!='pending'):
                raise HTTPException(409,'Only pending setup can report preflight failure')
            message=redact(data['message'])
            if data['event'] in ('failed','exit'):
                if row['status']!='ready':
                    message=message or 'Runner exited before independent helper verification'
                    db.execute("UPDATE helper_bootstrap_sessions SET status='failed',last_error=?,updated=? WHERE id=?",(message,time.time(),sid))
                db.execute('DELETE FROM helper_bootstrap_tokens WHERE session_id=?',(sid,))
            db.execute('INSERT INTO helper_bootstrap_events(session_id,attempt,event,message,created) VALUES (?,?,?,?,?)',(sid,data['attempt'],data['event'],message,time.time()))
            return self.view(self.row(db,device,sid))
    def instructions(self,sid,d):
        status=d['origin']+'/api/device/helper-bootstrap/sessions/'+sid
        pre=f'''Set up only this PC's background helper. Do not run a task worker during setup.\nRead credentials privately from {d['credential_file']}; never print credentials. Use its device_token as Bearer authentication for GET {status}. Include User-Agent: helper-runner/1.0 on every HTTP request, including Python urllib requests. On HTTP errors, stop polling and inspect the status/error; correct the request or report the failure instead of blindly looping. Only the server status 'ready' proves completion; wait for that status then exit. Do not claim success yourself. Report useful failures without secrets.\n'''
        if d['reuse_existing']:
            return pre+'A healthy compatible helper already exists. Do not overwrite, reinstall, unload or modify it. Wait for fresh heartbeats and server readiness.'
        root=d['installation_root']; sha=d['release']['sha256']; python=d['python']
        config_code=f'''import json,os,pathlib,plistlib
root=pathlib.Path({root!r}); credentials=json.loads(pathlib.Path({d['credential_file']!r}).read_text())
root.mkdir(parents=True,exist_ok=True); root.chmod(0o700)
config={{**credentials,'server_url':{d['origin']!r},'state_dir':str(root),'model':{d.get('model',os.environ.get('HELPER_BOOTSTRAP_MODEL','abliterated-model-large-flash'))!r},'task_agent_command':[{python!r},str(root/'runtime/task_agent.py'),'--config',str(root/'config.json')],'uploader_binary':str(root/'bin/device-helper'),'helper_bin':str(root/'bin/device-helper'),'poll_seconds':15}}
config.update({{k+'_bin':v for k,v in {d['cli_paths']!r}.items()}})
p=root/'config.json'; p.write_text(json.dumps(config)); p.chmod(0o600)
helper=root/'bin/device-helper'; helper.chmod(0o700)
plist=pathlib.Path.home()/'Library/LaunchAgents'/{(d['label']+'.plist')!r}; plist.parent.mkdir(parents=True,exist_ok=True)
contents={{'Label':{d['label']!r},'ProgramArguments':[str(helper),'--config',str(p)],'WorkingDirectory':str(root),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':15,'ProcessType':'Background','AbandonProcessGroup':True,'StandardOutPath':str(root/'helper.stdout.log'),'StandardErrorPath':str(root/'helper.stderr.log'),'EnvironmentVariables':{{'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'}}}}
plist.write_bytes(plistlib.dumps(contents)); plist.chmod(0o600)
'''
        return pre+f'''Execute these ordinary shell steps, repairing errors where needed. Preserve any unrelated installation. Never remove quarantine metadata or conceal the service. If a destination already has matching files/config, reuse them. Never unload another helper. Download to a private temporary directory, verify SHA-256 before extraction, then extract into {root}. Package contains bin/device-helper and runtime/ support files, no installer.\n```sh
set -eu
umask 077
stage=$(mktemp -d)
curl --fail --show-error --location --retry 3 --retry-all-errors {shlex.quote(d['origin']+'/api/public/helper-bootstrap/artifacts/'+sha+'.zip')} -o "$stage/helper.zip"
echo "{sha}  $stage/helper.zip" | shasum -a 256 -c -
mkdir -p {shlex.quote(root)}
unzip -qo "$stage/helper.zip" -d {shlex.quote(root)}
{shlex.quote(python)} - <<'PYSETUP'
{config_code}PYSETUP
plutil -lint "$HOME/Library/LaunchAgents/{d['label']}.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/{d['label']}.plist"
launchctl print "gui/$(id -u)/{d['label']}"
```
If already loaded, inspect the exact label and config before treating bootstrap's duplicate error as success. Never invoke the helper's built-in install command. Poll server status approximately every 5 seconds, using Python urllib Request with headers including User-Agent: helper-runner/1.0 and credentials read from the private file (never echo a token or put it directly into command text); on HTTP errors inspect and repair the request or report the failure, rather than retrying blindly. Status ready requires two helper heartbeats at least 15 seconds apart. Stop after the session deadline and report a precise failure if not ready.'''

def install_routes(app,store,device):
    service=Bootstrap(store);store.helper_bootstrap=service
    @app.get('/api/public/helper-bootstrap/releases/current')
    def release():return service.release()
    @app.get('/api/public/helper-bootstrap/artifacts/{sha}.zip')
    def artifact(sha:str):
        if not re.fullmatch('[0-9a-f]{64}',sha):raise HTTPException(404,'Artifact not found')
        path=Path(os.environ.get('HELPER_BOOTSTRAP_RELEASES','/var/lib/research-workers/helper-bootstrap-releases'))/(sha+'.zip')
        if not path.is_file():raise HTTPException(404,'Artifact not found')
        return FileResponse(path,media_type='application/zip',headers={'Cache-Control':'public,max-age=31536000,immutable'})
    @app.post('/api/device/helper-bootstrap/sessions')
    def create(data:Create,request:Request,device_id=Depends(device)):
        origin=os.environ.get('PUBLIC_BASE_URL','https://34.14.204.251').rstrip('/')
        return service.create(device_id,data.model_dump(),origin)
    @app.get('/api/device/helper-bootstrap/sessions/{sid}')
    def status(sid:str,device_id=Depends(device)):
        with store.db() as db:return service.view(service.row(db,device_id,sid))
    @app.get('/api/device/helper-bootstrap/sessions/{sid}/events')
    def event_list(sid:str,device_id=Depends(device)):
        with store.db() as db:
            service.row(db,device_id,sid)
            return {'events':[dict(r) for r in db.execute('SELECT attempt,event,message,created FROM helper_bootstrap_events WHERE session_id=? ORDER BY seq DESC LIMIT 100',(sid,))]}
    @app.get('/api/device/helper-bootstrap/sessions/{sid}/instructions')
    def instructions(sid:str,device_id=Depends(device)):
        with store.db() as db:
            row=service.row(db,device_id,sid)
            document=json.loads(row['document'])
            return {'instructions':row['instructions'],'release':document['release'],'signed_setup':document.get('signed_setup')}
    @app.post('/api/device/helper-bootstrap/sessions/{sid}/attempt')
    def attempt(sid:str,data:Attempt,device_id=Depends(device)):return service.attempt(device_id,sid,data.previous_runner_stopped)
    @app.post('/api/device/helper-bootstrap/sessions/{sid}/events')
    def events(sid:str,data:Event,device_id=Depends(device)):return service.event(device_id,sid,data.model_dump())
    return service
