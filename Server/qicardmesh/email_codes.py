"""Email login codes in the configured Redis cache; no plaintext codes at rest."""

import hashlib
import hmac
import math
import secrets
import smtplib
import time
import uuid

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.template.loader import render_to_string

from config.error import ErrorCode


class EmailCodeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code.name)


def normalize_email(email):
    if not isinstance(email, str):
        raise ValidationError('Invalid email')
    email = email.strip().lower()
    validate_email(email)
    return email


def _key(email):
    return 'qicardmesh:auth:email-code:' + hashlib.sha256(email.encode()).hexdigest()


def _digest(email, generation, code):
    return hmac.new(
        settings.SECRET_KEY.encode(), f'{email}:{generation}:{code}'.encode(), hashlib.sha256
    ).hexdigest()


def send_email_code(email):
    """Send a code and store its email-bound digest, enforcing a shared cooldown."""
    email = normalize_email(email)
    key = _key(email)
    if not cache.add(key + ':cooldown', True, timeout=settings.EMAIL_CODE_SEND_INTERVAL):
        raise EmailCodeError(ErrorCode.EMAIL_SEND_TOO_FAST)

    code = f'{secrets.randbelow(1_000_000):06d}'
    generation = uuid.uuid4().hex
    html = render_to_string('qicardmesh/email_code.html', {
        'email_code': code,
        'expires_minutes': math.ceil(settings.EMAIL_CODE_TTL / 60),
    })
    message = EmailMultiAlternatives(
        subject='Quarbor Logic 奇桦科技-验证码',
        body=f'您的登录验证码是：{code}。{settings.EMAIL_CODE_TTL // 60} 分钟内有效，请勿转发。',
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
    )
    message.attach_alternative(html, 'text/html')
    try:
        if not settings.DEFAULT_FROM_EMAIL or message.send() != 1:
            raise EmailCodeError(ErrorCode.EMAIL_SEND_FAILED)
    except (smtplib.SMTPException, OSError, ImproperlyConfigured) as exc:
        # Retain the cooldown on failure to prevent retry storms.
        raise EmailCodeError(ErrorCode.EMAIL_SEND_FAILED) from exc

    cache.set(key, {
        'digest': _digest(email, generation, code),
        'generation': generation,
        'expires_at': time.time() + settings.EMAIL_CODE_TTL,
    }, timeout=settings.EMAIL_CODE_TTL)


def verify_email_code(email, email_code):
    """Consume a valid code once; return False for expiry, mismatch or exhaustion.

    Atomic cache.add claims and generation-specific keys prevent concurrent
    requests from using the same code twice or consuming a newer resend.
    Redis errors propagate: callers must fail closed, never accept a code.
    """
    try:
        email = normalize_email(email)
    except ValidationError:
        return False
    if not isinstance(email_code, str) or len(email_code) != 6 or not email_code.isascii() or not email_code.isdigit():
        return False
    key = _key(email)
    record = cache.get(key)
    if not record:
        return False
    remaining = math.ceil(record['expires_at'] - time.time())
    if remaining <= 0:
        return False
    generation_key = key + ':' + record['generation']
    if cache.get(generation_key + ':used'):
        return False
    # add() and incr() are atomic in Django's Redis cache backend.
    attempts_key = generation_key + ':attempts'
    cache.add(attempts_key, 0, timeout=remaining)
    try:
        attempts = cache.incr(attempts_key)
    except ValueError:  # Expired between reading the record and incrementing.
        return False
    if attempts > settings.EMAIL_CODE_MAX_ATTEMPTS:
        return False
    if not hmac.compare_digest(record['digest'], _digest(email, record['generation'], email_code)):
        return False
    return cache.add(generation_key + ':used', True, timeout=remaining)
