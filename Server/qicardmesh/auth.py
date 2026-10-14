from functools import wraps

from django.contrib.auth import get_user_model
from redis.exceptions import RedisError

from config.error import ErrorCode

from .response import response_base
from .tokens import get_access_session


def bearer_token(request):
    value = request.headers.get("Authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def bearer_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        token = bearer_token(request)
        try:
            session = get_access_session(token)
        except RedisError:
            return response_base(None, code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
        if not session:
            return response_base(
                None,
                code=ErrorCode.TOKEN_INVALID,
                status=401,
            )

        user = get_user_model().objects.filter(id=session.get("user_id"), is_active=True).first()
        if user is None:
            return response_base(
                None,
                code=ErrorCode.USER_INVALID,
                status=401,
            )
        request.api_user = user
        request.api_access_token = token
        request.api_access_session = session
        return view(request, *args, **kwargs)

    return wrapped
