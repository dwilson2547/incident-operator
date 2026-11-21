# Database

The schema is defined once, in `src/incident_operator/models.py` (SQLAlchemy 2.0 models). `migrations/` holds the
Alembic revisions that build it; `schema.upgrade` runs them, under an advisory lock so replicas starting together
take turns.

## Changing the schema

1. Change the model.
2. Against a scratch database at the current head (the test Postgres from README: Develop):
   `.venv/bin/alembic upgrade head && .venv/bin/alembic revision --autogenerate -m "<what and why>"`.
   `alembic.ini` points at `io_alembic` on that container; create it first if it is missing.
3. Read the generated revision before keeping it. Autogenerate does not see CHECK constraint changes or partial-index
   conditions: write those by hand (`op.drop_constraint` + `op.create_check_constraint`).
4. `pytest tests/test_schema.py` builds the schema from the migrations and from the models and compares what Postgres
   built, CHECKs and index conditions included. It fails until the migration matches the model.

State columns (`status`, `session`, `trigger`, `notified`) are stored as text and read back as the enums in
`states.py`. A new enum value changes a CHECK constraint, so it needs a migration (step 3).

## Databases from before Alembic

Revision `0001` is the schema the startup DDL built. A database it built already has every table, so it is stamped at
`0001` rather than upgraded (upgrading would try to create the tables again):

    python -c 'import os; from alembic import command; from incident_operator import schema
    command.stamp(schema.alembic_config(os.environ["DATABASE_URL"]), "0001")'

Stamping only creates the `alembic_version` table. Production's schema was compared with `0001` by a schema dump diff
(2026-11-21) and was identical.
