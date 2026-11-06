"""Script orchestration tests: no provider CLI or Windows environment required."""
from __future__ import annotations

from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import sqlite3
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / 'scripts'))

import workspace_cli as cli  # noqa: E402
from agent_workspace.core import Workspace


class WorkspaceScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / '.local' / 'workspace.db'

    def test_setup_bootstraps_two_distinct_agents_and_a_pilot_task(self):
        out = StringIO()
        with redirect_stdout(out):
            cli._setup(self.db)
        printed = out.getvalue()
        self.assertIn('ONE-TIME OPERATOR CREDENTIAL', printed)
        self.assertIn('ONE-TIME CODEX AGENT CREDENTIAL', printed)
        self.assertIn('ONE-TIME CLAUDE AGENT CREDENTIAL', printed)
        operator = printed.split('ONE-TIME OPERATOR CREDENTIAL (store securely): ')[1].splitlines()[0]
        codex = printed.split('ONE-TIME CODEX AGENT CREDENTIAL: ')[1].splitlines()[0]
        claude = printed.split('ONE-TIME CLAUDE AGENT CREDENTIAL: ')[1].splitlines()[0]
        self.assertEqual(len({operator, codex, claude}), 3)
        ws = Workspace(self.db)
        operator_session = ws.open_session(operator)['session_token']
        self.assertEqual(ws.open_session(codex)['role'], 'agent')
        self.assertEqual(ws.open_session(claude)['role'], 'agent')
        tasks = ws.list_tasks(operator_session)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['status'], 'Ready')
        raw_db = self.db.read_bytes()
        for credential in (operator, codex, claude):
            self.assertNotIn(credential.encode('ascii'), raw_db)

    def test_setup_is_idempotent_and_does_not_reprint_secrets(self):
        with redirect_stdout(StringIO()):
            cli._setup(self.db)
        output = StringIO()
        with redirect_stdout(output):
            cli._setup(self.db)
        self.assertIn('already initialized', output.getvalue())
        self.assertNotIn('CREDENTIAL', output.getvalue())
        with closing(sqlite3.connect(self.db)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM principals').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)

    def test_setup_never_overwrites_existing_uninitialized_database(self):
        self.db.parent.mkdir(parents=True)
        self.db.write_bytes(b'not a database')
        with self.assertRaises(cli.SetupError):
            cli._setup(self.db)
        self.assertEqual(self.db.read_bytes(), b'not a database')

    def test_connect_uses_only_non_secret_mcp_commands(self):
        commands = []
        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            return SimpleNamespace(returncode=(1 if cmd[2] == 'get' else 0))
        with patch.object(cli.shutil, 'which', side_effect=lambda p: p), \
                patch.object(cli.subprocess, 'run', side_effect=fake_run):
            with redirect_stdout(StringIO()):
                cli._connect('both')
        self.assertEqual(len(commands), 4)
        codex, claude = commands[1], commands[3]
        self.assertEqual(codex[1:4], ['mcp','add','agent_workspace'])
        self.assertEqual(claude[1:7], ['mcp','add','--scope','local','--transport','stdio'])
        for command in (codex, claude):
            self.assertIn(str(cli.MCP_ENTRY), command)
            self.assertNotIn('AW_AGENT_CREDENTIAL', ' '.join(command))
            self.assertNotIn('awc_', ' '.join(command))

    def test_connect_does_not_replace_existing_registration(self):
        with patch.object(cli.shutil, 'which', return_value='codex'), \
                patch.object(cli.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            with redirect_stdout(StringIO()):
                cli._connect('codex')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][1:3], ['mcp', 'get'])

    def test_missing_cli_is_nonfatal_during_connect(self):
        with patch.object(cli.shutil, 'which', return_value=None):
            with redirect_stdout(StringIO()) as output:
                cli._connect('both')
        self.assertIn('not installed', output.getvalue())

    def test_run_agent_does_not_launch_if_broker_missing(self):
        with patch.object(cli.shutil, 'which', return_value='codex'), \
                patch.object(cli, '_health', return_value=False), \
                patch.object(cli.getpass, 'getpass') as prompt:
            with self.assertRaises(cli.SetupError):
                cli._run_agent('codex', 8765)
            prompt.assert_not_called()

    def test_run_agent_keeps_credential_out_of_cli_arguments_and_stdout(self):
        calls = []
        def capture(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return SimpleNamespace(returncode=0)
        report = {'stdio': {'handshake':True}, 'real_provider_verified':False}
        with patch.object(cli.shutil,'which',return_value='codex'), \
                patch.object(cli,'_health',return_value=True), \
                patch.object(cli.getpass,'getpass',return_value='awc_SUPER_PRIVATE'), \
                patch.object(cli,'preflight_run',return_value=(report,True)), \
                patch.object(cli.subprocess,'run',side_effect=capture), \
                redirect_stdout(StringIO()) as output:
            self.assertEqual(cli._run_agent('codex',8765),0)
        cmd, kw = calls[0]
        self.assertNotIn('awc_SUPER_PRIVATE',' '.join(cmd))
        self.assertEqual(kw['env']['AW_AGENT_CREDENTIAL'],'awc_SUPER_PRIVATE')
        self.assertIn('AW_AGENT_CREDENTIAL',cmd[-1])  # names, not values
        self.assertNotIn('awc_SUPER_PRIVATE',output.getvalue())

    def test_preflight_failure_never_starts_provider(self):
        with patch.object(cli.shutil,'which',return_value='claude'), \
                patch.object(cli,'_health',return_value=True), \
                patch.object(cli.getpass,'getpass',return_value='awc_PRIVATE'), \
                patch.object(cli,'preflight_run',return_value=({'status':'failed'},False)), \
                patch.object(cli.subprocess,'run') as run, redirect_stdout(StringIO()):
            with self.assertRaises(cli.SetupError):
                cli._run_agent('claude',8765)
            run.assert_not_called()

    def test_doctor_does_not_disclose_credentials(self):
        with patch.object(cli, '_health',return_value=False), \
                patch.object(cli.shutil,'which',return_value=None), \
                redirect_stdout(StringIO()) as output:
            report = cli._doctor(8765,self.db)
        self.assertFalse(report['real_provider_verified'])
        self.assertFalse(report['workspace_initialized'])
        self.assertFalse(report['broker_reachable'])
        self.assertEqual(json.loads(output.getvalue()),report)

    def test_doctor_detects_individual_mcp_registrations_without_claiming_real_client_support(self):
        calls = []
        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0 if command[0] == 'codex' else 1)
        with patch.object(cli.shutil, 'which', side_effect=lambda name: name), \
                patch.object(cli.subprocess, 'run', side_effect=fake_run), \
                patch.object(cli, '_health', return_value=False), \
                redirect_stdout(StringIO()) as output:
            result = cli._doctor(8765, self.db)
        self.assertTrue(result['codex_installed'])
        self.assertTrue(result['claude_installed'])
        self.assertTrue(result['codex_mcp_registered'])
        self.assertFalse(result['claude_mcp_registered'])
        self.assertFalse(result['real_provider_verified'])
        self.assertEqual(len(calls), 2)
        self.assertEqual([call[0][1:] for call in calls],
                         [['mcp', 'get', 'agent_workspace']] * 2)
        self.assertTrue(all(call[1]['capture_output'] and call[1]['timeout'] == 5 for call in calls))
        self.assertEqual(result, json.loads(output.getvalue()))

    def test_doctor_skips_mcp_probes_when_cli_is_missing(self):
        with patch.object(cli.shutil, 'which', return_value=None), \
                patch.object(cli.subprocess, 'run') as run, \
                patch.object(cli, '_health', return_value=False), \
                redirect_stdout(StringIO()):
            result = cli._doctor(8765, self.db)
        run.assert_not_called()
        self.assertFalse(result['codex_mcp_registered'])
        self.assertFalse(result['claude_mcp_registered'])

    def test_doctor_swallows_registration_failures_without_echoing_sensitive_cli_output(self):
        secret = 'private-mcp-config-token'
        def broken(*_args, **_kwargs):
            raise OSError(secret)
        with patch.object(cli.shutil, 'which', side_effect=lambda p: p), \
                patch.object(cli.subprocess, 'run', side_effect=broken), \
                patch.object(cli, '_health', return_value=False), \
                redirect_stdout(StringIO()) as output:
            result = cli._doctor(8765, self.db)
        self.assertFalse(result['codex_mcp_registered'])
        self.assertFalse(result['claude_mcp_registered'])
        self.assertNotIn(secret, output.getvalue())

    def test_powershell_wrapper_remains_thin_and_has_no_secrets(self):
        wrapper = (HERE / 'workspace.ps1').read_text(encoding='utf-8')
        self.assertIn('scripts/workspace_cli.py',wrapper)
        self.assertIn('@args',wrapper)
        self.assertNotIn('AW_AGENT_CREDENTIAL',wrapper)


if __name__ == '__main__':
    unittest.main()
