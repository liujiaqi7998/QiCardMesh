import json
import time

from django.conf import settings
from django.contrib.auth import authenticate, update_session_auth_hash
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from redis.exceptions import RedisError

from config.error import ErrorCode

from .auth import bearer_required
from .email_codes import EmailCodeError, normalize_email, send_email_code, verify_email_code
from .response import response_base
from .tokens import get_access_session, issue_tokens, refresh_tokens, revoke_session


def _json_body(request: HttpRequest):
    try:
        value = json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _method_error():
    return response_base(None, code=ErrorCode.METHOD_NOT_ALLOWED)


def _user_data(user):
    return {"id": user.id, "username": user.get_username(), "email": user.email}


def _mqtt_deny():
    return JsonResponse(
        {"result": "deny", "is_superuser": False, "expire_at": 0, "acl": []}
    )


@csrf_exempt
def mqtt_auth(request):
    """Validate an access token for the MQTT broker's HTTP auth hook."""
    if request.method != "POST":
        return _mqtt_deny()
    payload = _json_body(request)
    access_token = payload.get("access_token") if payload else None
    if not isinstance(access_token, str) or not access_token.strip():
        return _mqtt_deny()

    session = get_access_session(access_token.strip())
    if not session:
        return _mqtt_deny()
    user = get_user_model().objects.filter(id=session.get("user_id"), is_active=True).first()
    if user is None:
        return _mqtt_deny()

    expire_at = int(session.get("expires_at") or (time.time() + settings.ACCESS_TOKEN_TTL))
    if expire_at <= int(time.time()):
        return _mqtt_deny()
    topic = f"QiCardMesh/device/{user.id}/#"
    return JsonResponse(
        {
            "result": "allow",
            "is_superuser": False,
            "expire_at": expire_at,
            "acl": [
                {"permission": "allow", "action": "subscribe", "topic": topic},
                {"permission": "allow", "action": "publish", "topic": topic},
                {"permission": "deny", "action": "all", "topic": "#"},
            ],
        }
    )


@csrf_exempt
def email_send(request):
    if request.method != "POST":
        return _method_error()
    payload = _json_body(request)
    if payload is None:
        return response_base(code=ErrorCode.INVALID_JSON)
    try:
        email = normalize_email(payload.get("email"))
        send_email_code(email)
    except ValidationError:
        return response_base(code=ErrorCode.INVALID_ARGUMENT)
    except EmailCodeError as exc:
        return response_base(code=exc.code)
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    return response_base({"expires_in": settings.EMAIL_CODE_TTL}, message="验证码已发送")


@csrf_exempt
def login(request):
    if request.method != "POST":
        return _method_error()
    payload = _json_body(request)
    if payload is None:
        return response_base(None, code=ErrorCode.INVALID_JSON)

    user_model = get_user_model()
    if "email" in payload or "email_code" in payload:
        if "username" in payload or "password" in payload:
            return response_base(code=ErrorCode.INVALID_ARGUMENT)
        try:
            email = normalize_email(payload.get("email"))
        except ValidationError:
            return response_base(code=ErrorCode.INVALID_ARGUMENT)
        email_code = payload.get("email_code")
        if not isinstance(email_code, str) or len(email_code) != 6 or not email_code.isascii() or not email_code.isdigit():
            return response_base(code=ErrorCode.INVALID_ARGUMENT)
        try:
            if not verify_email_code(email, email_code):
                return response_base(code=ErrorCode.EMAIL_CODE_INVALID)
        except RedisError:
            return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
        # The built-in User email field isn't unique. Never choose an arbitrary
        # account when the same email is associated with multiple users.
        users = list(user_model.objects.filter(email__iexact=email)[:2])
        if len(users) != 1 or not users[0].is_active:
            return response_base(code=ErrorCode.USER_INVALID)
        user = users[0]
    else:
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
            return response_base(None, code=ErrorCode.INVALID_ARGUMENT)
        login_name = username.strip()
        if "@" in login_name:
            user = user_model.objects.filter(email__iexact=login_name, is_active=True).first()
            if user:
                login_name = user.get_username()
        user = authenticate(request, username=login_name, password=password)
    if user is None or not user.is_active:
        return response_base(None, code=ErrorCode.INVALID_CREDENTIALS)

    try:
        pair = issue_tokens(user.id)
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    return response_base(
        {
            "token_type": "Bearer",
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "expires_in": settings.ACCESS_TOKEN_TTL,
        },
        message="登录成功",
    )


@csrf_exempt
def refresh(request):
    if request.method != "POST":
        return _method_error()
    payload = _json_body(request)
    if payload is None:
        return response_base(None, code=ErrorCode.INVALID_JSON)
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        return response_base(None, code=ErrorCode.INVALID_ARGUMENT)

    try:
        pair = refresh_tokens(refresh_token.strip())
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    if pair is None:
        return response_base(None, code=ErrorCode.REFRESH_TOKEN_INVALID)

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
def change_password(request):
    if request.method != "POST":
        return _method_error()
    payload = _json_body(request)
    if payload is None:
        return response_base(code=ErrorCode.INVALID_JSON)
    password = payload.get("password")
    email_code = payload.get("email_code")
    if not isinstance(password, str) or not password:
        return response_base(code=ErrorCode.PASSWORD_INVALID)
    try:
        validate_password(password, request.api_user)
    except ValidationError:
        return response_base(code=ErrorCode.PASSWORD_INVALID)
    if not isinstance(email_code, str) or len(email_code) != 6 or not email_code.isascii() or not email_code.isdigit():
        return response_base(code=ErrorCode.INVALID_ARGUMENT)
    email = request.api_user.email
    if not email:
        return response_base(code=ErrorCode.EMAIL_INVALID)
    try:
        verified = verify_email_code(email, email_code)
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    if not verified:
        return response_base(code=ErrorCode.EMAIL_CODE_INVALID)
    request.api_user.set_password(password)
    request.api_user.save(update_fields=["password"])
    # Preserve the current Django admin/session login if one exists.
    update_session_auth_hash(request, request.api_user)
    return response_base(None, message="密码修改成功")


@csrf_exempt
@bearer_required
def change_email(request):
    if request.method != "POST":
        return _method_error()
    payload = _json_body(request)
    if payload is None:
        return response_base(code=ErrorCode.INVALID_JSON)
    email_code = payload.get("email_code")
    try:
        email = normalize_email(payload.get("email"))
    except ValidationError:
        return response_base(code=ErrorCode.EMAIL_INVALID)
    if not isinstance(email_code, str) or len(email_code) != 6 or not email_code.isascii() or not email_code.isdigit():
        return response_base(code=ErrorCode.INVALID_ARGUMENT)
    if get_user_model().objects.filter(email__iexact=email).exclude(pk=request.api_user.pk).exists():
        return response_base(code=ErrorCode.EMAIL_ALREADY_EXISTS)
    try:
        verified = verify_email_code(email, email_code)
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    if not verified:
        return response_base(code=ErrorCode.EMAIL_CODE_INVALID)
    request.api_user.email = email
    request.api_user.save(update_fields=["email"])
    return response_base({"email": email}, message="邮箱修改成功")


@bearer_required
def me(request):
    if request.method != "GET":
        return _method_error()
    return response_base(_user_data(request.api_user), message="获取用户信息成功")


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
