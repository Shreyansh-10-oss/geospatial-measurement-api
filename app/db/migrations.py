"""Run the versioned schema upgrades; callers hold exclusive workspace ownership."""
from pathlib import Path
from alembic import command
from alembic.config import Config


def upgrade_database(engine):
    root = Path(__file__).resolve().parents[2]
    configuration = Config()
    configuration.set_main_option("script_location", str(root / "migrations"))
    with engine.connect() as connection:
        # SQLite batch migrations rebuild referenced tables. Disable FK enforcement
        # only on this migration connection; validate and restore it before serving.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        configuration.attributes["connection"] = connection
        try:
            command.upgrade(configuration, "head")
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("Database upgrade produced foreign-key violations")
            connection.commit()
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
