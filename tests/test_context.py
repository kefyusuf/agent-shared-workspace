"""M2.3 context gateway: contract-first acceptance tests."""
import concurrent.futures
import pathlib
import sqlite3
from contextlib import closing
import tempfile
import unittest

from agent_workspace import Workspace, WorkspaceError


class Clock:
    def __init__(self):
        self.value = 2_100_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / 'context.db'
        self.clock = Clock()
        self.ws = Workspace(self.db, clock=self.clock)
        root = self.ws.bootstrap('context', self.tmp.name)
        self.operator_cred = root['operator_credential']
        self.op = self.ws.open_session(self.operator_cred)['session_token']
        a = self.ws.enroll_agent(self.op, 'claude', 'claude')
        b = self.ws.enroll_agent(self.op, 'codex', 'codex')
        c = self.ws.enroll_agent(self.op, 'other', 'local')
        self.a_id, self.b_id, self.c_id = a['agent_id'], b['agent_id'], c['agent_id']
        self.a_cred, self.b_cred = a['credential'], b['credential']
        self.a = self.ws.open_session(self.a_cred)['session_token']
        self.b = self.ws.open_session(self.b_cred)['session_token']
        self.c = self.ws.open_session(c['credential'])['session_token']
        self.task = self.ws.create_task(self.op, 'Auth refresh', 'Tests and review', 'task')
        self.task_id = self.task['task_id']
        self.lease = self.ws.claim_task(self.a, self.task_id, 0, 'claim', 60)
        self.refs = [{'kind': 'task', 'ref': self.task_id}]

    def err(self, code, fn):
        with self.assertRaises(WorkspaceError) as ex:
            fn()
        self.assertEqual(code, ex.exception.code)

    def publish(self, key='snapshot', *, token=None, task_id=None, version=None,
                fence=None, summary='Refresh token path explored', next_steps='Add negative test',
                refs=None, changed_paths=None, blockers=None):
        return self.ws.publish_context(
            token or self.a, task_id or self.task_id,
            self.lease['version'] if version is None else version,
            self.lease['fence_token'] if fence is None else fence,
            summary, next_steps, self.refs if refs is None else refs, key,
            changed_paths=['src/auth/token.py'] if changed_paths is None else changed_paths,
            blockers=blockers)

    def handoff(self, snap, key='handoff', *, token=None, recipient=None, action='Continue test coverage'):
        return self.ws.send_handoff(token or self.a, snap['snapshot_id'],
                                    recipient or self.b_id, action, key)

    def test_version_five_and_snapshot_persists(self):
        self.assertEqual(5, self.ws.schema_version())
        rec = self.publish()
        ws2 = Workspace(self.db, clock=self.clock)
        view = ws2.get_context(self.a, rec['snapshot_id'])
        self.assertEqual(self.task_id, view['task_id'])
        self.assertEqual(self.lease['lease_id'], view['lease_id'])
        self.assertEqual(self.refs, view['source_refs'])
        self.assertEqual(['src/auth/token.py'], view['changed_paths'])
        self.assertEqual('agent_reported', view['verification'])

    def test_publisher_authorized_only_with_current_lease_and_version(self):
        self.err('STALE_FENCE', lambda: self.publish('other', token=self.b))
        self.err('VERSION_CONFLICT', lambda: self.publish('wrongver', version=0))
        self.err('STALE_FENCE', lambda: self.publish('wrongfence', fence=999))
        self.err('NOT_FOUND', lambda: self.publish('unknown-task', task_id='absent'))
        self.err('FORBIDDEN', lambda: self.publish('operator', token=self.op))

    def test_snapshot_is_private_until_explicit_handoff_or_task_lease(self):
        snap = self.publish()
        self.err('NOT_FOUND', lambda: self.ws.get_context(self.b, snap['snapshot_id']))
        self.err('NOT_FOUND', lambda: self.ws.get_context(self.c, snap['snapshot_id']))
        self.assertEqual(snap['snapshot_id'], self.ws.get_context(self.op, snap['snapshot_id'])['snapshot_id'])
        self.assertEqual([], self.ws.list_task_contexts(self.b, self.task_id)['snapshots'])
        self.assertEqual([], self.ws.context_changes(self.b)['changes'])

    def test_handoff_target_receives_snapshot_but_not_other_agents(self):
        snap = self.publish()
        hand = self.handoff(snap)
        self.assertEqual(snap['snapshot_id'], hand['snapshot_id'])
        self.assertEqual(self.b_id, hand['recipient_agent_id'])
        self.assertEqual('Continue test coverage', self.ws.handoff_inbox(self.b)['handoffs'][0]['action_required'])
        self.assertEqual(snap['snapshot_id'], self.ws.get_context(self.b, snap['snapshot_id'])['snapshot_id'])
        self.err('NOT_FOUND', lambda: self.ws.get_context(self.c, snap['snapshot_id']))
        self.assertEqual([], self.ws.handoff_inbox(self.c)['handoffs'])

    def test_handoff_does_not_transfer_lease_or_move_task(self):
        snap = self.publish()
        self.handoff(snap)
        status = self.ws.get_task(self.op, self.task_id)
        self.assertEqual('InProgress', status['status'])
        self.assertEqual(self.a_id, status['owner_agent_id'])
        self.err('STALE_FENCE', lambda: self.publish('recipient', token=self.b))

    def test_follow_on_owner_can_read_previous_context_after_reclaim(self):
        snap = self.publish()
        released = self.ws.release_task(self.a, self.task_id, self.lease['version'],
                                        self.lease['fence_token'], 'release')
        self.ws.claim_task(self.b, self.task_id, released['version'], 'claim-b')
        self.assertEqual(snap['snapshot_id'], self.ws.get_context(self.b, snap['snapshot_id'])['snapshot_id'])
        self.assertEqual(1, len(self.ws.list_task_contexts(self.b, self.task_id)['snapshots']))
        self.err('NOT_FOUND', lambda: self.ws.get_context(self.c, snap['snapshot_id']))

    def test_lease_expiry_revokes_current_holder_read_not_handoff_recipient(self):
        snap = self.publish()
        self.handoff(snap)
        self.clock.advance(61)
        # Persistent recipient grant remains valid; expired lease is not an access grant.
        self.assertEqual(snap['snapshot_id'], self.ws.get_context(self.b, snap['snapshot_id'])['snapshot_id'])
        self.err('LEASE_EXPIRED', lambda: self.publish('late'))

    def test_handoff_requires_current_lease_and_own_snapshot(self):
        snap = self.publish()
        self.err('FORBIDDEN', lambda: self.handoff(snap, 'not-author', token=self.b))
        self.err('NOT_FOUND', lambda: self.handoff({'snapshot_id':'absent'}, 'bad'))
        self.clock.advance(61)
        self.err('LEASE_EXPIRED', lambda: self.handoff(snap, 'late'))

    def test_handoff_is_recipient_scoped_and_idempotent(self):
        snap = self.publish()
        one = self.handoff(snap)
        two = self.handoff(snap)
        self.assertEqual(one, two)
        self.err('IDEMPOTENCY_CONFLICT', lambda: self.handoff(snap, action='Different action'))
        self.assertEqual(1, len(self.ws.handoff_inbox(self.b)['handoffs']))
        self.err('INVALID_TRANSITION', lambda: self.handoff(snap, 'new-handoff-same-target'))

    def test_handoff_cannot_target_author_or_disabled_or_other_project(self):
        snap = self.publish()
        self.err('VALIDATION_FAILED', lambda: self.handoff(snap, recipient=self.a_id))
        self.err('NOT_FOUND', lambda: self.handoff(snap, recipient='unknown'))
        self.ws.disable_agent(self.op, self.c_id, 'disable-c', 'No longer permitted')
        self.err('NOT_FOUND', lambda: self.handoff(snap, recipient=self.c_id))

    def test_private_message_reference_not_revealed_to_recipient(self):
        message = self.ws.send_message(self.a, self.c_id, 'private body', 'private')
        refs = self.refs + [{'kind':'message','ref':message['message_id']}]
        snap = self.publish(refs=refs)
        self.handoff(snap)
        reader = self.ws.get_context(self.b, snap['snapshot_id'])
        self.assertEqual(self.refs, reader['source_refs'])
        self.assertEqual(1, reader['hidden_source_count'])
        self.assertNotIn(message['message_id'], str(reader))
        self.assertNotIn('private body', str(reader))
        self.assertEqual(refs, self.ws.get_context(self.a, snap['snapshot_id'])['source_refs'])

    def test_cannot_cite_unowned_message(self):
        message = self.ws.send_message(self.b, self.c_id, 'private', 'notowned')
        self.err('NOT_FOUND', lambda: self.publish(refs=[{'kind':'message','ref':message['message_id']}]))

    def test_unapproved_decisions_and_stale_memories_cannot_be_sourced(self):
        proposal = self.ws.propose_decision(self.a, 'Use FTS5', 'Small and local', self.refs, 'proposal')
        self.err('NOT_FOUND', lambda: self.publish(refs=[{'kind':'decision','ref':proposal['decision_id']}]))
        self.ws.decide_decision(self.op, proposal['decision_id'], 0, 'accepted', 'approved', 'accept')
        memory = self.ws.record_memory(self.a, 'finding', 'TTL expiry', 'Short-lived', self.refs,
                                       'memory', task_id=self.task_id, ttl_seconds=60)
        refs = [{'kind':'decision','ref':proposal['decision_id']},
                {'kind':'memory','ref':memory['memory_id']}]
        snap = self.publish(refs=refs)
        self.handoff(snap)
        self.assertEqual(refs, self.ws.get_context(self.b, snap['snapshot_id'])['source_refs'])
        self.clock.advance(61)
        rec = self.ws.get_context(self.b, snap['snapshot_id'])
        self.assertEqual([refs[0]], rec['source_refs'])
        self.assertEqual(1, rec['stale_source_count'])
        self.err('LEASE_EXPIRED', lambda: self.publish('late', refs=refs))

    def test_source_must_belong_to_this_task_if_task_scoped(self):
        other = self.ws.create_task(self.op, 'Other task', 'Other', 'another')
        self.err('NOT_FOUND', lambda: self.publish(refs=[{'kind':'task','ref':other['task_id']}]))
        self.err('VALIDATION_FAILED', lambda: self.publish(refs=[]))
        self.err('VALIDATION_FAILED', lambda: self.publish(refs=self.refs * 2))

    def test_paths_are_relative_metadata_never_file_reads(self):
        for paths in (['../secrets.env'], ['/etc/passwd'], ['C:\\secrets.txt'],
                      ['src\\nested.py'], ['.'], ['src//wrong'], ['src/../x'], ['x\x00y']):
            self.err('VALIDATION_FAILED', lambda p=paths: self.publish('bad-' + repr(p), changed_paths=p))
        self.err('PAYLOAD_REJECTED', lambda: self.publish('many', changed_paths=[f'file{i}' for i in range(40)]))
        snap = self.publish(changed_paths=['src/code.py', 'docs/design.md'])
        self.assertEqual(['src/code.py', 'docs/design.md'],
                         self.ws.get_context(self.a, snap['snapshot_id'])['changed_paths'])

    def test_long_bodies_and_non_str_inputs_rejected(self):
        self.err('PAYLOAD_REJECTED', lambda: self.publish('huge', summary='x'*5000))
        self.err('VALIDATION_FAILED', lambda: self.publish('empty', summary=' '))
        self.err('VALIDATION_FAILED', lambda: self.publish('list', next_steps=['do thing']))
        self.err('PAYLOAD_REJECTED', lambda: self.handoff(self.publish('one'), 'huge-action', action='a'*5000))

    def test_list_task_contexts_paginates_and_truncates(self):
        for i in range(5):
            self.publish(f's-{i}', summary=f'Checkpoint-{i}-'+'ü'*200)
        first = self.ws.list_task_contexts(self.a, self.task_id, limit=2)
        self.assertEqual(2, len(first['snapshots']))
        self.assertTrue(first['has_more'])
        self.assertLessEqual(len(first['snapshots'][0]['summary_preview'].encode('utf-8')), 256)
        second = self.ws.list_task_contexts(self.a, self.task_id, limit=3, before_seq=first['next_cursor'])
        self.assertEqual(3, len(second['snapshots']))
        self.assertFalse({s['snapshot_id'] for s in first['snapshots']} &
                         {s['snapshot_id'] for s in second['snapshots']})
        self.err('VALIDATION_FAILED', lambda: self.ws.list_task_contexts(self.a, self.task_id, limit=101))

    def test_handoff_inbox_pagination_is_stable(self):
        for i in range(3):
            snap = self.publish(f'snap-{i}')
            self.handoff(snap, f'hand-{i}')
        first = self.ws.handoff_inbox(self.b, limit=2)
        self.assertEqual(2, len(first['handoffs']))
        rest = self.ws.handoff_inbox(self.b, after_seq=first['next_cursor'], limit=2)
        self.assertEqual(1, len(rest['handoffs']))
        self.assertFalse(first['handoffs'][0]['handoff_id'] == rest['handoffs'][0]['handoff_id'])

    def test_changes_do_not_leak_private_context_or_body(self):
        snap = self.publish()
        self.assertEqual([], self.ws.context_changes(self.b)['changes'])
        self.handoff(snap)
        events = self.ws.context_changes(self.b)['changes']
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(snap['snapshot_id'], events[-1]['snapshot_id'])
        self.assertNotIn('Refresh token path explored', str(events))
        self.assertEqual([], self.ws.context_changes(self.c)['changes'])
        part = self.ws.context_changes(self.b, limit=1)
        next_page = self.ws.context_changes(self.b, after_event_id=part['next_cursor'])
        self.assertFalse({x['event_id'] for x in part['changes']} &
                         {x['event_id'] for x in next_page['changes']})

    def test_revoked_or_disabled_principal_cannot_access_snapshots(self):
        snap = self.publish()
        self.handoff(snap)
        self.ws.disable_agent(self.op, self.b_id, 'disable-b', 'No longer permitted')
        self.err('UNAUTHENTICATED', lambda: self.ws.get_context(self.b, snap['snapshot_id']))
        self.err('UNAUTHENTICATED', lambda: self.ws.handoff_inbox(self.b))

    def test_duplicate_snapshot_is_idempotent_across_sessions(self):
        a2 = self.ws.open_session(self.a_cred)['session_token']
        first = self.publish()
        self.assertEqual(first, self.publish(token=a2))
        self.err('IDEMPOTENCY_CONFLICT', lambda: self.publish(summary='Modified'))

    def test_concurrent_handoff_single_recipient_winner(self):
        snap = self.publish()
        def send(key):
            try:
                return self.handoff(snap,key)
            except WorkspaceError as err:
                return err.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(send, ['one', 'two']))
        self.assertEqual(1, sum(isinstance(x,dict) for x in result))
        self.assertIn('INVALID_TRANSITION', result)
        self.assertEqual(1, len(self.ws.handoff_inbox(self.b)['handoffs']))

    def test_atomic_snapshot_creation_rollback(self):
        self.ws.inject_failpoint('before_context_commit_event')
        with self.assertRaises(RuntimeError):
            self.publish()
        self.ws.inject_failpoint(None)
        with closing(sqlite3.connect(self.db)) as db, db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM context_snapshots').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT count(*) FROM context_sources').fetchone()[0])
        self.assertEqual([], self.ws.list_task_contexts(self.a,self.task_id)['snapshots'])

    def test_handoff_rollback_does_not_grant_access(self):
        snap = self.publish()
        self.ws.inject_failpoint('before_handoff_commit_event')
        with self.assertRaises(RuntimeError):
            self.handoff(snap)
        self.ws.inject_failpoint(None)
        self.err('NOT_FOUND', lambda: self.ws.get_context(self.b, snap['snapshot_id']))
        self.assertEqual([], self.ws.handoff_inbox(self.b)['handoffs'])

    def test_migration_preserves_v4_records(self):
        # This test demonstrates a downgrade to the last release must be rejected,
        # and the upgrade should never lose existing task or snapshot records.
        s = self.publish()
        second = Workspace(self.db, clock=self.clock)
        self.assertEqual(5, second.schema_version())
        self.assertEqual(self.task_id, second.get_task(self.op, self.task_id)['task_id'])
        self.assertEqual(s['snapshot_id'], second.get_context(self.a, s['snapshot_id'])['snapshot_id'])

    def test_context_view_is_bounded_without_including_source_bodies(self):
        msg = self.ws.send_message(self.a, self.b_id, 'DO_NOT_EXPOSE_RAW_MESSAGE_BODY', 'message')
        snap = self.publish(refs=[{'kind':'message','ref':msg['message_id']}])
        self.handoff(snap)
        read = self.ws.get_context(self.b,snap['snapshot_id'])
        self.assertNotIn('DO_NOT_EXPOSE_RAW_MESSAGE_BODY', str(read))
        self.assertNotIn('token_budget', read)

    def test_handoff_does_not_grant_access_to_other_snapshots(self):
        shared = self.publish('shared')
        private = self.publish('private')
        self.handoff(shared)
        self.assertEqual(shared['snapshot_id'], self.ws.get_context(self.b, shared['snapshot_id'])['snapshot_id'])
        self.err('NOT_FOUND', lambda: self.ws.get_context(self.b, private['snapshot_id']))
        visible = self.ws.list_task_contexts(self.b, self.task_id)['snapshots']
        self.assertEqual([shared['snapshot_id']], [x['snapshot_id'] for x in visible])

    def test_cross_workspace_snapshot_not_found(self):
        snap = self.publish()
        other = Workspace(pathlib.Path(self.tmp.name)/'other.db',clock=self.clock)
        boot = other.bootstrap('other', self.tmp.name)
        op = other.open_session(boot['operator_credential'])['session_token']
        self.err('NOT_FOUND', lambda: other.get_context(op, snap['snapshot_id']))
        self.assertEqual([], other.context_changes(op)['changes'])

    def test_retracted_memory_reference_is_hidden_after_snapshot(self):
        mem = self.ws.record_memory(self.a,'finding','Observed timeout','Unconfirmed finding',self.refs,
                                    'mem-retract',task_id=self.task_id)
        snap = self.publish(refs=[{'kind':'memory','ref':mem['memory_id']}])
        self.handoff(snap)
        self.ws.retract_memory(self.a,mem['memory_id'],'Superseded by current behavior','retract-mem')
        view = self.ws.get_context(self.b,snap['snapshot_id'])
        self.assertEqual([],view['source_refs'])
        self.assertEqual(1,view['stale_source_count'])

    def test_superseded_decision_reference_does_not_look_current(self):
        old = self.ws.propose_decision(self.a,'Use SQLite','Local prototype',self.refs,'decision-old')
        self.ws.decide_decision(self.op,old['decision_id'],0,'accepted','agreed','old-accept')
        snap = self.publish(refs=[{'kind':'decision','ref':old['decision_id']}])
        newer = self.ws.propose_decision(self.a,'Use same SQLite','Clarification',self.refs,'decision-new',
                                         supersedes_decision_id=old['decision_id'])
        self.ws.decide_decision(self.op,newer['decision_id'],0,'accepted','updated','new-accept')
        self.handoff(snap)
        view = self.ws.get_context(self.b,snap['snapshot_id'])
        self.assertEqual([],view['source_refs'])
        self.assertEqual(1,view['stale_source_count'])

    def test_handoff_after_task_release_or_new_lease_is_denied(self):
        snap = self.publish()
        released = self.ws.release_task(self.a,self.task_id,self.lease['version'],
                                        self.lease['fence_token'],'end')
        self.err('STALE_FENCE',lambda: self.handoff(snap))
        self.ws.claim_task(self.b,self.task_id,released['version'],'newowner')
        self.err('STALE_FENCE',lambda: self.handoff(snap,'later'))

    def test_revoked_session_requires_new_token_to_access_handoff(self):
        snap = self.publish()
        self.handoff(snap)
        sid = self.ws.session_info(self.b)['session_id']
        self.ws.revoke_session(self.op,sid,'revoke-b','logout')
        self.err('UNAUTHENTICATED',lambda: self.ws.get_context(self.b,snap['snapshot_id']))
        # Grant is scoped to persistent principal, not a revoked session.
        reconnected = self.ws.open_session(self.b_cred)['session_token']
        self.assertEqual(snap['snapshot_id'],self.ws.get_context(reconnected,snap['snapshot_id'])['snapshot_id'])

    def test_partial_v4_to_v5_schema_is_rejected_transactionally(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            for table in ('context_sources','context_handoffs','context_snapshots'):
                db.execute('DROP TABLE '+table)
            db.execute('DELETE FROM schema_migrations WHERE version=5')
            db.execute('PRAGMA user_version=4')
            db.execute('CREATE TABLE context_snapshots (snapshot_id TEXT)')
        self.err('UNSUPPORTED_SCHEMA', lambda: Workspace(self.db, clock=self.clock))
        with closing(sqlite3.connect(self.db)) as db, db:
            self.assertEqual(4, db.execute('PRAGMA user_version').fetchone()[0])
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='context_sources'").fetchone())

    def test_handoff_exposes_no_credential_material(self):
        snap = self.publish()
        self.handoff(snap)
        data = str(self.ws.get_context(self.b,snap['snapshot_id']))+str(self.ws.handoff_inbox(self.b))
        self.assertNotIn(self.a_cred, data)
        self.assertNotIn(self.b_cred, data)
        self.assertNotIn(self.a, data)
        self.assertNotIn(self.b, data)


if __name__ == '__main__':
    unittest.main()
