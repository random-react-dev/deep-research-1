"""Local HTTP fault checks for the server instruction's shell download pattern."""
import hashlib
import http.server
import io
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
import threading
import unittest
import zipfile


class DownloadFaultTests(unittest.TestCase):
    def test_live_instruction_pattern_keeps_retry_and_fail_closed(self):
        source = (Path(__file__).resolve().parents[1] / 'server/helper_bootstrap.py').read_text()
        self.assertIn('curl --fail --show-error --location --retry 3 --retry-all-errors ', source)
        self.assertIn('set -eu\numask 077', source)
        self.assertLess(source.index('shasum -a 256 -c -'), source.index('unzip -qo'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='helper-download-fault-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('runtime/fixture.txt', os.urandom(65536))
        self.data = buffer.getvalue()
        self.requests = 0
        self.drop_first = False
        fixture = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                fixture.requests += 1
                self.send_response(200)
                self.send_header('Content-Length', str(len(fixture.data)))
                self.end_headers()
                if fixture.drop_first and fixture.requests == 1:
                    self.wfile.write(fixture.data[:len(fixture.data)//2])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                else:
                    self.wfile.write(fixture.data)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def download(self, digest, retry_all_errors=True):
        url = 'http://127.0.0.1:' + str(self.server.server_port) + '/artifact.zip'
        archive = self.root / 'helper.zip'
        destination = self.root / 'unpacked'
        script = '\n'.join([
            'set -eu', 'umask 077',
            '/usr/bin/curl --fail --show-error --location --retry 3 ' + ('--retry-all-errors ' if retry_all_errors else '') + shlex.quote(url) + ' -o ' + shlex.quote(str(archive)),
            'echo ' + shlex.quote(digest + '  ' + str(archive)) + ' | shasum -a 256 -c -',
            'mkdir -p ' + shlex.quote(str(destination)),
            'unzip -qo ' + shlex.quote(str(archive)) + ' -d ' + shlex.quote(str(destination)),
        ])
        return subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True, timeout=20)

    def test_halfway_disconnect_retries_then_verifies_and_extracts(self):
        self.drop_first = True
        result = self.download(hashlib.sha256(self.data).hexdigest())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.requests, 2)
        self.assertEqual((self.root / 'helper.zip').read_bytes(), self.data)
        self.assertTrue((self.root / 'unpacked/runtime/fixture.txt').is_file())

    def test_wrong_checksum_never_extracts(self):
        result = self.download('0' * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.requests, 1)
        self.assertFalse((self.root / 'unpacked').exists())

    def test_original_retry_flag_does_not_recover_partial_body(self):
        self.drop_first = True
        result = self.download(hashlib.sha256(self.data).hexdigest(), retry_all_errors=False)
        self.assertEqual(result.returncode, 18, result.stderr)
        self.assertEqual(self.requests, 1)
        self.assertFalse((self.root / 'unpacked').exists())


if __name__ == '__main__': unittest.main()
