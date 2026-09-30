"""Second lock for remote access: verify the Cloudflare Access login on every tunnelled request.

Cloudflare Access is the first lock. It sits in front of the public hostname and only lets the
allowed email through. This module is the second. It runs inside the app and refuses any request
that arrived through Cloudflare without a valid, signed Access token for the right application
and the right person - so if the Access policy is ever deleted, loosened or attached to the wrong
app, the tracker still does not open.

Local use is unchanged. A browser on this machine talking to 127.0.0.1:8765 carries no Cloudflare
headers and a loopback Host, and passes straight through.

How a request is classified:
  REMOTE if it carries any header Cloudflare's edge adds, or its Host is not a loopback name.
         Cloudflare's edge always sets Cf-Connecting-Ip itself, overwriting anything a client
         sends, so a request from the internet cannot pass itself off as local.
  LOCAL  otherwise.

Keying on Host also closes DNS rebinding - a malicious page pointing its own domain at 127.0.0.1
to read a local app from inside your browser. That request carries the attacker's Host, so it is
REMOTE and needs a token it cannot have.

Every failure is closed: missing config, an unreachable key endpoint, a malformed, expired or
forged token, the wrong audience, issuer or email - all refused. Nothing here falls back to
allowing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Mapping

import jwt
from jwt import PyJWKClient

from . import scoring

log = logging.getLogger("jobtrack.access")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
EDGE_HEADERS = ("cf-connecting-ip", "cf-ray", "cf-access-jwt-assertion")
TOKEN_HEADER = "cf-access-jwt-assertion"
TOKEN_COOKIE = "CF_Authorization"

# Only ever fetch signing keys from a Cloudflare Access team domain. A typo in config must not be
# able to point key discovery at some other host.
_TEAM_DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$")

_jwks_clients: dict[str, PyJWKClient] = {}


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    email: str | None = None


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _host_only(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):                     # [::1]:8765
        return h[1:h.index("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def is_remote(headers: Mapping[str, str]) -> bool:
    h = _lower(headers)
    if any(name in h for name in EDGE_HEADERS):
        return True
    # A missing Host is treated as remote too - fail closed.
    return _host_only(h.get("host", "")) not in LOOPBACK_HOSTS


def settings() -> dict:
    return scoring.load_config().get("remote_access") or {}


def _signing_key(team_domain: str, token: str):
    """Public key for this token, from the team's published key set (cached, refetched on an
    unknown key id, so Cloudflare's routine key rotation needs nothing from us)."""
    client = _jwks_clients.get(team_domain)
    if client is None:
        client = PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs",
                             cache_jwk_set=True, lifespan=3600, timeout=10)
        _jwks_clients[team_domain] = client
    return client.get_signing_key_from_jwt(token).key


def verify_remote(headers: Mapping[str, str], cookies: Mapping[str, str]) -> Verdict:
    cfg = settings()
    team = (cfg.get("team_domain") or "").strip().lower()
    aud = (cfg.get("aud") or "").strip()
    allowed = {e.strip().lower() for e in cfg.get("allowed_emails") or [] if e.strip()}

    if not _TEAM_DOMAIN.match(team) or not aud or not allowed:
        return Verdict(False, "remote access is not configured")

    token = _lower(headers).get(TOKEN_HEADER) or cookies.get(TOKEN_COOKIE)
    if not token:
        return Verdict(False, "no Cloudflare Access token")

    try:
        key = _signing_key(team, token)
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=aud, issuer=f"https://{team}",
            options={"require": ["exp", "iat", "aud", "iss"]}, leeway=30)
    except jwt.PyJWKClientError as e:
        # covers both "key endpoint unreachable" and "no published key matches this token"
        return Verdict(False, f"no usable Cloudflare signing key: {e}")
    except jwt.InvalidTokenError as e:
        return Verdict(False, f"invalid token: {type(e).__name__}")

    email = str(claims.get("email") or "").strip().lower()
    if email not in allowed:
        # Service tokens carry no email and land here too, deliberately.
        return Verdict(False, "token email is not on the allow list", email or None)
    return Verdict(True, "ok", email)
