"""Two simulated independent agent sessions, no model/API keys required."""
from pathlib import Path
import json
import tempfile

from agent_workspace import Workspace


def main():
    with tempfile.TemporaryDirectory(prefix="agent-workspace-pilot-") as temp:
        db_path = Path(temp) / "workspace.sqlite"
        workspace = Workspace(db_path)
        boot = workspace.bootstrap("Pilot Repository", temp)
        operator = workspace.open_session(boot["operator_credential"])["session_token"]
        codex_enrollment = workspace.enroll_agent(operator, "codex", "codex")
        claude_enrollment = workspace.enroll_agent(operator, "claude", "claude-code")
        codex = workspace.open_session(codex_enrollment["credential"])["session_token"]
        claude = workspace.open_session(claude_enrollment["credential"])["session_token"]

        task = workspace.create_task(operator, "Verify authentication", "Record token expiry behavior", "task-1")
        claim = workspace.claim_task(codex, task["task_id"], task["version"], "claim-1")
        message = workspace.send_message(codex, claude_enrollment["agent_id"],
                                         "Please review the expiry edge case.", "message-1")
        # A new Workspace object models another client/process restarting against the same store.
        other_client = Workspace(db_path)
        inbox = other_client.inbox(claude)["messages"]
        receipt = other_client.ack_message(claude, message["message_id"], "ack-1")
        result = {
            "task": workspace.get_task(operator, task["task_id"])["status"],
            "lease_fence": claim["fence_token"],
            "messages_seen_by_claude": len(inbox),
            "message_acknowledged": receipt["acknowledged_at"] is not None,
            "audit_events": len(workspace.audit_events(operator)["events"]),
            "provider_sessions_executed": False,
        }
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
