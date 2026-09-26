"""TRMNL e-ink display support (merged in from the standalone trmnl-mcp server).

An independent tool set that shares only the MCP server with the reMarkable
side: nothing here imports reMarkable code, and nothing on the reMarkable side
drives the display. Agents write to it with the trmnl_* tools. Configuration
and the shared push quota live where trmnl-mcp kept them:
~/.config/trmnl/config.json and ~/.local/state/trmnl. See docs/trmnl.md.
"""

__version__ = "0.2.0"  # trmnl client version (was trmnl-mcp 0.1.0)
