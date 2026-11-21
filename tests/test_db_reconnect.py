import psycopg
from conftest import URL, needs_pg, sql
from fastapi.testclient import TestClient

from incident_operator import db
from incident_operator.app import create_webhook_app

BACKEND_PID = "SELECT pg_backend_pid() AS pid"


@needs_pg
def test_store_reconnects_after_the_server_drops_its_connection():
    store = db.Store(URL, pool_size=1)
    pid = sql(store, BACKEND_PID)[0]["pid"]
    with psycopg.connect(URL) as other:                     # what a postgres restart does to every client
        other.execute("SELECT pg_terminate_backend(%s)", (pid,))
    # pre_ping finds the dead connection on checkout and opens a new one: no failed pass.
    assert sql(store, BACKEND_PID)[0]["pid"] != pid
    store.close()


@needs_pg
def test_pool_replaces_dropped_connections_before_handing_them_out(store):
    with TestClient(create_webhook_app(store)) as wh:
        assert [wh.get("/metrics").status_code for _ in range(4)] == [200] * 4     # several pooled connections open
        sql(store, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                   "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                   " AND backend_type = 'client backend'")
        # Without a check on checkout, each request takes one dead connection and answers 500 until the pool is
        # emptied of them: the run of 500s on the UI after a postgres restart.
        assert [wh.get("/metrics").status_code for _ in range(6)] == [200] * 6
