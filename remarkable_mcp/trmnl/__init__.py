"""TRMNL e-ink display support (merged in from the standalone trmnl-mcp server).

The display mirrors what is waiting on the reMarkable (see workflows.live) and
agents can write to it directly with the trmnl_* tools. Configuration and the
shared push quota live where trmnl-mcp kept them: ~/.config/trmnl/config.json
and ~/.local/state/trmnl.
"""

__version__ = "0.2.0"  # trmnl client version (was trmnl-mcp 0.1.0)
