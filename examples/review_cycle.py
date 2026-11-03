"""M1.2 end-to-end simulation. Neither tests nor external CI are executed here."""
from __future__ import annotations

from pathlib import Path
import json
import tempfile

from agent_workspace import Workspace


def main():
    with tempfile.TemporaryDirectory(prefix="agent-workspace-review-") as tmp:
        path = Path(tmp) / "workspace.sqlite"
        ws = Workspace(path)
        boot = ws.bootstrap("Review Example", tmp)
        operator = ws.open_session(boot["operator_credential"])["session_token"]
        codex_info = ws.enroll_agent(operator, "codex", "codex")
        claude_info = ws.enroll_agent(operator, "claude", "claude-code")
        codex = ws.open_session(codex_info["credential"])["session_token"]
        claude = ws.open_session(claude_info["credential"])["session_token"]

        task = ws.create_task(operator, "Cover auth refresh", "A 401 refresh returns an explicit error", "task-1")
        claim = ws.claim_task(codex, task["task_id"], task["version"], "claim-1")
        message = ws.send_message(codex, claude_info["agent_id"], "Please check the auth refresh plan", "msg-1")
        ws.ack_message(claude, message["message_id"], "ack-1")

        # A reported successful test is an UNTRUSTED claim until a human reviewer inspects it.
        proof = ws.add_evidence(codex, task["task_id"], claim["version"], claim["fence_token"],
                                "evidence-1", "test_report", "ci://example/auth-42", "passed",
                                "Example fixture: 4 tests reported passing")
        handoff = ws.submit_for_review(codex, task["task_id"], proof["version"],
                                        claim["fence_token"], "handoff-1",
                                        "Auth tests summarized; verifier must inspect referenced report")
        reviewer = Workspace(path)
        inspection = reviewer.verify_evidence(operator, task["task_id"], proof["evidence_id"],
                                               handoff["version"], "manual-inspection-1", "verified",
                                               "EXAMPLE ONLY: operator confirms fixture report")
        approval = reviewer.approve_review(operator, task["task_id"], inspection["version"],
                                           "approval-1", "EXAMPLE ONLY: acceptance criteria checked")
        print(json.dumps({
            "task_status": ws.get_task(operator, task["task_id"])["status"],
            "review_status": reviewer.get_review(operator, task["task_id"])["status"],
            "evidence_verification": "operator_attestation (simulation)",
            "approval_event_id": approval["event_id"],
            "live_agents_run": False,
            "actual_tests_executed_by_workspace": False,
        }, indent=2))


if __name__ == "__main__":
    main()
