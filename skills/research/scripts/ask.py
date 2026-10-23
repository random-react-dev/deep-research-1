#!/usr/bin/env python3
"""Start research once and inspect its saved receipt."""
import argparse
import contextlib
import codecs
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import threading
import launch
import urllib.request
import urllib.parse
import urllib.error

ClientError = RuntimeError

SERVICE = 'https://ask-gateway.r95426882.workers.dev'
PROTOCOL = 2
DEFAULT_STATE = Path.home() / 'Library/Caches/ResearchRunner'
STATE_DIR = DEFAULT_STATE
CONNECTION_DIR = launch.DEFAULT_STATE
PROVIDER = 'codex'
SCRIPT = Path(__file__).resolve()


def private_dir(path):
    if path.is_symlink():
        raise ClientError('State directory must not be a symlink.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_json(path, value):
    """Replace receipts atomically and sync the containing directory."""
    temp = path.parent / ('.write-' + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def job_path(job_id):
    if not re.fullmatch(r'ask_[a-f0-9]{32}', job_id):
        raise ClientError('Invalid job ID.')
    path = STATE_DIR / 'jobs' / job_id
    if path.is_symlink():
        raise ClientError('Job directory must not be a symlink.')
    return path


def read_job(job_id):
    try:
        return json.loads((job_path(job_id) / 'receipt.json').read_text())
    except FileNotFoundError:
        raise ClientError('Job not found.') from None


@contextlib.contextmanager
def client_lock():
    private_dir(STATE_DIR)
    fd = os.open(STATE_DIR / 'client.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    try:
        deadline = time.monotonic() + 0.75
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ClientError('Local state is busy. Retry the same question; it will reuse its saved job.')
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def retry_command(receipt):
    return 'cd -- ' + shlex.quote(receipt['workspace']) + ' && ' + shlex.join(
        [sys.executable, str(SCRIPT), '--state-dir', str(STATE_DIR), '--server-url', receipt.get('service', SERVICE), '--provider', receipt.get('provider', PROVIDER), '--new', receipt['question']])


def terminal_state(receipt):
    """An exclusive atomic link latches failure even if a runner responds late."""
    directory = job_path(receipt['job_id'])
    returned = directory / 'returned.json'
    if returned.exists(): receipt = {**receipt, **json.loads(returned.read_text())}
    outcome = directory / 'outcome.json'
    if outcome.exists():
        return {**receipt, **json.loads(outcome.read_text())}
    deadline = receipt.get('deadline_at')
    if deadline is not None and time.time() >= deadline and receipt['status'] in ('preparing', 'running'):
        value = {'status': 'failed', 'failure': 'deadline',
                 'message': 'The local waiting deadline expired. The server task was not cancelled and may still be running.',
                 'finished_at': time.time(), 'updated_at': time.time(),
                 'error': 'The local waiting deadline expired. The server task was not cancelled and may still be running.'}
        temporary = directory / ('.outcome-' + uuid.uuid4().hex)
        try:
            write_json(temporary, value)
            try:
                os.link(temporary, outcome)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return {**receipt, **json.loads(outcome.read_text())}
    return receipt


@contextlib.contextmanager
def free_job_locks(directory):
    descriptors = []
    available = True
    try:
        for name in ('run.lock', 'operation.lock'):
            fd = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                available = False
                break
        yield available
    finally:
        for fd in descriptors:
            os.close(fd)


def make_room(jobs):
    """Called under client.lock; active and recoverable requests are retained."""
    receipts = [terminal_state(read_job(path.parent.name)) for path in jobs.glob('ask_*/receipt.json')]
    if len(receipts) < 200:
        return
    eligible = [r for r in receipts if r['status'] in ('ready', 'failed', 'runner_finished') and
                (r.get('result_returned_at') or time.time() - r.get('finished_at', r['created_at']) > 7*86400)]
    for receipt in sorted(eligible, key=lambda item: (not bool(item.get('result_returned_at')), item['created_at'])):
        directory = job_path(receipt['job_id'])
        with free_job_locks(directory) as available:
            if available:
                shutil.rmtree(directory)
                if len(receipts) - 1 < 200:
                    return
                receipts.remove(receipt)
    if len(receipts) >= 220:
        raise ClientError('The 220-job limit is reached. Retrieve finished results or wait for retention eligibility before creating another request.')


def command_for(job_id, operation='status'):
    return shlex.join([sys.executable, str(SCRIPT), '--state-dir', str(STATE_DIR), '--' + operation, job_id])


def show(receipt, result=False):
    raw = receipt
    receipt = terminal_state(receipt)
    ready = raw.get('status') == 'ready' and bool(raw.get('report_path'))
    if result and ready:
        receipt = raw
    failed = receipt['status'] == 'failed'
    next_command = command_for(receipt['job_id'], 'result' if ready or receipt.get('failure') == 'deadline' else 'status')
    if failed and receipt.get('failure') != 'deadline':
        next_command = retry_command(receipt)
    heartbeat = job_path(receipt['job_id']) / 'heartbeat.json'
    beat = json.loads(heartbeat.read_text()) if heartbeat.exists() else {}
    message = receipt.get('message', 'Research takes time. Check this same job; do not submit it again.')
    active = False
    lock_path = job_path(receipt['job_id']) / 'run.lock'
    if lock_path.exists():
        probe = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try: fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: active = True
        finally: os.close(probe)
    if not failed and not ready and (not active or time.time() - beat.get('updated_at', receipt.get('created_at', time.time())) > 15):
        message = 'Local coordination is stale. Resume this same request; an active lock will prevent overlapping processing.'
        next_command = command_for(receipt['job_id'], 'resume')
    output = {'job_id': receipt['job_id'], 'status': receipt['status'], 'phase': receipt.get('phase', receipt['status']),
              'last_update': receipt.get('updated_at', receipt.get('created_at')), 'error': receipt.get('error') or (message if failed else None),
              'next_command': next_command, 'message': message}
    if failed:
        output['new_request_fallback'] = retry_command(receipt)
    if ready and result:
        # Read one extra byte to signal truncation without loading a large report.
        limit = 200 * 1024
        with open(raw['report_path'], 'rb') as stream:
            data = stream.read(limit + 1)
        text = codecs.getincrementaldecoder('utf-8')(errors='replace').decode(data[:limit], final=len(data) <= limit)
        encoded = text.encode('utf-8')
        output.update(report_path=raw['report_path'], deliverables=raw.get('deliverables', []),
                      report=encoded[:limit].decode('utf-8', errors='ignore'),
                      report_truncated=len(data) > limit or len(encoded) > limit)
        marker = job_path(receipt['job_id']) / 'returned.json'
        write_json(marker, {'result_returned_at': time.time()})
    print(json.dumps(output), flush=True)
    return 0 if ready and result else 1 if failed else 2 if result else 0


def spawn_coordinator(receipt, readonly=False):
    directory = job_path(receipt['job_id'])
    fd = os.open(directory / 'run.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        logfd = os.open(directory / 'run.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(logfd, 'ab') as log:
            args = [sys.executable, str(SCRIPT), '--state-dir', str(STATE_DIR), '--coordinate', receipt['job_id'], '--lock-fd', str(fd)]
            if readonly:
                args.append('--read-only')
            subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, pass_fds=(fd,),
                             start_new_session=True, cwd=receipt['workspace'])
    finally:
        os.close(fd)  # Closing this reference does not unlock the child's reference.


def start(question, fresh=False, timeout=3600):
    question = question.strip()
    if not question or len(question.encode()) > 2048:
        raise ClientError('Provide a focused question of 1–2,048 UTF-8 bytes.')
    workspace = str(Path.cwd().resolve())
    question_hash = hashlib.sha256(question.encode()).hexdigest()
    with client_lock():
        jobs = STATE_DIR / 'jobs'
        private_dir(jobs)
        if not fresh:
            matches = []
            for path in jobs.glob('ask_*/receipt.json'):
                receipt = read_job(path.parent.name)
                if (receipt['question_hash'], receipt['workspace'], receipt['service'], receipt.get('provider','codex')) == (question_hash, workspace, SERVICE, PROVIDER):
                    matches.append(receipt)
            if matches:
                receipt = terminal_state(max(matches, key=lambda r: r['created_at']))
                if receipt['status'] in ('preparing', 'running'):
                    spawn_coordinator(receipt)
                show(receipt)
                return receipt
        # Fail before committing a new job; status and recovery remain independent of CLI availability.
        launch.prerequisites(PROVIDER)
        make_room(jobs)
        job_id = 'ask_' + uuid.uuid4().hex
        directory = job_path(job_id)
        private_dir(directory)
        receipt = {'job_id': job_id, 'question': question, 'question_hash': question_hash,
                   'workspace': workspace, 'service': SERVICE, 'idempotency_key': uuid.uuid4().hex,
                   'created_at': time.time(), 'status': 'preparing', 'launcher_protocol': PROTOCOL,
                   'provider': PROVIDER, 'connection_dir': str(CONNECTION_DIR)}
        receipt['deadline_at'] = receipt['created_at'] + timeout
        write_json(directory / 'receipt.json', receipt)
        spawn_coordinator(receipt)
        show(receipt)
        return receipt


def update(receipt, **values):
    receipt.update(values, updated_at=time.time())
    write_json(job_path(receipt['job_id']) / 'receipt.json', receipt)


def connect_research(receipt):
    """Only the signed launcher connects the computer; this client never installs code."""
    connection = Path(receipt['connection_dir'])
    record = connection / 'receipt.json'
    if not record.exists():
        command = [sys.executable, str(SCRIPT.with_name('launch.py')), 'start', '--provider', receipt['provider'],
                   '--server-url', receipt['service'], '--state-dir', str(connection)]
        if connection != launch.DEFAULT_STATE:
            command += ['--launchagent-label', 'com.helper-runner.research.' + hashlib.sha256(str(connection).encode()).hexdigest()[:12],
                        '--label', 'ResearchRunner-' + platform.node()]
        result = subprocess.run(command, capture_output=True, text=True, timeout=40)
        if result.returncode:
            raise ClientError('Connection launcher failed: ' + result.stderr[:1000])
        update(receipt, phase='connecting', message='Connecting this Mac. Check this same research job.')
        return False
    status = json.loads(record.read_text())
    if status.get('status') == 'failed':
        raise ClientError('Connection setup failed: ' + str(status.get('message', 'inspect connection status')))
    if status.get('status') != 'ready':
        update(receipt, phase='connecting', message='Waiting for the server to verify this Mac connection.')
        return False
    session = json.loads((connection / 'session.json').read_text())
    credentials = Path(session['credential_file'])
    config = json.loads(credentials.read_text())
    root = Path(config['state_dir'])
    installed = root / 'config.json'
    installed_config = json.loads(installed.read_text()) if installed.is_file() else config
    if installed_config.get('model') != launch.EXPECTED_MODEL or not installed_config.get('research_gateway_enabled'):
        raise ClientError('Existing connection is incompatible with this research model or gateway. It was not modified. Use a separate --state-dir for an isolated connection.')
    runtime = root / 'runtime/common.py'
    if not runtime.is_file() or 'preflight_research' not in runtime.read_text():
        raise ClientError('Existing connection lacks the required research runtime. It was not modified.')
    update(receipt, credential_file=str(credentials), device_id=config['device_id'], phase='connected')
    return True


def connection_config(receipt):
    config = json.loads(Path(receipt['credential_file']).read_text())
    previous, selected = launch.origin(config['server_url']), launch.origin(receipt['service'])
    if previous != selected and not {previous, selected} <= launch.SERVER_ALIASES:
        raise ClientError('Saved connection belongs to another service.')
    if receipt.get('device_id') and receipt['device_id'] != config.get('device_id'):
        raise ClientError('Saved research belongs to another computer.')
    return {**config, 'server_url':selected}


def observe(receipt, config):
    identifier = urllib.parse.quote(receipt['task_id'], safe='')
    data = launch.api(config, '/api/device/research/tasks/' + identifier)
    if data['device_id'] != receipt['device_id']:
        raise ClientError('Task belongs to another computer.')
    state = data['state']
    phase = {'pending':'queued', 'running':'researching', 'completed':'retrieving'}.get(state,state)
    values = dict(server_state=state, phase=phase, last_server_update=time.time())
    if state in ('failed','cancelled'):
        message = str(data.get('error') or 'Research ' + state)
        values.update(status='failed', error=message, message=message, finished_at=time.time())
    else:
        values.update(status='running', error=None, message='Research is ' + phase + '. Check this same job.')
    update(receipt, **values)
    return data


def download(config, path, stream, size, digest):
    request = urllib.request.Request(config['server_url'] + path, headers={
        'Authorization':'Bearer ' + config['device_token'], 'User-Agent':'helper-runner/1.0', 'Accept-Encoding':'identity'})
    checksum, count = hashlib.sha256(), 0
    with urllib.request.build_opener(launch.NoRedirect()).open(request, timeout=30) as response:
        while True:
            chunk = response.read(min(1024*1024, size-count+1))
            if not chunk: break
            count += len(chunk)
            if count > size: raise ClientError('Result exceeds its declared size.')
            checksum.update(chunk)
            stream.write(chunk)
    if count != size: raise ConnectionError('Interrupted result download')
    if checksum.hexdigest() != digest: raise ClientError('Result checksum verification failed.')


def retrieve(receipt, config, data):
    outputs = (data.get('result') or {}).get('outputs', [])
    if not isinstance(outputs, list) or len(outputs) > 128:
        raise ClientError('Invalid result manifest.')
    if not any(item.get('path') == 'report.md' and not item.get('generated_response') for item in outputs):
        raise ClientError('The completed attempt did not provide report.md.')
    directory = job_path(receipt['job_id']) / 'results'
    private_dir(directory)
    names, delivered, pending = set(), [], False
    for item in outputs:
        relative = item['path']
        parts = relative.split('/')
        if (not relative or '\\' in relative or any(not part or part in ('.','..') or part.startswith('.') for part in parts)
                or any(ord(c)<32 for c in relative) or ':' in relative or relative.casefold() in names):
            raise ClientError('The result manifest contains an unsafe or duplicate path.')
        names.add(relative.casefold())
        target = directory
        for part in parts[:-1]:
            target = target / part
            private_dir(target)
        target = target / parts[-1]
        if target.is_symlink(): raise ClientError('Result file cannot be a symlink.')
        size, digest = item['size'], item['sha256']
        if type(size) is not int or not 0 <= size <= 2*1024**3 or not re.fullmatch('[a-f0-9]{64}',digest):
            raise ClientError('Invalid result checksum or size.')
        if target.is_file() and target.stat().st_size == size:
            checksum = hashlib.sha256()
            with target.open('rb') as stream:
                for block in iter(lambda: stream.read(1024*1024), b''): checksum.update(block)
            if checksum.hexdigest() == digest:
                delivered.append(str(target.resolve())); continue
        if item.get('delivery') == 'blocked': raise ClientError('A deliverable could not be returned: ' + relative)
        identifier = urllib.parse.quote(item['id'], safe='')
        if item.get('delivery') == 'background':
            transfer = launch.api(config, '/api/device/uploads/' + identifier)
            if transfer.get('state') != 'ready': pending=True; continue
            url = '/api/device/uploads/' + identifier + '/download'
        else:
            url = '/api/device/files/' + identifier
        temporary = target.parent / ('.download-' + hashlib.sha256(relative.encode()).hexdigest())
        try:
            temporary.unlink(missing_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                download(config, url, stream, size, digest)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,target)
            fd = os.open(target.parent,os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally: temporary.unlink(missing_ok=True)
        delivered.append(str(target.resolve()))
    if not pending:
        update(receipt,status='ready',phase='ready',error=None,message='Report ready. Read it before answering.',
               report_path=str((directory/'report.md').resolve()),deliverables=delivered,
               completed_attempt=data.get('attempt_id'),finished_at=time.time())


def coordinate(job_id, lock_fd=None, readonly=False):
    directory = job_path(job_id)
    fd = lock_fd if lock_fd is not None else os.open(directory / 'run.lock', os.O_RDWR | os.O_CREAT, 0o600)
    stop = threading.Event()
    def heartbeat():
        while not stop.is_set():
            write_json(directory / 'heartbeat.json', {'pid': os.getpid(), 'updated_at': time.time()})
            stop.wait(2)
    thread = None
    try:
        if lock_fd is None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
        with client_lock():
            receipt = read_job(job_id)
        if terminal_state(receipt)['status'] not in ('preparing', 'running') and not readonly:
            return 0
        thread = threading.Thread(target=heartbeat, daemon=True); thread.start()
        end = time.time() + 300 if readonly else receipt.get('deadline_at', time.time()+3600)
        while time.time() < end:
            receipt = read_job(job_id)
            if receipt['status'] == 'ready':
                return 0
            if not readonly and terminal_state(receipt)['status'] == 'failed':
                return 1
            try:
                if not receipt.get('credential_file'):
                    if readonly:
                        return 2  # Never bootstrap or submit after the local deadline.
                    if not connect_research(receipt):
                        stop.wait(2)
                        continue
                config = connection_config(receipt)
                if not receipt.get('task_id'):
                    if readonly:
                        try:
                            data = launch.api(config, '/api/device/research/tasks/lookup?idempotency_key=' + urllib.parse.quote(receipt['idempotency_key'], safe=''))
                        except launch.APIError as error:
                            if error.code == 404: return 2
                            raise
                        update(receipt, task_id=data['id'], device_id=config['device_id'])
                    else:
                        if time.time() >= receipt['deadline_at']: break
                        submission = receipt.get('submission') or {'question':receipt['question'], 'provider':receipt['provider'], 'idempotency_key':receipt['idempotency_key']}
                        update(receipt, submission=submission, device_id=config['device_id'], status='running', phase='submitting')
                        data = launch.api(config, '/api/device/research/tasks', submission)
                        update(receipt, task_id=data['task_id'], server_state=data['state'])
                data = observe(receipt, config)
                if data['state'] == 'completed':
                    retrieve(receipt, config, data)
                    if receipt['status'] == 'ready': return 0
                if receipt['status'] == 'failed': return 1
                if readonly and data['state'] != 'completed': return 2
            except launch.APIError as error:
                if error.code not in (408, 429, 500, 502, 503, 504): raise
                update(receipt, phase='waiting_for_connection', message='Service temporarily unavailable; retrying the same request.')
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                update(receipt, phase='waiting_for_connection', message='Connection interrupted; retrying the same request.')
            except RuntimeError as error:
                if str(error) != 'Server connection failed': raise
                update(receipt, phase='waiting_for_connection', message='Connection interrupted; retrying the same request.')
            stop.wait(2)
        if not readonly: terminal_state(read_job(job_id))
        return 1
    except (ClientError, OSError, ValueError, KeyError) as exc:
        receipt = read_job(job_id)
        if receipt['status'] not in ('ready', 'failed'):
            receipt.update(status='failed', error=str(exc), message=str(exc), updated_at=time.time())
            write_json(directory / 'receipt.json', receipt)
        return 1
    finally:
        stop.set()
        if thread: thread.join(timeout=3)
        os.close(fd)


def main():
    global STATE_DIR, CONNECTION_DIR, SERVICE, PROVIDER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('question', nargs='?')
    parser.add_argument('--new', action='store_true')
    parser.add_argument('--state-dir', type=Path, default=STATE_DIR)
    parser.add_argument('--server-url', default=SERVICE)
    parser.add_argument('--provider', choices=('codex','claude'), default=PROVIDER)
    parser.add_argument('--timeout-seconds', type=int, default=3600)
    parser.add_argument('--status', metavar='JOB_ID')
    parser.add_argument('--result', metavar='JOB_ID')
    parser.add_argument('--resume', metavar='JOB_ID')
    parser.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--read-only', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--coordinate', metavar='JOB_ID', help=argparse.SUPPRESS)
    args = parser.parse_args()
    STATE_DIR = args.state_dir.expanduser().resolve()
    CONNECTION_DIR = launch.DEFAULT_STATE if STATE_DIR == DEFAULT_STATE.resolve() else STATE_DIR / 'connection'
    SERVICE = launch.origin(args.server_url)
    PROVIDER = args.provider
    if args.timeout_seconds <= 0:
        parser.error('--timeout-seconds must be positive.')
    if sum(value is not None for value in (args.question, args.status, args.result, args.resume, args.coordinate)) != 1 or args.new and args.question is None:
        parser.error('Use a question, --status JOB_ID, or an internal request operation.')
    if args.status or args.result or args.resume:
        identifier = args.status or args.result or args.resume
        receipt = read_job(identifier)
        state = terminal_state(receipt)
        if args.resume and state['status'] in ('preparing', 'running'):
            with client_lock(): spawn_coordinator(receipt)
        # Deadline recovery only observes the same task: it cannot submit or execute.
        if args.result and state.get('failure') == 'deadline' and receipt.get('status') != 'ready':
            with client_lock(): spawn_coordinator(receipt, readonly=True)
        return show(receipt, result=bool(args.result))
    if args.coordinate:
        return coordinate(args.coordinate, args.lock_fd, args.read_only)
    receipt = start(args.question, args.new, args.timeout_seconds)
    return 1 if terminal_state(receipt)['status'] == 'failed' else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ClientError, OSError, ValueError, KeyError) as exc:
        print('Ask: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
