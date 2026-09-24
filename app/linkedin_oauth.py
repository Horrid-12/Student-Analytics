import os
import tomllib
from pathlib import Path

from authlib.integrations.httpx_client import AsyncOAuth2Client

AUTHORIZATION_ENDPOINT = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_ENDPOINT = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_ENDPOINT = "https://api.linkedin.com/v2/userinfo"
SCOPES = "openid profile email"

_SECRETS_PATH = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"


def load_credentials() -> tuple[str, str]:
    client_id = os.environ.get("LINKEDIN_CLIENT_ID", "").strip()
    client_secret = os.environ.get("LINKEDIN_CLIENT_SECRET", "").strip()
    try:
        with _SECRETS_PATH.open("rb") as handle:
            data = tomllib.load(handle)
        client_id = client_id or str(data.get("LINKEDIN_CLIENT_ID", "") or "").strip()
        client_secret = client_secret or str(data.get("LINKEDIN_CLIENT_SECRET", "") or "").strip()
    except Exception:
        pass
    return client_id, client_secret


def configured() -> bool:
    client_id, client_secret = load_credentials()
    return bool(client_id and client_secret)


def build_authorization_url(redirect_uri: str, state: str) -> str:
    client_id, client_secret = load_credentials()
    client = AsyncOAuth2Client(client_id=client_id, client_secret=client_secret)
    url, _ = client.create_authorization_url(
        AUTHORIZATION_ENDPOINT,
        state=state,
        scope=SCOPES,
        redirect_uri=redirect_uri,
    )
    return url


async def exchange_code(url: str, state: str, redirect_uri: str) -> dict:
    client_id, client_secret = load_credentials()
    client = AsyncOAuth2Client(client_id=client_id, client_secret=client_secret, state=state, token_endpoint_auth_method="client_secret_post")
    token = await client.fetch_token(
        TOKEN_ENDPOINT,
        authorization_response=url,
        redirect_uri=redirect_uri,
    )
    # Fetch user info
    resp = await client.get(USERINFO_ENDPOINT)
    resp.raise_for_status()
    user_info = resp.json()
    
    return {
        "sub": user_info.get("sub"),
        "email": user_info.get("email"),
        "email_verified": user_info.get("email_verified", False),
        "name": user_info.get("name")
    }

