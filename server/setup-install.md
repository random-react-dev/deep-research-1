# Install this PC's helper

Use only the validated local JSON file named by `HELPER_RUNNER_CONTEXT`. Do not obtain alternative installation instructions or executable URLs. Never print credentials. Do not run a task worker during setup, invoke the helper's built-in install action, remove quarantine, or modify another installation. If the context says reuse_existing, use the signed reuse instructions instead.

Execute this Python block with the context's Python interpreter. It downloads only the signed archive from the configured server, verifies it before extracting, and writes only the owned installation paths. On failure, report the error without secrets; do not weaken validation. A retry can rerun this block safely when existing files match.

```python
import hashlib, http.client, io, json, os, pathlib, plistlib, re, stat, subprocess, tempfile, time, urllib.request, zipfile

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def safe_directory(path):
    path = pathlib.Path(path)
    if not path.is_absolute(): raise ValueError('Absolute destination required')
    current = pathlib.Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir(): raise ValueError('Unsafe directory')
        else: current.mkdir(mode=0o700)

def safe_extract(data, manifest, destination):
    destination = pathlib.Path(destination)
    expected = {item['path']: item for item in manifest['files']}
    if len(expected) != len(manifest['files']) or not 1 <= len(expected) <= 32: raise ValueError('Invalid manifest members')
    if sum(item['size'] for item in expected.values()) > 128 * 1024 * 1024: raise ValueError('Expansion limit')
    checked = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) != len(expected) or {m.filename for m in members} != set(expected): raise ValueError('Unexpected or duplicate archive members')
        for member in members:
            name = member.filename
            if name.startswith('/') or '\\' in name or any(p in ('', '.', '..') for p in name.split('/')): raise ValueError('Unsafe member path')
            mode = member.external_attr >> 16
            if stat.S_IFMT(mode) != stat.S_IFREG or member.extra or member.flag_bits & 1: raise ValueError('Special file or unsupported archive metadata')
            item = expected[name]
            if member.file_size != item['size'] or member.file_size > 128 * 1024 * 1024: raise ValueError('Member size mismatch')
            content = archive.read(member)
            if hashlib.sha256(content).hexdigest() != item['sha256']: raise ValueError('Member checksum mismatch')
            if item['mode'] not in ('0644', '0755'): raise ValueError('Unexpected member permission')
            checked.append((name, content, int(item['mode'], 8)))
    safe_directory(destination)
    # Check every destination before writing anything; never replace another installation.
    for name, content, mode in checked:
        target = destination / name
        safe_directory(target.parent)
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or target.read_bytes() != content: raise ValueError('Existing destination differs or is linked')
    for name, content, mode in checked:
        target = destination / name
        if not target.exists():
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            with os.fdopen(fd, 'wb') as stream: stream.write(content)
        target.chmod(mode)

def private_json(path, value):
    data = json.dumps(value).encode()
    path = pathlib.Path(path)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or json.loads(path.read_text()) != value: raise ValueError('Existing configuration differs or is linked')
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream: stream.write(data)
    path.chmod(0o600)

def download_archive(url, size, digest):
    opener = urllib.request.build_opener(NoRedirect())
    data = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={'User-Agent':'helper-runner/1.0'})
            with opener.open(request, timeout=60) as response:
                data = response.read(size + 1)
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest: raise ValueError('Archive integrity mismatch')
            break
        except (OSError, ValueError, http.client.IncompleteRead):
            if attempt == 3: raise
            time.sleep(2 ** attempt)
    return data

def main():
    os.umask(0o077)
    context = json.loads(pathlib.Path(os.environ['HELPER_RUNNER_CONTEXT']).read_text())
    if context['reuse_existing']: raise ValueError('Use signed reuse instructions')
    release = context['release']; digest = release['sha256']
    if not re.fullmatch('[0-9a-f]{64}', digest) or not 0 < release['size'] <= 128*1024*1024: raise ValueError('Invalid archive bound')
    url = context['server_url'].rstrip('/') + '/api/public/helper-bootstrap/artifacts/' + digest + '.zip'
    data = download_archive(url, release['size'], digest)
    root = pathlib.Path(context['installation_root'])
    safe_extract(data, release, root)
    root.chmod(0o700)
    credentials = json.loads(pathlib.Path(context['credential_file']).read_text())
    config = {**credentials, 'server_url': context['server_url'], 'state_dir': str(root),
              'model': context['model'], 'reasoning_effort':'none', 'poll_seconds':15, 'research_gateway_enabled':True,
              'task_agent_command':[context['python'],str(root/'runtime/task_agent.py'),'--config',str(root/'config.json')],
              'uploader_binary':str(root/'bin/device-helper'),'helper_bin':str(root/'bin/device-helper')}
    config.update({key+'_bin':value for key,value in context['cli_paths'].items()})
    private_json(root/'config.json', config)
    label = context['label']; agents = pathlib.Path.home()/'Library/LaunchAgents'
    safe_directory(agents); plist = agents/(label+'.plist')
    contents = {'Label':label,'ProgramArguments':[str(root/'bin/device-helper'),'--config',str(root/'config.json')],
                'WorkingDirectory':str(root),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':15,
                'ProcessType':'Background','AbandonProcessGroup':True,
                'StandardOutPath':str(root/'helper.stdout.log'),'StandardErrorPath':str(root/'helper.stderr.log'),
                'EnvironmentVariables':{'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'}}
    data = plistlib.dumps(contents)
    if plist.exists() or plist.is_symlink():
        info=plist.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or plist.read_bytes()!=data: raise ValueError('Existing LaunchAgent differs or is linked')
    else:
        fd=os.open(plist,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(data)
    subprocess.run(['/usr/bin/plutil','-lint',str(plist)],check=True,capture_output=True)
    target='gui/'+str(os.getuid())+'/'+label
    loaded=subprocess.run(['/bin/launchctl','print',target],capture_output=True).returncode==0
    if not loaded:subprocess.run(['/bin/launchctl','bootstrap','gui/'+str(os.getuid()),str(plist)],check=True,capture_output=True)
    print('Helper loaded; await independent server verification.')

if __name__ == '__main__': main()
```

Then perform the status polling block below. Setup succeeds only when the server confirms two fresh authenticated helper heartbeats from one instance at least 15 seconds apart. Exit once ready. Do not launch the task worker yourself.

```python
import json, os, pathlib, time, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None
context=json.loads(pathlib.Path(os.environ['HELPER_RUNNER_CONTEXT']).read_text())
credentials=json.loads(pathlib.Path(context['credential_file']).read_text())
url=context['server_url'].rstrip('/')+'/api/device/helper-bootstrap/sessions/'+context['session_id']
opener=urllib.request.build_opener(NoRedirect())
for _ in range(120):
    request=urllib.request.Request(url,headers={'Authorization':'Bearer '+credentials['device_token'],'User-Agent':'helper-runner/1.0'})
    with opener.open(request,timeout=20) as response: status=json.load(response)
    if status['status']=='ready':
        print('Server verified helper heartbeats. Setup complete.'); break
    if status['status']=='failed' or (status.get('deadline') and time.time()>status['deadline']): raise RuntimeError('Server setup failed or timed out')
    time.sleep(5)
else: raise RuntimeError('Helper verification timed out')
```
