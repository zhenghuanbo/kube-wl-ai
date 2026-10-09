"""The audit trail has to say *why* a request failed.

``AuditMiddleware`` reads the reason from ``scope['state']`` once the response has
been produced, and nothing ever wrote it — so every row the middleware created
recorded ``status='error'`` with ``error_message`` NULL. Rejecting a "新建资源规格"
therefore left no trace of which field broke which rule; the only way to find out
was to reproduce the request by hand and read the response body.

These tests pin both halves of the fix: the reason is now recorded, and it is built
from the field path and the rule only — never from what was submitted.
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

SPECS = "/api/v1/resource-specs"
USERS = "/api/v1/users"


async def rejected_row(audit_rows: Any) -> Any:
    """The single audit row for the request the test just made."""
    rows = await audit_rows()
    rejected = [row for row in rows if row.status == "error"]
    assert len(rejected) == 1, f"expected exactly one rejected request, got {len(rejected)}"
    return rejected[0]


async def test_a_rejected_body_records_which_field_was_wrong(
    admin_client: AsyncClient, audit_rows: Any
) -> None:
    """A Chinese identifier is the mistake this form actually invites.

    The row has to name the field and the rule, because "error" on its own is
    indistinguishable from every other way the request could have failed.
    """
    response = await admin_client.post(
        SPECS,
        json={
            "name": "小规格",
            "cpuMilli": 2000,
            "memoryMb": 4096,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    assert response.status_code == 422

    row = await rejected_row(audit_rows)

    assert row.error_message is not None
    assert "name" in row.error_message, "the row must name the offending field"
    assert "pattern" in row.error_message, "the row must name the rule that was broken"


async def test_a_rejected_body_in_a_numbered_field_names_it_too(
    admin_client: AsyncClient, audit_rows: Any
) -> None:
    """Lower bounds are rejected by a different Pydantic rule than patterns are."""
    response = await admin_client.post(
        SPECS,
        json={"name": "tiny", "cpuMilli": 50, "memoryMb": 64, "gpuType": "none", "gpuCount": 0},
    )
    assert response.status_code == 422

    row = await rejected_row(audit_rows)

    assert row.error_message is not None
    assert "cpuMilli" in row.error_message
    assert "memoryMb" in row.error_message


async def test_a_rejected_value_never_reaches_the_audit_trail(
    admin_client: AsyncClient, audit_rows: Any
) -> None:
    """A too-short password is a 422 like any other.

    Pydantic reports the rejected value next to the rule, and that value is a
    plaintext credential. The audit table is only admin-readable, which is not the
    same as being a safe place for one — so the reason is assembled from ``loc``
    and ``msg``, and the submitted value is deliberately dropped.
    """
    secret = "hunter2"
    response = await admin_client.post(
        USERS, json={"username": "short-password", "password": secret}
    )
    assert response.status_code == 422
    # Pydantic really did hand the handler the value — the point is what we did
    # with it, so this is the control for the assertion below.
    assert secret in response.text

    row = await rejected_row(audit_rows)

    assert row.error_message is not None
    assert "password" in row.error_message
    assert secret not in row.error_message, "the rejected value must not be recorded"
