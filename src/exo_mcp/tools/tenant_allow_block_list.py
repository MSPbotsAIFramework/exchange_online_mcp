"""Tenant-wide Allow/Block List: add Block entries (PRD-21619).

New-/Get-TenantAllowBlockListItems need the Security Administrator (or
Organization Management) GDAP role -- not Exchange Administrator, which
every other tool in this connector uses (docs/gdap-role-map.yaml). This is
the first tool in this connector that is not an Exchange Administrator
action.

Idempotency follows this connector's established precondition-read-then-write
pattern (groups.py's _is_already_member, mailbox.py's exo_add_email_alias
dedup loop), not cmdlet error-text parsing: Microsoft does not document a
confirmed "already exists" error for New-TenantAllowBlockListItems, and a
duplicate add may not reliably error at all (docs/tickets/PRD-21619.md §
Tech design, Option analysis).
"""

from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import ExoClient, ExoError
from ._common import NO_CREDENTIALS, project
from .mailbox import _datetime_to_z, _parse_offset_datetime

# The three values the ticket names. The cmdlet also supports "IP", which is
# out of scope for this ticket (docs/tickets/PRD-21619.md § Product design,
# Open questions) -- an additive enum extension if a future ticket wants it.
_LIST_TYPES = ("Sender", "Url", "FileHash")

_ENTRY_FIELDS = {
    "Identity": "identity",
    "Value": "value",
    "ExpirationDate": "expiration_date",
    "Notes": "notes",
}

_ROLE_REQUIREMENT = (
    "Adding or reading tenant Allow/Block List entries requires the Security "
    "Administrator (or Organization Management) role, not Exchange "
    "Administrator"
)


def _role_error(exc: ExoError) -> str:
    """Remap a 401/403 to a message naming the role this tool actually needs.

    AC10: a generic "unauthorized" would wrongly suggest Exchange
    Administrator (this connector's documented default role) is sufficient.
    """
    if exc.code == "unauthorized":
        return ExoError(
            exc.status_code,
            f"{_ROLE_REQUIREMENT}. Upstream: {exc.message}",
            code="unauthorized",
            retryable=False,
        ).to_envelope()
    return exc.to_envelope()


def _project_entry(record: dict) -> dict:
    return project(record, _ENTRY_FIELDS)


def _match_existing(entry: str, existing: list[dict]) -> dict | None:
    """Case-insensitive match of `entry` against already-blocked records'
    projected `value` field -- same shape as groups.py's _is_already_member."""
    needle = entry.strip().lower()
    for record in existing:
        value = record.get("value")
        if isinstance(value, str) and value.strip().lower() == needle:
            return record
    return None


def register(mcp: FastMCP, client_factory: Callable[[], ExoClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True))
    async def exo_add_tenant_block_list_entry(
        list_type: Annotated[
            str,
            Field(description=f"One of {', '.join(_LIST_TYPES)}."),
        ],
        entries: Annotated[
            list[str],
            Field(
                description=(
                    "One or more values to block, matching list_type: sender "
                    "addresses, URLs or file hashes. Evaluated independently -- "
                    "a duplicate or malformed entry does not fail the others."
                )
            ),
        ],
        no_expiration: Annotated[
            bool,
            Field(
                description=(
                    "True for a block that never expires. Mutually exclusive "
                    "with expiration_date. Leave both unset to let Exchange "
                    "apply its own default expiry."
                )
            ),
        ] = False,
        expiration_date: Annotated[
            str | None,
            Field(
                description=(
                    "ISO-8601 date/time with a required timezone offset, e.g. "
                    "'2026-12-01T00:00:00+00:00'. Mutually exclusive with "
                    "no_expiration."
                )
            ),
        ] = None,
        notes: Annotated[
            str | None,
            Field(description="Free-text note stored with the new entries."),
        ] = None,
    ) -> str:
        """Add one or more Block entries to the tenant-wide Allow/Block List.

        Safe to call repeatedly: an entry already blocked for the same
        list_type is reported already_blocked, never as an error. Block
        only -- this tool does not manage Allow entries, edit an existing
        entry's expiration, or remove entries. Needs the Security
        Administrator (or Organization Management) GDAP role, unlike every
        other tool in this connector.
        """
        if not entries:
            return ExoError(
                400, "Provide at least one entry.", code="invalid_argument", retryable=False
            ).to_envelope()
        if list_type not in _LIST_TYPES:
            return ExoError(
                400,
                f"list_type must be one of {', '.join(_LIST_TYPES)}, got '{list_type}'",
                code="invalid_argument",
                retryable=False,
            ).to_envelope()
        if no_expiration and expiration_date is not None:
            return ExoError(
                400,
                "no_expiration and expiration_date are mutually exclusive.",
                code="invalid_argument",
                retryable=False,
            ).to_envelope()

        parsed_expiration = None
        if expiration_date is not None:
            try:
                parsed_expiration = _parse_offset_datetime(expiration_date, "expiration_date")
            except ExoError as exc:
                return exc.to_envelope()

        client = client_factory()
        if client is None:
            return NO_CREDENTIALS

        try:
            existing_records = await client.invoke(
                "Get-TenantAllowBlockListItems", {"ListType": list_type}
            )
        except ExoError as exc:
            return _role_error(exc)
        # Open question (docs/tickets/PRD-21619.md): not confirmed against a
        # live tenant whether ListType alone excludes Allow entries: filter
        # client-side on Action so an Allow entry with the same value is
        # never mistaken for an existing Block.
        existing = [
            _project_entry(record)
            for record in existing_records
            if record.get("Action") in (None, "Block")
        ]

        already: dict[str, dict] = {}
        to_write: list[str] = []
        for entry in entries:
            match = _match_existing(entry, existing)
            if match is not None:
                already[entry] = {"entry": entry, "result": "already_blocked", **match}
            else:
                to_write.append(entry)

        written: dict[str, dict] = {}
        if to_write:
            try:
                records = await client.invoke(
                    "New-TenantAllowBlockListItems",
                    {
                        "ListType": list_type,
                        "Entries": to_write,
                        "Block": True,
                        "NoExpiration": True if no_expiration else None,
                        "ExpirationDate": (
                            _datetime_to_z(parsed_expiration) if parsed_expiration else None
                        ),
                        "Notes": notes,
                    },
                )
            except ExoError as exc:
                return _role_error(exc)

            by_value: dict[str, dict] = {}
            for record in records:
                raw_value = record.get("Value")
                if isinstance(raw_value, str):
                    by_value[raw_value.strip().lower()] = record

            for entry in to_write:
                record = by_value.get(entry.strip().lower())
                if record is None:
                    # Upstream didn't echo this entry -- treat as written
                    # rather than silently dropping it from the response.
                    written[entry] = {"entry": entry, "result": "added", "value": entry}
                    continue
                error_message = record.get("Error")
                if error_message:
                    written[entry] = {
                        "entry": entry,
                        "result": "invalid",
                        "message": error_message,
                    }
                    continue
                written[entry] = {
                    "entry": entry,
                    "result": "added",
                    **_project_entry(record),
                }

        results = [already.get(entry) or written.get(entry) for entry in entries]
        accepted = any(r["result"] in ("added", "already_blocked") for r in results)

        return dump_json_capped(
            {"list_type": list_type, "results": results, "accepted": accepted}
        )
