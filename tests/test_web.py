from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pakt import trakt
from pakt.config import Config, ServerConfig, set_last_sync
from pakt.plex import client as plex_client
from pakt.web import app as web_app
from pakt.web import state
from pakt.web.auth import MAX_FAILURES, hash_password

PASSWORD = "correct horse"


@pytest.fixture(autouse=True)
def fresh_app_state(monkeypatch):
    """Empty the app's module caches, giving each test a fresh config read."""
    monkeypatch.setattr(state, "_config_cache", (0, None))
    monkeypatch.setattr(state, "_cache", {})


def local_client() -> TestClient:
    """A browser on the same machine, opening the UI by loopback address."""
    return TestClient(web_app.create_app(), base_url="http://127.0.0.1:7258", client=("127.0.0.1", 50000))


def remote_client() -> TestClient:
    """A browser on another device on the LAN."""
    return TestClient(web_app.create_app(), base_url="http://192.168.1.5:7258", client=("192.168.1.20", 50000))


def set_password(password: str = PASSWORD) -> None:
    """Set a web UI password the way `pakt password` does."""
    config = Config.load()
    config.web.password_hash = hash_password(password)
    config.web.session_secret = "s" * 32
    config.save()


def log_in(client: TestClient, password: str = PASSWORD):
    """Post the login form from the UI's own origin."""
    return client.post("/login", data={"password": password}, headers={"origin": str(client.base_url)},
                       follow_redirects=False)


def test_local_browser_needs_no_login():
    """A loopback browser opens the dashboard and API straight away."""
    client = local_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 200


def test_remote_browser_without_password_is_told_to_set_one():
    """With no password set, another device is sent to a page explaining `pakt password`."""
    client = remote_client()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert "pakt password" in client.get("/login").text
    assert client.get("/api/status").status_code == 401
    assert log_in(client).status_code == 403


def test_remote_login_with_the_right_password():
    """The right password sets a strict, HTTP-only session cookie that opens the API."""
    set_password()
    client = remote_client()
    response = log_in(client)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/status").status_code == 200
    assert "Log out" in client.get("/").text


def test_wrong_password_is_refused():
    """A wrong password gets the form again with an error and no session."""
    set_password()
    client = remote_client()
    response = log_in(client, "wrong password")
    assert response.status_code == 401 and "Wrong password" in response.text
    assert client.get("/api/status").status_code == 401


def test_repeated_misses_lock_the_address_out():
    """After five misses even the right password waits out the lockout."""
    set_password()
    client = remote_client()
    for _ in range(MAX_FAILURES):
        log_in(client, "wrong password")
    assert log_in(client).status_code == 429


def test_logout_ends_the_session():
    """Logging out clears the cookie, and the API asks for a login again."""
    set_password()
    client = remote_client()
    log_in(client)
    client.post("/logout", headers={"origin": str(client.base_url)})
    assert client.get("/api/status").status_code == 401


def test_new_password_ends_existing_sessions():
    """Setting a new password logs out every device."""
    set_password()
    client = remote_client()
    log_in(client)
    set_password("another password")
    state.invalidate_config_cache()
    assert client.get("/api/status").status_code == 401


def test_proxy_on_this_machine_still_needs_a_login():
    """A request forwarded by a local reverse proxy is treated as remote."""
    client = local_client()
    response = client.get("/api/status", headers={"x-forwarded-for": "203.0.113.4"})
    assert response.status_code == 401


def test_login_page_and_icons_are_public():
    """The login page and icons load without a session."""
    client = remote_client()
    assert client.get("/login").status_code == 200
    assert client.get("/favicon.ico").status_code == 200


def test_local_browser_skips_the_login_page():
    """A loopback browser asking for the login page goes to the dashboard."""
    response = local_client().get("/login", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"


@pytest.mark.parametrize("host", ["evil.example:7258", "127.0.0.1.nip.io:7258"])
def test_unknown_host_names_are_refused(host):
    """A DNS name the user has not allowed gets a 403; DNS rebinding needs one."""
    assert local_client().get("/api/status", headers={"host": host}).status_code == 403


@pytest.mark.parametrize("host", ["localhost:7258", "[::1]:7258", "127.0.0.1:7258"])
def test_loopback_host_names_are_answered(host):
    """Loopback names and addresses always reach the UI."""
    assert local_client().get("/api/status", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("origin, status", [
    ("https://evil.example", 403),
    ("null", 403),
    ("http://127.0.0.1:9999", 403),
    ("http://127.0.0.1:7258", 200),
])
def test_writes_from_other_origins_are_refused(origin, status):
    """A write must come from the UI's own origin; another site's page gets a 403."""
    response = local_client().post("/api/sync/cancel", headers={"origin": origin})
    assert response.status_code == status


def test_writes_without_an_origin_pass():
    """Scripts and the tray send no Origin and are let through."""
    assert local_client().post("/api/sync/cancel").status_code == 200


def test_allowed_proxy_domain_may_write():
    """A proxy that rewrites Host still works when its domain is in web.allowed_hosts."""
    config = Config.load()
    config.web.allowed_hosts = ["pakt.example.com"]
    config.save()
    client = local_client()
    assert client.post("/api/sync/cancel", headers={"origin": "https://pakt.example.com"}).status_code == 200
    assert client.post("/api/sync/cancel", headers={"origin": "http://1.2.3.4"}).status_code == 403


def test_responses_forbid_framing():
    """Every response tells browsers not to frame it or guess its type."""
    headers = local_client().get("/api/status").headers
    assert headers["x-frame-options"] == "DENY"
    assert headers["content-security-policy"] == "frame-ancestors 'none'"
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_off(path):
    """The generated API docs pages are not served."""
    assert local_client().get(path).status_code == 404


def test_trakt_device_code_stays_on_the_server(monkeypatch):
    """The page gets the code to show, but polling uses the code the server stored."""
    async def device_code(self):
        """Stand in for Trakt's device code endpoint."""
        return {"device_code": "real-code", "user_code": "ABCD1234", "verification_url": "https://auth.trakt.tv/activate",
                "expires_in": 600, "interval": 5}

    checked = []

    async def check_device_token(self, code):
        """Approve the device code and record which code was polled."""
        checked.append(code)
        return trakt.DeviceAuthResult(status=trakt.DeviceAuthStatus.SUCCESS, token={
            "access_token": "a", "refresh_token": "r", "created_at": 1, "expires_in": 2})

    monkeypatch.setattr(trakt.TraktClient, "device_code", device_code)
    monkeypatch.setattr(trakt.TraktClient, "check_device_token", check_device_token)
    client = local_client()
    started = client.post("/api/trakt/auth").json()
    assert "device_code" not in started and started["user_code"] == "ABCD1234"
    assert client.post("/api/trakt/auth/poll?device_code=attacker").json()["status"] == "authenticated"
    assert checked == ["real-code"]
    assert Config.load().trakt.access_token == "a"


def test_poll_without_a_login_in_progress_fails():
    """Polling before starting a device login returns an error."""
    assert local_client().post("/api/trakt/auth/poll").json()["status"] == "error"


def test_plex_pin_check_is_post_only():
    """Checking a Plex PIN, which can save a token, needs a POST."""
    assert local_client().get("/api/plex/pin/1").status_code == 405


def add_server_config(name: str = "Home") -> None:
    """Add one server with a token, the way setup leaves it."""
    config = Config.load()
    config.servers = [ServerConfig(name=name, url="http://192.168.1.5:32400", token="SECRET")]
    config.save()


def test_server_list_never_carries_tokens():
    """The servers list shows each server's settings but not its token."""
    add_server_config()
    servers = local_client().get("/api/servers").json()["servers"]
    assert servers[0]["name"] == "Home" and "token" not in servers[0]
    assert "SECRET" not in local_client().get("/api/servers").text


def test_server_overrides_are_set_and_cleared():
    """Overrides sent for a server replace its old ones; a null falls back to the global setting."""
    add_server_config()
    client = local_client()
    client.put("/api/servers/Home", json={"sync": {"watched_plex_to_trakt": False}})
    assert Config.load().servers[0].sync.watched_plex_to_trakt is False
    client.put("/api/servers/Home", json={"sync": {"watched_plex_to_trakt": None}})
    assert Config.load().servers[0].sync.watched_plex_to_trakt is None


def test_config_update_applies_sync_settings_and_rejects_bad_ones():
    """Sent sync settings change and the rest stay; an unknown rating priority is refused."""
    client = local_client()
    client.post("/api/config", json={"watched_plex_to_trakt": False, "rating_priority": "trakt"})
    sync = Config.load().sync
    assert sync.watched_plex_to_trakt is False and sync.rating_priority == "trakt" and sync.watched_trakt_to_plex
    assert client.post("/api/config", json={"rating_priority": "bogus"}).status_code == 422


class ReachablePlex:
    """PlexClient stand-in for a server that answers with two libraries."""

    def __init__(self, server_config, account_token=""):
        self.server = SimpleNamespace(friendlyName="Living Room")

    def connect(self):
        """Connect at once."""

    def get_library_names(self):
        """Get one movie and one show library."""
        return {"movie": ["Movies"], "show": ["TV Shows"]}


def test_server_added_by_url_and_token_can_be_tested(monkeypatch):
    """The setup wizard's manual path adds a server by URL and token, then tests it by name."""
    monkeypatch.setattr(plex_client, "PlexClient", ReachablePlex)
    client = local_client()
    added = client.post("/api/servers", json={"name": "default", "url": "http://192.168.1.5:32400", "token": "t"})
    assert added.json()["status"] == "ok"
    tested = client.post("/api/servers/default/test").json()
    assert tested == {"status": "ok", "server_name": "Living Room",
                      "movie_libraries": ["Movies"], "show_libraries": ["TV Shows"]}


def test_trakt_logout_clears_the_tokens(monkeypatch):
    """Logging out of Trakt revokes the token and forgets both tokens."""
    async def revoke_token(self):
        """Stand in for Trakt accepting the revocation."""
        return True

    monkeypatch.setattr(trakt.TraktClient, "revoke_token", revoke_token)
    config = Config.load()
    config.trakt.access_token, config.trakt.refresh_token = "a", "r"
    config.save()
    assert local_client().post("/api/trakt/logout").json() == {"status": "ok", "revoked": True}
    saved = Config.load().trakt
    assert saved.access_token == "" and saved.refresh_token == ""


def test_scheduler_status_shows_the_last_sync_when_off():
    """With the scheduler off, the status still carries when the last sync finished."""
    finished = datetime(2026, 10, 27, 6, 1, tzinfo=timezone.utc)
    set_last_sync(finished)
    status = local_client().get("/api/scheduler/status").json()
    assert status["next_run"] is None and status["last_run"] == finished.isoformat()


def test_trakt_client_id_cannot_be_changed_over_the_api():
    """The config endpoint ignores Trakt API keys."""
    before = Config.load().trakt.client_id
    local_client().post("/api/config", json={"trakt_client_id": "attacker"})
    assert Config.load().trakt.client_id == before
