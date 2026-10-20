"""Add shared resource and configured-effort observations to native Claude Code.

Native installation, commands, sessions, conversion and errors remain inherited.
"""

from harbor.agents.installed.claude_code import ClaudeCode
from anonbench1_adapters.standalone_observations import StandaloneObservations


class ObservedClaudeCode(StandaloneObservations, ClaudeCode):
    pass
