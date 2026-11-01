"""Tests that touch the database run against a real Postgres when INCIDENT_OPERATOR_TEST_DATABASE_URL is set to its
URL. A throwaway one: `docker run -d --name io-pg -e POSTGRES_PASSWORD=<any> -p 127.0.0.1:55444:5432 postgres:17`, then
point the variable at user postgres, that password, 127.0.0.1:55444, database postgres (README: Develop).
Each test gets empty tables."""
import os

import pytest

from incident_operator import db

URL = os.environ.get("INCIDENT_OPERATOR_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not URL, reason="INCIDENT_OPERATOR_TEST_DATABASE_URL not set")


OURS = {"incidents", "evidence", "model_turns"}


@pytest.fixture
def conn():
    c = db.connect(URL)
    # The tests drop and recreate their tables: refuse a database that holds anything else, in case the URL reaches
    # some other local Postgres (it happened: another project's container answered on the same port and password).
    other = {r["table_name"] for r in c.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'").fetchall()} - OURS
    if other:
        c.close()
        pytest.exit(f"refusing to test against {URL}: it has other tables ({sorted(other)[:5]}…)", returncode=2)
    c.execute("DROP TABLE IF EXISTS evidence, model_turns, incidents")
    db.migrate(c)
    yield c
    c.close()


def alert(fp="fp1", status="firing", **labels):
    labels = {"alertname": "KubePodCrashLooping", "severity": "warning", **labels}
    return {"status": status, "fingerprint": fp, "labels": labels, "startsAt": "2026-11-01T10:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z", "generatorURL": "http://prometheus/graph?g0.expr=x",
            "annotations": {"summary": "Pod is crash looping.", "description": "Pod gyopart/x restarts."}}
