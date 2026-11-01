"""M2.1 controlled demonstration; does not run any AI model or external service."""
import json
import tempfile
from pathlib import Path

from agent_workspace import Workspace


with tempfile.TemporaryDirectory() as tmp:
    ws = Workspace(Path(tmp) / 'workspace.db')
    setup = ws.bootstrap('demo', tmp)
    operator = ws.open_session(setup['operator_credential'])['session_token']
    claude = ws.enroll_agent(operator, 'claude', 'claude')
    codex = ws.enroll_agent(operator, 'codex', 'codex')
    a = ws.open_session(claude['credential'])['session_token']
    b = ws.open_session(codex['credential'])['session_token']
    task = ws.create_task(operator, 'Define persistence rule', 'Record rationale', 'task-1')
    proposal = ws.propose_decision(
        a, 'Maintain a single local coordination database',
        'The MVP needs atomic transactions, not distributed consensus.',
        [{'kind': 'task', 'ref': task['task_id']}], 'decision-1'
    )
    before = ws.knowledge_brief(b)
    reviewed = ws.decide_decision(operator, proposal['decision_id'], 0,
                                  'accepted', 'Checked against M0 product scope', 'review-1')
    after = ws.knowledge_brief(b)
    full = ws.get_decision(b, proposal['decision_id'])
    print(json.dumps({
        'proposal_status': proposal['status'],
        'visible_to_other_agent_before_approval': len(before['decisions']),
        'review_status': reviewed['status'],
        'visible_to_other_agent_after_approval': len(after['decisions']),
        'full_decision_cites_task': full['source_refs'] == [{'kind': 'task', 'ref': task['task_id']}],
        'external_providers_contacted': False
    }, indent=2))
