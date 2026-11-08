"""Two HTTP apps in one process (design §13):

- webhook (port 8081, Service only, never behind the ingress): `POST /alertmanager`, Alertmanager's webhook receiver,
  and `GET /metrics` for the operator's own alerts. Kept off the UI's port so the ingress can't be used to inject
  alerts.
- ui (port 8080, LAN ingress): the incident list and detail page, its JSON, approving or rejecting fixes, and
  re-running triage with another model.

Sign-in (oidc.py) is on when OIDC_ISSUER is set, with OIDC_CLIENT_ID, OIDC_CLIENT_SECRET and PUBLIC_URL: then every
page and API route needs a session, and who may sign in at all is the authentik group bound to the application.
Without it the pages are open (LAN only) and fixes can't be approved, unless ALLOW_ANONYMOUS_DECISIONS=1 (local runs
and tests only).
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from html import escape
from importlib.resources import files
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import __version__, db, investigate, oidc
from .triage import rerun_models

log = logging.getLogger("incident_operator.app")


def _pool_app(database_url: str, title: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # check: test each connection on checkout and replace it if postgres has dropped it (a restart, a terminated
        # session). Without it every pooled connection answers one request with a 500 before the pool discards it.
        app.state.pool = ConnectionPool(database_url, kwargs={"row_factory": dict_row, "autocommit": True},
                                        open=True, max_size=4, check=ConnectionPool.check_connection)
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


def create_webhook_app(database_url: str, engine=None) -> FastAPI:
    app = _pool_app(database_url, "incident-operator webhook")

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(c=Depends(_conn)):
        """Prometheus text format, read from the database on each scrape (one scrape a minute)."""
        h = db.health(c)
        lines = ["# TYPE incident_operator_incidents gauge"]
        lines += [f'incident_operator_incidents{{status="{k}"}} {v}' for k, v in sorted(h["incidents"].items())]
        lines += ["# TYPE incident_operator_fixes gauge"]
        lines += [f'incident_operator_fixes{{status="{k}"}} {v}' for k, v in sorted(h["fixes"].items())]
        lines += ["# HELP incident_operator_oldest_waiting_seconds Age of the oldest incident not yet triaged (0: none).",
                  "# TYPE incident_operator_oldest_waiting_seconds gauge",
                  f"incident_operator_oldest_waiting_seconds {float(h['oldest_waiting']):.0f}",
                  "# HELP incident_operator_oldest_unmailed_seconds Age of the oldest answered incident not yet mailed.",
                  "# TYPE incident_operator_oldest_unmailed_seconds gauge",
                  f"incident_operator_oldest_unmailed_seconds {float(h['oldest_unmailed']):.0f}"]
        if engine is not None and engine.last_pass is not None:
            lines += ["# HELP incident_operator_last_pass_timestamp_seconds When the triage loop last completed a pass.",
                      "# TYPE incident_operator_last_pass_timestamp_seconds gauge",
                      f"incident_operator_last_pass_timestamp_seconds {engine.last_pass:.0f}"]
        return "\n".join(lines) + "\n"

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


class Rerun(BaseModel):
    model: str


PUBLIC_PATHS = {"/healthz", oidc.FLOW_PATH + "/login", oidc.FLOW_PATH + "/callback"}
RETRY_COOKIE = "io_oidc_retry"


def _failed_page(next_path: str, reason: str) -> HTMLResponse:
    retry = f"{oidc.FLOW_PATH}/login?next={quote(next_path, safe='')}"
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Sign-in didn't complete</title>"
        "<body style='font:15px system-ui;background:#14161b;color:#e4e5e9;padding:40px'>"
        f"<h2>Sign-in didn't complete</h2><p style='color:#9096a3'>{escape(reason)}</p>"
        f"<p><a style='color:#7852ee' href='{retry}'>Try again</a></p>", status_code=401)


def _add_signin(app: FastAPI, env) -> callable:
    """gpu-scheduler's dashboard sign-in (gpu_scheduler/dashboard.py), unchanged in shape. Returns current_user."""
    issuer, secret = env.get("OIDC_ISSUER", ""), env.get("OIDC_CLIENT_SECRET", "")
    if issuer and not secret:
        raise RuntimeError("OIDC_ISSUER is set without OIDC_CLIENT_SECRET")
    public_url = env.get("PUBLIC_URL", "")
    secure = public_url.startswith("https://")

    def current_user(request: Request) -> dict | None:
        return oidc.verify_session(secret, request.cookies.get(oidc.SESSION_COOKIE)) if issuer else None

    if not issuer:
        return current_user
    provider = oidc.Provider(issuer, env.get("OIDC_CLIENT_ID", "incident-operator"), secret)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path not in PUBLIC_PATHS and current_user(request) is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "sign-in required"}, status_code=401)
            return RedirectResponse(f"{oidc.FLOW_PATH}/login?next={quote(request.url.path, safe='')}")
        return await call_next(request)

    @app.get(oidc.FLOW_PATH + "/login")
    def oidc_login(next: str = "/"):
        url, flow = oidc.begin(provider, public_url, next)
        r = RedirectResponse(url)
        r.set_cookie(oidc.FLOW_COOKIE, flow, max_age=oidc.FLOW_TTL_S, httponly=True, samesite="lax",
                     secure=secure, path=oidc.FLOW_PATH)
        return r

    @app.get(oidc.FLOW_PATH + "/callback")
    def oidc_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        # A failed callback recovers: continue if already signed in, else restart the sign-in once, then a page.
        flow = request.cookies.get(oidc.FLOW_COOKIE)
        next_path = oidc.flow_next(flow)
        try:
            if error:
                raise oidc.SignInRefused(f"authentik said: {error}")
            user, next_path = oidc.finish(provider, public_url, flow, code, state)
        except oidc.SignInRefused as exc:
            if current_user(request):
                return RedirectResponse(next_path)
            if not request.cookies.get(RETRY_COOKIE):
                r = RedirectResponse(f"{oidc.FLOW_PATH}/login?next={quote(next_path, safe='')}")
                r.set_cookie(RETRY_COOKIE, "1", max_age=120, httponly=True, samesite="lax", secure=secure,
                             path=oidc.FLOW_PATH)
                return r
            r = _failed_page(next_path, str(exc))
            r.delete_cookie(RETRY_COOKIE, path=oidc.FLOW_PATH)
            return r
        r = RedirectResponse(next_path)
        r.set_cookie(oidc.SESSION_COOKIE, oidc.sign_session(secret, user), max_age=oidc.SESSION_TTL_S,
                     httponly=True, samesite="lax", secure=secure)
        r.delete_cookie(oidc.FLOW_COOKIE, path=oidc.FLOW_PATH)
        r.delete_cookie(RETRY_COOKIE, path=oidc.FLOW_PATH)
        return r
    return current_user


def create_ui_app(database_url: str, library=None, env=None) -> FastAPI:
    env = os.environ if env is None else env
    app = _pool_app(database_url, "incident-operator")
    page = files("incident_operator").joinpath("static/index.html").read_text()
    current_user = _add_signin(app, env)
    anonymous_ok = env.get("ALLOW_ANONYMOUS_DECISIONS") == "1" and not env.get("OIDC_ISSUER")
    models = rerun_models(env)

    def decider(request: Request) -> str:
        """Who is approving or rejecting. Signed in, or the explicit local-run override; never the open LAN page."""
        user = current_user(request)
        if user:
            return user.get("name") or user["sub"]
        if anonymous_ok:
            return "anonymous (local)"
        raise HTTPException(403, "this needs sign-in (OIDC_ISSUER is not set)")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return page

    @app.get("/api/incidents")
    def incidents(c=Depends(_conn)):
        covered = (lambda labels: library.match(labels) is not None) if library else None
        return {"incidents": db.list_incidents(c), "needs_runbook": db.needs_runbook(c, covered)}

    @app.get("/api/incidents/{incident_id}")
    def incident(incident_id: int, c=Depends(_conn)):
        inc = db.get_incident(c, incident_id)
        if inc is None:
            raise HTTPException(404, "no such incident")
        inc["related"] = db.related(c, inc)
        for r in inc["related"]["nearby"]:
            r["subject"] = investigate.alert_subject(r["labels"])
        inc["rerun_models"] = models
        return inc

    def json_only(request: Request) -> None:          # a dependency: this has to run before the body is validated
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "send application/json")

    @app.post("/api/incidents/{incident_id}/rerun", dependencies=[Depends(json_only)])
    def rerun(incident_id: int, body: Rerun, request: Request, c=Depends(_conn)):
        # JSON only, signed in: the same rules as deciding on a fix. It spends GPU time and supersedes a waiting fix.
        who = decider(request)
        if body.model not in {m["name"] for m in models}:
            raise HTTPException(422, f"{body.model!r} isn't a model a re-run can use")
        why_not = db.begin_rerun(c, incident_id, body.model, who)
        if why_not:
            raise HTTPException(404 if why_not == "no such incident" else 409, why_not)
        log.info("incident %s: re-run with %s by %s", incident_id, body.model, who)
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        user = current_user(request)
        return {"user": user and (user.get("name") or user["sub"]), "can_decide": bool(user) or anonymous_ok}

    def decide(run_id: int, approve: bool, request: Request, c) -> dict:
        # JSON only: a cross-site form can't send it without a preflight, and the session cookie is SameSite=Lax.
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "send application/json")
        who = decider(request)
        if not db.decide_op_run(c, run_id, approve, who):
            raise HTTPException(409, "this fix isn't waiting for a decision (already decided, or suggest-only)")
        log.info("fix run %s %s by %s", run_id, "approved" if approve else "rejected", who)
        return db.get_op_run(c, run_id)

    @app.post("/api/fixes/{run_id}/approve")
    def approve(run_id: int, request: Request, c=Depends(_conn)):
        return decide(run_id, True, request, c)

    @app.post("/api/fixes/{run_id}/reject")
    def reject(run_id: int, request: Request, c=Depends(_conn)):
        return decide(run_id, False, request, c)

    @app.get("/api/runbooks")
    def runbooks(c=Depends(_conn)):
        lib = library.describe() if library else {"runbooks": [], "operations": [], "errors": []}
        used = {r["runbook"]: r for r in db.runbook_usage(c)}
        for r in lib["runbooks"]:
            u = used.get(r["name"]) or {}
            r["incidents"], r["last_matched"] = u.get("incidents", 0), u.get("last_matched")
        records = {r["operation"]: r for r in db.operation_records(c)}
        for o in lib["operations"]:
            if o["mutating"]:
                o["record"] = records.get(o["name"]) or {}
                o["runbooks"] = {r["name"]: r["fixes"][o["name"]] for r in lib["runbooks"] if o["name"] in r["fixes"]}
        return lib
    return app
