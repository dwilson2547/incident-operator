import psycopg
import pytest
from conftest import URL, needs_pg

from incident_operator import db


@needs_pg
def test_connection_reopens_after_the_server_drops_it():
    conn = db.connect(URL)
    pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    with psycopg.connect(URL) as other:                     # what a postgres restart does to every client
        other.execute("SELECT pg_terminate_backend(%s)", (pid,))

    with pytest.raises(psycopg.OperationalError):           # the pass that hits the drop fails, as before
        conn.execute("SELECT 1")
    row = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()   # the next one gets a new connection
    assert row["pid"] != pid
    conn.close()


@needs_pg
def test_pool_replaces_dropped_connections_before_handing_them_out(conn):
    from fastapi.testclient import TestClient

    from incident_operator.app import create_webhook_app

    with TestClient(create_webhook_app(URL)) as wh:
        wh.app.state.pool.wait()                                   # every pooled connection open
        assert wh.get("/metrics").status_code == 200
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend'")
        # Without a check on checkout, each request takes one dead connection and answers 500 until the pool is
        # emptied of them: the run of 500s on the UI after a postgres restart.
        assert [wh.get("/metrics").status_code for _ in range(6)] == [200] * 6
