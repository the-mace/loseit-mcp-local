"""App-API login, token refresh, and auth-cookie merge (no network)."""
import json
import stat
from urllib.parse import parse_qs

import httpx
import pytest

import auth
import scraper

EXPIRES = "Wed, 04 Nov 2026 12:39:00 GMT"


def _set_cookies(token: str) -> list[tuple[str, str]]:
    return [
        ("set-cookie", f"fn_authed=1; Domain=loseit.com; Path=/; Expires={EXPIRES}; Secure"),
        ("set-cookie", f"fn_auth={token}; Domain=loseit.com; Path=/; Expires={EXPIRES}; Secure; HttpOnly"),
        ("set-cookie", f"liauth={token}; Domain=loseit.com; Path=/; Expires={EXPIRES}; Secure; HttpOnly"),
        ("set-cookie", "__cf_bm=x; Domain=loseit.com; Path=/"),
    ]


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(access: str, refresh: str = "rt-1"):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.forms.append(parse_qs(request.content.decode()))
        handler.headers.append(request.headers)
        return httpx.Response(
            200,
            headers=_set_cookies(access),
            json={"access_token": access, "refresh_token": refresh, "token_type": "bearer", "expires_in": 1209600},
        )

    handler.forms, handler.headers = [], []
    return handler


def _state_names(path) -> dict:
    return {c["name"]: c for c in json.loads(path.read_text())["cookies"]}


def test_password_login_saves_tokens_private_and_seeds_state(tmp_path):
    handler = _ok("at-1")
    tokens_path, state_path = tmp_path / "tokens.json", tmp_path / "state.json"

    auth.password_login("me@example.com", "pw", state_path, tokens_path, client=_client(handler))

    assert handler.forms[0] == {"grant_type": ["password"], "username": ["me@example.com"], "password": ["pw"]}
    assert handler.headers[0]["x-loseit-device"]
    tokens = json.loads(tokens_path.read_text())
    assert tokens["access_token"] == "at-1" and tokens["refresh_token"] == "rt-1"
    assert "pw" not in tokens_path.read_text()
    assert stat.S_IMODE(tokens_path.stat().st_mode) == 0o600
    cookies = _state_names(state_path)
    assert set(cookies) == {"fn_auth", "liauth", "fn_authed"}
    assert cookies["fn_auth"]["value"] == "at-1"
    assert cookies["fn_auth"]["domain"] == ".loseit.com"
    assert cookies["fn_auth"]["httpOnly"] is True and cookies["fn_authed"]["httpOnly"] is False
    assert cookies["fn_auth"]["expires"] == pytest.approx(1793795940.0)


def test_relogin_keeps_device_id(tmp_path):
    tokens_path, state_path = tmp_path / "tokens.json", tmp_path / "state.json"
    auth.save_tokens({"device_id": "DEV-1", "username": "me@example.com", "access_token": "a", "refresh_token": "r"}, tokens_path)
    handler = _ok("at-2")

    auth.password_login("me@example.com", "pw", state_path, tokens_path, client=_client(handler))

    assert handler.headers[0]["x-loseit-device"] == "DEV-1"
    assert json.loads(tokens_path.read_text())["device_id"] == "DEV-1"


def test_refresh_sends_both_tokens_and_replaces_only_auth_cookies(tmp_path):
    tokens_path, state_path = tmp_path / "tokens.json", tmp_path / "state.json"
    auth.save_tokens({"device_id": "DEV-1", "username": "u", "access_token": "at-old", "refresh_token": "rt-1"}, tokens_path)
    state_path.write_text(json.dumps({
        "cookies": [
            {"name": "fn_auth", "value": "at-old", "domain": ".loseit.com", "expires": 1.0},
            {"name": "JSESSIONID", "value": "keep", "domain": "www.loseit.com", "expires": -1},
        ],
        "origins": [{"origin": "https://www.loseit.com", "localStorage": []}],
    }))
    handler = _ok("at-new")

    assert auth.refresh_session(state_path, tokens_path, client=_client(handler)) is True

    assert handler.forms[0] == {"grant_type": ["refresh_token"], "refresh_token": ["rt-1"], "access_token": ["at-old"]}
    cookies = _state_names(state_path)
    assert cookies["fn_auth"]["value"] == "at-new"
    assert cookies["JSESSIONID"]["value"] == "keep"
    assert json.loads(state_path.read_text())["origins"][0]["origin"] == "https://www.loseit.com"
    assert json.loads(tokens_path.read_text())["access_token"] == "at-new"


def test_refresh_stores_rotated_refresh_token(tmp_path):
    tokens_path = tmp_path / "tokens.json"
    auth.save_tokens({"device_id": "D", "username": "u", "access_token": "a", "refresh_token": "rt-1"}, tokens_path)

    auth.refresh_session(tmp_path / "state.json", tokens_path, client=_client(_ok("a2", refresh="rt-2")))

    assert json.loads(tokens_path.read_text())["refresh_token"] == "rt-2"


def test_refresh_without_tokens_is_a_noop(tmp_path):
    def handler(request):
        raise AssertionError("no request expected")

    assert auth.refresh_session(tmp_path / "state.json", tmp_path / "tokens.json", client=_client(handler)) is False
    assert not (tmp_path / "state.json").exists()


def test_rejected_refresh_raises_and_keeps_saved_tokens(tmp_path):
    tokens_path = tmp_path / "tokens.json"
    saved = {"device_id": "D", "username": "u", "access_token": "a", "refresh_token": "r"}
    auth.save_tokens(saved, tokens_path)

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "expired"})

    with pytest.raises(auth.AuthError, match="HTTP 400: expired"):
        auth.refresh_session(tmp_path / "state.json", tokens_path, client=_client(handler))
    assert json.loads(tokens_path.read_text()) == saved


def test_run_exits_with_login_hint_when_refresh_rejected(tmp_path, monkeypatch, capsys):
    def reject(state_path):
        raise auth.AuthError("LoseIt refresh_token login failed (HTTP 401).")

    monkeypatch.setattr(auth, "refresh_session", reject)
    with pytest.raises(SystemExit) as exc:
        scraper.run(scraper.date(2026, 10, 1), headed=False)
    assert exc.value.code == 1
    assert "scraper.py login" in capsys.readouterr().out


def test_run_falls_through_to_saved_session_on_network_error(tmp_path, monkeypatch, capsys):
    def offline(state_path):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(auth, "refresh_session", offline)
    monkeypatch.setattr(scraper, "STATE_PATH", tmp_path / "missing.json")
    with pytest.raises(SystemExit):
        scraper.run(scraper.date(2026, 10, 1), headed=False)
    out = capsys.readouterr().out
    assert "trying the saved one" in out
    assert "No saved session found" in out
