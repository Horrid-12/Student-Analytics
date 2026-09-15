"""Google OAuth transport for sign-in (authlib + httpx), kept out of auth.py so
auth.py stays stdlib-only.

This module owns the network half only: building the consent URL and exchanging
the callback code for userinfo. The college-domain gate, role resolution and
user upsert live in app.auth (pure logic, directly tested). Routes call
``build_authorization_url`` and ``exchange_code``; tests monkeypatch
``exchange_code`` to swap the network half for fake claims.
"""

import os
import tomllib
from pathlib import Path

from authlib.integrations.httpx_client import AsyncOAuth2Client

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
SCOPES = "openid email profile"

_SECRETS_PATH = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"


def load_credentials() -> tuple[str, str]:
    """Google client id/secret from ``GOOGLE_CLIENT_ID``/``GOOGLE_CLIENT_SECRET``
    env vars first, then the top-level keys in ``.streamlit/secrets.toml``
    (gitignored) — mirrors github_client.load_token. Never hardcoded."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    try:
        with _SECRETS_PATH.open("rb") as handle:
            data = tomllib.load(handle)
        client_id = client_id or str(data.get("GOOGLE_CLIENT_ID", "") or "").strip()
        client_secret = client_secret or str(data.get("GOOGLE_CLIENT_SECRET", "") or "").strip()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return client_id, client_secret


def configured() -> bool:
    client_id, client_secret = load_credentials()
    return bool(client_id and client_secret)


def build_authorization_url(redirect_uri: str, state: str, allowed_domain: str | None = None) -> str:
    """Google consent URL. ``hd`` hints the Workspace domain; it is a UX helper
    only — the server-side gate in auth.authorize_domain is what actually
    enforces the allowlist."""
    client_id, client_secret = load_credentials()
    client = AsyncOAuth2Client(client_id=client_id, client_secret=client_secret)
    params = {"access_type": "online"}
    if allowed_domain:
        params["hd"] = allowed_domain
    url, _ = client.create_authorization_url(
        AUTHORIZATION_ENDPOINT,
        state=state,
        scope=SCOPES,
        redirect_uri=redirect_uri,
        **params,
    )
    return url


async def exchange_code(authorization_response: str, state: str, redirect_uri: str) -> dict:
    """Trade the callback code for userinfo claims, normalized to
    ``sub/email/email_verified/name/hd/picture``. Raises on a Google error
    (OAuthError, network, or non-2xx userinfo)."""
    client_id, client_secret = load_credentials()
    client = AsyncOAuth2Client(client_id=client_id, client_secret=client_secret)
    token = await client.fetch_token(
        TOKEN_ENDPOINT,
        authorization_response=authorization_response,
        state=state,
        redirect_uri=redirect_uri,
    )
    access_token = token.get("access_token")
    if not access_token:
        raise RuntimeError("Google exchange returned no access token")
    response = await client.get(USERINFO_ENDPOINT)
    response.raise_for_status()
    raw = response.json()
    return {
        "sub": raw.get("sub") or "",
        "email": raw.get("email") or "",
        "email_verified": bool(raw.get("email_verified")),
        "name": raw.get("name") or raw.get("given_name") or "",
        "hd": raw.get("hd") or raw.get("hosted_domain") or "",
        "picture": raw.get("picture") or "",
    }