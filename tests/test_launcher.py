import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/research/scripts/launch.py'
spec = importlib.util.spec_from_file_location('launcher', SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_only_two_installed_files(self):
        files = [p.relative_to(SCRIPT.parents[1]).as_posix() for p in SCRIPT.parents[1].rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts]
        self.assertEqual(sorted(files), ['SKILL.md', 'scripts/ask.py', 'scripts/launch.py'])

    def test_private_atomic_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'x.json'
            launcher.save(path, {'x': 1})
            self.assertEqual(launcher.read(path), {'x': 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_origin_validation(self):
        for value in ('http://remote.example', 'https://a/path', 'https://x:secret@a', 'https://a?secret=x'):
            with self.assertRaises(ValueError):
                launcher.origin(value)
        self.assertEqual(launcher.origin('https://a/'), 'https://a')

    def test_only_selected_cli_required(self):
        with patch.object(launcher.sys, 'platform', 'darwin'), patch.object(launcher.platform, 'machine', return_value='arm64'), patch.object(launcher.shutil, 'which', return_value='/bin/echo') as lookup:
            launcher.prerequisites('claude')
            lookup.assert_called_once_with('claude')

    def test_optional_installed_cli_paths_are_available_without_requiring_them(self):
        with patch.object(launcher.shutil, 'which', return_value='/bin/echo') as lookup:
            paths = launcher.available_clis('codex', '/selected/codex')
            self.assertEqual(paths['codex'], '/selected/codex')
            self.assertEqual(paths['claude'], str(Path('/bin/echo').resolve()))
            lookup.assert_called_once_with('claude')
        with patch.object(launcher.shutil, 'which', return_value=None):
            self.assertEqual(launcher.available_clis('claude', '/selected/claude'), {'claude':'/selected/claude'})

    def test_isolated_provider_settings(self):
        settings = {'model': 'deepseek-flash', 'base_url': 'https://example.com/api/model', 'model_token': 'secret-value', 'model_catalog': {'models': []}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'OPENAI_API_KEY': 'personal', 'ANTHROPIC_AUTH_TOKEN': 'personal'}):
            for provider in ('codex', 'claude'):
                args, env = launcher.command(provider, provider, Path(directory) / provider, settings)
                self.assertNotIn('secret-value', ' '.join(args))
                self.assertNotIn('OPENAI_API_KEY', env)
                self.assertNotEqual(env.get('ANTHROPIC_AUTH_TOKEN'), 'personal')
                if provider == 'claude': self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'secret-value')
                self.assertNotIn('127.0.0.1', str(env))
                self.assertIn('https://example.com/api/model', str(args) + str(env))

    def test_log_never_records_model_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events'
            launcher.summarize(io.BytesIO(b'{"type":"item.completed","text":"secret"}\nsecret\n'), path, ('secret',))
            self.assertNotIn('secret', path.read_text())
            self.assertIn('item.completed', path.read_text())

    def test_registration_persists_intent_before_api(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            def request(config, path, body=None, authenticated=True):
                self.assertTrue((state / 'enrollment-intent.json').exists())
                if path == '/api/admin/enrollments':
                    return {'code': 'code'}
                self.assertEqual(launcher.read(state / 'enrollment-intent.json')['code'], 'code')
                return {'device_id': body['device_id'], 'device_token': 'token'}
            with patch.object(launcher, 'api', side_effect=request):
                config, path, reuse = launcher.registration(state, 'https://example.com', 'test', False)
                self.assertFalse(reuse)
                self.assertEqual(config['device_token'], 'token')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_real_fake_cli_and_deadline_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen([sys.executable, '-c', 'import time; print("hello", flush=True); time.sleep(30)'], stdout=subprocess.PIPE, start_new_session=True)
            self.assertEqual(process.stdout.readline(), b'hello\n')
            launcher.stop(process)
            self.assertIsNotNone(process.poll())
            process.stdout.close()

    def test_false_cli_success_requires_server_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            launcher.save(state / 'receipt.json', {'server_url': 'https://example.com', 'label': 'test', 'reuse': False,
                          'provider': 'codex', 'binary': sys.executable, 'launchagent_label': 'test'})
            attempts = []
            config = {'server_url': 'https://example.com', 'device_id': 'device', 'device_token': 'token', 'state_dir': directory}
            def request(config, path, body=None, authenticated=True):
                if path == launcher.PREFIX:
                    return {'id': '11111111-1111-4111-8111-111111111111', 'instructions_url': 'https://example.com/instructions'}
                if path.endswith('/attempt'):
                    attempts.append(1)
                    return {'attempt': len(attempts), 'model': 'm', 'model_token': 't', 'base_url': 'https://example.com'}
                return {'status': 'pending', 'attempt': len(attempts)}
            fd = os.open(state / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
            with patch.object(launcher, 'registration', return_value=(config, state / 'device.json', False)), patch.object(launcher, 'api', side_effect=request), patch.object(launcher, 'verified_setup', return_value={'release': {}, 'model': launcher.EXPECTED_MODEL, 'install_instructions':'test', 'reuse_instructions':'test'}), patch.object(launcher, 'validate_settings'), patch.object(launcher, 'command', side_effect=lambda provider, binary, folder, settings: (folder.mkdir(parents=True, exist_ok=True) or [sys.executable, '-c', 'import sys; sys.stdin.read(); print("{}")'], os.environ.copy())):
                launcher.worker(state, fd)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(launcher.read(state / 'receipt.json')['status'], 'failed')

    def test_local_http_request_and_no_cross_origin_redirect(self):
        import http.server
        import threading
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1:1/leak')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"status":"ready"}')
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = {'server_url': 'http://127.0.0.1:' + str(server.server_port), 'device_token': 'private'}
            self.assertEqual(launcher.api(config, '/status')['status'], 'ready')
            with self.assertRaises(launcher.APIError):
                launcher.api(config, '/redirect')
            self.assertEqual(seen, ['Bearer private', 'Bearer private'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_inherited_lock_prevents_second_owner_after_parent_exit(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lock'
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], pass_fds=(fd,), start_new_session=True)
            os.close(fd)
            probe = os.open(path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                launcher.stop(process)
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                launcher.stop(process)
                os.close(probe)

    def test_expired_enrollment_renews_once_same_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            launcher.save(state / 'enrollment-intent.json', {'device_id': 'stable', 'enrollment_nonce': 'same', 'code': 'expired'})
            codes = []
            def request(config, path, body=None, authenticated=True):
                if path == '/api/admin/enrollments':
                    return {'code': 'fresh'}
                codes.append(body['code'])
                self.assertEqual(body['device_id'], 'stable')
                self.assertEqual(body['enrollment_nonce'], 'same')
                if body['code'] == 'expired':
                    raise launcher.APIError(401)
                return {'device_id': 'stable', 'device_token': 'private'}
            with patch.object(launcher, 'api', side_effect=request):
                launcher.registration(state, 'https://example.com', 'test', False)
            self.assertEqual(codes, ['expired', 'fresh'])

    def test_verified_heartbeat_stops_hanging_cli_after_grace(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        try:
            with patch.object(launcher, 'api', return_value={'status': 'ready'}) as request:
                result = launcher.wait_for_runner(process, {}, '/session', timeout=2, interval=.01, ready_grace=.03)
            self.assertEqual(result['status'], 'ready')
            self.assertIsNotNone(process.poll())
            request.assert_called_once()
        finally:
            launcher.stop(process)

    def test_readiness_poll_errors_do_not_falsely_finish(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        try:
            with patch.object(launcher, 'api', side_effect=RuntimeError('unavailable')) as request:
                result = launcher.wait_for_runner(process, {}, '/session', timeout=.05, interval=.01, ready_grace=.01)
            self.assertIsNone(result)
            self.assertIsNotNone(process.poll())
            self.assertGreater(request.call_count, 1)
        finally:
            launcher.stop(process)

    def test_status_command_preserves_interpreter_and_space_paths(self):
        import shlex
        with tempfile.TemporaryDirectory(prefix='helper runner ') as directory:
            output = io.StringIO()
            with patch.object(launcher, 'prerequisites', return_value=sys.executable), patch.object(launcher.subprocess, 'Popen') as spawn, patch.object(sys, 'argv', [str(SCRIPT), 'start', '--state-dir', directory]), patch('sys.stdout', output):
                spawn.return_value.pid = 123
                launcher.main()
            result = json.loads(output.getvalue())
            self.assertEqual(shlex.split(result['status_command']), [sys.executable, str(SCRIPT.resolve()), 'status', '--state-dir', str(Path(directory).resolve())])


class SignedSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import base64
        cls.folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.folder.name)
        cls.key = cls.root / 'private.pem'
        subprocess.run(['/usr/bin/openssl', 'genrsa', '-out', str(cls.key), '3072'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.public = subprocess.check_output(['/usr/bin/openssl', 'rsa', '-in', str(cls.key), '-pubout'], stderr=subprocess.DEVNULL).decode()

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def signed(self, **changes):
        import base64, hashlib
        document = {'schema':1,'revision':'test','issued_at':100,'expires_at':200,
                    'model':launcher.EXPECTED_MODEL,'release':{'sha256':'a'*64},
                    'model_catalog_sha256':hashlib.sha256(b'{"models":[]}').hexdigest(),
                    'install_instructions':'Install the signed release.','reuse_instructions':'Verify the existing helper.'}
        document.update(changes)
        payload = json.dumps(document).encode()
        source = self.root / 'payload'
        source.write_bytes(payload)
        signature = subprocess.check_output(['/usr/bin/openssl', 'dgst', '-sha256', '-sign', str(self.key), str(source)])
        return {'signed_setup':{'payload':base64.b64encode(payload).decode(), 'signature':base64.b64encode(signature).decode()}}, document

    def test_real_openssl_signature_and_tamper_rejection(self):
        import base64
        envelope, document = self.signed()
        with patch.object(launcher, 'PUBLIC_KEY', self.public):
            self.assertEqual(launcher.verified_setup(envelope, now=150), document)
            envelope['signed_setup']['payload'] = base64.b64encode(b'{}').decode()
            with self.assertRaises(RuntimeError): launcher.verified_setup(envelope, now=150)

    def test_wrong_key_expired_and_unexpected_model(self):
        envelope, _ = self.signed()
        with self.assertRaises(RuntimeError): launcher.verified_setup(envelope, now=150)
        with patch.object(launcher, 'PUBLIC_KEY', self.public):
            with self.assertRaises(RuntimeError): launcher.verified_setup(envelope, now=201)
            wrong_model, _ = self.signed(model='another-model')
            with self.assertRaises(RuntimeError): launcher.verified_setup(wrong_model, now=150)

    def test_catalog_and_crosshost_settings_rejected(self):
        _, document = self.signed()
        settings = {'attempt':1,'model_token':'secret','model':launcher.EXPECTED_MODEL,
                    'base_url':'https://example.com/api/model','model_catalog':{'models':[]}}
        launcher.validate_settings(settings, document, 'https://example.com', 'codex')
        for changed in ({'base_url':'https://other.example/api/model'}, {'model_catalog':{'models':['injected']}}, {'model':'other'}, {'unexpected':'value'}):
            with self.assertRaises(RuntimeError): launcher.validate_settings({**settings, **changed}, document, 'https://example.com', 'codex')

    def test_session_id_cannot_inject_url(self):
        for identifier in ('../instructions', 'https://other.example/x', 'not-a-uuid'):
            with self.assertRaises(RuntimeError): launcher.session_endpoint(identifier)

    def test_tamper_reports_attempt_zero_without_model_start(self):
        import base64
        envelope, _ = self.signed()
        envelope['signed_setup']['payload'] = base64.b64encode(b'{}').decode()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            launcher.save(state / 'receipt.json', {'server_url':'https://example.com', 'label':'test', 'reuse':False,
                          'provider':'codex', 'binary':sys.executable, 'launchagent_label':'test'})
            config = {'server_url':'https://example.com', 'device_id':'device', 'device_token':'token', 'state_dir':directory}
            calls = []
            def request(config, path, body=None, authenticated=True):
                calls.append((path, body))
                if path == launcher.PREFIX:
                    return {'id':'11111111-1111-4111-8111-111111111111', 'attempt':0}
                if path.endswith('/instructions'):
                    return envelope
                return {'status':'pending', 'attempt':0}
            fd = os.open(state / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
            with patch.object(launcher, 'registration', return_value=(config, state / 'device.json', False)), patch.object(launcher, 'api', side_effect=request), patch.object(launcher, 'PUBLIC_KEY', self.public), patch.object(launcher, 'command') as build_command:
                launcher.worker(state, fd)
                build_command.assert_not_called()
            self.assertFalse(any(path.endswith('/attempt') for path, _ in calls))
            failed = [body for path, body in calls if path.endswith('/events')]
            self.assertEqual(len(failed), 1)
            self.assertEqual((failed[0]['attempt'], failed[0]['event']), (0, 'failed'))
            self.assertEqual(launcher.read(state / 'receipt.json')['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
