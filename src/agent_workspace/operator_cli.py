"""Trusted local operator commands, deliberately NOT exposed to MCP or the broker."""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path

from .core import Workspace, WorkspaceError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Agent Workspace trusted local operator CLI')
    sub = parser.add_subparsers(dest='command',required=True)
    enroll = sub.add_parser('enroll',help='Issue a new agent credential (shown once)')
    enroll.add_argument('--db',type=Path,required=True)
    enroll.add_argument('--name',required=True)
    enroll.add_argument('--provider',required=True)
    task = sub.add_parser('create-task',help='Create a Ready pilot task as the operator')
    task.add_argument('--db',type=Path,required=True)
    task.add_argument('--title',required=True)
    task.add_argument('--acceptance-criteria',required=True)
    task.add_argument('--request-key',required=True)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error('database does not exist; initialize the broker first')
    workspace = Workspace(args.db)
    try:
        credential = getpass.getpass('Local operator credential: ')
        opened = workspace.open_session(credential)
        operator = opened['session_token']
        try:
            if opened['role'] != 'operator':
                raise WorkspaceError('FORBIDDEN')
            if args.command == 'enroll':
                result = workspace.enroll_agent(operator,args.name,args.provider)
            else:
                result = workspace.create_task(operator,args.title,args.acceptance_criteria,args.request_key)
        finally:
            workspace.close_session(operator)
    except WorkspaceError as err:
        # Only stable error code, never credential or traceback.
        parser.exit(2, 'Operator command failed: '+err.code+'\n')
    print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
