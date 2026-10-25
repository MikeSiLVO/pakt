"""Password login for the web UI when it is reached from another device."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections.abc import Mapping
from urllib.parse import urlsplit

SESSION_COOKIE = "pakt_session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD_LENGTH = 8
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip")


def hash_password(password: str) -> str:
    """Hash a password with scrypt and a random salt, in the form config.json stores."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    """True when the password matches a hash made by hash_password."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except ValueError:
        return False
    return hmac.compare_digest(candidate.hex(), digest)


def _signature(expires: str, secret: str, password_hash: str) -> str:
    """Sign a session's expiry with the password and secret, voiding it when either changes."""
    return hmac.new(f"{secret}:{password_hash}".encode(), expires.encode(), hashlib.sha256).hexdigest()


def make_session(secret: str, password_hash: str) -> str:
    """Make a signed session token that lasts 30 days."""
    expires = str(int(time.time()) + SESSION_SECONDS)
    return f"{expires}.{_signature(expires, secret, password_hash)}"


def session_valid(token: str, secret: str, password_hash: str) -> bool:
    """True when a session token is unexpired and signed for the current password."""
    expires, _, signature = token.partition(".")
    if not secret or not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(signature, _signature(expires, secret, password_hash))


def is_local(client_host: str | None, host_header: str, headers: Mapping[str, str]) -> bool:
    """True for a request from this machine by a loopback name, with no proxy in between."""
    if any(name in headers for name in PROXY_HEADERS):
        return False
    try:
        if not ipaddress.ip_address(client_host or "").is_loopback:
            return False
    except ValueError:
        return False
    name = urlsplit(f"//{host_header}").hostname or ""
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


class LoginThrottle:
    """Failed logins per address, locking an address out for a minute after five misses."""

    def __init__(self) -> None:
        self._failures: dict[str, tuple[int, float]] = {}

    def locked(self, address: str) -> bool:
        """Whether the address is still locked out."""
        count, since = self._failures.get(address, (0, 0.0))
        return count >= MAX_FAILURES and time.time() - since < LOCKOUT_SECONDS

    def record_failure(self, address: str) -> None:
        """Count a failed login, starting the count over after a full lockout."""
        count, _ = self._failures.get(address, (0, 0.0))
        if count >= MAX_FAILURES:
            count = 0
        self._failures[address] = (count + 1, time.time())

    def clear(self, address: str) -> None:
        """Forget an address's failures after it logs in."""
        self._failures.pop(address, None)
