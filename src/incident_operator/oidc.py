"""UI sign-in through authentik: OIDC authorization code flow with PKCE, and a signed session cookie. The
Copied from gpu-scheduler's dashboard (gpu_scheduler/oidc.py), itself scrape-job-manager's in one module: no user
table, the verified claims become the session cookie directly. Who may sign in is decided in authentik (a group binding on the application), not here.

State, nonce, the PKCE verifier and the page to return to ride in a short-lived HttpOnly cookie scoped to the
callback path. The session is payload + HMAC-SHA256 keyed by the client secret, so rotating it signs everyone out.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx
import jwt

log = logging.getLogger("incident_operator.oidc")

FLOW_COOKIE = "io_oidc"
FLOW_PATH = "/api/auth/oidc"
FLOW_TTL_S = 600
SESSION_COOKIE = "io_session"
SESSION_TTL_S = 12 * 3600
USER_AGENT = "incident-operator"


class SignInRefused(Exception):
    pass


class Provider:
    """authentik's endpoints and signing keys, fetched on first use."""

    def __init__(self, issuer: str, client_id: str, client_secret: str):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._meta: dict | None = None
        self._jwks: jwt.PyJWKClient | None = None
        self._lock = threading.Lock()

    def metadata(self) -> dict:
        with self._lock:
            if self._meta is None:
                r = httpx.get(self.issuer + "/.well-known/openid-configuration", timeout=10)
                r.raise_for_status()
                self._meta = r.json()
                # Named User-Agent: PyJWKClient fetches with urllib, and Cloudflare (in front of auth.danwils.com)
                # 403s the default Python-urllib one.
                self._jwks = jwt.PyJWKClient(self._meta["jwks_uri"], cache_keys=True,
                                             headers={"User-Agent": USER_AGENT})
            return self._meta

    def exchange(self, code: str, verifier: str, redirect_uri: str) -> dict:
        r = httpx.post(self.metadata()["token_endpoint"], timeout=15,
                       auth=(self.client_id, self.client_secret),
                       data={"grant_type": "authorization_code", "code": code,
                             "redirect_uri": redirect_uri, "code_verifier": verifier})
        if r.status_code != 200:
            log.warning("oidc: token endpoint %s: %s", r.status_code, r.text[:300])
            raise SignInRefused("authentik refused the sign-in code")
        return r.json()

    def verify(self, id_token: str, nonce: str) -> dict:
        try:
            self.metadata()
            key = self._jwks.get_signing_key_from_jwt(id_token).key
            claims = jwt.decode(
                id_token, key, algorithms=["RS256", "ES256"],
                # authentik's issuer keeps its trailing slash: compare against the discovery document's, not the
                # configured URL trimmed.
                audience=self.client_id, issuer=self._meta["issuer"],
                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            log.warning("oidc: ID token rejected: %s", exc)
            raise SignInRefused("the sign-in could not be verified") from None
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise SignInRefused("the sign-in could not be verified")
        return claims


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def local_path(path: str | None) -> str:
    """`path` if it is a path on this site, else "/": the return-to page must never send a fresh sign-in off to
    another origin."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


def flow_next(flow_cookie: str | None) -> str:
    """The return-to page recorded in a flow cookie ("/" if there's none or it's unreadable)."""
    parts = (flow_cookie or "").split(".")
    try:
        return local_path(_unb64(parts[3]).decode()) if len(parts) == 4 else "/"
    except ValueError:
        return "/"


def redirect_uri(public_url: str) -> str:
    return f"{public_url.rstrip('/')}{FLOW_PATH}/callback"


def begin(provider: Provider, public_url: str, next_path: str = "/") -> tuple[str, str]:
    """Return (authorization URL, flow cookie value)."""
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    url = provider.metadata()["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": provider.client_id,
        "redirect_uri": redirect_uri(public_url), "scope": "openid email profile",
        "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return url, f"{state}.{nonce}.{verifier}.{_b64(local_path(next_path).encode())}"


def finish(provider: Provider, public_url: str, flow_cookie: str | None, code: str, state: str) -> tuple[dict, str]:
    """Return (session user, local path to land on)."""
    parts = (flow_cookie or "").split(".")
    if len(parts) != 4 or not state or not secrets.compare_digest(parts[0], state):
        raise SignInRefused("the sign-in expired or came from another window")
    _, nonce, verifier, _ = parts
    tokens = provider.exchange(code, verifier, redirect_uri(public_url))
    claims = provider.verify(tokens.get("id_token") or "", nonce)
    user = {"sub": claims["sub"], "name": claims.get("name") or claims.get("preferred_username") or claims["sub"]}
    return user, flow_next(flow_cookie)


def sign_session(secret: str, user: dict) -> str:
    payload = _b64(json.dumps({**user, "exp": int(time.time()) + SESSION_TTL_S}).encode())
    sig = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_session(secret: str, cookie: str | None) -> dict | None:
    if not cookie or "." not in cookie:
        return None
    payload, sig = cookie.rsplit(".", 1)
    want = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, want):
        return None
    try:
        user = json.loads(_unb64(payload))
    except ValueError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("exp"), int) or user["exp"] < time.time():
        return None
    return user
