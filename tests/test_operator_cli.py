"""Operator enrollment is opt-in and never exposed to untrusted MCP tools."""
import contextlib
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_workspace.core import Workspace
from agent_workspace.operator_cli import main


class OperatorCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'workspace.db'
        self.ws=Workspace(self.db)
        self.root=self.ws.bootstrap('operator demo',self.tmp.name)

    def test_enroll_credential_once_without_printing_operator_credential(self):
        output=StringIO()
        with patch('agent_workspace.operator_cli.getpass.getpass',return_value=self.root['operator_credential']):
            with contextlib.redirect_stdout(output):
                result=main(['enroll','--db',str(self.db),'--name','codex','--provider','codex'])
        self.assertEqual(result,0)
        self.assertNotIn(self.root['operator_credential'],output.getvalue())
        import json
        agent=json.loads(output.getvalue())
        self.assertEqual(self.ws.open_session(agent['credential'])['role'],'agent')

    def test_enroll_bad_credential_fails_without_echo(self):
        output=StringIO()
        with patch('agent_workspace.operator_cli.getpass.getpass',return_value='private-wrong-secret'):
            with contextlib.redirect_stderr(output):
                with self.assertRaises(SystemExit) as err:
                    main(['enroll','--db',str(self.db),'--name','x','--provider','x'])
        self.assertEqual(err.exception.code,2)
        self.assertNotIn('private-wrong-secret',output.getvalue())

    def test_create_task_is_operator_only_and_idempotent(self):
        import json
        args=['create-task','--db',str(self.db),'--title','Compatibility pilot',
              '--acceptance-criteria','Read and hand off verified context',
              '--request-key','pilot-compat-task']
        with patch('agent_workspace.operator_cli.getpass.getpass',return_value=self.root['operator_credential']):
            with contextlib.redirect_stdout(StringIO()) as output:
                self.assertEqual(main(args),0)
            first=json.loads(output.getvalue())
            with contextlib.redirect_stdout(StringIO()) as output:
                self.assertEqual(main(args),0)
            self.assertEqual(first,json.loads(output.getvalue()))
        self.assertEqual(first['status'],'Ready')
        self.assertEqual(len(self.ws.list_tasks(self.ws.open_session(self.root['operator_credential'])['session_token'])),1)

    def test_missing_db_never_creates_new_workspace(self):
        nonexistent=Path(self.tmp.name)/'nonexistent.db'
        with contextlib.redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                main(['enroll','--db',str(nonexistent),'--name','x','--provider','x'])
        self.assertFalse(nonexistent.exists())


if __name__=='__main__':
    unittest.main()
