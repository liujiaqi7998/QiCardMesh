import hashlib
import hmac
import secrets
import time
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from pydantic import ValidationError

from .schemas import AccessSession, RefreshSession, TokenPair


ACCESS_PREFIX = "qicardmesh:auth:access:"
REFRESH_PREFIX = "qicardmesh:auth:refresh:"
ACCESS_TOKEN_PREFIX = "access_"
REFRESH_TOKEN_PREFIX = "refresh_"


def _digest(token: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _access_key(token: str) -> str:
    return f"{ACCESS_PREFIX}{_digest(token)}"


def _refresh_key(token: str) -> str:
    return f"{REFRESH_PREFIX}{_digest(token)}"


def issue_tokens(user_id: int) -> TokenPair:
    session_id = uuid.uuid4().hex
    access_token = f"{ACCESS_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    refresh_token = f"{REFRESH_TOKEN_PREFIX}{secrets.token_urlsafe(48)}"
    access_digest = _digest(access_token)
    refresh_digest = _digest(refresh_token)
    access_expires_at = int(time.time()) + settings.ACCESS_TOKEN_TTL

    cache.set(
        _access_key(access_token),
        AccessSession(user_id=user_id, session_id=session_id, refresh_digest=refresh_digest,
                      expires_at=access_expires_at).model_dump(),
        timeout=settings.ACCESS_TOKEN_TTL,
    )
    cache.set(
        _refresh_key(refresh_token),
        RefreshSession(user_id=user_id, session_id=session_id, access_digest=access_digest).model_dump(),
        timeout=settings.REFRESH_TOKEN_TTL,
    )
    return TokenPair(access_token=access_token, refresh_token=refresh_token, session_id=session_id)


def get_access_session(token: str | None) -> AccessSession | None:
    if not token:
        return None
    value = cache.get(_access_key(token))
    try:
        return AccessSession.model_validate(value) if value is not None else None
    except ValidationError:
        return None


def get_refresh_session(token: str | None) -> RefreshSession | None:
    if not token:
        return None
    value = cache.get(_refresh_key(token))
    try:
        return RefreshSession.model_validate(value) if value is not None else None
    except ValidationError:
        return None


def revoke_access(token: str) -> None:
    if token:
        cache.delete(_access_key(token))


def revoke_refresh(token: str) -> None:
    if token:
        cache.delete(_refresh_key(token))


def revoke_session(access_token: str, access_session: AccessSession | None) -> None:
    """Revoke an access token and its paired refresh token."""
    revoke_access(access_token)
    if access_session:
        refresh_digest = access_session.refresh_digest
        if refresh_digest:
            cache.delete(f"{REFRESH_PREFIX}{refresh_digest}")


def refresh_tokens(refresh_token: str):
    """Rotate a refresh token and return a new pair, or ``None`` if invalid."""
    session = get_refresh_session(refresh_token)
    if not session:
        return None

    user_id = session.user_id
    if not user_id or not get_user_model().objects.filter(id=user_id, is_active=True).exists():
        revoke_refresh(refresh_token)
        return None

    # Rotation makes a refresh token single-use and invalidates the old access token.
    revoke_refresh(refresh_token)
    old_access_digest = session.access_digest
    if old_access_digest:
        cache.delete(f"{ACCESS_PREFIX}{old_access_digest}")
    return issue_tokens(user_id)
