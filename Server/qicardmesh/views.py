import json

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from django.http import HttpRequest
from django.views.decorators.csrf import csrf_exempt

from .auth import bearer_required
from .response import response_base
from .tokens import issue_tokens, refresh_tokens, revoke_session


def _json_body(request: HttpRequest):
    try:
        value = json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _method_error():
    return response_base(None, code="METHOD_NOT_ALLOWED", message="请求方法不支持", status=405)


def _user_data(user):
    return {"id": user.id, "username": user.get_username(), "email": user.email}


@csrf_exempt
def login(request):
    if request.method != "POST":
        return _method_error()
    payload = _json_body(request)
    if payload is None:
        return response_base(None, code="INVALID_JSON", message="请求体必须是 JSON 对象", status=400)

    username = str(payload.get("username", "")).strip()
    password = payload.get("password")
    if not username or not isinstance(password, str) or not password:
        return response_base(None, code="INVALID_ARGUMENT", message="username 和 password 不能为空", status=400)

    user_model = get_user_model()
    login_name = username
    if "@" in username:
        user = user_model.objects.filter(email__iexact=username, is_active=True).first()
        if user:
            login_name = user.get_username()
    user = authenticate(request, username=login_name, password=password)
    if user is None or not user.is_active:
        return response_base(None, code="AUTH_INVALID_CREDENTIALS", message="用户名或密码错误", status=401)

    pair = issue_tokens(user.id)
    return response_base(
        {
            "token_type": "Bearer",
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "expires_in": settings.ACCESS_TOKEN_TTL,
            "user": _user_data(user),
        },
        message="登录成功",
    )


@csrf_exempt
def refresh(request):
    if request.method != "POST":
        return _method_error()
    payload = _json_body(request)
    if payload is None:
        return response_base(None, code="INVALID_JSON", message="请求体必须是 JSON 对象", status=400)
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        return response_base(None, code="INVALID_ARGUMENT", message="refresh_token 不能为空", status=400)

    pair = refresh_tokens(refresh_token.strip())
    if pair is None:
        return response_base(None, code="AUTH_REFRESH_INVALID", message="refresh_token 无效或已过期", status=401)

    return response_base(
        {
            "token_type": "Bearer",
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "expires_in": settings.ACCESS_TOKEN_TTL,
        },
        message="刷新成功",
    )


@csrf_exempt
@bearer_required
def logout(request):
    if request.method != "POST":
        return _method_error()
    revoke_session(request.api_access_token, request.api_access_session)
    return response_base(None, message="退出登录成功")


@bearer_required
def api_root(request):
    if request.method != "GET":
        return _method_error()
    return response_base({"user": _user_data(request.api_user)}, message="认证成功")
