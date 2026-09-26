"""Configuration for trmnl-mcp.

Resolution order (first hit wins):
  1. Environment variables TRMNL_PLUGIN_UUID / TRMNL_IMAGE_PLUGIN_UUID
  2. JSON file at $TRMNL_CONFIG or ~/.config/trmnl/config.json

The plugin UUID is effectively a write credential for the display, so it
lives in the config file (mode 0600), never in the MCP server registration.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "trmnl" / "config.json"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "trmnl"


class ConfigError(Exception):
    """Raised when no usable plugin UUID can be found."""


@dataclass
class Config:
    plugin_uuid: str
    image_plugin_uuid: str | None = None
    api_base: str = "https://trmnl.com/api"
    rate_limit_per_hour: int = 12
    max_payload_bytes: int = 2048
    state_dir: Path = field(default_factory=lambda: DEFAULT_STATE_DIR)
    source: str = "env"


def config_path() -> Path:
    return Path(os.environ.get("TRMNL_CONFIG") or DEFAULT_CONFIG_PATH)


def load_config() -> Config:
    path = config_path()
    data: dict = {}
    source = "env"
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path} is not valid JSON: {e}") from e
        source = str(path)

    plugin_uuid = os.environ.get("TRMNL_PLUGIN_UUID") or data.get("plugin_uuid")
    if not plugin_uuid:
        raise ConfigError(
            "No TRMNL plugin UUID configured. Set TRMNL_PLUGIN_UUID or write "
            f'{{"plugin_uuid": "..."}} to {path}. The UUID is the last path '
            "segment of the private plugin's webhook URL "
            "(https://trmnl.com/api/custom_plugins/<uuid>)."
        )

    state_dir = Path(
        os.environ.get("TRMNL_STATE_DIR") or data.get("state_dir") or DEFAULT_STATE_DIR
    )

    return Config(
        plugin_uuid=plugin_uuid,
        image_plugin_uuid=os.environ.get("TRMNL_IMAGE_PLUGIN_UUID")
        or data.get("image_plugin_uuid"),
        api_base=data.get("api_base", "https://trmnl.com/api").rstrip("/"),
        rate_limit_per_hour=int(data.get("rate_limit_per_hour", 12)),
        max_payload_bytes=int(data.get("max_payload_bytes", 2048)),
        state_dir=state_dir,
        source=source,
    )
