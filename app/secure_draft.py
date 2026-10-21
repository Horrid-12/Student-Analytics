"""Encrypted, self-contained drafts for the sign-up wizard.

The sign-up flow collects personal and academic details across four phases
(college email, name, PRN, branch, division, semester, linked GitHub /
LinkedIn / HackerRank handles, profile-picture choice). None of that may be
persisted before the student finishes: the account is created once, at the
end of phase 4, and nothing about the sign-up belongs in the database while
it is still in progress.

The draft therefore travels in the short-lived ``gsad_signup`` cookie,
encrypted with AES-GCM under a key derived from ``AUTH_SECRET``. The client
can neither read the payload nor tamper with it (a modified cookie fails to
decrypt and the wizard simply restarts), and the server keeps no copy — so
a phase 1-3 sign-up writes nothing to Neon/Postgres.

Kept out of ``app/auth.py`` so that module stays stdlib-only.
"""

import base64
import json
import os
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12


def seal(payload: dict, key: bytes, ttl_seconds: int) -> str | None:
    """Encrypt ``payload`` into an opaque, URL-safe cookie value.

    The expiry travels inside the sealed blob, so a stale draft is rejected
    on read even if the cookie's own max-age has not expired yet. Returns
    None when the payload cannot be encrypted."""
    if not isinstance(payload, dict) or not key:
        return None
    data = {k: v for k, v in payload.items() if k != "exp"}
    data["exp"] = int(time.time()) + int(ttl_seconds)
    try:
        blob = json.dumps(data, separators=(",", ":"), default=str).encode("utf-8")
        nonce = os.urandom(_NONCE_BYTES)
        sealed = AESGCM(key).encrypt(nonce, blob, None)
        return base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")
    except Exception:
        return None


def unseal(token: str | None, key: bytes) -> dict | None:
    """Decrypt a draft cookie value.

    Returns None for a missing, malformed, tampered, expired or
    differently-keyed token — the caller then restarts the wizard."""
    if not token or not key:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if len(raw) <= _NONCE_BYTES:
            return None
        nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        blob = AESGCM(key).decrypt(nonce, sealed, None)
        data = json.loads(blob.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        if int(data.get("exp") or 0) < int(time.time()):
            return None
        data.pop("exp", None)
        return data
    except Exception:
        return None
