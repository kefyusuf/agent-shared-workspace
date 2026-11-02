"""Credential-safe local readiness checks cannot claim actual CLI compatibility."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from agent_workspace.broker import create_server
from agent_workspace.core import Workspace

ROOT = Path(__file__).resolve().parents[1]


class HostPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.ws = Workspace(Path(cls.temp.name) / 'workspace.db')
        boot = cls.ws.bootstrap('preflight pilot', cls.temp.name)
        op = cls.ws.open_session(boot['operator_credential'])['session_token']
        cls.agent = cls.ws.enroll_agent(op, 'preflight', 'codex')
        cls.server = create_server(cls.ws, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def invoke(self, credential='', host='none', port=None):
        env = os.environ.copy()
        env['AW_AGENT_CREDENTIAL'] = credential
        # This script may be run without a package installation.
        env['PYTHONPATH'] = str(ROOT / 'src')
        command = [sys.executable, str(ROOT / 'scripts' / 'host_preflight.py'),
                   '--port', str(port or self.server.server_port), '--host', host]
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=20, env=env, cwd=ROOT)

    def test_success_is_not_real_provider_verification(self):
        result = self.invoke(self.agent['credential'])
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['broker_reachable'])
        self.assertEqual(data['status'], 'PREFLIGHT_PASSED_PROVIDER_TEST_PENDING')
        self.assertEqual(data['stdio'], {'handshake':True, 'tool_inventory':True, 'agent_identity':True})
        self.assertFalse(data['real_provider_verified'])
        self.assertNotIn(self.agent['credential'], result.stdout + result.stderr)
        self.assertNotIn('session_token', result.stdout)

    def test_no_credential_is_fail_closed_and_redacted(self):
        result = self.invoke()
        self.assertEqual(result.returncode,1)
        data = json.loads(result.stdout)
        self.assertEqual(data['status'], 'PREFLIGHT_FAILED')
        self.assertFalse(data['agent_credential_supplied'])

    def test_bad_credential_fails_without_echo(self):
        credential = 'very-sensitive-bad-credential'
        result = self.invoke(credential)
        self.assertEqual(result.returncode,1)
        self.assertFalse(json.loads(result.stdout)['stdio']['agent_identity'])
        self.assertNotIn(credential, result.stdout+result.stderr)

    def test_missing_broker_fails(self):
        import socket
        probe = socket.socket()
        probe.bind(('127.0.0.1',0))
        port=probe.getsockname()[1]
        # No other process can take the bound port during probe.
        try:
            result=self.invoke(self.agent['credential'],port=port)
            self.assertEqual(result.returncode,1)
            self.assertFalse(json.loads(result.stdout)['broker_reachable'])
        finally:
            probe.close()


if __name__ == '__main__':
    unittest.main()
