"""M1.3 security/recovery contracts. Written before implementation."""
import pathlib
import os
import sqlite3
import tempfile
import unittest

from agent_workspace import Workspace, WorkspaceError


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecoverySecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / 'workspace.db'
        self.clock = Clock()
        self.ws = Workspace(self.db, clock=self.clock)
        init = self.ws.bootstrap('test-project', self.temp.name)
        self.op_credential = init['operator_credential']
        self.op = self.ws.open_session(self.op_credential)['session_token']
        self.agent = self.ws.enroll_agent(self.op, 'claude', 'claude')
        self.cred = self.agent['credential']
        self.a = self.ws.open_session(self.cred)['session_token']
        self.other = self.ws.enroll_agent(self.op, 'codex', 'codex')
        self.b = self.ws.open_session(self.other['credential'])['session_token']

    def err(self, code, func):
        with self.assertRaises(WorkspaceError) as ctx:
            func()
        self.assertEqual(code, ctx.exception.code)

    def claim(self, idx=0):
        task = self.ws.create_task(self.op, f'task {idx}', 'pass tests', f't{idx}')
        lease = self.ws.claim_task(self.a, task['task_id'], 0, f'c{idx}')
        return task, lease

    @unittest.skipUnless(os.name == 'posix', 'POSIX filesystem protection')
    def test_database_created_private_before_open(self):
        self.assertEqual(0o600, self.db.stat().st_mode & 0o777)
        target = pathlib.Path(self.temp.name) / 'real.db'
        with sqlite3.connect(target) as db:
            db.execute('CREATE TABLE safe (id INTEGER)')
        symlink = pathlib.Path(self.temp.name) / 'alias.db'
        symlink.symlink_to(target)
        self.err('UNSAFE_STORAGE', lambda: Workspace(symlink, clock=self.clock))

    def test_schema_version_is_explicit_and_persists(self):
        self.assertEqual(5, self.ws.schema_version())
        self.assertEqual(5, Workspace(self.db, clock=self.clock).schema_version())
        with sqlite3.connect(self.db) as db:
            self.assertEqual([1, 2, 3, 4, 5], [x[0] for x in db.execute('SELECT version FROM schema_migrations ORDER BY version')])

    def test_upgrade_legacy_version_zero_with_records_preserved(self):
        task, lease = self.claim()
        with sqlite3.connect(self.db) as db:
            db.execute('DROP TABLE schema_migrations')
            db.execute('DROP TABLE context_handoffs')
            db.execute('DROP TABLE context_sources')
            db.execute('DROP TABLE context_snapshots')
            db.execute('DROP TABLE knowledge_sources')
            db.execute('DROP TABLE knowledge_decisions')
            db.execute('DROP TABLE memory_fts')
            db.execute('DROP TABLE memory_sources')
            db.execute('DROP TABLE memory_entries')
            db.execute('PRAGMA user_version = 0')
        upgraded = Workspace(self.db, clock=self.clock)
        self.assertEqual(5, upgraded.schema_version())
        self.assertEqual('InProgress', upgraded.get_task(self.op, task['task_id'])['status'])
        self.assertEqual(lease['fence_token'], 1)

    def test_rejects_newer_unknown_schema_without_overwriting(self):
        with sqlite3.connect(self.db) as db:
            db.execute('PRAGMA user_version = 99')
        self.err('UNSUPPORTED_SCHEMA', lambda: Workspace(self.db, clock=self.clock))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(99, db.execute('PRAGMA user_version').fetchone()[0])

    def test_rejects_partial_unversioned_database(self):
        path = pathlib.Path(self.temp.name) / 'partial.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE projects (project_id TEXT PRIMARY KEY)')
        self.err('UNSUPPORTED_SCHEMA', lambda: Workspace(path, clock=self.clock))
        with sqlite3.connect(path) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='principals'").fetchone())

    def test_revoke_session_invalidates_session_and_requeues_tasks(self):
        task, lease = self.claim()
        rev = self.ws.revoke_session(self.op, self.ws.session_info(self.a)['session_id'], 'revoke-a', 'operator termination')
        self.assertEqual(1, rev['released_task_count'])
        self.assertEqual('Ready', self.ws.get_task(self.op, task['task_id'])['status'])
        self.err('UNAUTHENTICATED', lambda: self.ws.get_task(self.a, task['task_id']))
        new_claim = self.ws.claim_task(self.b, task['task_id'], lease['version'] + 1, 'claim-after-revoke')
        self.assertGreater(new_claim['fence_token'], lease['fence_token'])
        self.assertEqual(rev, self.ws.revoke_session(self.op, rev['session_id'], 'revoke-a', 'operator termination'))

    def test_revocation_is_transactional_under_failure(self):
        task, lease = self.claim()
        sid = self.ws.session_info(self.a)['session_id']
        before = len(self.ws.audit_events(self.op)['events'])
        self.ws.inject_failpoint('after_session_retirement_write')
        with self.assertRaises(RuntimeError):
            self.ws.revoke_session(self.op, sid, 'failed-revoke', 'testing rollback')
        reopened = Workspace(self.db, clock=self.clock)
        self.assertEqual('InProgress', reopened.get_task(self.a, task['task_id'])['status'])
        self.assertEqual(before, len(reopened.audit_events(self.op)['events']))
        self.assertEqual(1, reopened.revoke_session(self.op, sid, 'failed-revoke', 'testing rollback')['released_task_count'])

    def test_session_revocation_does_not_disable_identity_or_other_session(self):
        second = self.ws.open_session(self.cred)['session_token']
        sid = self.ws.session_info(self.a)['session_id']
        self.ws.revoke_session(self.op, sid, 'one-only', 'close single session')
        self.ws.list_tasks(second)
        self.ws.open_session(self.cred)

    def test_disable_revokes_every_session_and_releases_multiple_tasks(self):
        second = self.ws.open_session(self.cred)['session_token']
        task1, _ = self.claim(1)
        task2 = self.ws.create_task(self.op, 'second', 'tests', 'task2')
        self.ws.claim_task(second, task2['task_id'], 0, 'claim2')
        result = self.ws.disable_agent(self.op, self.agent['agent_id'], 'ban-all', 'offboard')
        self.assertEqual(2, result['revoked_session_count'])
        self.assertEqual(2, result['released_task_count'])
        for tok in (self.a, second):
            self.err('UNAUTHENTICATED', lambda tok=tok: self.ws.inbox(tok))
        self.assertEqual('Ready', self.ws.get_task(self.op, task1['task_id'])['status'])
        self.assertEqual('Ready', self.ws.get_task(self.op, task2['task_id'])['status'])

    def test_revocation_blocks_replay_of_completed_idempotent_mutation(self):
        sent = self.ws.send_message(self.a, self.other['agent_id'], 'hello', 'sent-once')
        sid = self.ws.session_info(self.a)['session_id']
        self.ws.revoke_session(self.op, sid, 'cancel', 'retired')
        self.err('UNAUTHENTICATED', lambda: self.ws.send_message(self.a, self.other['agent_id'], 'hello', 'sent-once'))
        self.assertEqual(1, len(self.ws.inbox(self.b)['messages']))

    def test_operator_cannot_revoke_unknown_session(self):
        self.err('NOT_FOUND', lambda: self.ws.revoke_session(self.op, 'unknown', 'bad', 'reason'))

    def test_agent_cannot_revoke_another_session(self):
        self.err('FORBIDDEN', lambda: self.ws.revoke_session(self.a, self.ws.session_info(self.b)['session_id'], 'bad', 'reason'))

    def test_session_close_revokes_only_self_and_lease(self):
        task, lease = self.claim()
        self.ws.close_session(self.a)
        self.err('UNAUTHENTICATED', lambda: self.ws.inbox(self.a))
        self.assertEqual('Ready', self.ws.get_task(self.op, task['task_id'])['status'])
        self.ws.inbox(self.b)
        new_a = self.ws.open_session(self.cred)['session_token']
        self.err('STALE_FENCE', lambda: self.ws.renew_task(new_a, task['task_id'], lease['version']+1, lease['fence_token'], 'old-fence'))

    def test_disabling_agent_blocks_existing_sessions_and_new_logins(self):
        task, lease = self.claim()
        result = self.ws.disable_agent(self.op, self.agent['agent_id'], 'disable-agent', 'access removed')
        self.assertEqual(1, result['revoked_session_count'])
        self.assertEqual(1, result['released_task_count'])
        self.err('UNAUTHENTICATED', lambda: self.ws.list_tasks(self.a))
        self.err('UNAUTHENTICATED', lambda: self.ws.open_session(self.cred))
        self.assertEqual('Ready', self.ws.get_task(self.op, task['task_id'])['status'])
        self.assertEqual(result, self.ws.disable_agent(self.op, self.agent['agent_id'], 'disable-agent', 'access removed'))

    def test_agent_cannot_disable_principal(self):
        self.err('FORBIDDEN', lambda: self.ws.disable_agent(self.a, self.agent['agent_id'], 'no', 'reason'))

    def test_agent_disable_does_not_erase_mail_or_history(self):
        msg = self.ws.send_message(self.b, self.agent['agent_id'], 'handoff', 'msg')
        self.ws.disable_agent(self.op, self.agent['agent_id'], 'disable', 'retired')
        self.assertTrue(any(e['action'] == 'agent.disabled' for e in self.ws.audit_events(self.op)['events']))
        with sqlite3.connect(self.db) as db:
            self.assertEqual('handoff', db.execute('SELECT body FROM messages WHERE message_id=?', (msg['message_id'],)).fetchone()[0])

    def test_credential_rotation_invalidates_old_password_and_sessions(self):
        task, lease = self.claim()
        rotated = self.ws.rotate_agent_credential(self.op, self.agent['agent_id'])
        self.assertNotEqual(rotated['credential'], self.cred)
        self.err('UNAUTHENTICATED', lambda: self.ws.open_session(self.cred))
        self.err('UNAUTHENTICATED', lambda: self.ws.get_task(self.a, task['task_id']))
        fresh = self.ws.open_session(rotated['credential'])['session_token']
        self.assertEqual('Ready', self.ws.get_task(fresh, task['task_id'])['status'])
        self.assertEqual(1, rotated['released_task_count'])
        self.assertEqual(1, rotated['revoked_session_count'])
        self.assertNotIn(rotated['credential'].encode('utf-8'), self.db.read_bytes())

    def test_rotation_preserves_audit_without_secret_in_any_stored_result(self):
        rotated = self.ws.rotate_agent_credential(self.op, self.agent['agent_id'])
        with sqlite3.connect(self.db) as db:
            rows = db.execute('SELECT response_json FROM idempotency').fetchall()
            self.assertTrue(all(rotated['credential'] not in r[0] for r in rows))
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM audit_events WHERE action='agent.credential_rotated'").fetchone()[0])
        self.assertFalse(any(rotated['credential'] in repr(e) for e in self.ws.audit_events(self.op)['events']))

    def test_agents_cannot_rotate_credentials(self):
        self.err('FORBIDDEN', lambda: self.ws.rotate_agent_credential(self.a, self.other['agent_id']))

    def test_recovery_requeues_expired_lease_and_is_idempotent(self):
        task, lease = self.claim()
        self.clock.advance(61)
        result = self.ws.recover_abandoned_tasks(self.op, 'recover-1')
        self.assertEqual([task['task_id']], result['recovered_task_ids'])
        self.assertEqual('Ready', self.ws.get_task(self.op, task['task_id'])['status'])
        self.assertEqual(result, self.ws.recover_abandoned_tasks(self.op, 'recover-1'))
        new_lease = self.ws.claim_task(self.b, task['task_id'], lease['version']+1, 'new-claim')
        self.assertGreater(new_lease['fence_token'], lease['fence_token'])

    def test_recovery_requeues_expired_session_even_if_lease_active(self):
        task, lease = self.claim()
        self.clock.advance(self.ws.SESSION_TTL_SECONDS + 1)
        operator = self.ws.open_session(self.op_credential)['session_token']
        result = self.ws.recover_abandoned_tasks(operator, 'recover-expired-session')
        self.assertEqual([task['task_id']], result['recovered_task_ids'])

    def test_recovery_does_not_release_valid_active_task(self):
        task, _ = self.claim()
        result = self.ws.recover_abandoned_tasks(self.op, 'recover-not-yet')
        self.assertEqual([], result['recovered_task_ids'])
        self.assertEqual('InProgress', self.ws.get_task(self.op, task['task_id'])['status'])

    def test_recovery_is_operator_only(self):
        self.err('FORBIDDEN', lambda: self.ws.recover_abandoned_tasks(self.a, 'no'))

    def test_recovery_rolls_back_on_injected_crash(self):
        task, lease = self.claim()
        self.clock.advance(61)
        self.ws.inject_failpoint('after_recovery_write')
        with self.assertRaises(RuntimeError):
            self.ws.recover_abandoned_tasks(self.op, 'will-crash')
        restarted = Workspace(self.db, clock=self.clock)
        self.assertEqual('InProgress', restarted.get_task(self.op, task['task_id'])['status'])
        result = restarted.recover_abandoned_tasks(self.op, 'will-crash')
        self.assertEqual([task['task_id']], result['recovered_task_ids'])


if __name__ == '__main__':
    unittest.main()
