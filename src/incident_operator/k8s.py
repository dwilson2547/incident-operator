"""The Kubernetes API client every part of the operator shares.

Evidence collection and read operations only call `get`. `post` and `delete` create and remove probe Jobs in the
operator's own namespace (ops.Runner._probe), and the executor uses `delete`, `post` and `patch` for approved fixes.
What each caller may actually do is set by its service account's RBAC, not by this class.

Tests pass fakes with the same methods.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TIMEOUT = 15


class _ServiceAccountToken:
    """The pod's service-account token. The kubelet rotates the file, so it is re-read whenever it changes."""

    def __init__(self, path: str):
        self.path, self._mtime, self._token = path, 0.0, ""

    def __call__(self) -> str:
        mtime = os.stat(self.path).st_mtime
        if mtime != self._mtime:
            with open(self.path) as f:
                self._token = f.read().strip()
            self._mtime = mtime
        return self._token


class K8s:
    def __init__(self, base: str | None = None, token: str | None = None, verify: Any = True):
        """In the cluster (no `base`), use the service account's API address, token and CA. Otherwise talk to `base`,
        e.g. `kubectl proxy` at http://127.0.0.1:8001, with an optional fixed token."""
        if base is None:
            base = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
            self._token = _ServiceAccountToken(f"{SA_DIR}/token")
            verify = f"{SA_DIR}/ca.crt"
        else:
            self._token = lambda: token or ""
        self.base = base
        self._http = httpx.Client(base_url=base, timeout=TIMEOUT, verify=verify)

    @classmethod
    def from_env(cls) -> K8s:
        """KUBE_API_URL when set (local runs), else the in-cluster service account."""
        url = os.environ.get("KUBE_API_URL")
        return cls(base=url) if url else cls()

    def _request(self, method: str, path: str, *, ok_404: bool = False, **kw) -> httpx.Response:
        headers = kw.pop("headers", {})
        if token := self._token():
            headers["Authorization"] = f"Bearer {token}"
        r = self._http.request(method, path, headers=headers, **kw)
        if not (ok_404 and r.status_code == 404):
            r.raise_for_status()
        return r

    def get(self, path: str, params: dict | None = None, text: bool = False) -> Any:
        r = self._request("GET", path, params=params)
        return r.text if text else r.json()

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, json=body).json()

    def patch(self, path: str, body: dict) -> dict:
        """A JSON merge patch."""
        return self._request("PATCH", path, content=json.dumps(body),
                             headers={"Content-Type": "application/merge-patch+json"}).json()

    def delete(self, path: str, params: dict | None = None) -> None:
        """Already gone counts as deleted."""
        self._request("DELETE", path, params=params, ok_404=True)
