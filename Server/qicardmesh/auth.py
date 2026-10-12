from functools import wraps

from django.contrib.auth import get_user_model

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
        session = get_access_session(token)
        if not session:
            return response_base(
                None,
                code="AUTH_TOKEN_INVALID",
                message="access_token 无效或已过期",
                status=401,
            )

        user = get_user_model().objects.filter(id=session.get("user_id"), is_active=True).first()
        if user is None:
            return response_base(
                None,
                code="AUTH_USER_INVALID",
                message="用户不存在或已停用",
                status=401,
            )
        request.api_user = user
        request.api_access_token = token
        request.api_access_session = session
        return view(request, *args, **kwargs)

    return wrapped
