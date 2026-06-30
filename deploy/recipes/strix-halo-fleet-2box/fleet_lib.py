"""Shared helpers for the edge-fleet config control plane PoC (stdlib only).

This module is intentionally dependency-free so the central control plane (CCP),
the pull agent, and the local mock router can all import it on a bare edge box
without a virtualenv. It provides:

- content hashing that matches the router's ``GET /config/hash`` contract
  (SHA256 over the raw active-config bytes),
- HMAC signing/verification of the desired-config bundle (the CCP <-> agent
  trust boundary; see PL-0036),
- tiny JSON/text HTTP helpers built on urllib.

See docs/agent/plans/pl-0036-edge-fleet-config-control-plane.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request

BUNDLE_FIELDS = ("version", "sha256", "config", "signature")


def sha256_hex(data: bytes) -> str:
    """SHA256 hex digest of raw bytes (matches the router /config/hash contract)."""
    return hashlib.sha256(data).hexdigest()


def sign_bundle(signing_key: str, version: str, config_sha256: str, config_text: str) -> str:
    """HMAC-SHA256 over the canonical bundle preimage (version, hash, config)."""
    msg = (version + "\n" + config_sha256 + "\n" + config_text).encode("utf-8")
    return hmac.new(signing_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def build_bundle(signing_key: str, version: str, config_text: str) -> dict:
    """Build a signed desired-config bundle the agent can verify and apply."""
    config_sha256 = sha256_hex(config_text.encode("utf-8"))
    return {
        "version": version,
        "sha256": config_sha256,
        "config": config_text,
        "signature": sign_bundle(signing_key, version, config_sha256, config_text),
    }


def verify_bundle(signing_key: str, bundle: dict):
    """Return ``(ok, reason)``. ``ok=False`` means the agent must NOT apply it.

    Two independent checks must both pass: the embedded content hash must match
    the config bytes, and the HMAC signature must match under the shared key.
    Uses constant-time comparison to avoid timing oracles.
    """
    if not isinstance(bundle, dict):
        return False, "bundle is not an object"
    for key in BUNDLE_FIELDS:
        if key not in bundle:
            return False, "missing field: " + key
    config_text = bundle["config"]
    if not isinstance(config_text, str):
        return False, "config must be a string"
    expected_sha = sha256_hex(config_text.encode("utf-8"))
    if not hmac.compare_digest(expected_sha, str(bundle["sha256"])):
        return False, "content hash mismatch (config does not match sha256)"
    expected_sig = sign_bundle(signing_key, str(bundle["version"]), expected_sha, config_text)
    if not hmac.compare_digest(expected_sig, str(bundle["signature"])):
        return False, "signature mismatch (untrusted or tampered bundle)"
    return True, "ok"


def _request(url: str, method: str, body: bytes = None, token: str = None,
             content_type: str = None, timeout: float = 10.0):
    req = urllib.request.Request(url, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, raw


def http_get_json(url: str, token: str = None, timeout: float = 10.0):
    """GET ``url`` and parse the JSON body. Returns ``(status, obj)``."""
    status, raw = _request(url, "GET", token=token, timeout=timeout)
    return status, json.loads(raw) if raw else {}


def http_post_json(url: str, payload: dict, token: str = None, timeout: float = 10.0):
    """POST a JSON ``payload``. Returns ``(status, obj)``."""
    body = json.dumps(payload).encode("utf-8")
    status, raw = _request(url, "POST", body=body, token=token,
                           content_type="application/json", timeout=timeout)
    return status, json.loads(raw) if raw else {}


def http_get_text(url: str, token: str = None, timeout: float = 10.0):
    """GET ``url`` and return the raw text body. Returns ``(status, text)``."""
    return _request(url, "GET", token=token, timeout=timeout)
