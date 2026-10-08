"""Versioned SQLite migrations, run under exclusive workspace ownership."""
from alembic import context
from sqlalchemy import create_engine
from app.config import Settings
from app.utils.ownership import exclusive_workspace


def run(connection):
    context.configure(connection=connection, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("These SQLite upgrades require an online connection")
connection = context.config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    settings = Settings()
    with exclusive_workspace(settings):
        engine = create_engine(settings.database_url)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.commit()
                run(connection)
        finally:
            engine.dispose()
