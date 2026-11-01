"""M2.2 Shared Memory: behavior-first acceptance and regression tests."""
import concurrent.futures
import pathlib
import sqlite3
import tempfile
import unittest

from agent_workspace import Workspace, WorkspaceError


class Clock:
    def __init__(self):
        self.value = 2_000_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / 'workspace.db'
        self.clock = Clock()
        self.ws = Workspace(self.db, clock=self.clock)
        initial = self.ws.bootstrap('memory-test', self.tmp.name)
        self.op = self.ws.open_session(initial['operator_credential'])['session_token']
        a = self.ws.enroll_agent(self.op, 'claude', 'claude')
        b = self.ws.enroll_agent(self.op, 'codex', 'codex')
        self.a_id, self.b_id = a['agent_id'], b['agent_id']
        self.a_credential = a['credential']
        self.a = self.ws.open_session(a['credential'])['session_token']
        self.b = self.ws.open_session(b['credential'])['session_token']
        task = self.ws.create_task(self.op, 'Investigate race', 'Prove recovery', 'task-1')
        self.task_id = task['task_id']
        self.sources = [{'kind': 'task', 'ref': self.task_id}]

    def error(self, code, fn):
        with self.assertRaises(WorkspaceError) as e:
            fn()
        self.assertEqual(code, e.exception.code)

    def remember(self, key='memory-1', *, token=None, kind='finding', title='Lease recovery race',
                 content='Concurrent agent claims require fencing.', refs=None, task_id=None,
                 supersedes=None, ttl=None):
        return self.ws.record_memory(token or self.a, kind, title, content,
                                     refs if refs is not None else self.sources, key,
                                     task_id=task_id, supersedes_memory_id=supersedes,
                                     ttl_seconds=ttl)

    def test_version_four_and_memory_shared_across_agents(self):
        self.assertEqual(5, self.ws.schema_version())
        result = self.remember(task_id=self.task_id)
        self.assertEqual('active', result['status'])
        full = self.ws.get_memory(self.b, result['memory_id'])
        self.assertEqual('finding', full['kind'])
        self.assertEqual(self.a_id, full['author_id'])
        self.assertEqual(self.task_id, full['task_id'])
        self.assertEqual(self.sources, full['source_refs'])
        self.assertEqual('agent_reported', full['verification'])
        self.assertEqual('active', full['effective_status'])
        self.assertEqual([], self.ws.knowledge_brief(self.b)['decisions'])

    def test_allowed_kinds_and_immutable_content(self):
        for i, kind in enumerate(('observation', 'finding', 'failed_attempt', 'lesson')):
            m = self.remember(f'kind-{i}', kind=kind, title=f'Note {kind}')
            self.assertEqual(kind, self.ws.get_memory(self.b, m['memory_id'])['kind'])
        self.error('VALIDATION_FAILED', lambda: self.remember('bad', kind='decision'))
        self.error('VALIDATION_FAILED', lambda: self.remember('blank', title=''))
        self.error('VALIDATION_FAILED', lambda: self.remember('empty', content='  '))
        self.error('PAYLOAD_REJECTED', lambda: self.remember('huge', content='x' * 20001))
        self.assertEqual(4, len(self.ws.search_memories(self.b)['memories']))

    def test_source_required_and_validated(self):
        self.error('VALIDATION_FAILED', lambda: self.remember(refs=[]))
        self.error('VALIDATION_FAILED', lambda: self.remember(refs=self.sources * 2))
        self.error('NOT_FOUND', lambda: self.remember(refs=[{'kind': 'task', 'ref': 'nonexistent'}]))
        self.error('VALIDATION_FAILED', lambda: self.remember(refs=[{'kind': 'external', 'ref': 'file:///etc/passwd'}]))
        self.error('VALIDATION_FAILED', lambda: self.remember(refs=[{'kind': 'external', 'ref': 'https://u:p@example.org'}]))
        self.error('VALIDATION_FAILED', lambda: self.remember(refs=[{'kind': 'task', 'ref': self.task_id, 'extra': 1}]))
        self.error('NOT_FOUND', lambda: self.remember(task_id='wrong-task'))

    def test_external_reference_not_verified(self):
        m = self.remember(refs=[{'kind': 'external', 'ref': 'https://example.org/notes'}])
        full = self.ws.get_memory(self.b, m['memory_id'])
        self.assertFalse(full['external_sources_verified'])
        self.assertEqual('agent_reported', full['verification'])

    def test_private_message_source_is_not_leaked_to_other_agent(self):
        c = self.ws.enroll_agent(self.op, 'third', 'test')
        third = self.ws.open_session(c['credential'])['session_token']
        msg = self.ws.send_message(self.a, self.b_id, 'Secret: not public', 'send-private')
        ref = [{'kind': 'message', 'ref': msg['message_id']}]
        m = self.remember(refs=ref, content='Non-sensitive derived observation')
        self.assertEqual(ref, self.ws.get_memory(self.b, m['memory_id'])['source_refs'])
        other = self.ws.get_memory(third, m['memory_id'])
        self.assertEqual([], other['source_refs'])
        self.assertEqual(1, other['hidden_source_count'])
        self.assertNotIn(msg['message_id'], str(other))
        self.assertNotIn('Secret: not public', str(other))
        self.assertEqual(ref, self.ws.get_memory(self.op, m['memory_id'])['source_refs'])
        self.error('NOT_FOUND', lambda: self.ws.record_memory(third, 'finding', 'bad', 'bad', ref, 'bad-msg'))

    def test_search_fts_and_bounded_results(self):
        self.remember('one', title='SQLite transaction rollback', content='atomic transactions')
        self.remember('two', title='Python authorization', content='token revocation')
        self.remember('three', title='SQLite WAL', content='snapshot isolation')
        matches = self.ws.search_memories(self.b, query='sqlite')
        self.assertEqual(2, len(matches['memories']))
        self.assertTrue(all('content' not in item for item in matches['memories']))
        self.assertEqual(1, len(self.ws.search_memories(self.b, query='SQLite WAL')['memories']))
        self.assertEqual([], self.ws.search_memories(self.b, query='no-such-phrase')['memories'])
        self.assertEqual(1, len(self.ws.search_memories(self.b, query='python', kind='finding')['memories']))
        self.error('VALIDATION_FAILED', lambda: self.ws.search_memories(self.b, query='!!!'))
        self.error('VALIDATION_FAILED', lambda: self.ws.search_memories(self.b, query='sqlite', limit=501))
        self.error('PAYLOAD_REJECTED', lambda: self.ws.search_memories(self.b, query='x' * 2048))
        self.error('VALIDATION_FAILED', lambda: self.ws.search_memories(self.b, kind='unknown'))
        self.error('VALIDATION_FAILED', lambda: self.ws.search_memories(self.b, before_seq=-1))

    def test_hostile_fts_syntax_is_safe(self):
        self.remember(content='SQL query injection should not succeed')
        self.assertEqual([], self.ws.search_memories(self.b, query='" OR * AND')['memories'])
        self.error('VALIDATION_FAILED', lambda: self.ws.search_memories(self.b, query='? ( ] ['))
        self.assertEqual([], self.ws.search_memories(self.b, query='OR UNION SELECT')['memories'])

    def test_query_has_unicode_support(self):
        self.remember(title='Türkçe çalışıyor', content='Öğrenilen dersler')
        self.assertEqual(1, len(self.ws.search_memories(self.b, query='Türkçe')['memories']))
        self.assertEqual(1, len(self.ws.search_memories(self.b, query='Öğrenilen')['memories']))

    def test_bounded_preview_and_pagination_stable(self):
        for i in range(5):
            self.remember(f'k-{i}', title=f'Finding-{i}', content='ü' * 2000)
        first = self.ws.search_memories(self.b, limit=2)
        self.assertEqual(2, len(first['memories']))
        self.assertTrue(first['has_more'])
        self.assertLessEqual(len(first['memories'][0]['content_preview'].encode('utf-8')), 325)
        self.assertTrue(first['memories'][0]['preview_truncated'])
        self.remember('newer', title='Newest', content='After cursor acquired')
        second = self.ws.search_memories(self.b, before_seq=first['next_cursor'], limit=3)
        first_ids = {item['memory_id'] for item in first['memories']}
        second_ids = {item['memory_id'] for item in second['memories']}
        self.assertFalse(first_ids & second_ids)
        self.assertNotIn(self.ws.search_memories(self.b, limit=1)['memories'][0]['memory_id'], second_ids)

    def test_search_excludes_superseded_and_retracted_fts_hits(self):
        old = self.remember('old', title='StaleOnlyMemoryMarker')
        newer = self.remember('new-version', title='FreshReplacement',
                              supersedes=old['memory_id'])
        self.assertEqual([], self.ws.search_memories(self.b, query='StaleOnlyMemoryMarker')['memories'])
        self.assertEqual(newer['memory_id'], self.ws.search_memories(
            self.b, query='FreshReplacement')['memories'][0]['memory_id'])
        self.ws.retract_memory(self.a, newer['memory_id'], 'Not reproducible', 'new-retract')
        self.assertEqual([], self.ws.search_memories(self.b, query='FreshReplacement')['memories'])

    def test_expiry_metadata_exposed_on_active_search_and_change_stream_only_has_metadata(self):
        rec = self.remember(ttl=120, content='Sensitive internal finding')
        found = self.ws.search_memories(self.b)
        self.assertEqual(self.clock.value + 120, found['memories'][0]['expires_at'])
        event = self.ws.memory_changes(self.b)['changes'][0]
        self.assertEqual(rec['memory_id'], event['memory_id'])
        self.assertNotIn('Sensitive internal finding', str(event))
        self.clock.advance(121)
        self.assertEqual([], self.ws.search_memories(self.b)['memories'])
        # TTL expiry has no background process and does not add an audit event.
        self.assertEqual(1, len(self.ws.memory_changes(self.b)['changes']))

    def test_idempotent_record_across_session_rotation(self):
        first = self.remember()
        second_session = self.ws.open_session(self.a_credential)['session_token']
        self.assertEqual(first, self.remember(token=second_session))
        self.error('IDEMPOTENCY_CONFLICT', lambda: self.remember(token=second_session, title='Different'))
        self.assertEqual(1, len(self.ws.search_memories(self.b)['memories']))

    def test_expiration_excludes_search_but_not_historical_owner_read(self):
        m = self.remember(ttl=60)
        self.clock.advance(61)
        self.assertEqual([], self.ws.search_memories(self.b)['memories'])
        self.error('NOT_FOUND', lambda: self.ws.get_memory(self.b, m['memory_id']))
        owner = self.ws.get_memory(self.a, m['memory_id'])
        self.assertEqual('expired', owner['effective_status'])
        self.assertTrue(owner['is_stale'])
        self.error('VALIDATION_FAILED', lambda: self.remember('bad-ttl', ttl=10))
        self.error('VALIDATION_FAILED', lambda: self.remember('bad-ttl2', ttl=True))

    def test_supersession_atomic_and_old_hidden(self):
        first = self.remember()
        updated = self.remember('new', title='Lease recovery resolved', supersedes=first['memory_id'])
        prior = self.ws.get_memory(self.a, first['memory_id'])
        self.assertEqual('superseded', prior['status'])
        self.assertTrue(prior['is_stale'])
        self.assertEqual(updated['memory_id'], prior['superseded_by'])
        self.assertEqual([updated['memory_id']], [m['memory_id'] for m in self.ws.search_memories(self.b)['memories']])
        self.error('INVALID_TRANSITION', lambda: self.remember('conflict', supersedes=first['memory_id']))
        self.error('FORBIDDEN', lambda: self.ws.record_memory(
            self.b, 'finding', 'Another agent change', 'Not my observation', self.sources,
            'unauth', supersedes_memory_id=updated['memory_id']))

    def test_operator_may_correct_memory(self):
        first = self.remember()
        updated = self.remember('fix', token=self.op, title='Corrected', supersedes=first['memory_id'])
        self.assertEqual(updated['memory_id'], self.ws.get_memory(self.b, first['memory_id'])['superseded_by'])

    def test_retract_author_or_operator_only_and_idempotent(self):
        m = self.remember()
        self.error('FORBIDDEN', lambda: self.ws.retract_memory(self.b, m['memory_id'], 'wrong', 'b-retract'))
        first = self.ws.retract_memory(self.a, m['memory_id'], 'Claim invalidated', 'a-retract')
        self.assertEqual(first, self.ws.retract_memory(self.a, m['memory_id'], 'Claim invalidated', 'a-retract'))
        self.assertEqual('retracted', first['status'])
        self.assertEqual([], self.ws.search_memories(self.b)['memories'])
        self.error('NOT_FOUND', lambda: self.ws.get_memory(self.b, m['memory_id']))
        self.assertTrue(self.ws.get_memory(self.op, m['memory_id'])['is_stale'])
        self.error('INVALID_TRANSITION', lambda: self.ws.retract_memory(self.a, m['memory_id'], 'Again', 'another'))
        n = self.remember('n')
        self.assertEqual('retracted', self.ws.retract_memory(self.op, n['memory_id'], 'Operator review', 'op-ret')['status'])

    def test_concurrent_supersession_has_single_winner(self):
        first = self.remember()
        def do(index):
            try:
                return self.remember(f'concurrent-{index}', title=f'new-{index}', supersedes=first['memory_id'])
            except WorkspaceError as error:
                return error.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(do, range(2)))
        self.assertEqual(1, sum(isinstance(r, dict) for r in results))
        self.assertEqual('INVALID_TRANSITION', next(r for r in results if isinstance(r, str)))

    def test_failpoint_rolls_back_memory_and_audit(self):
        first = self.remember()
        before = len(self.ws.audit_events(self.op)['events'])
        self.ws.inject_failpoint('before_memory_commit_event')
        with self.assertRaises(RuntimeError):
            self.remember('fail', supersedes=first['memory_id'])
        reopened = Workspace(self.db, clock=self.clock)
        self.assertEqual('active', reopened.get_memory(self.b, first['memory_id'])['status'])
        self.assertEqual(1, len(reopened.search_memories(self.b)['memories']))
        self.assertEqual(before, len(reopened.audit_events(self.op)['events']))
        self.assertEqual('active', reopened.get_memory(self.b, reopened.record_memory(
            self.a, 'finding', 'After restart', 'Commit works', self.sources, 'fail',
            supersedes_memory_id=first['memory_id'])['memory_id'])['status'])

    def test_changes_delta_scoped_and_bounded(self):
        first = self.remember()
        initial = self.ws.memory_changes(self.b)
        self.assertEqual(['memory.recorded'], [e['action'] for e in initial['changes']])
        cursor = initial['next_cursor']
        self.remember('new', supersedes=first['memory_id'])
        next_page = self.ws.memory_changes(self.b, after_event_id=cursor, limit=1)
        self.assertEqual(1, len(next_page['changes']))
        self.assertTrue(next_page['has_more'])
        after = self.ws.memory_changes(self.b, after_event_id=next_page['next_cursor'])
        self.assertIn('memory.recorded', [e['action'] for e in after['changes']])
        self.error('VALIDATION_FAILED', lambda: self.ws.memory_changes(self.b, limit=999))

    def test_revoked_agent_cannot_read_or_write(self):
        m = self.remember()
        info = self.ws.session_info(self.a)
        self.ws.revoke_session(self.op, info['session_id'], 'stop', 'revoke-a')
        self.error('UNAUTHENTICATED', lambda: self.ws.record_memory(
            self.a, 'finding', 'bad', 'revoked', self.sources, 'revoked-write'))
        self.error('UNAUTHENTICATED', lambda: self.ws.get_memory(self.a, m['memory_id']))
        self.error('UNAUTHENTICATED', lambda: self.ws.search_memories(self.a))
        self.error('UNAUTHENTICATED', lambda: self.ws.memory_changes(self.a))

    def test_reopen_persists_fts_memory_and_changes(self):
        m = self.remember()
        reopened = Workspace(self.db, clock=self.clock)
        self.assertEqual(5, reopened.schema_version())
        self.assertEqual(m['memory_id'], reopened.search_memories(self.b, query='fencing')['memories'][0]['memory_id'])
        self.assertGreater(len(reopened.memory_changes(self.b)['changes']), 0)

    def test_cross_project_isolation(self):
        other_path = pathlib.Path(self.tmp.name) / 'other.db'
        other = Workspace(other_path, clock=self.clock)
        init = other.bootstrap('other', self.tmp.name)
        other_token = other.open_session(init['operator_credential'])['session_token']
        m = self.remember()
        self.error('NOT_FOUND', lambda: other.get_memory(other_token, m['memory_id']))
        self.assertEqual([], other.search_memories(other_token, query='lease')['memories'])
        self.assertEqual([], other.memory_changes(other_token)['changes'])

    def test_migration_schema_checks(self):
        with sqlite3.connect(self.db) as con:
            con.execute('PRAGMA user_version=6')
        self.error('UNSUPPORTED_SCHEMA', lambda: Workspace(self.db, clock=self.clock))


if __name__ == '__main__':
    unittest.main()
