from collections.abc import Callable
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .._json import dump_json_capped

# docs/gdap-role-map.yaml lives two levels up from this file's package root
# (src/exo_mcp/tools/gdap_roles.py -> repo root -> docs/).
_ROLE_MAP_PATH = Path(__file__).resolve().parents[3] / "docs" / "gdap-role-map.yaml"

CONNECTOR_NAME = "exchange_online_mcp"


def load_role_map() -> dict:
    """Load docs/gdap-role-map.yaml. Packaged alongside the source, not
    fetched over the network -- see PRD-21193's Tech design."""
    with _ROLE_MAP_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def register(mcp: FastMCP, client_factory: Callable) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    async def exo_list_gdap_roles() -> str:
        """List the Entra directory role(s) needed to perform each of this
        connector's tools, so the Agent Platform can show "which GDAP role a
        step needs" (PRD-21193) instead of hard-coding it.

        No parameters, no tenant context -- the mapping is static and
        versioned with this connector, not per-customer data. Calling it
        under any tenant context returns the same bytes every time.
        """
        return dump_json_capped({"connector": CONNECTOR_NAME, "tools": load_role_map()})
