from __future__ import annotations

import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from url_crawler_service import orm  # noqa: F401  imported so both tables register on Base
from url_crawler_service.db import Base
from url_crawler_service.settings import Settings

config = context.config
if config.config_file_name is not None:
    # Leave loggers created before this call alone; alembic also runs in-process from the tests.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def do_run_migrations(connection: sa.Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(Settings.from_env().database_url, poolclass=sa.pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


asyncio.run(run_migrations_online())
