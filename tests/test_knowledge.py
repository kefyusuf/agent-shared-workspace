"""M2.1 governed knowledge: acceptance-driven tests, written before implementation."""
import concurrent.futures
import pathlib
import sqlite3
import tempfile
import unittest

from agent_workspace import Workspace, WorkspaceError


class Clock:
    def __init__(self):
        self.value = 1_900_000_000.0

    def __call__(self):
        return self.value


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = pathlib.Path(self.tmp.name) / 'workspace.db'
        self.clock = Clock()
        self.ws = Workspace(self.db_path, clock=self.clock)
        bootstrap = self.ws.bootstrap('knowledge-test', self.tmp.name)
        self.op = self.ws.open_session(bootstrap['operator_credential'])['session_token']
        agent_a = self.ws.enroll_agent(self.op, 'claude', 'claude')
        agent_b = self.ws.enroll_agent(self.op, 'codex', 'codex')
        self.a = self.ws.open_session(agent_a['credential'])['session_token']
        self.b = self.ws.open_session(agent_b['credential'])['session_token']
        self.a_id = agent_a['agent_id']
        self.b_id = agent_b['agent_id']
        self.task = self.ws.create_task(self.op, 'Architecture task', 'Define interface', 'task-source')
        self.source = [{'kind': 'task', 'ref': self.task['task_id']}]

    def err(self, code, call):
        with self.assertRaises(WorkspaceError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)

    def propose(self, key='propose-1', *, source_refs=None, supersedes=None, token=None, statement='Keep API domain-first'):
        return self.ws.propose_decision(token or self.a, statement, 'Make interfaces stable',
                                        source_refs if source_refs is not None else self.source,
                                        key, supersedes_decision_id=supersedes)

    def accept(self, decision, key='accept-1'):
        return self.ws.decide_decision(self.op, decision['decision_id'], decision['version'],
                                       'accepted', 'Reviewed against project contract', key)

    def test_proposed_is_not_authoritative(self):
        proposal = self.propose()
        self.assertEqual('proposed', proposal['status'])
        self.assertEqual(0, proposal['version'])
        self.assertEqual([], self.ws.knowledge_brief(self.b)['decisions'])
        self.assertEqual([], self.ws.list_decisions(self.b)['decisions'])

    def test_operator_approves_and_other_agent_reads(self):
        proposal = self.propose()
        done = self.accept(proposal)
        self.assertEqual(('accepted', 1), (done['status'], done['version']))
        result = self.ws.get_decision(self.b, proposal['decision_id'])
        self.assertEqual('accepted', result['status'])
        self.assertEqual(self.a_id, result['proposed_by'])
        self.assertIsNotNone(result['decided_by'])
        self.assertEqual(self.source, result['source_refs'])
        self.assertEqual([proposal['decision_id']],
                         [x['decision_id'] for x in self.ws.knowledge_brief(self.b)['decisions']])

    def test_agents_cannot_approve_or_reject(self):
        proposal = self.propose()
        for action in ('accepted', 'rejected'):
            self.err('FORBIDDEN', lambda: self.ws.decide_decision(
                self.b, proposal['decision_id'], 0, action, 'invalid', 'agent-' + action))
        self.assertEqual('proposed', self.ws.get_decision(self.a, proposal['decision_id'])['status'])

    def test_other_agent_cannot_read_draft(self):
        proposal = self.propose()
        self.err('NOT_FOUND', lambda: self.ws.get_decision(self.b, proposal['decision_id']))
        self.assertEqual('proposed', self.ws.get_decision(self.a, proposal['decision_id'])['status'])
        self.assertEqual(1, len(self.ws.list_decisions(self.op, status='proposed')['decisions']))
        self.err('FORBIDDEN', lambda: self.ws.list_decisions(self.b, status='proposed'))

    def test_rejection_never_enters_brief(self):
        proposal = self.propose()
        rejected = self.ws.decide_decision(self.op, proposal['decision_id'], 0,
                                           'rejected', 'Unsubstantiated', 'reject-1')
        self.assertEqual('rejected', rejected['status'])
        self.assertEqual([], self.ws.knowledge_brief(self.a)['decisions'])
        self.err('NOT_FOUND', lambda: self.ws.get_decision(self.b, proposal['decision_id']))
        self.err('INVALID_TRANSITION', lambda: self.ws.decide_decision(
            self.op, proposal['decision_id'], 1, 'accepted', 'later', 'accept-later'))

    def test_requires_source_and_validates_internal_reference(self):
        self.err('VALIDATION_FAILED', lambda: self.propose(source_refs=[]))
        self.err('NOT_FOUND', lambda: self.propose(source_refs=[{'kind': 'task', 'ref': 'unknown'}]))
        self.err('VALIDATION_FAILED', lambda: self.propose(source_refs=[{'kind': 'file', 'ref': 'README'}]))
        self.err('VALIDATION_FAILED', lambda: self.propose(source_refs=[{'kind': 'external', 'ref': 'file:///etc/passwd'}]))
        self.err('VALIDATION_FAILED', lambda: self.propose(source_refs=[{'kind': 'external', 'ref': 'https://user:secret@example.com/a'}]))
        self.err('VALIDATION_FAILED', lambda: self.propose(source_refs=[{'kind': 'task', 'ref': self.task['task_id'], 'secret': 'oops'}]))
        self.err('VALIDATION_FAILED', lambda: self.propose(source_refs=self.source * 2))

    def test_external_reference_is_not_claimed_verified(self):
        external = [{'kind': 'external', 'ref': 'https://example.org/design'}]
        proposal = self.propose(source_refs=external)
        self.accept(proposal)
        result = self.ws.get_decision(self.b, proposal['decision_id'])
        self.assertEqual(external, result['source_refs'])
        self.assertFalse(result['external_sources_verified'])

    def test_message_source_must_exist(self):
        msg = self.ws.send_message(self.a, self.b_id, 'Architectural motivation', 'msg1')
        ref = [{'kind': 'message', 'ref': msg['message_id']}]
        p = self.propose(source_refs=ref)
        self.accept(p)
        self.assertEqual(ref, self.ws.get_decision(self.b, p['decision_id'])['source_refs'])

    def test_authorized_source_does_not_leak_mailbox(self):
        msg = self.ws.send_message(self.a, self.b_id, 'private agent exchange', 'msg-2')
        p = self.propose(source_refs=[{'kind':'message', 'ref':msg['message_id']}])
        self.accept(p)
        d = self.ws.get_decision(self.b, p['decision_id'])
        self.assertNotIn('private agent exchange', str(d))
        self.assertEqual(msg['message_id'], d['source_refs'][0]['ref'])

    def test_version_conflict_and_atomic_replay(self):
        p = self.propose()
        first = self.accept(p)
        self.assertEqual(first, self.ws.decide_decision(self.op, p['decision_id'], 0,
                    'accepted', 'Reviewed against project contract', 'accept-1'))
        self.err('IDEMPOTENCY_CONFLICT', lambda: self.ws.decide_decision(self.op,
                    p['decision_id'], 0, 'rejected', 'Different payload', 'accept-1'))
        self.err('VERSION_CONFLICT', lambda: self.ws.decide_decision(self.op,
                    p['decision_id'], 0, 'rejected', 'stale', 'stale'))
        self.assertEqual('accepted', self.ws.get_decision(self.b, p['decision_id'])['status'])

    def test_proposal_replay_across_sessions_no_duplicate(self):
        result = self.propose()
        same = self.ws.open_session(self.ws.enroll_agent(self.op, 'worker3', 'codex')['credential'])['session_token']
        # Different principal can use its own key; same principal must replay exactly.
        cred = self.ws.rotate_agent_credential(self.op, self.a_id)['credential']
        next_session = self.ws.open_session(cred)['session_token']
        self.assertEqual(result, self.propose(token=next_session))
        self.err('IDEMPOTENCY_CONFLICT', lambda: self.propose(token=next_session, statement='Changed'))
        self.assertEqual(1, len(self.ws.list_decisions(self.op, status='proposed')['decisions']))
        self.assertIsNotNone(same)

    def test_supersede_accepted_only_by_new_accepted_decision(self):
        first = self.propose()
        self.accept(first)
        second = self.propose(key='new', supersedes=first['decision_id'], statement='Use updated API')
        self.assertEqual('accepted', self.ws.get_decision(self.b, first['decision_id'])['status'])
        self.assertEqual(1, len(self.ws.knowledge_brief(self.b)['decisions']))
        self.accept(second, 'approve-new')
        old = self.ws.get_decision(self.b, first['decision_id'])
        self.assertEqual('superseded', old['status'])
        self.assertEqual(second['decision_id'], old['superseded_by'])
        self.assertEqual(2, old['version'])
        self.assertEqual([second['decision_id']],
                         [d['decision_id'] for d in self.ws.knowledge_brief(self.b)['decisions']])

    def test_rejected_successor_does_not_supersede(self):
        first = self.propose()
        self.accept(first)
        second = self.propose(key='successor', supersedes=first['decision_id'])
        self.ws.decide_decision(self.op, second['decision_id'], 0, 'rejected', 'No', 'reject-successor')
        self.assertEqual('accepted', self.ws.get_decision(self.b, first['decision_id'])['status'])

    def test_supersession_race_has_single_winner(self):
        first = self.propose()
        self.accept(first)
        successors = [self.propose(key=f'new-{i}', supersedes=first['decision_id'], statement=f'new {i}')
                      for i in range(2)]
        def approve(index):
            try:
                return self.accept(successors[index], f'approve-{index}')
            except WorkspaceError as ex:
                return ex.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(approve, (0, 1)))
        self.assertEqual(1, sum(isinstance(r, dict) for r in results))
        self.assertIn(next(r for r in results if isinstance(r, str)), ('INVALID_TRANSITION', 'VERSION_CONFLICT'))
        self.assertEqual(1, len(self.ws.knowledge_brief(self.b)['decisions']))

    def test_provenance_is_immutable_and_reopen_persists(self):
        p = self.propose()
        self.accept(p)
        reopened = Workspace(self.db_path, clock=self.clock)
        result = reopened.get_decision(self.b, p['decision_id'])
        self.assertEqual(self.source, result['source_refs'])
        self.assertEqual('Keep API domain-first', result['statement'])
        self.assertIsNotNone(result['decision_event_id'])

    def test_pagination_stable_and_bounded(self):
        for i in range(4):
            p = self.propose(key=f'p-{i}', statement=f'decision {i}')
            self.accept(p, f'a-{i}')
        first = self.ws.list_decisions(self.b, limit=2)
        second = self.ws.list_decisions(self.b, after_seq=first['next_cursor'], limit=2)
        self.assertEqual(4, len(first['decisions'] + second['decisions']))
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.err('VALIDATION_FAILED', lambda: self.ws.list_decisions(self.b, limit=1000))
        self.err('VALIDATION_FAILED', lambda: self.ws.list_decisions(self.b, after_seq=-1))

    def test_brief_is_bounded_and_indicates_truncation(self):
        for i in range(3):
            p = self.propose(key=f'p-{i}', statement=f'decision {i}')
            self.accept(p, f'a-{i}')
        brief = self.ws.knowledge_brief(self.b, limit=2)
        self.assertEqual(2, len(brief['decisions']))
        self.assertTrue(brief['has_more'])

    def test_crash_rolls_back_approval_and_supersession(self):
        old = self.propose()
        self.accept(old)
        new = self.propose(key='new', supersedes=old['decision_id'])
        before = len(self.ws.audit_events(self.op)['events'])
        self.ws.inject_failpoint('before_knowledge_approval_event')
        with self.assertRaises(RuntimeError):
            self.accept(new, 'fail-approve')
        reopened = Workspace(self.db_path, clock=self.clock)
        self.assertEqual('proposed', reopened.get_decision(self.a, new['decision_id'])['status'])
        self.assertEqual('accepted', reopened.get_decision(self.b, old['decision_id'])['status'])
        self.assertEqual(before, len(reopened.audit_events(self.op)['events']))
        reopened.decide_decision(self.op, new['decision_id'], 0, 'accepted',
                                 'Reviewed against project contract', 'fail-approve')

    def test_audit_contains_authored_and_approved_events(self):
        p = self.propose()
        self.accept(p)
        actions = [event['action'] for event in self.ws.audit_events(self.op)['events']]
        self.assertIn('knowledge.proposed', actions)
        self.assertIn('knowledge.accepted', actions)

    def test_cross_project_reference_is_rejected(self):
        other_db = pathlib.Path(self.tmp.name) / 'other.db'
        other = Workspace(other_db, clock=self.clock)
        init = other.bootstrap('other', self.tmp.name)
        op = other.open_session(init['operator_credential'])['session_token']
        foreign = other.create_task(op, 'Foreign task', 'Not authorized', 'other-task')
        self.err('NOT_FOUND', lambda: self.propose(source_refs=[{'kind':'task', 'ref':foreign['task_id']}]))

    def test_schema_migration_from_v2_preserves_existing_rows(self):
        # Simulate the actual M1.3 versioned database: known base tables remain,
        # new knowledge schema is missing and user_version tracks version 2.
        with sqlite3.connect(self.db_path) as db:
            db.execute('DROP TABLE IF EXISTS context_handoffs')
            db.execute('DROP TABLE IF EXISTS context_sources')
            db.execute('DROP TABLE IF EXISTS context_snapshots')
            db.execute('DELETE FROM schema_migrations WHERE version=5')
            db.execute('DROP TABLE IF EXISTS memory_fts')
            db.execute('DROP TABLE IF EXISTS memory_sources')
            db.execute('DROP TABLE IF EXISTS memory_entries')
            db.execute('DELETE FROM schema_migrations WHERE version=4')
            db.execute('DROP TABLE IF EXISTS knowledge_sources')
            db.execute('DROP TABLE IF EXISTS knowledge_decisions')
            db.execute('DELETE FROM schema_migrations WHERE version=3')
            db.execute('PRAGMA user_version=2')
        upgraded = Workspace(self.db_path, clock=self.clock)
        self.assertEqual(5, upgraded.schema_version())
        self.assertEqual(self.task['task_id'], upgraded.get_task(self.op, self.task['task_id'])['task_id'])
        proposed = upgraded.propose_decision(self.a, 'New contract', 'Upgrade successful',
                                            self.source, 'migrate-1')
        self.assertEqual('proposed', proposed['status'])

    def test_reject_missing_knowledge_schema_when_v3(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute('DROP TABLE IF EXISTS knowledge_sources')
            db.execute('PRAGMA user_version=3')
        self.err('UNSUPPORTED_SCHEMA', lambda: Workspace(self.db_path, clock=self.clock))

    def test_agent_cannot_launder_another_agents_private_message_as_source(self):
        outsider = self.ws.enroll_agent(self.op, 'outsider', 'local')
        outsider_token = self.ws.open_session(outsider['credential'])['session_token']
        msg = self.ws.send_message(self.a, self.b_id, 'only sender and recipient', 'private-src')
        private_ref = [{'kind': 'message', 'ref': msg['message_id']}]
        self.err('NOT_FOUND', lambda: self.propose(token=outsider_token, source_refs=private_ref,
                                                   key='launder-private'))
        # The operator may explicitly choose to cite it; source content is never copied.
        self.assertEqual('proposed', self.propose(token=self.op, source_refs=private_ref,
                                                   key='operator-cites')['status'])

    def test_brief_shows_newest_active_decisions_not_oldest(self):
        proposals = []
        for i in range(4):
            p = self.propose(key=f'latest-{i}', statement=f'Rule {i}')
            self.accept(p, f'accept-latest-{i}')
            proposals.append(p)
        ids = [d['decision_id'] for d in self.ws.knowledge_brief(self.b, limit=2)['decisions']]
        self.assertEqual([proposals[3]['decision_id'], proposals[2]['decision_id']], ids)

    def test_corrupt_version_two_migration_log_is_not_silently_repaired(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute('DROP TABLE knowledge_sources')
            db.execute('DROP TABLE knowledge_decisions')
            db.execute('DELETE FROM schema_migrations WHERE version=2')
            db.execute('PRAGMA user_version=2')
        self.err('UNSUPPORTED_SCHEMA', lambda: Workspace(self.db_path, clock=self.clock))
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(2, db.execute('PRAGMA user_version').fetchone()[0])
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='knowledge_decisions'").fetchone())

    def test_supersession_records_distinct_event(self):
        first = self.propose()
        self.accept(first)
        successor = self.propose(key='s2', supersedes=first['decision_id'], statement='Better rule')
        self.accept(successor, 's2-accepted')
        old = self.ws.get_decision(self.b, first['decision_id'])
        self.assertNotEqual(old['decision_event_id'], old['superseded_event_id'])
        evs = self.ws.audit_events(self.op)['events']
        self.assertEqual('knowledge.superseded', next(e['action'] for e in evs
                            if e['event_id'] == old['superseded_event_id']))

    def test_invalid_unserializable_sources_are_domain_errors(self):
        self.err('VALIDATION_FAILED', lambda: self.propose(source_refs=[{'kind':'task', 'ref':{1, 2}}]))
        self.err('VALIDATION_FAILED', lambda: self.propose(source_refs=[{'kind':'external', 'ref':'https://example.org/a b'}]))
        self.err('VALIDATION_FAILED', lambda: self.propose(source_refs=[{'kind':'external', 'ref':'https://example.org/#fragment'}]))

    def test_brief_is_compact_and_links_to_full_decision(self):
        long_statement = 'A' * 3500
        p = self.propose(key='huge', statement=long_statement)
        self.accept(p, 'huge-reviewed')
        brief = self.ws.knowledge_brief(self.b)['decisions'][0]
        self.assertEqual(p['decision_id'], brief['decision_id'])
        self.assertNotIn('rationale', brief)
        self.assertNotIn('source_refs', brief)
        self.assertTrue(brief['preview_truncated'])
        self.assertLessEqual(len(brief['statement_preview'].encode('utf-8')), 320)
        self.assertEqual(long_statement, self.ws.get_decision(self.b, p['decision_id'])['statement'])


if __name__ == '__main__':
    unittest.main()
