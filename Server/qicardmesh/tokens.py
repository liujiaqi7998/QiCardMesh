import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError
from redis.exceptions import RedisError

from .models import LoginDevice
from .schemas import AccessSession, RefreshSession, TokenPair


logger = logging.getLogger(__name__)
ACCESS_PREFIX = "qicardmesh:auth:access:"
REFRESH_PREFIX = "qicardmesh:auth:refresh:"
ACCESS_TOKEN_PREFIX = "access_"
REFRESH_TOKEN_PREFIX = "refresh_"


def _digest(token: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), token.encode(), hashlib.sha256).hexdigest()


def _access_key(token: str) -> str:
    return ACCESS_PREFIX + _digest(token)


def _refresh_key(token: str) -> str:
    return REFRESH_PREFIX + _digest(token)


def _new_pair():
    now = timezone.now()
    pair = TokenPair(
        access_token=ACCESS_TOKEN_PREFIX + secrets.token_urlsafe(32),
        refresh_token=REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(48),
        session_id=uuid.uuid4().hex,
    )
    fields = {
        "session_id": pair.session_id,
        "access_digest": _digest(pair.access_token),
        "refresh_digest": _digest(pair.refresh_token),
        "access_expires_at": now + timedelta(seconds=settings.ACCESS_TOKEN_TTL),
        "expires_at": now + timedelta(seconds=settings.REFRESH_TOKEN_TTL),
        "last_seen_at": now,
    }
    return pair, fields


def _cache_pair(user_id, pair, fields):
    cache.set(_access_key(pair.access_token), AccessSession(
        user_id=user_id, session_id=pair.session_id, refresh_digest=fields["refresh_digest"],
        expires_at=int(fields["access_expires_at"].timestamp()),
    ).model_dump(), timeout=settings.ACCESS_TOKEN_TTL)
    cache.set(_refresh_key(pair.refresh_token), RefreshSession(
        user_id=user_id, session_id=pair.session_id, access_digest=fields["access_digest"],
    ).model_dump(), timeout=settings.REFRESH_TOKEN_TTL)


def _discard_cache(access_digest, refresh_digest):
    # Database revocation is authoritative. Failed cleanup must not resurrect
    # a deleted/replaced device or make a successful revocation look failed.
    try:
        cache.delete_many([ACCESS_PREFIX + access_digest, REFRESH_PREFIX + refresh_digest])
    except RedisError:
        logger.warning("Revoked token cache cleanup failed; entries will expire")


def issue_tokens(user_id: int, *, device_id: str, platform: str = "", client_ip=None) -> TokenPair:
    pair, fields = _new_pair()
    old = None
    try:
        with transaction.atomic():
            old = LoginDevice.objects.filter(user_id=user_id, device_id=device_id).values(
                "access_digest", "refresh_digest").first()
            LoginDevice.objects.update_or_create(
                user_id=user_id, device_id=device_id,
                defaults={**fields, "platform": platform, "client_ip": client_ip,
                          "login_at": fields["last_seen_at"]},
            )
            _cache_pair(user_id, pair, fields)
    except Exception:
        _discard_cache(fields["access_digest"], fields["refresh_digest"])
        raise
    if old:
        _discard_cache(**old)
    return pair


def _read_session(token, key, model):
    if not token:
        return None
    value = cache.get(key(token))
    try:
        return model.model_validate(value) if value is not None else None
    except ValidationError:
        return None


def get_access_session(token: str | None) -> AccessSession | None:
    session = _read_session(token, _access_key, AccessSession)
    if session is None:
        return None
    now = timezone.now()
    device = LoginDevice.objects.filter(
        user_id=session.user_id, session_id=session.session_id,
        access_digest=_digest(token), refresh_digest=session.refresh_digest,
        access_expires_at__gt=now, expires_at__gt=now,
    )
    last_seen_at = device.values_list("last_seen_at", flat=True).first()
    if last_seen_at is None:
        return None
    # Throttle activity writes; the same predicate prevents reviving a session
    # deleted/replaced while this request was being authenticated.
    if last_seen_at < now - timedelta(seconds=60):
        device.filter(last_seen_at__lt=now - timedelta(seconds=60)).update(last_seen_at=now)
    return session


def get_refresh_session(token: str | None) -> RefreshSession | None:
    session = _read_session(token, _refresh_key, RefreshSession)
    if session is None or not LoginDevice.objects.filter(
        user_id=session.user_id, session_id=session.session_id,
        refresh_digest=_digest(token), access_digest=session.access_digest,
        expires_at__gt=timezone.now(),
    ).exists():
        return None
    return session


def delete_device(user_id, device_id):
    """Delete only an owned device; an already missing/foreign ID returns False."""
    with transaction.atomic():
        device = LoginDevice.objects.filter(pk=device_id, user_id=user_id)
        old = device.values("access_digest", "refresh_digest").first()
        if old is None:
            return False
        deleted, _ = device.delete()
    if deleted:
        _discard_cache(**old)
    return bool(deleted)


def revoke_session(access_token: str, access_session: AccessSession | None) -> None:
    if access_session:
        # Match the generation so a late logout cannot remove a newer login.
        LoginDevice.objects.filter(user_id=access_session.user_id,
                                   session_id=access_session.session_id).delete()
        _discard_cache(_digest(access_token), access_session.refresh_digest)


def refresh_tokens(refresh_token: str):
    session = get_refresh_session(refresh_token)
    if session is None:
        return None
    pair, fields = _new_pair()
    try:
        with transaction.atomic():
            # Compare-and-swap: exactly one rotation wins, including concurrent
            # requests. Never create a row here: a kicked device stays revoked.
            updated = LoginDevice.objects.filter(
                user_id=session.user_id, user__is_active=True,
                session_id=session.session_id, refresh_digest=_digest(refresh_token),
                expires_at__gt=timezone.now(),
            ).update(**fields)
            if not updated:
                return None
            _cache_pair(session.user_id, pair, fields)
    except Exception:
        _discard_cache(fields["access_digest"], fields["refresh_digest"])
        raise
    _discard_cache(session.access_digest, _digest(refresh_token))
    return pair
