"""Secret-redacted MCP preflight; creates and closes an ephemeral session.

Does not mutate tasks, messages, Brain, Memory or contexts. Not a real client test.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

EXPECTED_TOOLS = frozenset({
    'workspace_identity', 'task_list', 'task_get', 'task_claim', 'task_renew',
    'task_release', 'mail_inbox', 'mail_send', 'brain_brief', 'memory_search',
    'memory_record', 'context_publish', 'context_handoff', 'context_inbox',
    'context_get', 'context_changes',
})


def _broker_alive(port: int) -> bool:
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        conn.request('GET', '/v1/health', headers={'Host': f'127.0.0.1:{port}'})
        reply = conn.getresponse()
        return reply.status == 200 and reply.read(4096).startswith(b'{"ok":true')
    except (OSError, TimeoutError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _rpc(identifier: int, method: str, params: dict | None = None) -> str:
    item = {'jsonrpc': '2.0', 'id': identifier, 'method': method}
    if params is not None:
        item['params'] = params
    return json.dumps(item, separators=(',', ':')) + '\n'


def _wire_probe(port: int, credential: str, root: Path) -> dict[str, bool]:
    """Spawn real stdio transport and verify only identity/read-only operations."""
    checks = {'handshake': False, 'tool_inventory': False, 'agent_identity': False}
    if not credential:
        return checks
    env = os.environ.copy()
    env.update({'PYTHONPATH': str(root / 'src'), 'AW_BROKER_PORT': str(port),
                'AW_AGENT_CREDENTIAL': credential})
    payload = ''.join([
        _rpc(1, 'initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'local-preflight', 'version': '1.0'}}),
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
        _rpc(2, 'tools/list'),
        _rpc(3, 'tools/call', {'name':'workspace_identity','arguments':{}}),
    ])
    try:
        process = subprocess.run([sys.executable, '-m', 'agent_workspace.mcp_stdio'],
            input=payload, text=True, encoding='utf-8', capture_output=True,
            timeout=12, env=env, cwd=root, check=False)
        # Do not emit raw stdout/stderr. They may contain sensitive data from a
        # future adapter implementation; parse and disclose only booleans.
        if process.returncode:
            return checks
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        by_id = {reply.get('id'): reply for reply in replies if type(reply) is dict}
        init = by_id.get(1, {}).get('result', {})
        checks['handshake'] = init.get('protocolVersion') == '2025-11-25'
        inventory = by_id.get(2, {}).get('result', {}).get('tools', [])
        checks['tool_inventory'] = (
            type(inventory) is list and all(type(v) is dict for v in inventory)
            and {v.get('name') for v in inventory} == EXPECTED_TOOLS
            and len(inventory) == len(EXPECTED_TOOLS))
        result = by_id.get(3, {}).get('result', {})
        if not result.get('isError') and result.get('content'):
            identity = json.loads(result['content'][0]['text'])
            checks['agent_identity'] = identity.get('role') == 'agent' and bool(identity.get('agent_id'))
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
        pass
    return checks


def run(*, port: int, host: str, root: Path, credential: str) -> tuple[dict, bool]:
    host_found = True if host == 'none' else shutil.which(host) is not None
    broker_ok = _broker_alive(port)
    wire = _wire_probe(port, credential, root) if broker_ok else {
        'handshake':False, 'tool_inventory':False, 'agent_identity':False}
    passed = broker_ok and bool(credential) and all(wire.values()) and host_found
    return ({'gate':'M3.2-real-host-preflight',
             'broker_reachable':broker_ok,
             'agent_credential_supplied':bool(credential),
             'stdio':wire,
             'requested_host':host,
             'host_cli_found':host_found,
             'real_provider_verified':False,
             'status':'PREFLIGHT_PASSED_PROVIDER_TEST_PENDING' if passed else 'PREFLIGHT_FAILED'}, passed)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Read-only MCP preflight (never prints credentials)')
    p.add_argument('--host', choices=['none','codex','claude'], default='none')
    p.add_argument('--port', type=int, default=8765)
    args = p.parse_args(argv)
    if not 1 <= args.port <= 65535:
        p.error('port must be 1..65535')
    report, passed = run(port=args.port, host=args.host,
                        root=Path(__file__).resolve().parents[1],
                        credential=os.environ.get('AW_AGENT_CREDENTIAL',''))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
