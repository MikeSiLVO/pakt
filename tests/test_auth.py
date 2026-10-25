import time

import pytest

from pakt.web import auth


def test_password_round_trip():
    """A stored hash accepts its own password and nothing else."""
    stored = auth.hash_password("correct horse")
    assert auth.check_password("correct horse", stored)
    assert not auth.check_password("wrong horse", stored)


def test_password_hashes_are_salted():
    """The same password hashes differently each time."""
    assert auth.hash_password("same password") != auth.hash_password("same password")


@pytest.mark.parametrize("stored", ["", "plain", "md5$1$2$3$aa$bb", "scrypt$x$8$1$aa$bb"])
def test_malformed_hash_never_matches(stored):
    """A hash Pakt did not write is refused rather than raising."""
    assert not auth.check_password("anything", stored)


def test_session_is_valid_until_the_password_changes():
    """A session signed for one password stops working once the password changes."""
    token = auth.make_session("secret", "hash-a")
    assert auth.session_valid(token, "secret", "hash-a")
    assert not auth.session_valid(token, "secret", "hash-b")
    assert not auth.session_valid(token, "other-secret", "hash-a")


def test_expired_or_forged_sessions_fail(monkeypatch):
    """An expired, forged or garbled session is refused."""
    token = auth.make_session("secret", "hash")
    expires, _, signature = token.partition(".")
    assert not auth.session_valid(f"{int(expires) + 1}.{signature}", "secret", "hash")
    assert not auth.session_valid("garbage", "secret", "hash")
    assert not auth.session_valid(token, "", "hash")
    later = time.time() + auth.SESSION_SECONDS + 10
    monkeypatch.setattr(auth.time, "time", lambda: later)
    assert not auth.session_valid(token, "secret", "hash")


@pytest.mark.parametrize("client, host, headers, local", [
    ("127.0.0.1", "127.0.0.1:7258", {}, True),
    ("127.0.0.1", "localhost:7258", {}, True),
    ("::1", "[::1]:7258", {}, True),
    ("192.168.1.20", "192.168.1.5:7258", {}, False),
    ("127.0.0.1", "pakt.example.com", {}, False),
    ("127.0.0.1", "127.0.0.1:7258", {"x-forwarded-for": "203.0.113.4"}, False),
    ("127.0.0.1", "127.0.0.1:7258", {"forwarded": "for=203.0.113.4"}, False),
    ("testclient", "127.0.0.1:7258", {}, False),
    (None, "127.0.0.1:7258", {}, False),
])
def test_only_direct_loopback_requests_count_as_local(client, host, headers, local):
    """Local means loopback on both ends with no proxy headers; anything else must log in."""
    assert auth.is_local(client, host, headers) is local


def test_throttle_locks_after_five_misses_then_lifts(monkeypatch):
    """Five misses lock an address for a minute; a later miss starts the count over."""
    now = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    throttle = auth.LoginThrottle()
    for _ in range(auth.MAX_FAILURES - 1):
        throttle.record_failure("1.2.3.4")
    assert not throttle.locked("1.2.3.4")
    throttle.record_failure("1.2.3.4")
    assert throttle.locked("1.2.3.4")
    assert not throttle.locked("5.6.7.8")
    now[0] += auth.LOCKOUT_SECONDS + 1
    assert not throttle.locked("1.2.3.4")
    throttle.record_failure("1.2.3.4")
    assert not throttle.locked("1.2.3.4")


def test_throttle_clears_on_success():
    """A successful login forgets earlier misses."""
    throttle = auth.LoginThrottle()
    for _ in range(auth.MAX_FAILURES):
        throttle.record_failure("1.2.3.4")
    throttle.clear("1.2.3.4")
    assert not throttle.locked("1.2.3.4")
