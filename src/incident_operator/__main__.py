"""`python -m incident_operator`: the webhook, the UI and the triage loop, in one process.

All configuration is environment variables, set by the chart (helm/incident-operator/templates, from values.yaml) or by
start.sh locally; there are no defaults in code, and a missing one stops start-up (settings.py). Optional local-run
switches: KUBE_API_URL (`kubectl proxy` instead of the service account), EVIDENCE=off (no Kubernetes reads),
ALLOW_ANONYMOUS_DECISIONS=1 (approve fixes without sign-in).

`python -m incident_operator executor` runs fixes approved in the UI (executor.py), as its own Deployment.

`python -m incident_operator replay ...` re-runs stored incidents against another model or edited runbooks (replay.py).
"""
import asyncio
import contextlib
import logging
import signal
import sys
import threading

import uvicorn

from . import db
from .app import UiSettings, create_ui_app, create_webhook_app
from .k8s import K8s
from .library import Library
from .notify import Notifier, SmtpConfig
from .ops import Runner
from .settings import Env
from .triage import Config, Engine, GpuScheduler

log = logging.getLogger("incident_operator")


class _Server(uvicorn.Server):
    """Two servers share one process, so signals are handled once, by main(), not by each server (each would replace
    the other's handler and only one would stop)."""

    def install_signal_handlers(self) -> None:          # uvicorn < 0.29
        pass

    @contextlib.contextmanager
    def capture_signals(self):                          # uvicorn >= 0.29
        yield


def main() -> None:
    env = Env()
    log_level, url = env.str("LOG_LEVEL"), env.str("DATABASE_URL")
    runbooks_dir, operations_dir = env.str("RUNBOOKS_DIR"), env.str("OPERATIONS_DIR")
    ui_port, hook_port = env.int("UI_PORT"), env.int("WEBHOOK_PORT")
    cfg, smtp, ui = Config.from_env(env), SmtpConfig.from_env(env), UiSettings.from_env(env)
    # In the cluster: the service account. Locally: KUBE_API_URL, e.g. `kubectl proxy` at http://127.0.0.1:8001.
    k8s = None if env.optional("EVIDENCE") == "off" else K8s.from_env()
    runner = Runner.from_env(k8s, env)
    env.check()

    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("triage: %s, fallback %s after %ss; email %s", cfg.model, cfg.fallback, cfg.fallback_after_seconds,
             f"to {', '.join(smtp.to)}" if smtp.usable else "off")
    store = db.Store(url)                         # one pool for the engine thread and both servers
    store.migrate()
    library = Library(runbooks_dir, operations_dir, store=db.LastGood(store))
    library.refresh()
    engine = Engine(store, cfg, k8s, GpuScheduler(cfg), Notifier(smtp) if smtp.usable else None, runner,
                    library=library)
    threading.Thread(target=engine.run_forever, name="triage", daemon=True).start()

    servers = [_Server(uvicorn.Config(create_ui_app(store, ui, library), host="0.0.0.0", port=ui_port,
                                      log_level="warning")),
               _Server(uvicorn.Config(create_webhook_app(store, engine), host="0.0.0.0", port=hook_port,
                                      log_level="warning"))]

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
