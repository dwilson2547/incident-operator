"""Alembic's entry point: run against the connection schema.upgrade hands over, or (the `alembic` CLI, alembic.ini)
an engine made from the URL in the config."""
from alembic import context
from sqlalchemy import create_engine

from incident_operator.models import Base, EnumText


def render_item(type_, obj, autogen_context):
    """Migrations store a state column as the text it is in the database, not as the enum type that reads it: a
    migration must keep meaning what it meant when it was written, whatever the enums become."""
    if type_ == "type" and isinstance(obj, EnumText):
        return "sa.Text()"
    return False


def run(connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, render_item=render_item,
                      compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


config = context.config
if (conn := config.attributes.get("connection")) is not None:
    run(conn)
else:
    with create_engine(config.get_main_option("sqlalchemy.url") or "").connect() as c:
        run(c)
