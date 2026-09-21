"""Which migration the database is on, versus which one this code expects.

A schema that lags the models is not a subtle failure — it is a `no such column`
on the first query that touches the new column, raised from deep inside the ORM's
eager-loading machinery and surfaced as a 500. Nothing before that point notices:
the process starts, `/healthz` is 200, and `/readyz` used to answer "ok" because
`SELECT 1` succeeds on a database that is missing every recent migration.

So the revision is checked explicitly. The *expected* revision is read from the
migration scripts (cached — it is a filesystem walk), the *actual* one from the
single-row `alembic_version` table.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.logging import get_logger

logger = get_logger(__name__)

#: `backend/alembic.ini` sits beside the `app` package, so the head can be
#: resolved without depending on the process's working directory.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def expected_revision() -> str | None:
    """The migration this build expects, or ``None`` if it cannot be determined.

    ``None`` deliberately means "unknown", not "mismatch": an image that ships
    without its migration scripts (or one where Alembic is absent) cannot know
    what the database should be on, and failing readiness for that would break a
    deployment that manages migrations elsewhere. Only a *known* head that
    differs from the database is treated as a fault.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(_BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
        head = ScriptDirectory.from_config(config).get_current_head()
    except Exception as exc:  # pragma: no cover - depends on packaging
        logger.warning("schema_head_unavailable", error=str(exc))
        return None

    return head


async def current_revision(connection: AsyncConnection) -> str | None:
    """The revision the database is actually on.

    A missing ``alembic_version`` table is reported as ``None`` — an unstamped
    database has not run any migration, which is exactly the state worth telling
    the operator about.
    """
    try:
        found = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:
        return None
    return str(found) if found is not None else None


async def check_schema(connection: AsyncConnection) -> str:
    """Return a human-readable readiness verdict for the schema.

    The message names the fix, because the whole point is that an operator who
    forgot one command should be told which command.
    """
    expected = expected_revision()
    actual = await current_revision(connection)

    if expected is None:
        return f"unknown (database at {actual or 'unversioned'})"

    if actual == expected:
        return "ok"

    return (
        f"error: database at {actual or 'unversioned'}, "
        f"code expects {expected}; run `alembic upgrade head`"
    )
