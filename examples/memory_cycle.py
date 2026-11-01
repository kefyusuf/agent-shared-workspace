"""Local in-process shared-memory and delta simulation, not a real agent integration."""
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_workspace import Workspace


with TemporaryDirectory(prefix='agent-workspace-memory-') as temporary:
    ws = Workspace(Path(temporary) / 'workspace.db')
    bootstrap = ws.bootstrap('M2.2 local memory demo', temporary)
    op = ws.open_session(bootstrap['operator_credential'])['session_token']
    claude = ws.enroll_agent(op, 'claude-demo', 'claude')
    codex = ws.enroll_agent(op, 'codex-demo', 'codex')
    claude_token = ws.open_session(claude['credential'])['session_token']
    codex_token = ws.open_session(codex['credential'])['session_token']
    task = ws.create_task(op, 'Reproduce lease contention', 'Observe fencing behavior', 'demo-task')
    source = [{'kind':'task', 'ref':task['task_id']}]

    first = ws.record_memory(claude_token, 'finding', 'Lease contention finding',
                             'Two agent claims require a fencing token to avoid stale writes.',
                             source, 'demo-memory-1', task_id=task['task_id'])
    found = ws.search_memories(codex_token, query='fencing')
    assert [x['memory_id'] for x in found['memories']] == [first['memory_id']]
    full = ws.get_memory(codex_token, first['memory_id'])
    assert full['verification'] == 'agent_reported'
    assert ws.knowledge_brief(codex_token)['decisions'] == []

    second = ws.record_memory(claude_token, 'lesson', 'Lease contention resolution',
                              'Use short-lived leases with a monotonically increasing fence.',
                              source, 'demo-memory-2', supersedes_memory_id=first['memory_id'])
    assert ws.get_memory(codex_token, first['memory_id'])['is_stale']
    assert [x['memory_id'] for x in ws.search_memories(codex_token)['memories']] == [second['memory_id']]

    changes = ws.memory_changes(codex_token)
    assert [e['action'] for e in changes['changes']] == [
        'memory.recorded', 'memory.superseded', 'memory.recorded']
    print('Shared Memory: agent A records; agent B searches and reads; supersession tracked.')
    print('Effective memory count:', len(ws.search_memories(codex_token)['memories']))
    print('Incremental memory events:', len(changes['changes']))
    print('Brain decisions changed:', len(ws.knowledge_brief(codex_token)['decisions']))
