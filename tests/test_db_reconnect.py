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
