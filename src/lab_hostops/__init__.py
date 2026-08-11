"""lab-hostops — whitelisted host-operations MCP server for SDL2 lab machines.

Scope is deliberately narrow: inspect and (optionally) restart whitelisted
services, tail their logs, enumerate serial ports, and probe local
STATUS_SPEC ``/status`` endpoints. There is no shell tool and no path to any
device ``/control/*`` endpoint — hardware control belongs to the lab-skills
SDK (see ac-organic-lab/docs/AGENT_RULES.md §1.1).
"""

__version__ = "0.1.1"
