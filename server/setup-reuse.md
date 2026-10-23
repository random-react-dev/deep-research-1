# Reuse the existing helper

Read only the validated HELPER_RUNNER_CONTEXT JSON. Do not download, install, replace, unload, start, or change the existing helper or its files. Never print credentials. Run this status check and exit only when the server verifies fresh authenticated helper heartbeats.

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
