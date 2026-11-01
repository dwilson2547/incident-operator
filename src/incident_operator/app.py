"""Two HTTP apps in one process (design §13):

- webhook (port 8081, Service only, never behind the ingress): `POST /alertmanager`, Alertmanager's webhook receiver.
  Kept off the UI's port so the LAN ingress can't be used to inject alerts.
- ui (port 8080, LAN ingress): the incident list and detail page, and its read-only JSON.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from importlib.resources import files

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import __version__, db

log = logging.getLogger("incident_operator.app")


def _pool_app(database_url: str, title: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = ConnectionPool(database_url, kwargs={"row_factory": dict_row, "autocommit": True},
                                        open=True, max_size=4)
        yield
        app.state.pool.close()
    app = FastAPI(title=title, version=__version__, lifespan=lifespan)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}
    return app


def _conn(request: Request):
    with request.app.state.pool.connection() as c:
        yield c


def create_webhook_app(database_url: str) -> FastAPI:
    app = _pool_app(database_url, "incident-operator webhook")

    @app.post("/alertmanager")
    async def alertmanager(request: Request, c=Depends(_conn)):
        """Alertmanager webhook (payload version 4): one incident per firing fingerprint; resolves close it."""
        body = await request.json()
        opened = touched = resolved = 0
        for alert in body.get("alerts", []):
            if not alert.get("fingerprint"):
                continue
            if alert.get("status") == "resolved":
                resolved += db.record_resolved(c, alert) is not None
            else:
                _, created = db.record_firing(c, alert)
                opened += created
                touched += not created
        if opened or resolved:
            log.info("alertmanager: %d new incidents, %d re-sent, %d resolved", opened, touched, resolved)
        return {"opened": opened, "updated": touched, "resolved": resolved}
    return app


def create_ui_app(database_url: str, library=None) -> FastAPI:
    app = _pool_app(database_url, "incident-operator")
    page = files("incident_operator").joinpath("static/index.html").read_text()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return page

    @app.get("/api/incidents")
    def incidents(c=Depends(_conn)):
        return {"incidents": db.list_incidents(c), "needs_runbook": db.needs_runbook(c)}

    @app.get("/api/incidents/{incident_id}")
    def incident(incident_id: int, c=Depends(_conn)):
        inc = db.get_incident(c, incident_id)
        if inc is None:
            raise HTTPException(404, "no such incident")
        return inc

    @app.get("/api/runbooks")
    def runbooks(c=Depends(_conn)):
        lib = library.describe() if library else {"runbooks": [], "operations": [], "errors": []}
        used = {r["runbook"]: r for r in db.runbook_usage(c)}
        for r in lib["runbooks"]:
            u = used.get(r["name"]) or {}
            r["incidents"], r["last_matched"] = u.get("incidents", 0), u.get("last_matched")
        return lib
    return app
