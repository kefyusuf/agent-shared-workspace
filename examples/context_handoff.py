"""Local M2.3 handoff demonstration. No real coding agents are started."""
import pathlib
import tempfile
from agent_workspace import Workspace, WorkspaceError

with tempfile.TemporaryDirectory() as temp:
    workspace = Workspace(pathlib.Path(temp) / 'workspace.sqlite')
    project = workspace.bootstrap('Context demo',temp)
    operator = workspace.open_session(project['operator_credential'])['session_token']
    claude = workspace.enroll_agent(operator,'claude-code','claude')
    codex = workspace.enroll_agent(operator,'codex','codex')
    author = workspace.open_session(claude['credential'])['session_token']
    recipient = workspace.open_session(codex['credential'])['session_token']
    task = workspace.create_task(operator,'Refresh token tests','Verify expiry and retry','demo-task')
    claim = workspace.claim_task(author,task['task_id'],0,'demo-claim')
    memory = workspace.record_memory(author,'finding','Refresh token expiry',
        'Expiry handling needs an additional negative test.',
        [{'kind':'task','ref':task['task_id']}],'demo-memory',task_id=task['task_id'])
    snapshot = workspace.publish_context(author,task['task_id'],claim['version'],
        claim['fence_token'],'Inspected refresh token expiry branch',
        'Write a negative test and verify HTTP 401',
        [{'kind':'task','ref':task['task_id']},{'kind':'memory','ref':memory['memory_id']}],
        'demo-snapshot',changed_paths=['src/auth/refresh.py'])
    try:
        workspace.get_context(recipient,snapshot['snapshot_id'])
    except WorkspaceError as error:
        assert error.code=='NOT_FOUND'
    else:
        raise AssertionError('Snapshot unexpectedly readable without a grant')
    workspace.send_handoff(author,snapshot['snapshot_id'],codex['agent_id'],
                           'Continue the expiry tests','demo-handoff')
    delivered = workspace.handoff_inbox(recipient)['handoffs']
    assert len(delivered)==1
    view = workspace.get_context(recipient,delivered[0]['snapshot_id'])
    assert view['changed_paths']==['src/auth/refresh.py']
    assert any(x['kind']=='memory' for x in view['source_refs'])
    workspace.release_task(author,task['task_id'],claim['version'],claim['fence_token'],'demo-release')
    workspace.claim_task(recipient,task['task_id'],claim['version']+1,'demo-reclaim')
    assert workspace.get_context(recipient,snapshot['snapshot_id'])['summary']==view['summary']
    print('PASS: private snapshot -> explicit handoff -> targeted retrieval -> new lease owner')
    print('Handoff count:',len(delivered),'Source refs:',len(view['source_refs']))
