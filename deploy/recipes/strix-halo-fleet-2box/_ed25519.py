"""Vendored, dependency-free Ed25519 (RFC 8032) for the edge-fleet PoC.

REFERENCE IMPLEMENTATION -- NOT for production key volume. This is a compact,
pure-Python (``hashlib`` only) implementation of Ed25519 signing/verification,
kept here so the CCP can sign the desired-config bundle with a PRIVATE key and
agents can verify with only the PUBLIC key WITHOUT adding a third-party crypto
dependency (the whole PoC must import on a bare edge box with no virtualenv).

It is byte-compatible with the standard (validated against the RFC 8032 §7.1
test vectors -- run ``python3 _ed25519.py selftest``), so a production
deployment can drop in ``libsodium``/PyNaCl or ``cryptography`` and keep the
same keys and wire signatures. This code is constant-time-ish at best; it makes
NO timing/side-channel guarantees. For a real fleet, prefer a vetted native
library.

Public API:
    create_keypair(seed=None) -> (seed32_bytes, public32_bytes)
    publickey(seed32) -> public32_bytes
    sign(message, seed32, public32=None) -> signature64_bytes
    verify(signature64, message, public32) -> bool

CLI:
    python3 _ed25519.py keygen [--seed HEX] [--out-dir DIR]
    python3 _ed25519.py selftest
"""

from __future__ import annotations

# The single-letter names (H, P, Q, R, S, A, B, L, I) match the RFC 8032 and the
# canonical reference-implementation notation on purpose, so this vendored code
# can be checked against the spec line-for-line; keep the crypto naming as-is.
# ruff: noqa: N802, N803, N806

import hashlib
import os

# --- curve constants (Curve25519 / edwards25519, RFC 8032) -------------------
_b = 256
_q = 2 ** 255 - 19
# group order L
_L = 2 ** 252 + 27742317777372353535851937790883648493


def _H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _inv(x: int) -> int:
    return pow(x, _q - 2, _q)


_d = (-121665 * _inv(121666)) % _q
_I = pow(2, (_q - 1) // 4, _q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = (4 * _inv(5)) % _q
_Bx = _xrecover(_By)
_B = (_Bx % _q, _By % _q)


def _edwards_add(P, Q):
    x1, y1 = P
    x2, y2 = Q
    denom = _d * x1 * x2 * y1 * y2
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + denom)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - denom)
    return (x3 % _q, y3 % _q)


def _scalarmult(P, e: int):
    """Iterative double-and-add (no recursion, so large scalars are fine)."""
    result = (0, 1)  # neutral element
    base = P
    while e > 0:
        if e & 1:
            result = _edwards_add(result, base)
        base = _edwards_add(base, base)
        e >>= 1
    return result


def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _encodeint(y: int) -> bytes:
    return y.to_bytes(_b // 8, "little")


def _decodeint(s: bytes) -> int:
    return int.from_bytes(s, "little")


def _encodepoint(P) -> bytes:
    x, y = P
    # low bits are y; the top bit carries the sign of x
    val = (y & ((1 << (_b - 1)) - 1)) | ((x & 1) << (_b - 1))
    return val.to_bytes(_b // 8, "little")


def _decodepoint(s: bytes):
    val = int.from_bytes(s, "little")
    y = val & ((1 << (_b - 1)) - 1)
    x = _xrecover(y)
    if (x & 1) != ((val >> (_b - 1)) & 1):
        x = _q - x
    P = (x, y)
    if not _isoncurve(P):
        raise ValueError("decoded point is not on the curve")
    return P


def _isoncurve(P) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _q == 0


def _secret_scalar_and_prefix(seed: bytes):
    """Return (a, prefix) derived from the 32-byte seed per RFC 8032."""
    h = _H(seed)
    a = 2 ** (_b - 2) + sum(2 ** i * _bit(h, i) for i in range(3, _b - 2))
    return a, h[_b // 8:_b // 4]


def _coerce_seed(seed) -> bytes:
    if isinstance(seed, str):
        seed = bytes.fromhex(seed.strip())
    else:
        seed = bytes(seed)
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes (got %d)" % len(seed))
    return seed


def publickey(seed) -> bytes:
    """Derive the 32-byte public key from a 32-byte private seed."""
    seed = _coerce_seed(seed)
    a, _prefix = _secret_scalar_and_prefix(seed)
    return _encodepoint(_scalarmult(_B, a))


def create_keypair(seed=None):
    """Return ``(seed_bytes, public_bytes)``; generates a random seed if None."""
    seed = os.urandom(32) if seed is None else _coerce_seed(seed)
    return seed, publickey(seed)


def sign(message: bytes, seed, public=None) -> bytes:
    """Return the 64-byte Ed25519 signature of ``message`` under ``seed``."""
    seed = _coerce_seed(seed)
    a, prefix = _secret_scalar_and_prefix(seed)
    if public is None:
        public = _encodepoint(_scalarmult(_B, a))
    elif isinstance(public, str):
        public = bytes.fromhex(public.strip())
    r = _decodeint(_H(prefix + message)) % _L
    R = _scalarmult(_B, r)
    Renc = _encodepoint(R)
    k = _decodeint(_H(Renc + public + message)) % _L
    S = (r + k * a) % _L
    return Renc + _encodeint(S)


def verify(signature: bytes, message: bytes, public) -> bool:
    """Return True iff ``signature`` is a valid Ed25519 signature of ``message``."""
    if isinstance(public, str):
        public = bytes.fromhex(public.strip())
    if isinstance(signature, str):
        signature = bytes.fromhex(signature.strip())
    if len(signature) != 64 or len(public) != 32:
        return False
    try:
        R = _decodepoint(signature[:32])
        A = _decodepoint(public)
    except ValueError:
        return False
    S = _decodeint(signature[32:])
    if S >= _L:  # non-canonical S -> reject
        return False
    k = _decodeint(_H(signature[:32] + public + message)) % _L
    left = _scalarmult(_B, S)
    right = _edwards_add(R, _scalarmult(A, k))
    return left == right


# --- RFC 8032 §7.1 test vectors (seed, public, message_hex, signature) -------
_TEST_VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


def selftest() -> bool:
    """Validate against RFC 8032 §7.1 vectors + a tamper check. Raises on failure."""
    for seed_hex, pub_hex, msg_hex, sig_hex in _TEST_VECTORS:
        seed = bytes.fromhex(seed_hex)
        msg = bytes.fromhex(msg_hex)
        pub = publickey(seed)
        if pub.hex() != pub_hex:
            raise AssertionError("publickey mismatch for seed %s: %s" % (seed_hex, pub.hex()))
        sig = sign(msg, seed, pub)
        if sig.hex() != sig_hex:
            raise AssertionError("signature mismatch for seed %s: %s" % (seed_hex, sig.hex()))
        if not verify(sig, msg, pub):
            raise AssertionError("verify failed for seed %s" % seed_hex)
        tampered = bytearray(msg) + b"\x00"
        if verify(sig, bytes(tampered), pub):
            raise AssertionError("verify accepted a tampered message for seed %s" % seed_hex)
    return True


def _main(argv=None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="_ed25519", description="vendored Ed25519 helper")
    sub = p.add_subparsers(dest="cmd", required=True)
    kg = sub.add_parser("keygen", help="generate an Ed25519 keypair")
    kg.add_argument("--seed", default="", help="hex 32-byte seed (default: random)")
    kg.add_argument("--out-dir", default="", help="write seed/public to this dir")
    sub.add_parser("selftest", help="validate against RFC 8032 test vectors")
    args = p.parse_args(argv)

    if args.cmd == "selftest":
        selftest()
        print("ed25519 selftest OK (RFC 8032 vectors)")
        return 0

    seed = bytes.fromhex(args.seed) if args.seed else None
    seed, public = create_keypair(seed)
    seed_hex, public_hex = seed.hex(), public.hex()
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        secret_path = os.path.join(args.out_dir, "ccp_ed25519.seed")
        public_path = os.path.join(args.out_dir, "ccp_ed25519.pub")
        # 0600 for the private seed.
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(seed_hex + "\n")
        with open(public_path, "w", encoding="utf-8") as fh:
            fh.write(public_hex + "\n")
        print("wrote %s (private, 0600) and %s (public)" % (secret_path, public_path))
    print("seed   " + seed_hex)
    print("public " + public_hex)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
