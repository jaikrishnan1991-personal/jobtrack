"""Tests for app/access_guard.py - the in-app check on requests arriving through Cloudflare.

Real RS256 crypto throughout: a locally generated key stands in for Cloudflare's signing key, and
a second key stands in for an attacker's. Only key *discovery* is stubbed (no network).

Run:  python tests/test_access_guard.py      (or: python -m pytest tests)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from app import access_guard as g  # noqa: E402

TEAM = "exampleteam.cloudflareaccess.com"
AUD = "0" * 64
ME = "owner@example.com"
CFG = {"team_domain": TEAM, "aud": AUD, "allowed_emails": [ME]}

CF_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ATTACKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

_cfg = dict(CFG)
g.settings = lambda: _cfg
g._signing_key = lambda team, token: CF_KEY.public_key()

REMOTE = {"host": "jobs.example.com", "cf-connecting-ip": "203.0.113.7", "cf-ray": "abc-BOM"}


def token(key=CF_KEY, **over) -> str:
    now = int(time.time())
    claims = {"aud": [AUD], "iss": f"https://{TEAM}", "email": ME, "iat": now,
              "nbf": now, "exp": now + 3600, "type": "app", "sub": "user-id"}
    claims.update(over)
    for k in [k for k, v in claims.items() if v is None]:
        del claims[k]
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def with_token(tok: str) -> dict:
    return {**REMOTE, "cf-access-jwt-assertion": tok}


def set_cfg(**over) -> None:
    _cfg.clear()
    _cfg.update({**CFG, **over})


# ------------------------------------------------------------ classification

def test_loopback_hosts_are_local():
    for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765", "127.0.0.1", "LOCALHOST:8765"):
        assert not g.is_remote({"host": host}), host


def test_public_hostname_is_remote():
    assert g.is_remote({"host": "jobs.example.com"})


def test_dns_rebinding_host_is_remote():
    assert g.is_remote({"host": "attacker.example:8765"})


def test_cloudflare_header_makes_it_remote_even_with_local_host():
    assert g.is_remote({"host": "127.0.0.1:8765", "cf-connecting-ip": "203.0.113.7"})


def test_header_names_are_case_insensitive():
    assert g.is_remote({"Host": "127.0.0.1:8765", "CF-RAY": "abc"})


def test_missing_host_is_remote():
    assert g.is_remote({})


# ------------------------------------------------------------ verification

def test_valid_token_is_allowed():
    set_cfg()
    v = g.verify_remote(with_token(token()), {})
    assert v.allowed and v.email == ME, v


def test_token_in_cookie_is_allowed():
    set_cfg()
    v = g.verify_remote(REMOTE, {"CF_Authorization": token()})
    assert v.allowed, v


def test_email_match_ignores_case():
    set_cfg()
    assert g.verify_remote(with_token(token(email=ME.upper())), {}).allowed


def test_no_token_is_refused():
    set_cfg()
    assert not g.verify_remote(REMOTE, {}).allowed


def test_wrong_audience_is_refused():
    set_cfg()
    assert not g.verify_remote(with_token(token(aud=["some-other-app"])), {}).allowed


def test_wrong_issuer_is_refused():
    set_cfg()
    v = g.verify_remote(with_token(token(iss="https://otherteam.cloudflareaccess.com")), {})
    assert not v.allowed


def test_expired_token_is_refused():
    set_cfg()
    old = int(time.time()) - 7200
    assert not g.verify_remote(with_token(token(iat=old, nbf=old, exp=old + 60)), {}).allowed


def test_token_signed_by_another_key_is_refused():
    set_cfg()
    assert not g.verify_remote(with_token(token(key=ATTACKER_KEY)), {}).allowed


def test_email_not_on_list_is_refused():
    set_cfg()
    v = g.verify_remote(with_token(token(email="someone.else@gmail.com")), {})
    assert not v.allowed and "allow list" in v.reason


def test_token_without_email_is_refused():
    set_cfg()  # e.g. a Cloudflare service token
    assert not g.verify_remote(with_token(token(email=None)), {}).allowed


def test_alg_none_is_refused():
    set_cfg()
    now = int(time.time())
    forged = jwt.encode({"aud": [AUD], "iss": f"https://{TEAM}", "email": ME, "iat": now,
                         "exp": now + 3600}, None, algorithm="none")
    assert not g.verify_remote(with_token(forged), {}).allowed


def test_hmac_key_confusion_is_refused():
    """Classic attack: sign HS256 using the *public* key as the HMAC secret."""
    set_cfg()
    pub = CF_KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
    now = int(time.time())
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"aud": [AUD], "iss": f"https://{TEAM}", "email": ME,
                           "iat": now, "exp": now + 3600}).encode())
    sig = b64(hmac.new(pub, head + b"." + body, hashlib.sha256).digest())
    assert not g.verify_remote(with_token((head + b"." + body + b"." + sig).decode()), {}).allowed


def test_garbage_token_is_refused():
    set_cfg()
    assert not g.verify_remote(with_token("not.a.jwt"), {}).allowed


# ------------------------------------------------------------ configuration fails closed

def test_unconfigured_refuses_even_a_valid_token():
    for bad in ({"team_domain": ""}, {"aud": ""}, {"allowed_emails": []}):
        set_cfg(**bad)
        assert not g.verify_remote(with_token(token()), {}).allowed, bad
    set_cfg()


def test_team_domain_must_be_a_cloudflare_access_domain():
    for bad in ("evil.com", TEAM + ".evil.com", "https://" + TEAM):
        set_cfg(team_domain=bad)
        assert not g.verify_remote(with_token(token()), {}).allowed, bad
    set_cfg()


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  pass  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}  {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
