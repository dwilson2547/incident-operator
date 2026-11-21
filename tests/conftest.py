"""Tests that touch the database run against a real Postgres when INCIDENT_OPERATOR_TEST_DATABASE_URL is set to its
URL. A throwaway one: `docker run -d --name io-pg -e POSTGRES_PASSWORD=dev -p 127.0.0.1:55444:5432 postgres:17`, then
PGPASSWORD=dev and postgresql://postgres@127.0.0.1:55444/postgres (README: Develop).
Each test gets empty tables. `sql()` is for what a test does behind the Store's back: backdating a row, forcing a
state."""
import os

import pytest

from incident_operator import db

URL = os.environ.get("INCIDENT_OPERATOR_TEST_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(not URL, reason="INCIDENT_OPERATOR_TEST_DATABASE_URL not set")


OURS = {"incidents", "evidence", "model_turns", "op_runs", "library_last_good", "alembic_version"}


@pytest.fixture
def store():
    s = db.Store(URL)
    # The tests drop and recreate their tables: refuse a database that holds anything else, in case the URL reaches
    # some other local Postgres (it happened: another project's container answered on the same port and password).
    other = {r["table_name"] for r in sql(
        s, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")} - OURS
    if other:
        s.close()
        pytest.exit(f"refusing to test against {URL}: it has other tables ({sorted(other)[:5]}…)", returncode=2)
    sql(s, "DROP TABLE IF EXISTS alembic_version, library_last_good, op_runs, evidence, model_turns, incidents")
    s.migrate()
    yield s
    s.close()


def sql(store: db.Store, query: str, params: tuple = ()) -> list[dict]:
    """Raw SQL with psycopg's %s placeholders, in its own transaction; the rows as dicts."""
    with store.engine.begin() as c:
        r = c.exec_driver_sql(query, params)
        return [dict(m) for m in r.mappings()] if r.returns_rows else []


def alert(fp="fp1", status="firing", **labels):
    labels = {"alertname": "KubePodCrashLooping", "severity": "warning", **labels}
    return {"status": status, "fingerprint": fp, "labels": labels, "startsAt": "2026-11-01T10:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z", "generatorURL": "http://prometheus/graph?g0.expr=x",
            "annotations": {"summary": "Pod is crash looping.", "description": "Pod gyopart/x restarts."}}
