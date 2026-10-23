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

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/helper-runner/scripts/launch.py'
spec = importlib.util.spec_from_file_location('launcher', SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_only_two_installed_files(self):
        files = [p.relative_to(SCRIPT.parents[1]).as_posix() for p in SCRIPT.parents[1].rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts]
        self.assertEqual(sorted(files), ['SKILL.md', 'scripts/launch.py'])

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
                    return {'id': 'session', 'instructions_url': 'https://example.com/instructions'}
                if path.endswith('/attempt'):
                    attempts.append(1)
                    return {'attempt': len(attempts), 'model': 'm', 'model_token': 't', 'base_url': 'https://example.com'}
                return {'status': 'pending', 'attempt': len(attempts)}
            fd = os.open(state / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
            with patch.object(launcher, 'registration', return_value=(config, state / 'device.json', False)), patch.object(launcher, 'api', side_effect=request), patch.object(launcher, 'command', side_effect=lambda provider, binary, folder, settings: (folder.mkdir(parents=True, exist_ok=True) or [sys.executable, '-c', 'import sys; sys.stdin.read(); print("{}")'], os.environ.copy())):
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


if __name__ == '__main__':
    unittest.main()
