import json
import logging
import time
import types

import httpx
import pytest

from pakt import __version__, trakt
from pakt.config import TraktConfig


@pytest.fixture
def sleeps(monkeypatch):
    """Record the client's waits instead of sleeping through them."""
    waits: list[float] = []

    async def no_wait(seconds):
        """Note the wait and return at once."""
        waits.append(round(seconds, 1))

    monkeypatch.setattr(trakt, "asyncio", types.SimpleNamespace(sleep=no_wait))
    return waits


def api_client(handler, **config) -> trakt.TraktClient:
    """A client whose API requests go to a handler instead of Trakt."""
    client = trakt.TraktClient(TraktConfig(access_token="token", **config))
    client._client = httpx.AsyncClient(
        base_url=trakt.TRAKT_API_URL, transport=httpx.MockTransport(handler), headers=client._headers
    )
    return client


@pytest.fixture
def auth_requests(monkeypatch):
    """Send the client's OAuth requests to a handler, keeping each request it receives."""
    seen: list[httpx.Request] = []
    replies: list[httpx.Response] = []

    def handler(request):
        """Record the request and answer with the next queued reply."""
        seen.append(request)
        return replies.pop(0) if replies else httpx.Response(200, json={})

    def auth_client(self, timeout=30.0):
        """Build the OAuth client over the recording handler."""
        headers = {k: v for k, v in self._headers.items() if k != "Authorization"}
        return httpx.AsyncClient(base_url=trakt.TRAKT_AUTH_URL, headers=headers, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(trakt.TraktClient, "_auth_client", auth_client)
    return seen, replies


def test_requests_identify_pakt():
    """Every API request names Pakt and its version, which Trakt asks apps to send."""
    headers = trakt.TraktClient(TraktConfig(access_token="t"))._headers
    assert headers["User-Agent"] == f"pakt/{__version__}"
    assert headers["trakt-api-version"] == "2"
    assert headers["Authorization"] == "Bearer t"


async def test_pages_follow_the_page_count_and_stop_on_an_empty_page(sleeps):
    """Paging walks the reported pages but stops early at an empty one."""
    pages = {1: [1, 2], 2: [3], 3: []}

    def handler(request):
        """Serve numbered pages that claim five in total."""
        page = int(request.url.params["page"])
        return httpx.Response(200, json=pages.get(page, [99]), headers={"X-Pagination-Page-Count": "5"})

    assert await api_client(handler)._paginated_get("/x", limit=250) == [1, 2, 3]


@pytest.mark.parametrize("method, path", [
    ("get_collection_movies", "/sync/collection/movies"),
    ("get_collection_shows", "/sync/collection/shows"),
    ("get_watchlist_movies", "/sync/watchlist/movies"),
    ("get_watchlist_shows", "/sync/watchlist/shows"),
    ("get_watched_movies", "/sync/watched/movies"),
])
async def test_paginated_endpoints_send_page_and_limit(sleeps, method, path):
    """Collection, watchlist and watched reads ask for 250 per page from page 1."""
    seen = []

    def handler(request):
        """Record the query and answer with one empty page."""
        seen.append(request.url)
        return httpx.Response(200, json=[], headers={"X-Pagination-Page-Count": "1"})

    await getattr(api_client(handler), method)()
    assert seen[0].path == path
    assert seen[0].params["limit"] == "250" and seen[0].params["page"] == "1"


async def test_rate_limit_waits_then_backs_off(sleeps):
    """A 429 waits its Retry-After; one without the header backs off before the next try."""
    replies = iter([
        httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(429), httpx.Response(200, json=[]),
    ])
    response = await api_client(lambda r: next(replies))._request("GET", "/x")
    assert response.status_code == 200
    assert sleeps[0] == 4.0 and 10 <= sleeps[1] <= 11


async def test_rate_limit_gives_up_after_three_tries(sleeps):
    """Three 429s in a row raise TraktRateLimitError."""
    with pytest.raises(trakt.TraktRateLimitError):
        await api_client(lambda r: httpx.Response(429))._request("GET", "/x")


async def test_server_errors_retry_then_raise(sleeps, caplog):
    """Overload and Cloudflare errors are retried with a warning in the log, then raised."""
    with pytest.raises(httpx.HTTPStatusError):
        await api_client(lambda r: httpx.Response(522))._request("GET", "/x")
    assert len(sleeps) == 2
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2 and warnings[0].startswith("Trakt HTTP 522 on GET /x, retrying in")


@pytest.mark.parametrize("status, headers, error, text", [
    (401, {}, trakt.TraktAuthError, "pakt login"),
    (403, {}, trakt.TraktAuthError, "API key"),
    (423, {}, trakt.TraktAuthError, "locked"),
    (420, {}, trakt.TraktAccountLimitError, "Account limit exceeded. Upgrade"),
    (420, {"X-Account-Limit": "100", "X-VIP-User": "true"}, trakt.TraktAccountLimitError, "(100 items)"),
])
async def test_error_statuses_raise_clear_errors(sleeps, status, headers, error, text):
    """Auth, key, lock and account limit statuses each raise an error saying what to do."""
    with pytest.raises(error, match=text.replace("(", r"\(").replace(")", r"\)")):
        await api_client(lambda r: httpx.Response(status, headers=headers))._request("GET", "/x")


async def test_writes_are_spaced_a_second_apart(sleeps):
    """Back-to-back writes wait out Trakt's one-write-per-second limit."""
    client = api_client(lambda r: httpx.Response(201, json={}))
    for _ in range(3):
        await client._request("POST", "/sync/ratings", json={})
    assert len(sleeps) == 2 and all(0.9 <= s <= 1.0 for s in sleeps)


async def test_sync_posts_are_batched_and_responses_summed(sleeps):
    """Large writes go in batches per kind, and the batch responses add up."""
    batches = []

    def handler(request):
        """Answer each batch with its size and one unmatched item."""
        body = json.loads(request.content)
        kind = next(iter(body))
        batches.append((kind, len(body[kind])))
        return httpx.Response(201, json={"added": {kind: len(body[kind])}, "not_found": {kind: [{"title": "x"}]}})

    total = await api_client(handler).add_to_history(movies=[{}] * 1200, shows=[{}] * 150)
    assert batches == [("movies", 500), ("movies", 500), ("movies", 200), ("shows", 100), ("shows", 50)]
    assert total["added"] == {"movies": 1200, "shows": 150}
    assert len(total["not_found"]["movies"]) == 3


async def test_empty_writes_send_nothing(sleeps):
    """Nothing to send makes no request."""
    def handler(request):
        """Fail the test if any request is made."""
        raise AssertionError("no request expected")

    assert await api_client(handler).add_ratings(movies=[]) == {}


def refreshable(hours_left: float, saved: list) -> trakt.TraktClient:
    """A client inside the refresh window, saving any rotated token to a list."""
    config = TraktConfig(access_token="old", refresh_token="r", expires_at=int(time.time() + hours_left * 3600))
    return trakt.TraktClient(config, on_token_refresh=saved.append)


async def test_refresh_saves_the_rotated_token(auth_requests, caplog):
    """A good refresh updates the client, hands the new token to the callback and logs it."""
    seen, replies = auth_requests
    token = {"access_token": "new", "refresh_token": "r2", "created_at": 1, "expires_in": 2}
    replies.append(httpx.Response(200, json=token))
    saved: list = []
    client = refreshable(10, saved)
    await client._ensure_valid_token()
    assert client.config.access_token == "new" and saved == [token]
    assert "Trakt token refreshed" in caplog.messages
    body = json.loads(seen[0].content)
    assert seen[0].url.host == "auth.trakt.tv" and "client_secret" not in body


async def test_refused_refresh_keeps_a_working_token(auth_requests, caplog):
    """A refused refresh keeps the current token while it has time left, and warns how long."""
    _, replies = auth_requests
    replies.append(httpx.Response(400, json={"error": "invalid_grant", "error_description": "session not found"}))
    client = refreshable(10, [])
    await client._ensure_valid_token()
    assert client.config.access_token == "old"
    assert any("session not found" in m and "still works for 10h" in m for m in caplog.messages)


async def test_refused_refresh_of_an_expired_token_raises(auth_requests):
    """A refused refresh with no time left raises with Trakt's reason."""
    _, replies = auth_requests
    replies.append(httpx.Response(400, json={"error": "invalid_grant", "error_description": "session not found"}))
    with pytest.raises(trakt.TraktAuthError, match="session not found"):
        await refreshable(-1, [])._ensure_valid_token()


async def test_unexpected_refresh_reply_keeps_the_token(auth_requests):
    """A 200 that is not a token, such as a captcha page, leaves the stored token alone."""
    _, replies = auth_requests
    replies.append(httpx.Response(200, text="<html>captcha</html>"))
    saved: list = []
    client = refreshable(10, saved)
    await client._ensure_valid_token()
    assert client.config.access_token == "old" and saved == []


async def test_revoke_goes_to_the_auth_host_without_secret(auth_requests):
    """Logout revokes on auth.trakt.tv with the client ID and no bearer or secret."""
    seen, _ = auth_requests
    await trakt.TraktClient(TraktConfig(access_token="tok")).revoke_token()
    request = seen[0]
    assert str(request.url) == "https://auth.trakt.tv/oauth/revoke"
    assert "authorization" not in request.headers
    assert sorted(json.loads(request.content)) == ["client_id", "token"]


@pytest.mark.parametrize("status, expected", [
    (200, trakt.DeviceAuthStatus.SUCCESS),
    (400, trakt.DeviceAuthStatus.PENDING),
    (404, trakt.DeviceAuthStatus.INVALID_CODE),
    (409, trakt.DeviceAuthStatus.INVALID_CODE),
    (410, trakt.DeviceAuthStatus.EXPIRED),
    (418, trakt.DeviceAuthStatus.DENIED),
    (429, trakt.DeviceAuthStatus.RATE_LIMITED),
])
async def test_device_token_statuses(auth_requests, status, expected):
    """Each device token status Trakt documents maps to its own result."""
    seen, replies = auth_requests
    replies.append(httpx.Response(status, json={"access_token": "a"} if status == 200 else {}))
    result = await trakt.TraktClient(TraktConfig()).check_device_token("code")
    assert result.status == expected
    assert "client_secret" not in json.loads(seen[0].content)
