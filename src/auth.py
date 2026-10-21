"""
LoseIt auth through the iOS app's OAuth endpoint.

Observed by capturing the Lose It! iOS app's traffic (Oct 2026):
POST sync.loseit.com/account/login with a form body. grant_type=password
takes username/password; grant_type=refresh_token takes refresh_token and
also requires the current access_token (without it the server answers 400
"INVALID_ARGUMENT: missing access_token"). Both return JSON
{access_token, refresh_token, expires_in, ...} where the access token is a
14-day JWT, and both Set-Cookie fn_auth/liauth/fn_authed on .loseit.com
carrying that same access token, which is what www.loseit.com reads. A
refresh restarts the 14-day clock and, so far, returns the same refresh
token. Unlike the web login form, this endpoint has no reCAPTCHA.

Tokens live in ~/.loseit-data/tokens.json (mode 600). The password is
never stored.
"""
import json
import os
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path

import httpx

LOGIN_URL = "https://sync.loseit.com/account/login"
TOKENS_PATH = Path.home() / ".loseit-data" / "tokens.json"
AUTH_COOKIE_NAMES = frozenset({"fn_auth", "liauth", "fn_authed"})
# Headers copied from the iOS app (running on a Mac) so requests look like
# the client this endpoint was built for.
APP_VERSION = "18.5.400.8831"
USER_AGENT = f"LoseIt!/{APP_VERSION} (iOS 27.0; iPad8,6)"

LOGIN_HINT = (
    "Run `.venv/bin/python src/scraper.py login`, "
    "then `.venv/bin/python src/scraper.py run` to catch up."
)


class AuthError(RuntimeError):
    pass


def _headers(device_id: str) -> dict:
    offset = datetime.now().astimezone().utcoffset()
    return {
        "x-Loseit-Device-Type": "iPhone",
        "x-Loseit-Version": APP_VERSION,
        "x-Loseit-Device": device_id,
        "x-Loseit-HoursFromGMT": str(round(offset.total_seconds() / 3600)),
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }


def _cookies_from(resp: httpx.Response) -> list[dict]:
    """Auth Set-Cookie headers as Playwright storage_state cookies."""
    out = []
    for raw in resp.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(raw)
        for name, m in parsed.items():
            if name not in AUTH_COOKIE_NAMES:
                continue
            domain = m["domain"] or "loseit.com"
            expires = parsedate_to_datetime(m["expires"]).timestamp() if m["expires"] else -1
            out.append({
                "name": name,
                "value": m.value,
                "domain": "." + domain.lstrip("."),
                "path": m["path"] or "/",
                "expires": expires,
                "httpOnly": bool(m["httponly"]),
                "secure": bool(m["secure"]),
                "sameSite": (m["samesite"] or "Lax").capitalize(),
            })
    return out


def _token_request(form: dict, device_id: str, client: httpx.Client | None) -> tuple[dict, list[dict]]:
    owns = client is None
    client = client or httpx.Client(timeout=30)
    try:
        resp = client.post(LOGIN_URL, headers=_headers(device_id), data=form)
    finally:
        if owns:
            client.close()
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error_description") or resp.json().get("error")
        except (ValueError, AttributeError):
            detail = None
        raise AuthError(
            f"LoseIt {form['grant_type']} login failed (HTTP {resp.status_code}"
            + (f": {detail}" if detail else "")
            + ")."
        )
    body = resp.json()
    if "access_token" not in body:
        raise AuthError("LoseIt login response had no access_token.")
    return body, _cookies_from(resp)


def load_tokens(path: Path = TOKENS_PATH) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_tokens(tokens: dict, path: Path = TOKENS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(path, 0o600)


def write_auth_cookies(state_path: Path, cookies: list[dict]) -> None:
    """Replace the auth cookies in a Playwright storage_state file, keeping
    every other cookie and origin. Creates the file if missing."""
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {"cookies": [], "origins": []}
    names = {c["name"] for c in cookies}
    state["cookies"] = [c for c in state.get("cookies", []) if c.get("name") not in names] + cookies
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)


def password_login(
    username: str,
    password: str,
    state_path: Path,
    tokens_path: Path = TOKENS_PATH,
    client: httpx.Client | None = None,
) -> None:
    """Log in with email/password, save tokens, and seed the browser session."""
    existing = load_tokens(tokens_path) or {}
    device_id = existing.get("device_id") or str(uuid.uuid4()).upper()
    body, cookies = _token_request(
        {"grant_type": "password", "username": username, "password": password},
        device_id,
        client,
    )
    save_tokens(
        {
            "device_id": device_id,
            "username": username,
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
        },
        tokens_path,
    )
    write_auth_cookies(state_path, cookies)


def refresh_session(
    state_path: Path,
    tokens_path: Path = TOKENS_PATH,
    client: httpx.Client | None = None,
) -> bool:
    """Trade the saved refresh token for a new 14-day access token and write
    it into the browser session. Returns False when there are no saved
    tokens (browser-only login); raises AuthError when LoseIt rejects them."""
    tokens = load_tokens(tokens_path)
    if not tokens or not tokens.get("refresh_token"):
        return False
    body, cookies = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "access_token": tokens["access_token"],
        },
        tokens["device_id"],
        client,
    )
    tokens["access_token"] = body["access_token"]
    tokens["refresh_token"] = body.get("refresh_token") or tokens["refresh_token"]
    save_tokens(tokens, tokens_path)
    write_auth_cookies(state_path, cookies)
    return True
