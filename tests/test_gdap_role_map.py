"""Three-way consistency check for the GDAP role map (PRD-21193): every tool
this server registers must have exactly one row in docs/gdap-role-map.yaml
and exactly one row in README.md's Tools table, and the role values in the
yaml and the README must agree. This is the "a tool without a row fails the
connector's own check" guard the ticket asks for (AC7) -- it is deliberately
strict with no exceptions, including for exo_list_gdap_roles itself (see its
own role-map row's note).
"""

import re
from pathlib import Path

import pytest
import yaml

from exo_mcp.config import Settings
from exo_mcp.server import create_mcp_server

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PATH = REPO_ROOT / "docs" / "gdap-role-map.yaml"
README_PATH = REPO_ROOT / "README.md"

# README rows look like "| `exo_tool(args)` | semantics... | Role<br>Role2 |"
_ROW_RE = re.compile(r"^\| `(?P<tool>exo_\w+)(?:\(.*?\))?` \| .* \| (?P<role_cell>[^|]*) \|\s*$")


def _readme_role_rows() -> dict[str, list[str] | None]:
    rows: dict[str, list[str] | None] = {}
    for line in README_PATH.read_text(encoding="utf-8").splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        cell = m.group("role_cell").strip()
        rows[m.group("tool")] = None if cell == "unknown" else cell.split("<br>")
    return rows


async def _registered_tool_names() -> set[str]:
    mcp = create_mcp_server(Settings())
    tools = await mcp.list_tools()
    return {t.name for t in tools}


def check_consistency(
    registered: set[str], role_map: dict, readme_rows: dict[str, list[str] | None]
) -> list[str]:
    """Pure function (no I/O) so AC7's "catches an unmapped tool" claim is
    itself testable without mutating the real server or README."""
    problems = []
    yaml_keys = set(role_map.keys())
    readme_keys = set(readme_rows.keys())
    for tool in sorted(registered - yaml_keys):
        problems.append(f"{tool}: registered but missing from docs/gdap-role-map.yaml")
    for tool in sorted(yaml_keys - registered):
        problems.append(f"{tool}: in docs/gdap-role-map.yaml but not a registered tool")
    for tool in sorted(registered - readme_keys):
        problems.append(f"{tool}: registered but missing a row in README.md's Tools table")
    for tool in sorted(readme_keys - registered):
        problems.append(f"{tool}: has a README row but is not a registered tool")
    for tool in sorted(registered & yaml_keys & readme_keys):
        yaml_roles = role_map[tool]["roles"]
        readme_roles = readme_rows[tool]
        if (yaml_roles or None) != (readme_roles or None):
            problems.append(
                f"{tool}: yaml roles {yaml_roles!r} != README GDAP Role column {readme_roles!r}"
            )
    return problems


@pytest.mark.asyncio
async def test_gdap_role_map_matches_registered_tools_and_readme():
    registered = await _registered_tool_names()
    role_map = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    readme_rows = _readme_role_rows()

    assert len(role_map) == 20, f"expected 20 role-map entries, found {len(role_map)}"
    problems = check_consistency(registered, role_map, readme_rows)
    assert not problems, "\n".join(problems)


def test_every_role_map_entry_has_a_non_empty_note():
    role_map = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    for tool, entry in role_map.items():
        assert entry.get("note"), f"{tool}: note must not be empty (AC1/TC3)"
        roles = entry.get("roles")
        assert roles is None or (isinstance(roles, list) and len(roles) >= 1), (
            f"{tool}: roles must be null or a non-empty list"
        )


def test_real_tools_map_to_their_documented_role():
    """TC2/TC-EXO-031: every real tool maps to its documented GDAP role.

    This generalizes the old "every real tool needs Exchange Administrator"
    assumption rather than just extending it (PRD-21619 Tech design, Change
    list item 5): the 18 pre-existing tools still each assert
    ["Exchange Administrator"] exactly, nothing about them changes;
    exo_add_tenant_block_list_entry (PRD-21619) is the one tool that needs a
    different role (Security Administrator / Organization Management), since
    New-/Get-TenantAllowBlockListItems are not Exchange Administrator
    actions. Every tool's role is still pinned exactly -- this is a
    correctness fix the new tool's own addition forces, not a weakening.
    """
    role_map = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    real_tools = {k: v for k, v in role_map.items() if k != "exo_list_gdap_roles"}
    assert len(real_tools) == 19

    exchange_admin_tools = {
        k: v for k, v in real_tools.items() if k != "exo_add_tenant_block_list_entry"
    }
    assert len(exchange_admin_tools) == 18
    for tool, entry in exchange_admin_tools.items():
        assert entry["roles"] == ["Exchange Administrator"], tool

    assert real_tools["exo_add_tenant_block_list_entry"]["roles"] == [
        "Security Administrator",
        "Organization Management",
    ]


def test_consistency_check_catches_an_unmapped_tool():
    """AC7's own non-vacuousness proof (TC9): a tool registered but absent
    from the role map must be flagged, without touching the real server."""
    registered = {"exo_get_mailbox", "exo_a_dummy_unmapped_tool"}
    role_map = {"exo_get_mailbox": {"roles": ["Exchange Administrator"], "note": "x"}}
    readme_rows = {"exo_get_mailbox": ["Exchange Administrator"]}

    problems = check_consistency(registered, role_map, readme_rows)
    assert any("exo_a_dummy_unmapped_tool" in p for p in problems)

    registered2 = {"exo_get_mailbox"}
    assert check_consistency(registered2, role_map, readme_rows) == []


@pytest.mark.asyncio
async def test_exo_list_gdap_roles_is_static_and_byte_identical():
    """AC2: no tenant context, byte-identical on every call."""
    from exo_mcp.tools.gdap_roles import load_role_map

    mcp = create_mcp_server(Settings())
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "exo_list_gdap_roles" in names

    first = load_role_map()
    second = load_role_map()
    assert first == second

    result1 = await mcp.call_tool("exo_list_gdap_roles", {})
    result2 = await mcp.call_tool("exo_list_gdap_roles", {})
    assert result1 == result2
