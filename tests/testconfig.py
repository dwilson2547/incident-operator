"""Settings for tests: the service has no defaults in code, so tests build them here (values as in the chart's
values.yaml). Override any field by keyword."""
from incident_operator.app import UiSettings
from incident_operator.notify import SmtpConfig
from incident_operator.triage import Config

RERUN_MODELS = [{"name": "qwen35-9b", "label": "9B", "max_prompt_tokens": 48000},
                {"name": "gemma4-26b-a4b", "label": "Gemma", "max_prompt_tokens": 12000}]


def triage_config(**over) -> Config:
    return Config(**{
        "gpu_url": "http://gpu-scheduler.test", "gpu_token": "", "model": "qwen35-9b", "fallback": ["gemma4-26b-a4b"],
        "fallback_after_seconds": 60, "fallback_max_prompt_tokens": 12000, "deadline_seconds": 900,
        "max_new_tokens": 1024, "investigate_max_new_tokens": 1536, "primer_path": "",
        "repeat_window_seconds": 21600, "max_per_hour": 12, "skip_resolved_after_seconds": 3600,
        "rerun_models": RERUN_MODELS, **over})


def smtp_config(**over) -> SmtpConfig:
    return SmtpConfig(**{"host": "", "port": 587, "starttls": True, "username": "", "password": "", "sender": "",
                         "to": [], "public_url": "", **over})


def ui_settings(**over) -> UiSettings:
    """No sign-in and no anonymous decisions: the open LAN page."""
    return UiSettings(**{"public_url": "http://io.local", "oidc_issuer": "", "oidc_client_id": "",
                         "oidc_client_secret": "", "rerun_models": RERUN_MODELS, **over})
