from __future__ import annotations

from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.service.conftest import alembic_env


def test_committed_migration_matches_the_orm(engine: AsyncEngine, database_url: str) -> None:
    with alembic_env(database_url) as config:
        command.check(config)


def test_migration_downgrades_and_upgrades_again(engine: AsyncEngine, database_url: str) -> None:
    with alembic_env(database_url) as config:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
