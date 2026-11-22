"""Initialize the PostgreSQL schema for a Community Cloud deployment."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import ArgumentError

from repositories.database import Database, normalize_database_url


ROOT = Path(__file__).resolve().parents[1]
# Coordinate migrations if two app processes use the same database on restart.
MIGRATION_LOCK = 684019561


def neon_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("Configure DATABASE_URL in Streamlit Secrets")
    normalized = normalize_database_url(value)
    try:
        url = make_url(normalized)
    except ArgumentError:
        raise RuntimeError("DATABASE_URL must be a valid Neon connection URL") from None
    if (
        url.drivername != "postgresql+psycopg"
        or not (url.host or "").endswith(".neon.tech")
        or url.host.partition(".")[0].endswith("-pooler")
        or url.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}
        or not url.database
    ):
        raise RuntimeError("Use a direct Neon PostgreSQL connection with TLS")
    return normalized


def migrate_community_connection(connection: Connection) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


def upgrade_community_database(database: Database) -> None:
    with database.engine.begin() as connection:
        migrate_community_connection(connection)
