"""exo_add_tenant_block_list_entry (PRD-21619): add Block entries to the
tenant-wide Allow/Block List.

Mocked-upstream contract for New-TenantAllowBlockListItems used by these
tests: the "Value" array echoes one record per submitted entry, matched back
by its "Value" field; a record carrying an "Error" string means that one
entry was rejected while the rest still succeed (AC13). This shape is not
confirmed against a live tenant (docs/tickets/PRD-21619.md § Risks,
Risk 1) -- TC-EXO-029 tracks the real-tenant retest.
"""

import pytest
from conftest import tool_json

from exo_mcp.config import Settings
from exo_mcp.server import _credentials_var, create_mcp_server


@pytest.fixture
def mcp():
    return create_mcp_server(Settings(entra_login_base_url="https://login.example.test"))


async def call(mcp, credentials, args):
    token = _credentials_var.set(credentials)
    try:
        return tool_json(
            await mcp.call_tool("exo_add_tenant_block_list_entry", args)
        )
    finally:
        _credentials_var.reset(token)


@pytest.mark.asyncio
async def test_add_new_sender_entry_succeeds(mcp, upstream, credentials):
    upstream.responses["Get-TenantAllowBlockListItems"] = []
    upstream.responses["New-TenantAllowBlockListItems"] = [
        {
            "Identity": "contoso.com\\attacker@contoso.com",
            "Value": "attacker@contoso.com",
        }
    ]

    result = await call(
        mcp,
        credentials,
        {
            "list_type": "Sender",
            "entries": ["attacker@contoso.com"],
            "no_expiration": True,
        },
    )

    assert result["list_type"] == "Sender"
    assert result["accepted"] is True
    assert result["results"] == [
        {
            "entry": "attacker@contoso.com",
            "result": "added",
            "identity": "contoso.com\\attacker@contoso.com",
            "value": "attacker@contoso.com",
        }
    ]
    assert result["results"][0].get("expiration_date") is None

    write_call = next(
        r
        for r in upstream.invoke_requests
        if r["cmdlet"] == "New-TenantAllowBlockListItems"
    )
    assert write_call["parameters"] == {
        "ListType": "Sender",
        "Entries": ["attacker@contoso.com"],
        "Block": True,
        "NoExpiration": True,
    }


@pytest.mark.asyncio
async def test_add_existing_entry_is_idempotent(mcp, upstream, credentials):
    upstream.responses["Get-TenantAllowBlockListItems"] = [
        {
            "Identity": "contoso.com\\spam@evil.example",
            "Value": "spam@evil.example",
            "Action": "Block",
            "ExpirationDate": "2026-11-15T00:00:00Z",
        }
    ]

    result = await call(
        mcp,
        credentials,
        {
            "list_type": "Sender",
            "entries": ["spam@evil.example"],
            "no_expiration": True,
        },
    )

    assert result["results"] == [
        {
            "entry": "spam@evil.example",
            "result": "already_blocked",
            "identity": "contoso.com\\spam@evil.example",
            "value": "spam@evil.example",
            "expiration_date": "2026-11-15T00:00:00Z",
        }
    ]
    assert result["accepted"] is True
    assert not [
        r
        for r in upstream.invoke_requests
        if r["cmdlet"] == "New-TenantAllowBlockListItems"
    ]


@pytest.mark.asyncio
async def test_mixed_new_and_existing_entries_reported_independently(
    mcp, upstream, credentials
):
    upstream.responses["Get-TenantAllowBlockListItems"] = [
        {
            "Identity": "contoso.com\\spam@evil.example",
            "Value": "spam@evil.example",
            "Action": "Block",
        }
    ]
    upstream.responses["New-TenantAllowBlockListItems"] = [
        {"Identity": "contoso.com\\attacker@contoso.com", "Value": "attacker@contoso.com"}
    ]

    result = await call(
        mcp,
        credentials,
        {
            "list_type": "Sender",
            "entries": ["attacker@contoso.com", "spam@evil.example"],
            "no_expiration": True,
        },
    )

    assert [r["entry"] for r in result["results"]] == [
        "attacker@contoso.com",
        "spam@evil.example",
    ]
    assert result["results"][0]["result"] == "added"
    assert result["results"][1]["result"] == "already_blocked"

    write_call = next(
        r
        for r in upstream.invoke_requests
        if r["cmdlet"] == "New-TenantAllowBlockListItems"
    )
    assert write_call["parameters"]["Entries"] == ["attacker@contoso.com"]


@pytest.mark.asyncio
async def test_empty_entries_rejected_without_upstream_call(mcp, upstream, credentials):
    result = await call(mcp, credentials, {"list_type": "Sender", "entries": []})

    assert result["error"]["code"] == "invalid_argument"
    assert not upstream.invoke_requests


@pytest.mark.asyncio
async def test_unsupported_list_type_rejected(mcp, upstream, credentials):
    result = await call(
        mcp, credentials, {"list_type": "IP", "entries": ["1.2.3.4"]}
    )

    assert result["error"]["code"] == "invalid_argument"
    for name in ("Sender", "Url", "FileHash"):
        assert name in result["error"]["message"]
    assert not upstream.invoke_requests


@pytest.mark.asyncio
async def test_no_expiration_and_expiration_date_are_mutually_exclusive(
    mcp, upstream, credentials
):
    result = await call(
        mcp,
        credentials,
        {
            "list_type": "Sender",
            "entries": ["attacker@contoso.com"],
            "no_expiration": True,
            "expiration_date": "2026-12-01T00:00:00+00:00",
        },
    )

    assert result["error"]["code"] == "invalid_argument"
    assert not upstream.invoke_requests


@pytest.mark.asyncio
async def test_expiration_date_without_offset_rejected(mcp, upstream, credentials):
    result = await call(
        mcp,
        credentials,
        {
            "list_type": "Sender",
            "entries": ["attacker@contoso.com"],
            "expiration_date": "2026-12-01T00:00:00",
        },
    )

    assert result["error"]["code"] == "invalid_argument"
    assert not upstream.invoke_requests


@pytest.mark.asyncio
async def test_no_expiration_option_supplied_lets_upstream_default_apply(
    mcp, upstream, credentials
):
    upstream.responses["Get-TenantAllowBlockListItems"] = []
    upstream.responses["New-TenantAllowBlockListItems"] = [
        {
            "Identity": "contoso.com\\attacker@contoso.com",
            "Value": "attacker@contoso.com",
            "ExpirationDate": "2026-12-15T00:00:00Z",
        }
    ]

    result = await call(
        mcp,
        credentials,
        {"list_type": "Sender", "entries": ["attacker@contoso.com"]},
    )

    assert result["results"][0]["expiration_date"] == "2026-12-15T00:00:00Z"
    write_call = next(
        r
        for r in upstream.invoke_requests
        if r["cmdlet"] == "New-TenantAllowBlockListItems"
    )
    assert "NoExpiration" not in write_call["parameters"]
    assert "ExpirationDate" not in write_call["parameters"]


@pytest.mark.asyncio
async def test_missing_credentials_returns_not_configured(mcp):
    content = await mcp.call_tool(
        "exo_add_tenant_block_list_entry",
        {"list_type": "Sender", "entries": ["attacker@contoso.com"]},
    )
    result = tool_json(content)

    assert result["error"]["code"] == "not_configured"


@pytest.mark.asyncio
async def test_forbidden_response_names_required_role(mcp, upstream, credentials):
    upstream.responses["Get-TenantAllowBlockListItems"] = []
    upstream.cmdlet_errors["New-TenantAllowBlockListItems"] = (
        403,
        {"error": {"message": "Access denied"}},
    )

    result = await call(
        mcp,
        credentials,
        {
            "list_type": "Sender",
            "entries": ["attacker@contoso.com"],
            "no_expiration": True,
        },
    )

    assert result["error"]["code"] == "unauthorized"
    message = result["error"]["message"]
    assert "Security Administrator" in message
    assert "Organization Management" in message


@pytest.mark.asyncio
async def test_notes_forwarded_and_echoed(mcp, upstream, credentials):
    upstream.responses["Get-TenantAllowBlockListItems"] = []
    upstream.responses["New-TenantAllowBlockListItems"] = [
        {
            "Identity": "contoso.com\\attacker@contoso.com",
            "Value": "attacker@contoso.com",
            "Notes": "requested by SOC",
        }
    ]

    result = await call(
        mcp,
        credentials,
        {
            "list_type": "Sender",
            "entries": ["attacker@contoso.com"],
            "no_expiration": True,
            "notes": "requested by SOC",
        },
    )

    assert result["results"][0]["notes"] == "requested by SOC"
    write_call = next(
        r
        for r in upstream.invoke_requests
        if r["cmdlet"] == "New-TenantAllowBlockListItems"
    )
    assert write_call["parameters"]["Notes"] == "requested by SOC"


@pytest.mark.asyncio
async def test_url_list_type_accepted(mcp, upstream, credentials):
    upstream.responses["Get-TenantAllowBlockListItems"] = []
    upstream.responses["New-TenantAllowBlockListItems"] = [
        {"Identity": "contoso.com\\url1", "Value": "http://evil.example/"}
    ]

    result = await call(
        mcp,
        credentials,
        {
            "list_type": "Url",
            "entries": ["http://evil.example/"],
            "no_expiration": True,
        },
    )

    assert result["list_type"] == "Url"
    assert result["results"][0]["result"] == "added"
    get_call = next(
        r
        for r in upstream.invoke_requests
        if r["cmdlet"] == "Get-TenantAllowBlockListItems"
    )
    assert get_call["parameters"]["ListType"] == "Url"


@pytest.mark.asyncio
async def test_filehash_list_type_accepted(mcp, upstream, credentials):
    file_hash = "768a813668695ef2483b2bde7cf5d1b2db0423a0d3e63e498f3ab6f2eb13ea3"
    upstream.responses["Get-TenantAllowBlockListItems"] = []
    upstream.responses["New-TenantAllowBlockListItems"] = [
        {"Identity": "contoso.com\\hash1", "Value": file_hash}
    ]

    result = await call(
        mcp,
        credentials,
        {"list_type": "FileHash", "entries": [file_hash], "no_expiration": True},
    )

    assert result["list_type"] == "FileHash"
    assert result["results"][0]["result"] == "added"


@pytest.mark.asyncio
async def test_dedup_match_is_case_insensitive(mcp, upstream, credentials):
    upstream.responses["Get-TenantAllowBlockListItems"] = [
        {
            "Identity": "contoso.com\\x",
            "Value": "Attacker@Contoso.com",
            "Action": "Block",
        }
    ]

    result = await call(
        mcp,
        credentials,
        {
            "list_type": "Sender",
            "entries": ["attacker@contoso.com"],
            "no_expiration": True,
        },
    )

    assert result["results"][0]["result"] == "already_blocked"
    assert not [
        r
        for r in upstream.invoke_requests
        if r["cmdlet"] == "New-TenantAllowBlockListItems"
    ]


@pytest.mark.asyncio
async def test_one_malformed_entry_does_not_fail_the_others(mcp, upstream, credentials):
    upstream.responses["Get-TenantAllowBlockListItems"] = []
    upstream.responses["New-TenantAllowBlockListItems"] = [
        {"Identity": "contoso.com\\good", "Value": "attacker@contoso.com"},
        {"Value": "not-an-address", "Error": "The value 'not-an-address' is not valid."},
    ]

    result = await call(
        mcp,
        credentials,
        {
            "list_type": "Sender",
            "entries": ["attacker@contoso.com", "not-an-address"],
            "no_expiration": True,
        },
    )

    by_entry = {r["entry"]: r for r in result["results"]}
    assert by_entry["attacker@contoso.com"]["result"] == "added"
    assert by_entry["not-an-address"]["result"] == "invalid"
    assert "not valid" in by_entry["not-an-address"]["message"]
    assert result["accepted"] is True
