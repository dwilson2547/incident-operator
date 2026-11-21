"""Bringing a database up to the schema in models.py, with Alembic (migrations/)."""
from __future__ import annotations

from importlib.resources import files

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text


def engine_url(url: str) -> str:
    """DATABASE_URL is a libpq URL (`postgresql://`); SQLAlchemy names the driver in it."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def alembic_config(url: str = "") -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(files("incident_operator").joinpath("migrations")))
    if url:
        cfg.set_main_option("sqlalchemy.url", engine_url(url).replace("%", "%%"))
    return cfg


class NotStamped(RuntimeError):
    """The tables are there but Alembic has no record of them: a database the startup DDL built, before Alembic."""


def upgrade(engine: Engine) -> None:
    """Run any migration this database hasn't had. The lock makes replicas starting together take turns.

    Refuses a database that has the tables and no `alembic_version`: upgrading it would try to create them again.
    It is stamped at 0001 by hand once (docs/database.md), after checking its schema is 0001's."""
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('incident_operator.migrate'))"))
        tables = set(conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")).scalars())
        if "incidents" in tables and "alembic_version" not in tables:
            raise NotStamped("this database predates Alembic: stamp it at 0001 first (docs/database.md)")
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def upgrade_url(url: str) -> None:
    engine = create_engine(engine_url(url))
    try:
        upgrade(engine)
    finally:
        engine.dispose()
