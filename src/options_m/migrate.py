"""Schema migration.

There is one schema file and it is idempotent, so "migrating" means executing
it. That is deliberate: a six-day project does not need a migration framework,
and ``CREATE TABLE IF NOT EXISTS`` is easier to reason about at 3am than a
half-applied revision chain.
"""

from __future__ import annotations

import logging
from importlib import resources

from options_m.db import Database

logger = logging.getLogger(__name__)

SCHEMA_RESOURCE = "schema.sql"


def read_schema() -> str:
    """Load the DDL. Uses importlib.resources so it works from the wheel."""
    return resources.files("options_m").joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")


async def apply(db: Database) -> None:
    """Apply the schema. No-op when the database is not configured."""
    if not db.is_enabled:
        logger.info("skipping migrations; no database configured")
        return

    schema = read_schema()
    async with db.connection() as conn:
        # psycopg opens a transaction implicitly; the whole file applies or none
        # of it does.
        async with conn.cursor() as cur:
            await cur.execute(schema)
        await conn.commit()
    logger.info("schema applied")
