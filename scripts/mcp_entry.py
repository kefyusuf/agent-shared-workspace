"""Stable absolute-path MCP entrypoint for CLI client registrations.

Reads its agent credential only from its inherited process environment.
Never loads the SQLite file and never prints a credential.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from agent_workspace.mcp_stdio import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())
