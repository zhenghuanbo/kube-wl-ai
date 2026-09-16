"""Datetime helpers.

Small module, single purpose: **a `DateTime(timezone=True)` column does not
guarantee an aware value.** PostgreSQL's `timestamptz` returns one; SQLite — which
the smoke environment and the test suite use — returns a naive `datetime` that is
nonetheless UTC. Comparing the two raises `TypeError: can't subtract
offset-naive and offset-aware datetimes`, and it does so at the *comparison*, far
from the column that lost the offset.

This is a bug that reappears every time somebody writes `if row.expires_at <
datetime.now(UTC)`, so the awkward part is worth centralising rather than
re-deriving: the value needs normalising at the point of comparison, not at the
point of storage, because the storage behaviour differs by backend and the
comparison does not.

Note that a naive value is *assumed* UTC. That is correct for every column in
this schema — they are all written from `utc_now()` or from a UTC-aware source —
but it is an assumption, and it is why `as_aware` is the only sanctioned way to
read one back.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """The current time, always timezone-aware."""
    return datetime.now(UTC)


def as_aware(value: datetime | None, *, default: datetime | None = None) -> datetime | None:
    """Return ``value`` as an aware UTC datetime.

    ``None`` passes through as ``None`` so callers can chain it onto optional
    columns without a guard. Pass ``default`` to substitute a value for ``None``
    instead — which is how a NULL expiry means "never" without a branch at every
    call site.
    """
    if value is None:
        return default
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_past(value: datetime | None) -> bool:
    """Whether ``value`` is in the past. ``None`` is never in the past.

    ``None`` meaning "no expiry" rather than "expired at the epoch" is the
    fail-open choice, and it is the right one here: an API token with no expiry
    is deliberately long-lived, and a NULL refresh-token expiry would be a bug
    that this helper would otherwise turn into a silent authentication bypass.
    The schema makes both columns non-null where it matters.
    """
    normalised = as_aware(value)
    return normalised is not None and normalised <= utc_now()
