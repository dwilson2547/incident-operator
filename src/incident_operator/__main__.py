"""`python -m incident_operator`: the webhook (8081), the UI (8080) and the triage loop, in one process.

Env: DATABASE_URL; GPU_SCHEDULER_URL, GPU_SCHEDULER_TOKEN, TRIAGE_MODEL, TRIAGE_FALLBACK,
TRIAGE_FALLBACK_AFTER_SECONDS (triage.Config); SMTP_* and PUBLIC_URL (notify.SmtpConfig); PRIMER_PATH;
EVIDENCE=off to skip the Kubernetes reads (local runs without a cluster).
"""
import asyncio
import contextlib
import logging
import os
import signal
import threading

import uvicorn

from . import db
from .app import create_ui_app, create_webhook_app
from .evidence import K8s
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
    k8s = None if os.environ.get("EVIDENCE", "on") == "off" else K8s()
    engine = Engine(conn, cfg, k8s, GpuScheduler(cfg), Notifier(smtp) if smtp.usable else None)
    threading.Thread(target=engine.run_forever, name="triage", daemon=True).start()

    servers = [_Server(uvicorn.Config(create_ui_app(url), host="0.0.0.0", port=8080, log_level="warning")),
               _Server(uvicorn.Config(create_webhook_app(url), host="0.0.0.0", port=8081, log_level="warning"))]

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
    main()
