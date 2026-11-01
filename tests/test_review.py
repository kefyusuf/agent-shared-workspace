"""M1.2 review gate tests: written to fail before implementation."""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import textwrap
import pathlib
import tempfile
import threading
import unittest

from agent_workspace import Workspace, WorkspaceError


class FakeClock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = pathlib.Path(tmp.name) / "workspace.sqlite"
        self.clock = FakeClock()
        self.ws = Workspace(self.db, clock=self.clock)
        boot = self.ws.bootstrap("review-pilot", tmp.name)
        self.operator = self.ws.open_session(boot["operator_credential"])["session_token"]
        self.author = self.ws.enroll_agent(self.operator, "agent-a", "codex")
        self.other = self.ws.enroll_agent(self.operator, "agent-b", "claude")
        self.a = self.ws.open_session(self.author["credential"])["session_token"]
        self.b = self.ws.open_session(self.other["credential"])["session_token"]
        self.task = self.ws.create_task(self.operator, "Fix auth", "Refresh works with 401", "create-review")
        self.claim = self.ws.claim_task(self.a, self.task["task_id"], 0, "claim-review")

    @property
    def tid(self):
        return self.task["task_id"]

    def assert_error(self, code, func):
        with self.assertRaises(WorkspaceError) as ctx:
            func()
        self.assertEqual(ctx.exception.code, code)

    def evidence(self, *, token=None, version=None, fence=None, key="evidence-01",
                 kind="test_report", reference="ci://runs/42", result="passed", note="4 tests pass"):
        return self.ws.add_evidence(
            token or self.a, self.tid, self.claim["version"] if version is None else version,
            self.claim["fence_token"] if fence is None else fence,
            key, kind, reference, result, note,
        )

    def to_review(self, *, result="passed"):
        e = self.evidence(result=result, note=f"Tests {result}")
        review = self.ws.submit_for_review(self.a, self.tid, e["version"], self.claim["fence_token"],
                                            "submit-review", "Changed refresh handling; see test reference")
        return e, review

    def verify(self, evidence_id, version, *, verdict="verified", key="verify-01"):
        return self.ws.verify_evidence(self.operator, self.tid, evidence_id, version,
                                       key, verdict, "Manually inspected named report")

    def test_operator_is_not_allowed_to_own_work_it_would_review(self):
        created = self.ws.create_task(self.operator, "Do task", "A separate agent must implement", "operator-task")
        self.assert_error("FORBIDDEN", lambda: self.ws.claim_task(
            self.operator, created["task_id"], created["version"], "operator-claim"))

    def test_at11_done_is_impossible_without_review(self):
        self.assert_error("REVIEW_REQUIRED", lambda: self.ws.finish_task(self.a, self.tid, 1, "finish"))
        self.assert_error("INVALID_TRANSITION", lambda: self.ws.approve_review(self.operator, self.tid, 1, "approve", "Looked at output"))
        self.assertEqual("InProgress", self.ws.get_task(self.operator, self.tid)["status"])

    def test_at12_verified_evidence_and_independent_review_allows_done(self):
        e, submitted = self.to_review()
        self.assertEqual("Review", self.ws.get_task(self.operator, self.tid)["status"])
        self.assertEqual("pending", self.ws.get_review(self.b, self.tid)["status"])
        verified = self.verify(e["evidence_id"], submitted["version"])
        done = self.ws.approve_review(self.operator, self.tid, verified["version"], "approve-01", "Acceptance criteria checked")
        self.assertEqual("Done", done["status"])
        self.assertEqual("approved", self.ws.get_review(self.b, self.tid)["status"])
        fresh = Workspace(self.db, clock=self.clock)
        self.assertEqual("Done", fresh.get_task(self.b, self.tid)["status"])
        self.assertEqual("verified", fresh.get_review(self.b, self.tid)["evidence"][0]["verification_status"])
        self.assertEqual("operator_attestation", fresh.get_review(self.b, self.tid)["evidence"][0]["verification_method"])
        actions = [evt["action"] for evt in fresh.audit_events(self.operator)["events"]]
        self.assertIn("evidence.added", actions)
        self.assertIn("task.review_submitted", actions)
        self.assertIn("evidence.verified", actions)
        self.assertIn("task.review_approved", actions)

    def test_at13_owner_cannot_self_approve_or_verify(self):
        e, s = self.to_review()
        self.assert_error("FORBIDDEN", lambda: self.ws.verify_evidence(
            self.a, self.tid, e["evidence_id"], s["version"], "self-verify", "verified", "Trust me"))
        self.assert_error("FORBIDDEN", lambda: self.ws.approve_review(
            self.a, self.tid, s["version"], "self-approve", "Trust me"))

    def test_submission_requires_evidence_and_current_lease(self):
        self.assert_error("EVIDENCE_REQUIRED", lambda: self.ws.submit_for_review(
            self.a, self.tid, 1, self.claim["fence_token"], "without-evidence", "handoff"))
        e = self.evidence()
        self.assert_error("STALE_FENCE", lambda: self.ws.submit_for_review(
            self.b, self.tid, e["version"], self.claim["fence_token"], "foreign-submit", "handoff"))
        self.assert_error("VERSION_CONFLICT", lambda: self.ws.submit_for_review(
            self.a, self.tid, 1, self.claim["fence_token"], "stale-submit", "handoff"))

    def test_evidence_bound_to_lease_and_stale_fence_cannot_publish(self):
        self.clock.advance(61)
        next_claim = self.ws.claim_task(self.b, self.tid, 1, "claim-b")
        self.assert_error("STALE_FENCE", lambda: self.evidence(version=next_claim["version"]))
        self.assert_error("STALE_FENCE", lambda: self.ws.submit_for_review(
            self.a, self.tid, next_claim["version"], self.claim["fence_token"], "stale-submit", "handoff"))
        self.assert_error("EVIDENCE_REQUIRED", lambda: self.ws.submit_for_review(
            self.b, self.tid, next_claim["version"], next_claim["fence_token"], "no-b-evidence", "handoff"))

    def test_pending_verification_blocks_approval(self):
        _, s = self.to_review()
        self.assert_error("EVIDENCE_UNVERIFIED", lambda: self.ws.approve_review(
            self.operator, self.tid, s["version"], "no-check", "Need verification"))
        self.assertEqual("Review", self.ws.get_task(self.operator, self.tid)["status"])

    def test_failing_test_report_can_be_submitted_but_not_approved(self):
        e, s = self.to_review(result="failed")
        v = self.verify(e["evidence_id"], s["version"])
        self.assert_error("PASSING_TEST_REQUIRED", lambda: self.ws.approve_review(
            self.operator, self.tid, v["version"], "bad-pass", "Looked at output"))

    def test_commit_evidence_alone_does_not_count_as_passed_tests(self):
        e = self.evidence(kind="commit", reference="abc1234", result="not_applicable", note="Commit ref")
        s = self.ws.submit_for_review(self.a, self.tid, e["version"], self.claim["fence_token"],
                                      "commit-submit", "Implementation handoff")
        v = self.verify(e["evidence_id"], s["version"])
        self.assert_error("PASSING_TEST_REQUIRED", lambda: self.ws.approve_review(
            self.operator, self.tid, v["version"], "commit-only", "Inspected commit"))

    def test_rejected_evidence_blocks_approval_and_request_changes_requeues(self):
        e, s = self.to_review()
        r = self.verify(e["evidence_id"], s["version"], verdict="rejected")
        self.assert_error("EVIDENCE_UNVERIFIED", lambda: self.ws.approve_review(
            self.operator, self.tid, r["version"], "rejected-approve", "No"))
        changed = self.ws.request_changes(self.operator, self.tid, r["version"], "request-changes", "Report missing logs")
        self.assertEqual("Ready", changed["status"])
        self.assertEqual("changes_requested", self.ws.get_review(self.a, self.tid)["status"])
        claimed_again = self.ws.claim_task(self.a, self.tid, changed["version"], "new-cycle")
        self.assertGreater(claimed_again["fence_token"], self.claim["fence_token"])
        self.assert_error("INVALID_TRANSITION", lambda: self.ws.verify_evidence(
            self.operator, self.tid, e["evidence_id"], claimed_again["version"],
            "verify-old", "verified", "Old report"))
        self.assert_error("EVIDENCE_REQUIRED", lambda: self.ws.submit_for_review(
            self.a, self.tid, claimed_again["version"], claimed_again["fence_token"], "submit-again", "new handoff"))

    def test_evidence_validation_and_forbidden_reviewer_actions(self):
        self.assert_error("VALIDATION_FAILED", lambda: self.evidence(kind="test_report", result="not_applicable"))
        self.assert_error("VALIDATION_FAILED", lambda: self.evidence(kind="commit", result="passed"))
        self.assert_error("VALIDATION_FAILED", lambda: self.evidence(reference=""))
        e, s = self.to_review()
        self.assert_error("FORBIDDEN", lambda: self.ws.request_changes(
            self.b, self.tid, s["version"], "request-by-b", "please"))
        self.assert_error("FORBIDDEN", lambda: self.ws.verify_evidence(
            self.b, self.tid, e["evidence_id"], s["version"], "verify-by-b", "verified", "Looked"))

    def test_review_idempotency_and_version_conflict(self):
        e = self.evidence()
        self.assertEqual(e, self.evidence())
        self.assert_error("IDEMPOTENCY_CONFLICT", lambda: self.evidence(key="evidence-01", result="failed"))
        s = self.ws.submit_for_review(self.a, self.tid, e["version"], self.claim["fence_token"],
                                      "submit", "handoff")
        self.assertEqual(s, self.ws.submit_for_review(self.a, self.tid, e["version"], self.claim["fence_token"], "submit", "handoff"))
        v = self.verify(e["evidence_id"], s["version"])
        self.assertEqual(v, self.verify(e["evidence_id"], s["version"]))
        self.assert_error("VERSION_CONFLICT", lambda: self.ws.approve_review(
            self.operator, self.tid, s["version"], "stale-approve", "Validated"))
        self.assert_error("IDEMPOTENCY_CONFLICT", lambda: self.ws.verify_evidence(
            self.operator, self.tid, e["evidence_id"], s["version"], "verify-01", "rejected", "Changed"))

    def test_approval_rolls_back_with_event_on_injected_fault(self):
        e, s = self.to_review()
        v = self.verify(e["evidence_id"], s["version"])
        count = len(self.ws.audit_events(self.operator)["events"])
        self.ws.inject_failpoint("before_review_approval_event")
        with self.assertRaises(RuntimeError):
            self.ws.approve_review(self.operator, self.tid, v["version"], "fail-approve", "Checked")
        self.ws.inject_failpoint(None)
        self.assertEqual("Review", self.ws.get_task(self.operator, self.tid)["status"])
        self.assertEqual(count, len(self.ws.audit_events(self.operator)["events"]))
        self.assertEqual("Done", self.ws.approve_review(self.operator, self.tid, v["version"], "fail-approve", "Checked")["status"])

    def test_revision_requires_new_evidence_from_current_lease(self):
        e, s = self.to_review()
        requested = self.ws.request_changes(self.operator, self.tid, s["version"], "changes-1", "Please re-test")
        c = self.ws.claim_task(self.a, self.tid, requested["version"], "claim-new")
        self.assert_error("EVIDENCE_REQUIRED", lambda: self.ws.submit_for_review(
            self.a, self.tid, c["version"], c["fence_token"], "reuse-old", "Updated"))
        e2 = self.ws.add_evidence(self.a, self.tid, c["version"], c["fence_token"],
                                   "new-evidence", "test_report", "ci://runs/43", "passed", "5 tests pass")
        s2 = self.ws.submit_for_review(self.a, self.tid, e2["version"], c["fence_token"], "submit-new", "Retested")
        self.assertNotEqual(s["submission_id"], s2["submission_id"])
        self.assertEqual([e2["evidence_id"]], [row["evidence_id"] for row in self.ws.get_review(self.operator, self.tid)["evidence"]])

    def test_verified_failed_test_still_blocks_even_if_another_test_passed(self):
        passed = self.evidence()
        failed = self.ws.add_evidence(
            self.a, self.tid, passed["version"], self.claim["fence_token"],
            "failed-suite", "test_report", "ci://runs/44", "failed", "Integration suite failed")
        submission = self.ws.submit_for_review(
            self.a, self.tid, failed["version"], self.claim["fence_token"],
            "mixed-submit", "Integration failures remain")
        v1 = self.verify(passed["evidence_id"], submission["version"])
        v2 = self.verify(failed["evidence_id"], v1["version"], key="verify-failed")
        self.assert_error("PASSING_TEST_REQUIRED", lambda: self.ws.approve_review(
            self.operator, self.tid, v2["version"], "mixed-approve", "Tests reviewed"))

    def test_review_is_readable_after_new_process_restart(self):
        evidence, submitted = self.to_review()
        source = textwrap.dedent("""
            import json, os
            from agent_workspace import Workspace
            ws = Workspace(os.environ["AW_DB"])
            session = ws.open_session(os.environ["AW_AGENT_CREDENTIAL"])["session_token"]
            review = ws.get_review(session, os.environ["AW_TASK"])
            print(json.dumps({"status": review["status"],
                              "evidence_id": review["evidence"][0]["evidence_id"]}))
        """)
        env = dict(os.environ, AW_DB=str(self.db), AW_TASK=self.tid,
                   AW_AGENT_CREDENTIAL=self.other["credential"])
        env["PYTHONPATH"] = str(pathlib.Path(__file__).resolve().parents[1] / "src")
        child = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True,
                               env=env, timeout=15, check=True)
        import json
        self.assertEqual({"status": "pending", "evidence_id": evidence["evidence_id"]}, json.loads(child.stdout))
        self.assertEqual("Review", self.ws.get_task(self.operator, self.tid)["status"])

    def test_real_process_death_before_commit_does_not_publish_done(self):
        evidence, submitted = self.to_review()
        verified = self.verify(evidence["evidence_id"], submitted["version"])
        # The process dies after domain state writes but before event/commit.
        child_source = textwrap.dedent("""
            import os
            from agent_workspace import Workspace
            class KilledDuringCommit(Workspace):
                @staticmethod
                def _event(db, actor, action, kind, subject, payload, now):
                    if action == "task.review_approved":
                        os._exit(77)
                    return Workspace._event(db, actor, action, kind, subject, payload, now)
            ws = KilledDuringCommit(os.environ["AW_DB"])
            token = ws.open_session(os.environ["AW_OPERATOR_CREDENTIAL"])["session_token"]
            ws.approve_review(token, os.environ["AW_TASK"], int(os.environ["AW_VERSION"]),
                              "crash-before-commit", "Human checked evidence")
        """)
        env = dict(os.environ, AW_DB=str(self.db), AW_TASK=self.tid,
                   AW_VERSION=str(verified["version"]))
        # Retrieve the credential from the setup's original bootstrap safely (never print).
        # A new operator session token is sufficient for this test instead of the credential.
        child_source = child_source.replace(
            'ws.open_session(os.environ["AW_OPERATOR_CREDENTIAL"])["session_token"]',
            'os.environ["AW_OPERATOR_SESSION"]')
        env["AW_OPERATOR_SESSION"] = self.operator
        env["PYTHONPATH"] = str(pathlib.Path(__file__).resolve().parents[1] / "src")
        child = subprocess.run([sys.executable, "-c", child_source], capture_output=True,
                               text=True, env=env, timeout=15)
        self.assertEqual(77, child.returncode, child.stderr)
        recovered = Workspace(self.db, clock=self.clock)
        self.assertEqual("Review", recovered.get_task(self.operator, self.tid)["status"])
        self.assertEqual("pending", recovered.get_review(self.operator, self.tid)["status"])
        self.assertEqual(0, sum(e["action"] == "task.review_approved"
                                for e in recovered.audit_events(self.operator)["events"]))
        self.assertEqual("Done", recovered.approve_review(
            self.operator, self.tid, verified["version"], "crash-before-commit", "Human checked evidence")["status"])

    def test_approval_race_is_single_winner(self):
        e, s = self.to_review()
        v = self.verify(e["evidence_id"], s["version"])
        bar = threading.Barrier(2)

        def approve(key):
            bar.wait()
            try:
                return self.ws.approve_review(self.operator, self.tid, v["version"], key, "Verified output")
            except WorkspaceError as ex:
                return ex.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1, f2 = pool.submit(approve, "approve-race-1"), pool.submit(approve, "approve-race-2")
            results = [f1.result(), f2.result()]
        self.assertEqual(1, sum(isinstance(r, dict) for r in results))
        self.assertEqual("VERSION_CONFLICT", next(r for r in results if isinstance(r, str)))
        self.assertEqual(1, sum(e["action"] == "task.review_approved" for e in self.ws.audit_events(self.operator)["events"]))


if __name__ == "__main__":
    unittest.main()
