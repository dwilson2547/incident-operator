"""The migrations build exactly the schema models.py describes.

`alembic check` compares columns and indexes but not CHECK constraints or partial-index conditions, and those are where
the state enums live: a value added to an enum changes the model's CHECK, and only a migration changes the database's.
So this builds the schema both ways, each in its own Postgres schema, and compares what Postgres says it built."""
import pytest
from conftest import URL, needs_pg
from sqlalchemy import Engine, create_engine, text

from incident_operator import schema
from incident_operator.models import Base

FINGERPRINT = """
SELECT 'column' AS what, table_name || '.' || column_name AS name,
       concat_ws(' ', ordinal_position, data_type, udt_name, is_nullable, column_default) AS def
FROM information_schema.columns WHERE table_schema = current_schema() AND table_name <> 'alembic_version'
UNION ALL
SELECT 'constraint', conrelid::regclass::text || '.' || conname, pg_get_constraintdef(oid)
FROM pg_constraint WHERE connamespace = current_schema()::regnamespace
  AND conrelid::regclass::text NOT LIKE 'alembic_version%'
UNION ALL
SELECT 'index', indexname, indexdef FROM pg_indexes
WHERE schemaname = current_schema() AND tablename <> 'alembic_version'
"""


def built(engine: Engine, name: str) -> set[tuple[str, str, str]]:
    with engine.connect() as c:
        rows = c.execute(text(FINGERPRINT)).all()
    return {(w, n, d.replace(name + ".", "")) for w, n, d in rows}       # the schema name is in sequence and index defs


@pytest.fixture
def schemas():
    url = schema.engine_url(URL)
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    engines = {}
    for name in ("test_from_migrations", "test_from_models"):
        with admin.connect() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))
            c.execute(text(f"CREATE SCHEMA {name}"))
        engines[name] = create_engine(url, connect_args={"options": f"-csearch_path={name}"})
    yield engines
    for e in engines.values():
        e.dispose()
    with admin.connect() as c:
        for name in engines:
            c.execute(text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))
    admin.dispose()


@needs_pg
def test_migrations_build_the_models_schema(schemas):
    schema.upgrade(schemas["test_from_migrations"])
    Base.metadata.create_all(schemas["test_from_models"])
    migrated = built(schemas["test_from_migrations"], "test_from_migrations")
    modelled = built(schemas["test_from_models"], "test_from_models")
    assert len(migrated) > 50                                   # the query saw the tables at all
    assert sorted(modelled - migrated) == [], "the models changed with no migration: alembic revision --autogenerate"
    assert sorted(migrated - modelled) == []
