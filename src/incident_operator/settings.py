"""Reading the service's configuration from the environment.

Every setting comes from the deployment (the chart's values, or start.sh locally); the code holds no fallback values.
A missing variable is an error, and all missing ones are reported together at start-up. An empty value is allowed, and
means "off" where a setting says so (SMTP_HOST, OIDC_ISSUER).

Only the local-run switches are optional, read with `optional()`: KUBE_API_URL, EVIDENCE, ALLOW_ANONYMOUS_DECISIONS.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any


class MissingSettings(RuntimeError):
    def __init__(self, names: list[str]):
        super().__init__("missing environment variables: " + ", ".join(names))
        self.names = names


class Env:
    """Typed reads that collect what is missing. Read every setting, then call `check()` once, so one start-up error
    names everything that is missing:

        env = Env()
        cfg, smtp = Config.from_env(env), SmtpConfig.from_env(env)
        env.check()
    """

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._env = os.environ if environ is None else environ
        self.missing: list[str] = []

    def str(self, name: str) -> str:
        value = self._env.get(name)
        if value is None:
            self.missing.append(name)
            return ""
        return value

    def int(self, name: str) -> int:
        if name not in self._env:
            self.missing.append(name)
            return 0
        value = self._env[name]
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{name} must be an integer, not {value!r}") from None

    def bool(self, name: str) -> bool:
        value = self.str(name).lower()
        if value not in ("true", "false", ""):
            raise ValueError(f"{name} must be true or false, not {value!r}")
        return value == "true"

    def list(self, name: str) -> list[str]:
        """Comma-separated; empty means none."""
        return [v.strip() for v in self.str(name).split(",") if v.strip()]

    def json(self, name: str) -> Any:
        value = self.str(name)
        return json.loads(value) if value else None

    def optional(self, name: str) -> str | None:
        return self._env.get(name)

    def check(self) -> None:
        if self.missing:
            raise MissingSettings(self.missing)
