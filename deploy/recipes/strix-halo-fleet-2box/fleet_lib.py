"""Shared helpers for the edge-fleet config control plane PoC (stdlib only).

This module is intentionally dependency-free so the central control plane (CCP),
the pull agent, and the local mock router can all import it on a bare edge box
without a virtualenv. It provides:

- content hashing that matches the router's ``GET /config/hash`` contract
  (SHA256 over the raw active-config bytes),
- HMAC signing/verification of the desired-config bundle (the CCP <-> agent
  trust boundary; see PL-0036),
- tiny JSON/text HTTP helpers built on urllib.

See docs/agent/plans/pl-0040-edge-fleet-config-control-plane.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import ssl
import urllib.request

BUNDLE_FIELDS = ("version", "sha256", "config", "signature")

# Bundle signing modes (R4). HMAC is the default/fallback: a shared symmetric key
# any verifier could also forge with. Ed25519 is asymmetric: the CCP signs with a
# private seed, agents verify with only the public key (an edge box cannot forge a
# desired config). Ed25519 is a VENDORED reference impl (see _ed25519.py) so the
# stdlib-only / bare-box-importable property holds; production may swap in a
# native library and keep the same keys + wire format.
SIGN_HMAC = "hmac"
SIGN_ED25519 = "ed25519"


def _load_ed25519():
    try:
        from . import _ed25519  # type: ignore
    except Exception:  # pragma: no cover - normally run as scripts, not a package
        import _ed25519
    return _ed25519


def sha256_hex(data: bytes) -> str:
    """SHA256 hex digest of raw bytes (matches the router /config/hash contract)."""
    return hashlib.sha256(data).hexdigest()


def _preimage(
    version: str, config_sha256: str, config_text: str, ts: str = "", nonce: str = ""
) -> bytes:
    """Canonical bytes that get signed.

    With no freshness fields this is byte-identical to the ORIGINAL preimage
    (``version\\nsha256\\nconfig``), so default HMAC bundles verify exactly as
    before. When a timestamp/nonce is present (opt-in anti-replay) they are
    folded into the signed preimage so they cannot be altered without detection.
    """
    if ts or nonce:
        return "\n".join([version, config_sha256, ts, nonce, config_text]).encode(
            "utf-8"
        )
    return (version + "\n" + config_sha256 + "\n" + config_text).encode("utf-8")


def _coerce_key_bytes(value, length: int, what: str) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str):
        raw = bytes.fromhex(value.strip())
    else:
        raise ValueError("%s must be a hex string or bytes" % what)
    if len(raw) != length:
        raise ValueError("%s must be %d bytes (got %d)" % (what, length, len(raw)))
    return raw


class BundleSigner:
    """Signs the bundle preimage. HMAC (symmetric) or Ed25519 (asymmetric)."""

    def __init__(self, alg=SIGN_HMAC, hmac_key=None, ed25519_seed=None):
        self.alg = alg
        if alg == SIGN_HMAC:
            if not hmac_key:
                raise ValueError("HMAC signer requires a non-empty signing key")
            self._key = (
                hmac_key.encode("utf-8")
                if isinstance(hmac_key, str)
                else bytes(hmac_key)
            )
        elif alg == SIGN_ED25519:
            ed = _load_ed25519()
            if ed25519_seed is None:
                raise ValueError("Ed25519 signer requires a 32-byte seed")
            self._seed = _coerce_key_bytes(ed25519_seed, 32, "Ed25519 seed")
            self._public = ed.publickey(self._seed)
        else:
            raise ValueError("unknown signing alg: %r" % (alg,))

    @property
    def public_hex(self) -> str:
        return self._public.hex() if self.alg == SIGN_ED25519 else ""

    def sign(self, preimage: bytes) -> str:
        if self.alg == SIGN_HMAC:
            return hmac.new(self._key, preimage, hashlib.sha256).hexdigest()
        ed = _load_ed25519()
        return ed.sign(preimage, self._seed, self._public).hex()


class BundleVerifier:
    """Verifies a bundle signature. Enforces the configured algorithm so an
    Ed25519 verifier will NOT silently accept a downgraded HMAC bundle."""

    def __init__(self, alg=SIGN_HMAC, hmac_key=None, ed25519_public=None):
        self.alg = alg
        if alg == SIGN_HMAC:
            self._key = (
                hmac_key.encode("utf-8")
                if isinstance(hmac_key, str)
                else bytes(hmac_key or b"")
            )
        elif alg == SIGN_ED25519:
            self._public = _coerce_key_bytes(ed25519_public, 32, "Ed25519 public key")
        else:
            raise ValueError("unknown signing alg: %r" % (alg,))

    def verify(self, preimage: bytes, signature_hex: str, bundle_alg: str):
        """Return ``(ok, reason)`` for the signature check only."""
        if self.alg == SIGN_HMAC:
            if bundle_alg not in ("", SIGN_HMAC):
                return False, "expected an HMAC bundle, got alg=%r" % (bundle_alg,)
            expected = hmac.new(self._key, preimage, hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, signature_hex):
                return True, "ok"
            return False, "signature mismatch (untrusted or tampered bundle)"
        # Ed25519 verifier: reject anything not explicitly ed25519-signed, so an
        # attacker cannot strip the asymmetric signature down to a forgeable HMAC.
        if bundle_alg != SIGN_ED25519:
            return False, (
                "expected an ed25519-signed bundle, got alg=%r (downgrade attempt?)"
                % (bundle_alg or "hmac")
            )
        ed = _load_ed25519()
        try:
            sig = bytes.fromhex(signature_hex)
        except (ValueError, TypeError):
            return False, "signature is not valid hex"
        try:
            ok = ed.verify(sig, preimage, self._public)
        except Exception as exc:  # pragma: no cover - defensive
            return False, "ed25519 verify error: %s" % exc
        if ok:
            return True, "ok"
        return False, "signature mismatch (untrusted or tampered bundle)"


def hmac_signer(key) -> BundleSigner:
    return BundleSigner(SIGN_HMAC, hmac_key=key)


def hmac_verifier(key) -> BundleVerifier:
    return BundleVerifier(SIGN_HMAC, hmac_key=key)


def ed25519_signer(seed) -> BundleSigner:
    return BundleSigner(SIGN_ED25519, ed25519_seed=seed)


def ed25519_verifier(public) -> BundleVerifier:
    return BundleVerifier(SIGN_ED25519, ed25519_public=public)


def ed25519_keygen(seed=None):
    """Return ``(seed_hex, public_hex)`` for wiring keys into env/config."""
    ed = _load_ed25519()
    s, p = ed.create_keypair(seed)
    return s.hex(), p.hex()


def _as_signer(value) -> BundleSigner:
    return (
        value
        if isinstance(value, BundleSigner)
        else BundleSigner(SIGN_HMAC, hmac_key=value)
    )


def _as_verifier(value) -> BundleVerifier:
    return (
        value
        if isinstance(value, BundleVerifier)
        else BundleVerifier(SIGN_HMAC, hmac_key=value)
    )


def _read_key_material(env, inline_var: str, file_var: str) -> str:
    val = env.get(inline_var, "").strip()
    if val:
        return val
    path = env.get(file_var, "").strip()
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return ""


def signer_from_env(env=None) -> BundleSigner:
    """Build the CCP-side signer from env. Defaults to HMAC (backward compatible);
    ``FLEET_SIGN_MODE=ed25519`` selects asymmetric signing with a private seed."""
    env = os.environ if env is None else env
    mode = env.get("FLEET_SIGN_MODE", SIGN_HMAC).strip().lower() or SIGN_HMAC
    if mode == SIGN_ED25519:
        seed = _read_key_material(
            env, "FLEET_ED25519_SECRET", "FLEET_ED25519_SECRET_FILE"
        )
        if not seed:
            raise ValueError(
                "FLEET_SIGN_MODE=ed25519 needs FLEET_ED25519_SECRET or FLEET_ED25519_SECRET_FILE"
            )
        return ed25519_signer(seed)
    return hmac_signer(env.get("FLEET_SIGNING_KEY", ""))


def verifier_from_env(env=None) -> BundleVerifier:
    """Build the agent-side verifier from env (public key for Ed25519)."""
    env = os.environ if env is None else env
    mode = env.get("FLEET_SIGN_MODE", SIGN_HMAC).strip().lower() or SIGN_HMAC
    if mode == SIGN_ED25519:
        pub = _read_key_material(
            env, "FLEET_ED25519_PUBLIC", "FLEET_ED25519_PUBLIC_FILE"
        )
        if not pub:
            raise ValueError(
                "FLEET_SIGN_MODE=ed25519 needs FLEET_ED25519_PUBLIC or FLEET_ED25519_PUBLIC_FILE"
            )
        return ed25519_verifier(pub)
    return hmac_verifier(env.get("FLEET_SIGNING_KEY", ""))


def sign_bundle(
    signing_key: str, version: str, config_sha256: str, config_text: str
) -> str:
    """HMAC-SHA256 over the canonical bundle preimage (kept for backward compat).

    Equivalent to ``hmac_signer(signing_key).sign(_preimage(...))`` for the legacy
    (no timestamp/nonce) preimage; retained so any existing importer keeps working.
    """
    return hmac.new(
        signing_key.encode("utf-8"),
        _preimage(version, config_sha256, config_text),
        hashlib.sha256,
    ).hexdigest()


def build_bundle(signer, version: str, config_text: str, ts=None, nonce=None) -> dict:
    """Build a signed desired-config bundle the agent can verify and apply.

    ``signer`` may be a string HMAC key (default/back-compat) or a ``BundleSigner``
    (e.g. Ed25519). ``ts``/``nonce`` are optional freshness fields (opt-in
    anti-replay); when omitted the HMAC bundle is byte-identical to the original.
    """
    signer = _as_signer(signer)
    config_sha256 = sha256_hex(config_text.encode("utf-8"))
    ts = "" if ts is None else str(ts)
    nonce = "" if nonce is None else str(nonce)
    bundle = {"version": version, "sha256": config_sha256, "config": config_text}
    if signer.alg != SIGN_HMAC:
        bundle["alg"] = signer.alg
    if ts:
        bundle["ts"] = ts
    if nonce:
        bundle["nonce"] = nonce
    bundle["signature"] = signer.sign(
        _preimage(version, config_sha256, config_text, ts, nonce)
    )
    return bundle


def verify_bundle(verifier, bundle: dict):
    """Return ``(ok, reason)``. ``ok=False`` means the agent must NOT apply it.

    ``verifier`` may be a string HMAC key (default/back-compat) or a
    ``BundleVerifier`` (e.g. Ed25519). Two independent checks must both pass: the
    embedded content hash must match the config bytes, and the signature must
    verify under the configured algorithm. Uses constant-time comparison for HMAC
    and rejects a bundle whose declared algorithm does not match the verifier
    (anti-downgrade). Version monotonicity / freshness is enforced by the agent.
    """
    verifier = _as_verifier(verifier)
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
    version = str(bundle["version"])
    ts = str(bundle.get("ts", ""))
    nonce = str(bundle.get("nonce", ""))
    bundle_alg = str(bundle.get("alg", ""))
    preimage = _preimage(version, expected_sha, config_text, ts, nonce)
    return verifier.verify(preimage, str(bundle["signature"]), bundle_alg)


_CLIENT_CTX_CACHE = {}


def client_ssl_context(env=None):
    """Build (and cache) a client TLS context for https CCP URLs (R5/C1, opt-in).

    - ``FLEET_TLS_CA``: path to a CA/cert bundle to trust (e.g. the CCP's
      self-signed cert), so the agent verifies the CCP identity.
    - ``FLEET_TLS_INSECURE=1``: skip verification (dev/self-signed only).
    - ``FLEET_TLS_CLIENT_CERT`` + ``FLEET_TLS_CLIENT_KEY``: present a client
      certificate (mTLS, C1) so a CCP started with ``CCP_TLS_CLIENT_CA`` accepts
      the connection. BOTH must be set; either one alone is ignored (the
      connection stays server-auth only, exactly as before).

    With none set, the system default trust store is used (works for a CCP
    behind a normally-trusted certificate).
    """
    env = os.environ if env is None else env
    ca = env.get("FLEET_TLS_CA", "").strip()
    insecure = env.get("FLEET_TLS_INSECURE", "").strip().lower() in ("1", "true", "yes")
    client_cert = env.get("FLEET_TLS_CLIENT_CERT", "").strip()
    client_key = env.get("FLEET_TLS_CLIENT_KEY", "").strip()
    cache_key = (ca, insecure, client_cert, client_key)
    ctx = _CLIENT_CTX_CACHE.get(cache_key)
    if ctx is not None:
        return ctx
    ctx = ssl.create_default_context()
    if ca:
        ctx.load_verify_locations(ca)
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # C1 (mTLS): only present a client cert when BOTH the cert and key are given,
    # so the default / server-auth-only path is unchanged when they are unset.
    if client_cert and client_key:
        ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)
    _CLIENT_CTX_CACHE[cache_key] = ctx
    return ctx


def _request(
    url: str,
    method: str,
    body: bytes = None,
    token: str = None,
    content_type: str = None,
    timeout: float = 10.0,
    ssl_context=None,
):
    req = urllib.request.Request(url, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    # Plain HTTP stays the default; an https:// URL transparently enables TLS
    # (client verification configured via env). Keeps demos working unchanged.
    if url[:6].lower() == "https:":
        ctx = ssl_context or client_ssl_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8")
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
    status, raw = _request(
        url,
        "POST",
        body=body,
        token=token,
        content_type="application/json",
        timeout=timeout,
    )
    return status, json.loads(raw) if raw else {}


def http_get_text(url: str, token: str = None, timeout: float = 10.0):
    """GET ``url`` and return the raw text body. Returns ``(status, text)``."""
    return _request(url, "GET", token=token, timeout=timeout)
