"""The real Kubernetes client, against an in-process HTTP handler (the other tests use fakes in its place)."""
import json
import os

import httpx
import pytest

from incident_operator import k8s as k8s_mod
from incident_operator.k8s import K8s


def client(handler, **kw) -> tuple[K8s, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    k = K8s(base="http://api", **kw)
    k._http = httpx.Client(base_url="http://api", transport=httpx.MockTransport(record))
    return k, seen


def test_get_json_and_text_with_token():
    k, seen = client(lambda r: httpx.Response(200, text='{"items": []}'), token="t0k")
    assert k.get("/api/v1/pods", {"limit": "1"}) == {"items": []}
    assert k.get("/api/v1/pods", text=True) == '{"items": []}'
    assert seen[0].url == "http://api/api/v1/pods?limit=1"
    assert seen[0].headers["Authorization"] == "Bearer t0k"


def test_no_token_sends_no_auth_header():
    k, seen = client(lambda r: httpx.Response(200, json={}))
    k.get("/x")
    assert "Authorization" not in seen[0].headers


def test_patch_is_a_merge_patch():
    k, seen = client(lambda r: httpx.Response(200, json={"ok": 1}))
    assert k.patch("/x", {"metadata": {"annotations": {"a": "b"}}}) == {"ok": 1}
    assert seen[0].method == "PATCH"
    assert seen[0].headers["Content-Type"] == "application/merge-patch+json"
    assert json.loads(seen[0].content) == {"metadata": {"annotations": {"a": "b"}}}


def test_delete_treats_404_as_done_but_raises_on_other_errors():
    k, _ = client(lambda r: httpx.Response(404))
    k.delete("/gone")
    k, _ = client(lambda r: httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        k.delete("/forbidden")


def test_get_raises_on_404():
    k, _ = client(lambda r: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        k.get("/missing")


def test_service_account_token_is_reread_when_rotated(tmp_path):
    path = tmp_path / "token"
    path.write_text("first\n")
    token = k8s_mod._ServiceAccountToken(str(path))
    assert token() == "first"
    path.write_text("second\n")
    os.utime(path, (1, 1))          # a distinct mtime, whatever the filesystem's timestamp resolution
    assert token() == "second"
