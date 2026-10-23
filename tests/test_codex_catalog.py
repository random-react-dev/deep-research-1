"""Opt-in actual CLI schema check against a local HTTP fixture, not a model test."""
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import unittest
import zipfile


@unittest.skipUnless(os.environ.get('HELPER_RUNNER_TEST_RELEASE') and shutil.which('codex'),
                     'Set HELPER_RUNNER_TEST_RELEASE to a built ZIP to check the installed Codex CLI')
class CodexCatalogTests(unittest.TestCase):
    def test_actual_catalog_parses_before_local_network_request(self):
        self.capture(False)

    def test_actual_mcp_tools_are_namespaced_function_definitions(self):
        self.capture(True)

    def capture(self, with_mcp):
        received = threading.Event()
        payloads = []
        mcp_methods = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(405);self.end_headers()
            def do_POST(self):
                data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                value = json.loads(data)
                if self.path == '/mcp':
                    method = value.get('method');mcp_methods.append(method)
                    if 'id' not in value:
                        self.send_response(202);self.end_headers();return
                    if method == 'initialize':
                        result = {'protocolVersion':value['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
                    elif method == 'tools/list':
                        result = {'tools':[{'name':name,'description':'Local fixture tool','inputSchema':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}} for name in ('research_search','research_fetch')]}
                    else: result = {}
                    body = json.dumps({'jsonrpc':'2.0','id':value['id'],'result':result}).encode()
                    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
                payloads.append(value)
                received.set()
                body = b'{"error":{"message":"Local catalog parsing fixture; no model invoked"}}'
                self.send_response(400)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='helper-catalog-') as folder:
                root = Path(folder)
                (root / 'codex-home').mkdir(mode=0o700)
                with zipfile.ZipFile(os.environ['HELPER_RUNNER_TEST_RELEASE']) as release:
                    catalog = root / 'models.json'
                    catalog.write_bytes(release.read('runtime/models.json'))
                if with_mcp and os.environ.get('HELPER_RUNNER_DISABLE_SEARCH_TOOL'):
                    data=json.loads(catalog.read_text());data['models'][0]['supports_search_tool']=False
                    catalog.write_text(json.dumps(data))
                values = {
                    'model': 'abliterated-model-large-flash', 'model_provider': 'fixture',
                    'model_catalog_json': str(catalog), 'approval_policy': 'never',
                    'web_search': 'disabled',
                    'model_reasoning_effort': 'none',
                    'model_providers.fixture.name': 'Local test fixture',
                    'model_providers.fixture.base_url': 'http://127.0.0.1:' + str(server.server_port) + '/v1',
                    'model_providers.fixture.wire_api': 'responses',
                    'model_providers.fixture.env_key': 'CATALOG_FIXTURE_TOKEN',
                    'model_providers.fixture.requires_openai_auth': False,
                    'model_providers.fixture.request_max_retries': 0,
                    'model_providers.fixture.stream_max_retries': 0,
                }
                if with_mcp:
                    values.update({'mcp_servers.research-gateway.url':'http://127.0.0.1:'+str(server.server_port)+'/mcp',
                                   'mcp_servers.research-gateway.required':True,
                                   'mcp_servers.research-gateway.startup_timeout_sec':10})
                args = [shutil.which('codex'), 'exec', '--json', '--ephemeral', '--ignore-user-config',
                        '--skip-git-repo-check', '--sandbox', 'read-only', '-C', str(root)]
                for key, value in values.items(): args += ['-c', key + '=' + json.dumps(value)]
                for feature in ('plugins','remote_plugin','computer_use','browser_use','apps','hooks','multi_agent','multi_agent_v2','goals'):
                    args += ['--disable', feature]
                args += ['Return OK. Do not execute tools.']
                env = {'PATH': os.environ.get('PATH','/usr/bin:/bin'), 'HOME': str(root),
                       'CODEX_HOME': str(root / 'codex-home'), 'CATALOG_FIXTURE_TOKEN': 'dummy-local-only'}
                process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, start_new_session=True)
                try:
                    output, _ = process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    output, _ = process.communicate()
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                self.assertNotIn('unknown variant', output)
                self.assertTrue(received.is_set(), 'Catalog never reached local fixture: ' + output[-2000:])
                model = json.loads(catalog.read_text())['models'][0]
                if with_mcp and os.environ.get('HELPER_RUNNER_CAPTURE_REQUEST'):
                    Path(os.environ['HELPER_RUNNER_CAPTURE_REQUEST']).write_text(json.dumps(payloads[0],indent=2))
                if model.get('apply_patch_tool_type') is None:
                    tools = payloads[0].get('tools', [])
                    self.assertTrue(tools, 'Expected ordinary CLI tools')
                    self.assertTrue(all(tool.get('type') == 'function' or
                                        (with_mcp and tool.get('type') == 'namespace' and tool.get('tools') and
                                         all(child.get('type') == 'function' for child in tool['tools']))
                                        for tool in tools), tools)
                self.assertEqual(payloads[0].get('reasoning', {}).get('effort'), 'none')
                if with_mcp:
                    self.assertIn('initialize', mcp_methods)
                    self.assertIn('tools/list', mcp_methods)
                    names = {tool['name'] for tool in payloads[0]['tools']}
                    names.update(child['name'] for tool in payloads[0]['tools'] if tool.get('type')=='namespace' for child in tool['tools'])
                    self.assertTrue(any(name.endswith('research_search') for name in names), names)
                    self.assertTrue(any(name.endswith('research_fetch') for name in names), names)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__': unittest.main()
