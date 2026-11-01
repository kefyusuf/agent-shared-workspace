"""Demonstrate revocation and manual lease recovery with simulated sessions."""
import json
import tempfile
from pathlib import Path

from agent_workspace import Workspace, WorkspaceError


class Clock:
    now = 1_800_000_000.0

    def __call__(self):
        return self.now


with tempfile.TemporaryDirectory() as root:
    clock = Clock()
    ws = Workspace(Path(root) / "workspace.db", clock=clock)
    bootstrap = ws.bootstrap("demo", root)
    operator = ws.open_session(bootstrap["operator_credential"])["session_token"]
    first = ws.enroll_agent(operator, "codex", "codex")
    second = ws.enroll_agent(operator, "claude", "claude")
    codex = ws.open_session(first["credential"])["session_token"]
    claude = ws.open_session(second["credential"])["session_token"]

    task = ws.create_task(operator, "Implement endpoint", "Provide passing tests", "task")
    claim = ws.claim_task(codex, task["task_id"], 0, "first-claim")
    operator_revocation = ws.revoke_session(
        operator, ws.session_info(codex)["session_id"], "revoke", "operator cancelled job"
    )
    revoked_token_denied = False
    try:
        ws.list_tasks(codex)
    except WorkspaceError as error:
        revoked_token_denied = error.code == "UNAUTHENTICATED"
    second_claim = ws.claim_task(claude, task["task_id"], claim["version"] + 1, "second-claim")
    clock.now += 61
    recovered = ws.recover_abandoned_tasks(operator, "recover-expired-lease")
    print(json.dumps({
        "schema_version": ws.schema_version(),
        "revoked_session_denied": revoked_token_denied,
        "immediately_released_tasks": operator_revocation["released_task_count"],
        "new_fence_is_greater": second_claim["fence_token"] > claim["fence_token"],
        "expired_tasks_recovered": recovered["recovered_task_count"],
        "final_task_status": ws.get_task(operator, task["task_id"])["status"],
        "actual_provider_sessions_executed": False,
    }, indent=2))
