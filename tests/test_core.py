"""M1 acceptance-focused tests, written before the implementation."""
import concurrent.futures
import os
import subprocess
import sys
import textwrap
import json
import pathlib
import tempfile
import threading
import unittest

from agent_workspace import Workspace, WorkspaceError


class FakeClock:
    def __init__(self):
        self.value = 1_800_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "workspace.db"
        self.clock = FakeClock()
        self.ws = Workspace(self.path, clock=self.clock)
        result = self.ws.bootstrap("test-project", self.temp.name)
        self.project = result["project_id"]
        self.operator_credential = result["operator_credential"]
        self.operator = self.ws.open_session(self.operator_credential)["session_token"]
        self.agent_a = self.ws.enroll_agent(self.operator, "codex", "codex")
        self.agent_b = self.ws.enroll_agent(self.operator, "claude", "claude")
        self.a = self.ws.open_session(self.agent_a["credential"])["session_token"]
        self.b = self.ws.open_session(self.agent_b["credential"])["session_token"]

    def error_code(self, code, callable_):
        with self.assertRaises(WorkspaceError) as cm:
            callable_()
        self.assertEqual(code, cm.exception.code)

    def task(self, key="task-01"):
        return self.ws.create_task(self.operator, "Implement refresh validation", "Must handle 401 and retry", key)

    def test_single_winner_concurrent_claim_at01(self):
        task = self.task()
        gate = threading.Barrier(2)

        def claim(token, key):
            gate.wait()
            try:
                return self.ws.claim_task(token, task["task_id"], 0, key)
            except WorkspaceError as ex:
                return ex.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(claim, self.a, "claim-a")
            future_b = pool.submit(claim, self.b, "claim-b")
            results = [future_a.result(), future_b.result()]
        self.assertEqual(1, sum(isinstance(r, dict) for r in results))
        self.assertIn(next(r for r in results if isinstance(r, str)), {"VERSION_CONFLICT", "LEASE_HELD"})
        self.assertEqual("InProgress", self.ws.get_task(self.operator, task["task_id"])["status"])

    def test_independent_processes_compete_for_one_claim(self):
        t = self.task()
        start_dir = pathlib.Path(self.temp.name) / "sync"
        start_dir.mkdir()
        code = textwrap.dedent("""
            import json, os, pathlib, time
            from agent_workspace import Workspace, WorkspaceError
            w = Workspace(os.environ["AW_DB"])
            token = w.open_session(os.environ["AW_CREDENTIAL"])["session_token"]
            sync = pathlib.Path(os.environ["AW_SYNC"])
            (sync / os.environ["AW_KEY"]).write_text("ready")
            deadline = time.monotonic() + 10
            while len(list(sync.iterdir())) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            try:
                w.claim_task(token, os.environ["AW_TASK"], 0, os.environ["AW_KEY"])
                print(json.dumps({"result": "claimed"}))
            except WorkspaceError as e:
                print(json.dumps({"result": e.code}))
        """)
        common = os.environ.copy()
        common.update({"AW_DB": str(self.path), "AW_SYNC": str(start_dir), "AW_TASK": t["task_id"],
                       "PYTHONPATH": str(pathlib.Path(__file__).resolve().parents[1] / "src")})
        processes = []
        for key, cred in (("worker-a", self.agent_a["credential"]), ("worker-b", self.agent_b["credential"])):
            env = dict(common, AW_KEY=key, AW_CREDENTIAL=cred)
            processes.append(subprocess.Popen([sys.executable, "-c", code], env=env,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        results = []
        for process in processes:
            out, err = process.communicate(timeout=15)
            self.assertEqual(0, process.returncode, err)
            results.append(json.loads(out.strip())["result"])
        self.assertEqual(1, results.count("claimed"))
        self.assertIn(next(r for r in results if r != "claimed"), ("VERSION_CONFLICT", "LEASE_HELD"))

    def test_expired_lease_reclaim_fences_old_writer_at02(self):
        t = self.task()
        first = self.ws.claim_task(self.a, t["task_id"], 0, "ca", 20)
        self.clock.advance(21)
        second = self.ws.claim_task(self.b, t["task_id"], first["version"], "cb", 20)
        self.assertGreater(second["fence_token"], first["fence_token"])
        self.error_code("STALE_FENCE", lambda: self.ws.block_task(self.a, t["task_id"], second["version"], first["fence_token"], "bad", "obsolete"))
        self.assertEqual("InProgress", self.ws.get_task(self.operator, t["task_id"])["status"])

    def test_idempotent_claim_and_send_at03(self):
        t = self.task()
        args = (self.a, t["task_id"], 0, "claim-x")
        first = self.ws.claim_task(*args)
        self.assertEqual(first, self.ws.claim_task(*args))
        m = self.ws.send_message(self.a, self.agent_b["agent_id"], "Please review this", "mail-01")
        self.assertEqual(m, self.ws.send_message(self.a, self.agent_b["agent_id"], "Please review this", "mail-01"))
        self.assertEqual(1, len(self.ws.inbox(self.b)["messages"]))

    def test_retry_after_new_session_uses_original_idempotent_effect(self):
        m = self.ws.send_message(self.a, self.agent_b["agent_id"], "resume-safe", "durable-request")
        new_session = self.ws.open_session(self.agent_a["credential"])["session_token"]
        self.assertEqual(m, self.ws.send_message(new_session, self.agent_b["agent_id"], "resume-safe", "durable-request"))
        self.assertEqual(1, len(self.ws.inbox(self.b)["messages"]))

    def test_idempotency_collision_rejected_at04(self):
        self.ws.send_message(self.a, self.agent_b["agent_id"], "one", "same-key")
        self.error_code("IDEMPOTENCY_CONFLICT", lambda: self.ws.send_message(self.a, self.agent_b["agent_id"], "two", "same-key"))
        self.error_code("IDEMPOTENCY_CONFLICT", lambda: self.ws.create_task(self.a, "other", "criteria", "same-key"))

    def test_mail_survives_restart_at05(self):
        sent = self.ws.send_message(self.a, self.agent_b["agent_id"], "offline hello", "msg-02")
        fresh_ws = Workspace(self.path, clock=self.clock)
        received = fresh_ws.inbox(self.b)["messages"]
        self.assertEqual(sent["message_id"], received[0]["message_id"])
        self.assertEqual("offline hello", received[0]["body"])

    def test_inbox_does_not_ack_at06(self):
        message = self.ws.send_message(self.a, self.agent_b["agent_id"], "A question", "msg-03")
        self.ws.inbox(self.b)
        fresh = Workspace(self.path, clock=self.clock)
        self.assertIsNone(fresh.inbox(self.b)["messages"][0]["acknowledged_at"])
        self.assertEqual(message["message_id"], fresh.inbox(self.b)["messages"][0]["message_id"])

    def test_recipient_acknowledges_and_persists_at22(self):
        m = self.ws.send_message(self.a, self.agent_b["agent_id"], "Do this", "msg-04")
        self.error_code("FORBIDDEN", lambda: self.ws.ack_message(self.a, m["message_id"], "ack-wrong"))
        ack = self.ws.ack_message(self.b, m["message_id"], "ack-right")
        self.assertIsNotNone(ack["acknowledged_at"])
        self.assertIsNotNone(self.ws.get_message(self.b, m["message_id"])["read_at"])
        self.assertEqual(ack, self.ws.ack_message(self.b, m["message_id"], "ack-right"))
        self.assertEqual([], Workspace(self.path, clock=self.clock).inbox(self.b)["messages"])
        self.assertEqual(1, len(self.ws.inbox(self.b, include_acked=True)["messages"]))

    def test_stale_version_at14(self):
        t = self.task()
        claim = self.ws.claim_task(self.a, t["task_id"], 0, "ca")
        self.error_code("VERSION_CONFLICT", lambda: self.ws.renew_task(self.a, t["task_id"], 0, claim["fence_token"], "wrong"))
        self.assertEqual(claim["version"], self.ws.get_task(self.a, t["task_id"])["version"])

    def test_transactional_state_and_audit_at15(self):
        t = self.task()
        self.ws.claim_task(self.a, t["task_id"], 0, "ca")
        fresh = Workspace(self.path, clock=self.clock)
        self.assertEqual(1, fresh.get_task(self.operator, t["task_id"])["version"])
        events = fresh.audit_events(self.operator)["events"]
        self.assertEqual("task.claimed", events[-1]["action"])
        self.assertEqual(t["task_id"], events[-1]["subject_id"])

    def test_rollback_on_mid_transaction_failure_at16(self):
        t = self.task()
        before = len(self.ws.audit_events(self.operator)["events"])
        self.ws.inject_failpoint("after_claim_write")
        with self.assertRaises(RuntimeError):
            self.ws.claim_task(self.a, t["task_id"], 0, "claim-fail")
        self.ws.inject_failpoint(None)
        self.assertEqual(0, self.ws.get_task(self.operator, t["task_id"])["version"])
        self.assertEqual(before, len(self.ws.audit_events(self.operator)["events"]))
        self.ws.claim_task(self.a, t["task_id"], 0, "claim-fail")

    def test_forged_identity_rejected_at19(self):
        self.error_code("FORBIDDEN", lambda: self.ws.send_message(self.a, self.agent_b["agent_id"], "forged", "forged-01", claimed_sender_id=self.agent_b["agent_id"]))
        self.assertEqual([], self.ws.inbox(self.b)["messages"])

    def test_token_is_required_and_agent_cannot_enroll(self):
        self.error_code("UNAUTHENTICATED", lambda: self.ws.get_task("invalid", "some-task"))
        self.error_code("FORBIDDEN", lambda: self.ws.enroll_agent(self.a, "rogue", "any"))

    def test_messages_are_recipient_isolated(self):
        msg = self.ws.send_message(self.a, self.agent_b["agent_id"], "private", "mail-prv")
        self.error_code("FORBIDDEN", lambda: self.ws.get_message(self.a, msg["message_id"]))
        self.assertEqual("private", self.ws.get_message(self.b, msg["message_id"])["body"])

    def test_renew_and_release_are_fenced(self):
        t = self.task()
        c = self.ws.claim_task(self.a, t["task_id"], 0, "claim-01")
        renewed = self.ws.renew_task(self.a, t["task_id"], c["version"], c["fence_token"], "renew-01")
        self.assertGreater(renewed["expires_at"], c["expires_at"])
        released = self.ws.release_task(self.a, t["task_id"], renewed["version"], c["fence_token"], "release-01")
        self.assertEqual("Ready", released["status"])
        self.error_code("STALE_FENCE", lambda: self.ws.block_task(self.a, t["task_id"], released["version"], c["fence_token"], "block-x", "old"))

    def test_bound_but_expired_session_is_unusable(self):
        t = self.task()
        self.clock.advance(60 * 60 * 13)
        self.error_code("UNAUTHENTICATED", lambda: self.ws.claim_task(self.a, t["task_id"], 0, "claim-expired"))

    def test_task_cannot_be_done_without_review(self):
        t = self.task()
        self.error_code("REVIEW_REQUIRED", lambda: self.ws.finish_task(self.a, t["task_id"], 0, "premature"))
        self.assertEqual("Ready", self.ws.get_task(self.operator, t["task_id"])["status"])

    def test_recovery_after_blocked_task(self):
        t = self.task()
        c = self.ws.claim_task(self.a, t["task_id"], 0, "claim-block")
        blocked = self.ws.block_task(self.a, t["task_id"], c["version"], c["fence_token"], "block", "pending API contract")
        self.assertEqual("Blocked", blocked["status"])
        ready = self.ws.requeue_task(self.operator, t["task_id"], blocked["version"], "requeue")
        self.assertEqual("Ready", ready["status"])
        self.assertGreater(self.ws.claim_task(self.b, t["task_id"], ready["version"], "new")['fence_token'], c['fence_token'])

    def test_bootstrap_one_project_only(self):
        self.error_code("ALREADY_INITIALIZED", lambda: self.ws.bootstrap("second", self.temp.name))

    def test_validation_limits(self):
        self.error_code("PAYLOAD_REJECTED", lambda: self.ws.send_message(self.a, self.agent_b["agent_id"], "x" * 16385, "large"))
        self.error_code("VALIDATION_FAILED", lambda: self.ws.create_task(self.operator, "", "criteria", "empty-title"))


if __name__ == "__main__":
    unittest.main()
