"""Operator-only convenience launcher for the local Workspace prototype.

This is intentionally not an MCP tool or a production security boundary.
Secrets are displayed once in the trusted terminal at initial enrollment,
then supplied interactively to launch exactly one provider per process.
"""
from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
from contextlib import closing
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / '.local' / 'workspace.db'
SRC = ROOT / 'src'
MCP_ENTRY = ROOT / 'scripts' / 'mcp_entry.py'
sys.path.insert(0, str(SRC))

from agent_workspace.core import Workspace, WorkspaceError  # noqa: E402
from host_preflight import run as preflight_run  # noqa: E402


class SetupError(Exception):
    """Expected configuration or user-action error, without secret payloads."""


def _health(port: int) -> bool:
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        conn.request('GET', '/v1/health')
        res = conn.getresponse()
        return res.status == 200 and bool(json.loads(res.read(512)).get('ok'))
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _initialized(db: Path = DB) -> bool:
    if not db.is_file():
        return False
    try:
        with closing(sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True)) as con:
            return con.execute('SELECT EXISTS(SELECT 1 FROM projects)').fetchone()[0] == 1
    except sqlite3.Error:
        return False


def _operator_session(ws: Workspace) -> str:
    credential = getpass.getpass('Operator credential (not agent credential): ')
    session = ws.open_session(credential)
    if session['role'] != 'operator':
        ws.close_session(session['session_token'])
        raise SetupError('Operator role is required.')
    return session['session_token']


def _setup(db: Path = DB) -> None:
    if _initialized(db):
        print('Workspace already initialized. No credentials or agents were changed.')
        return
    if db.exists():
        raise SetupError('Database exists but project is not initialized; refusing to overwrite it.')
    db.parent.mkdir(parents=True, exist_ok=True)
    ws = Workspace(db)
    initial = ws.bootstrap('Agent Shared Workspace Pilot', ROOT)
    # IMPORTANT: generated credentials appear once in this trusted terminal only.
    # Do not redirect this output to logs, CI, an agent transcript, or GitHub.
    print('\nONE-TIME OPERATOR CREDENTIAL (store securely):', initial['operator_credential'], flush=True)
    token = ws.open_session(initial['operator_credential'])['session_token']
    try:
        for name in ('codex', 'claude'):
            agent = ws.enroll_agent(token, name, name)
            print(f'ONE-TIME {name.upper()} AGENT ID:', agent['agent_id'])
            print(f'ONE-TIME {name.upper()} AGENT CREDENTIAL:', agent['credential'], flush=True)
        task = ws.create_task(
            token,
            'MCP cross-agent handoff pilot',
            'Agent A publishes a sourced task snapshot; Agent B reads it and claims the released task',
            'workspace-setup-pilot-v1',
        )
        print('PILOT TASK ID:', task['task_id'])
    finally:
        ws.close_session(token)
    print('\nNext: workspace.ps1 connect; then workspace.ps1 broker in a separate terminal.')


def _enroll(provider: str, db: Path = DB) -> None:
    if not _initialized(db):
        raise SetupError('Run setup first.')
    ws = Workspace(db)
    token = _operator_session(ws)
    try:
        data = ws.enroll_agent(token, provider, provider)
        print('AGENT ID:', data['agent_id'])
        print('ONE-TIME AGENT CREDENTIAL (store securely):', data['credential'])
    finally:
        ws.close_session(token)


def _task(title: str, key: str, db: Path = DB) -> None:
    if not _initialized(db):
        raise SetupError('Run setup first.')
    ws = Workspace(db)
    token = _operator_session(ws)
    try:
        item = ws.create_task(token, title, 'Publish and hand off a sourced context snapshot', key)
        print('TASK ID:', item['task_id'], '| STATUS:', item['status'])
    finally:
        ws.close_session(token)


def _mcp_registered(provider: str) -> bool:
    """Check that a named MCP entry exists; this is NOT a live provider test."""
    executable = shutil.which(provider)
    if not executable:
        return False
    try:
        # Never print 'mcp get' output: old local configs may contain secrets.
        probe = subprocess.run(
            [executable, 'mcp', 'get', 'agent_workspace'],
            capture_output=True, text=True, errors='replace',
            timeout=5, check=False, cwd=ROOT,
        )
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _doctor(port: int, db: Path = DB) -> dict[str, bool | str]:
    result = {
        'python_3_11_plus': sys.version_info >= (3, 11),
        'workspace_initialized': _initialized(db),
        'broker_reachable': _health(port),
        'codex_installed': shutil.which('codex') is not None,
        'claude_installed': shutil.which('claude') is not None,
        'codex_mcp_registered': _mcp_registered('codex'),
        'claude_mcp_registered': _mcp_registered('claude'),
        'real_provider_verified': False,
    }
    print(json.dumps(result, indent=2))
    return result


def _connect_one(provider: str) -> None:
    cli = shutil.which(provider)
    if not cli:
        print(f'{provider}: CLI not installed; skipping registration.')
        return
    name = 'agent_workspace'
    check = subprocess.run([cli, 'mcp', 'get', name], capture_output=True, text=True,
                           errors='replace', check=False, cwd=ROOT)
    if check.returncode == 0:
        print(f'{provider}: {name} already registered; leaving existing configuration unchanged.')
        return
    # Registration contains only an absolute interpreter+script path; NEVER a secret.
    if provider == 'codex':
        cmd = [cli, 'mcp', 'add', name, '--', sys.executable, str(MCP_ENTRY)]
    else:
        cmd = [cli, 'mcp', 'add', '--scope', 'local', '--transport', 'stdio',
               name, '--', sys.executable, str(MCP_ENTRY)]
    if subprocess.run(cmd, cwd=ROOT, check=False).returncode != 0:
        raise SetupError(f'{provider} MCP registration failed; inspect its CLI help.')
    print(f'{provider}: registered {name} (credentials are NOT stored in config).')


def _connect(provider: str) -> None:
    providers = ('codex', 'claude') if provider == 'both' else (provider,)
    for p in providers:
        _connect_one(p)


def _run_agent(provider: str, port: int) -> int:
    if not shutil.which(provider):
        raise SetupError(f'{provider} CLI not found. Install it before running this command.')
    if not _health(port):
        raise SetupError('Broker is not running. In another terminal use: .\\workspace.ps1 broker')
    credential = getpass.getpass(f'{provider} AGENT credential (never operator credential): ')
    if not credential:
        raise SetupError('Agent credential cannot be empty.')
    report, passed = preflight_run(port=port, host=provider, root=ROOT, credential=credential)
    print(json.dumps(report, indent=2))
    if not passed:
        raise SetupError('MCP preflight failed; provider was NOT launched.')
    env = dict(os.environ)
    env.update({'AW_AGENT_CREDENTIAL': credential, 'AW_BROKER_PORT': str(port),
                'PYTHONPATH': str(SRC)})
    # Codex's MCP stdio bridge needs these variables explicitly forwarded.
    # CLI flags contain NAMES only, never their values. Claude receives inherited env.
    cmd = ([shutil.which(provider), '-c',
            'mcp_servers.agent_workspace.env_vars=["AW_AGENT_CREDENTIAL","AW_BROKER_PORT"]']
           if provider == 'codex' else [shutil.which(provider)])
    print(f'Launching {provider}. Preflight is not proof of provider compatibility.')
    return subprocess.run(cmd, env=env, cwd=ROOT, check=False).returncode


def _serve(port: int, db: Path = DB) -> int:
    if not _initialized(db):
        raise SetupError('Run setup first.')
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    return subprocess.run([sys.executable, '-m', 'agent_workspace.broker', 'serve',
                           '--db', str(db), '--port', str(port)],
                          cwd=ROOT, env=env, check=False).returncode


def _test() -> int:
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    return subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                          cwd=ROOT, env=env, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Single entry point for Agent Shared Workspace (Windows wrapper: workspace.ps1)')
    p.add_argument('--port', type=int, default=8765, help='Local broker port (default: 8765)')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('setup', help='One-time workspace + Codex/Claude identities + pilot task')
    sub.add_parser('doctor', help='Print non-secret local readiness report')
    sub.add_parser('test', help='Run all Python tests')
    sub.add_parser('broker', help='Start loopback-only broker in foreground (Ctrl+C to stop)')
    connect = sub.add_parser('connect', help='Register MCP with installed provider CLIs (no credentials persisted)')
    connect.add_argument('provider', choices=['both', 'codex', 'claude'], nargs='?', default='both')
    run = sub.add_parser('run', help='Preflight and launch a real provider with its own prompted credential')
    run.add_argument('provider', choices=['codex', 'claude'])
    enroll = sub.add_parser('enroll', help='Enroll an extra agent (trusted operator only)')
    enroll.add_argument('name')
    task = sub.add_parser('task', help='Add another pilot task (trusted operator only)')
    task.add_argument('--title', default='Agent handoff pilot')
    task.add_argument('--key', required=True, help='Unique idempotency key')
    args = p.parse_args(argv)
    if not 1 <= args.port <= 65535:
        p.error('port must be 1..65535')
    if sys.version_info < (3, 11):
        raise SetupError('Python 3.11+ required.')
    try:
        if args.command == 'setup':
            _setup()
        elif args.command == 'doctor':
            _doctor(args.port)
        elif args.command == 'test':
            return _test()
        elif args.command == 'broker':
            return _serve(args.port)
        elif args.command == 'connect':
            _connect(args.provider)
        elif args.command == 'run':
            return _run_agent(args.provider, args.port)
        elif args.command == 'enroll':
            _enroll(args.name)
        elif args.command == 'task':
            _task(args.title, args.key)
    except (SetupError, WorkspaceError) as e:
        # Do not echo user supplied credentials or opaque response payloads.
        print(f'Workspace command failed: {e.code if isinstance(e, WorkspaceError) else e}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
