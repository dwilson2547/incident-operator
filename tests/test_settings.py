"""Configuration: no defaults in code, every missing setting reported at once, and the chart setting all of them."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from incident_operator.app import UiSettings
from incident_operator.notify import SmtpConfig
from incident_operator.ops import Runner
from incident_operator.settings import Env, MissingSettings
from incident_operator.triage import Config

CHART = Path(__file__).resolve().parents[1] / "helm" / "incident-operator"


def read_service(env: Env) -> None:
    """Everything __main__.main reads, in the same way."""
    for name in ("LOG_LEVEL", "DATABASE_URL", "RUNBOOKS_DIR", "OPERATIONS_DIR"):
        env.str(name)
    env.int("UI_PORT"), env.int("WEBHOOK_PORT")
    Config.from_env(env), SmtpConfig.from_env(env), UiSettings.from_env(env), Runner.from_env(None, env)


def read_executor(env: Env) -> None:
    """Everything executor.main reads."""
    for name in ("LOG_LEVEL", "DATABASE_URL", "OPERATIONS_DIR"):
        env.str(name)


def test_every_missing_setting_is_reported_together():
    env = Env({"TRIAGE_MODEL": "m"})
    read_service(env)
    with pytest.raises(MissingSettings) as e:
        env.check()
    assert "TRIAGE_MODEL" not in e.value.names
    assert {"DATABASE_URL", "GPU_SCHEDULER_URL", "SMTP_HOST", "PUBLIC_URL", "OIDC_ISSUER", "PROMETHEUS_URL"} <= set(
        e.value.names)


def test_empty_smtp_host_and_issuer_turn_email_and_sign_in_off_without_their_other_settings():
    env = Env({"SMTP_HOST": "", "PUBLIC_URL": "http://x", "OIDC_ISSUER": "", "TRIAGE_RERUN_MODELS": ""})
    smtp, ui = SmtpConfig.from_env(env), UiSettings.from_env(env)
    env.check()
    assert not smtp.usable and ui.oidc_issuer == "" and ui.rerun_models == []


def test_set_smtp_host_and_issuer_need_their_other_settings():
    env = Env({"SMTP_HOST": "smtp.x", "PUBLIC_URL": "http://x", "OIDC_ISSUER": "https://auth.x",
               "TRIAGE_RERUN_MODELS": "[]"})
    SmtpConfig.from_env(env), UiSettings.from_env(env)
    assert {"SMTP_PORT", "SMTP_STARTTLS", "SMTP_FROM", "SMTP_TO", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"} <= set(
        env.missing)


def test_values_are_parsed_strictly():
    with pytest.raises(ValueError, match="TRIAGE_MAX_PER_HOUR"):
        Env({"TRIAGE_MAX_PER_HOUR": ""}).int("TRIAGE_MAX_PER_HOUR")
    with pytest.raises(ValueError, match="SMTP_STARTTLS"):
        Env({"SMTP_STARTTLS": "yes"}).bool("SMTP_STARTTLS")
    assert Env({"TRIAGE_FALLBACK": " a, ,b "}).list("TRIAGE_FALLBACK") == ["a", "b"]


@pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")
@pytest.mark.parametrize("values", [[], ["-f", str(CHART / "values-prod.yaml")]], ids=["default", "prod"])
def test_chart_sets_every_setting_the_service_reads(values):
    rendered = subprocess.run(["helm", "template", "io", str(CHART), *values], check=True, capture_output=True,
                              text=True).stdout
    deployments = {d["metadata"]["name"]: d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "Deployment"}
    for name, read in (("incident-operator", read_service), ("incident-operator-executor", read_executor)):
        container = deployments[name]["spec"]["template"]["spec"]["containers"][0]
        env = Env({e["name"]: e.get("value", "(from a Secret)") for e in container["env"]})
        read(env)
        env.check()
