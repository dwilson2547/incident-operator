"""`python -m incident_operator`: the webhook (8081), the UI (8080) and the triage loop, in one process.

Env: DATABASE_URL; GPU_SCHEDULER_URL, GPU_SCHEDULER_TOKEN, TRIAGE_MODEL, TRIAGE_FALLBACK,
TRIAGE_FALLBACK_AFTER_SECONDS (triage.Config); SMTP_* and PUBLIC_URL (notify.SmtpConfig); PRIMER_PATH;
UI_PORT, WEBHOOK_PORT (8080, 8081); KUBE_API_URL for local runs (`kubectl proxy`); EVIDENCE=off to skip the Kubernetes reads;
RUNBOOKS_DIR, OPERATIONS_DIR (/etc/incident-operator/{runbooks,operations}); PROMETHEUS_URL; OIDC_ISSUER,
OIDC_CLIENT_ID, OIDC_CLIENT_SECRET for sign-in (app.py).

`python -m incident_operator executor` runs fixes approved in the UI (executor.py), as its own Deployment.

`python -m incident_operator replay ...` re-runs stored incidents against another model or edited runbooks (replay.py).
"""
import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading

import uvicorn

from . import db
from .app import create_ui_app, create_webhook_app
from .evidence import K8s
from .library import Library
from .notify import Notifier, SmtpConfig
from .triage import Config, Engine, GpuScheduler


class _Server(uvicorn.Server):
    """Two servers share one process, so signals are handled once, by main(), not by each server (each would replace
    the other's handler and only one would stop)."""

    def install_signal_handlers(self) -> None:          # uvicorn < 0.29
        pass

    @contextlib.contextmanager
    def capture_signals(self):                          # uvicorn >= 0.29
        yield


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    url = os.environ["DATABASE_URL"]
    conn = db.connect(url)
    db.migrate(conn)

    cfg = Config.from_env()
    smtp = SmtpConfig.from_env()
    logging.info("triage: %s, fallback %s after %ss; email %s", cfg.model, cfg.fallback, cfg.fallback_after_seconds,
                 f"to {', '.join(smtp.to)}" if smtp.usable else "off")
    # In the cluster: the service account. Locally: KUBE_API_URL, e.g. `kubectl proxy` at http://127.0.0.1:8001.
    k8s = (None if os.environ.get("EVIDENCE", "on") == "off"
           else K8s(base=os.environ["KUBE_API_URL"]) if os.environ.get("KUBE_API_URL") else K8s())
    library = Library(os.environ.get("RUNBOOKS_DIR", "/etc/incident-operator/runbooks"),
                      os.environ.get("OPERATIONS_DIR", "/etc/incident-operator/operations"), store=db.LastGood(conn))
    library.refresh()
    engine = Engine(conn, cfg, k8s, GpuScheduler(cfg), Notifier(smtp) if smtp.usable else None, library=library)
    threading.Thread(target=engine.run_forever, name="triage", daemon=True).start()

    ui_port, hook_port = int(os.environ.get("UI_PORT", 8080)), int(os.environ.get("WEBHOOK_PORT", 8081))
    servers = [_Server(uvicorn.Config(create_ui_app(url, library), host="0.0.0.0", port=ui_port, log_level="warning")),
               _Server(uvicorn.Config(create_webhook_app(url, engine), host="0.0.0.0", port=hook_port, log_level="warning"))]

    def stop(*_):
        engine.stopping = True
        for s in servers:
            s.should_exit = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    async def serve():
        await asyncio.gather(*(s.serve() for s in servers))
    asyncio.run(serve())


if __name__ == "__main__":
    if sys.argv[1:2] == ["replay"]:
        from .replay import main as replay
        sys.exit(replay(sys.argv[2:]))
    if sys.argv[1:2] == ["executor"]:
        from .executor import main as executor
        sys.exit(executor())
    main()
