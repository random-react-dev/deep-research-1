"""Verify native provider routing without billed requests or fallback."""
import os,sys,json
from pathlib import Path
import httpx,pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
SOURCE=Path(os.environ.get('RESEARCH_SERVER_SOURCE',str(Path(__file__).resolve().parents[3]/'production/server')))
if not SOURCE.is_dir():pytest.skip('Set RESEARCH_SERVER_SOURCE for integration tests',allow_module_level=True)
sys.path.insert(0,str(SOURCE))
from provider_proxy import create_router

@pytest.mark.parametrize('route,upstream',[
 ('/api/model/responses','https://api.abliteration.ai/v1/responses'),
 ('/api/model/anthropic/v1/messages','https://api.abliteration.ai/v1/messages')])
def test_native_provider(route,upstream,monkeypatch):
 seen=[];original=httpx.AsyncClient
 def response(request):
  seen.append(request);return httpx.Response(200,stream=httpx.ByteStream(b'{}'),headers={'content-type':'application/json'})
 monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(**kw,transport=httpx.MockTransport(response)))
 monkeypatch.setenv('ABLITERATION_API_KEY','test-new-provider-key')
 app=FastAPI();app.include_router(create_router(lambda token,provider:{'scope':'helper-bootstrap','device_id':'test','provider':provider}))
 with TestClient(app) as client:
  r=client.post(route,headers={'Authorization':'Bearer scoped'},json={'model':'abliterated-model-large-flash','reasoning':{'effort':'high'},'thinking':{'type':'enabled','budget_tokens':8000},'output_config':{'effort':'high'}})
 assert r.status_code==200
 assert str(seen[0].url)==upstream
 assert seen[0].headers['authorization']=='Bearer test-new-provider-key'
 assert 'test-new-provider-key' not in r.text
 payload=json.loads(seen[0].content)
 if route.endswith('responses'):
  assert payload['reasoning']=={'effort':'none'}
 else:
  assert payload['thinking']=={'type':'disabled'}
  assert payload['output_config']['effort']=='low'

def test_no_deepseek_fallback(monkeypatch):
 monkeypatch.delenv('ABLITERATION_API_KEY',raising=False);monkeypatch.setenv('DEEPSEEK_API_KEY','old-provider-key')
 app=FastAPI();app.include_router(create_router(lambda token,provider:{'scope':'helper-bootstrap'}))
 with TestClient(app) as c:
  assert c.post('/api/model/responses',headers={'Authorization':'Bearer scoped'},json={'model':'abliterated-model-large-flash'}).status_code==503
  assert c.post('/api/model/responses',headers={'Authorization':'Bearer scoped'},json={'model':'deepseek-flash'}).status_code==400


def test_old_deepseek_thinking_unchanged(monkeypatch):
 seen=[];original=httpx.AsyncClient
 def response(request):
  seen.append(request);return httpx.Response(200,stream=httpx.ByteStream(b'{}'),headers={'content-type':'application/json'})
 monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(**kw,transport=httpx.MockTransport(response)))
 monkeypatch.setenv('DEEPSEEK_API_KEY','old-test-key')
 app=FastAPI();app.include_router(create_router(lambda token,provider:{'scope':'task','provider':provider}))
 with TestClient(app) as client:
  response=client.post('/api/model/anthropic/v1/messages',headers={'Authorization':'Bearer scoped'},json={'model':'deepseek-flash','thinking':{'type':'enabled','budget_tokens':8000},'output_config':{'effort':'high'}})
 assert response.status_code==200
 payload=json.loads(seen[0].content)
 assert str(seen[0].url)=='https://api.deepseek.com/anthropic/v1/messages'
 assert payload['thinking']=={'type':'enabled','budget_tokens':8000}
 assert payload['output_config']=={'effort':'high'}
