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


def upgrade(engine: Engine) -> None:
    """Run any migration this database hasn't had. The lock makes replicas starting together take turns."""
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('incident_operator.migrate'))"))
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def upgrade_url(url: str) -> None:
    engine = create_engine(engine_url(url))
    try:
        upgrade(engine)
    finally:
        engine.dispose()
