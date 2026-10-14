"""Redis-backed throttling for password login failures."""

import hashlib

from django.core.cache import cache


LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCKOUT_SECONDS = 60
_COUNTER_PREFIX = "qicardmesh:login-failures:"
_LOCK_PREFIX = "qicardmesh:login-lock:"


def _identifier_digest(identifier: str, scope: str) -> str:
    normalized = identifier.strip().casefold()
    return hashlib.sha256(f"{scope}:{normalized}".encode("utf-8")).hexdigest()


def _counter_key(identifier: str, scope: str) -> str:
    return _COUNTER_PREFIX + _identifier_digest(identifier, scope)


def _lock_key(identifier: str, scope: str) -> str:
    return _LOCK_PREFIX + _identifier_digest(identifier, scope)


def is_login_locked(identifier: str, scope: str = "password") -> bool:
    """Return whether the selected login method is locked for this identifier."""
    return cache.get(_lock_key(identifier, scope)) is not None


def record_login_failure(identifier: str, scope: str = "password") -> int:
    """Record one failure and lock the identifier after five failures."""
    key = _counter_key(identifier, scope)
    # add() initializes the counter atomically when the first request arrives;
    # incr() remains atomic for concurrent requests in Redis.
    cache.add(key, 0, timeout=LOGIN_LOCKOUT_SECONDS)
    count = cache.incr(key)
    if count >= LOGIN_FAILURE_LIMIT:
        cache.set(_lock_key(identifier, scope), True, timeout=LOGIN_LOCKOUT_SECONDS)
    return count


def clear_login_failures(identifier: str, scope: str = "password") -> None:
    cache.delete(_counter_key(identifier, scope))
    cache.delete(_lock_key(identifier, scope))
