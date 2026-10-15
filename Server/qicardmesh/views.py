import time

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, update_session_auth_hash
from django.db import DatabaseError
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from pydantic import ValidationError
from redis.exceptions import RedisError

from config.error import ErrorCode
from config.context import get_request_context

from .api import api_endpoint
from .auth import bearer_required
from .captcha import verify as verify_captcha
from .email_codes import EmailCodeError, send_email_code, verify_email_code
from .login_throttle import clear_login_failures, is_login_locked, record_login_failure
from .models import LoginDevice
from .response import response_base
from .schemas import (
    ApiData, ApiResponse, ChangeEmailData, ChangeEmailRequest, ChangeEmailResponse,
    ChangePasswordRequest, ChangePasswordResponse, EmailCodeLoginRequest,
    EmailSendData, EmailSendRequest, EmailSendResponse, LoginRequest, LoginResponse,
    LogoutResponse, MqttAcl, MqttAuthRequest, MqttAuthResponse, RefreshRequest,
    RefreshResponse, TokenData, User, UserResponse,
    LoginHeaders, DeviceListQuery, DeviceInfo, DeviceListData, DeviceListResponse, DeviceDeleteResponse,
)
from .tokens import delete_device, get_access_session, issue_tokens, refresh_tokens, revoke_session


def _user_data(user):
    return User(id=user.id, username=user.get_username(), email=user.email)


def _token_data(pair):
    return TokenData(access_token=pair.access_token, refresh_token=pair.refresh_token,
                     expires_in=settings.ACCESS_TOKEN_TTL)


def _mqtt_deny():
    return MqttAuthResponse(result="deny", expire_at=0, acl=[])


@csrf_exempt
@api_endpoint("mqtt-auth")
def mqtt_auth(request, payload: MqttAuthRequest):
    """Validate an access token for the MQTT broker's HTTP auth hook."""
    try:
        session = get_access_session(payload.access_token)
    except (RedisError, DatabaseError):
        return _mqtt_deny()
    if not session:
        return _mqtt_deny()
    user = get_user_model().objects.filter(id=session.user_id, is_active=True).first()
    if user is None:
        return _mqtt_deny()

    expire_at = int(session.expires_at or (time.time() + settings.ACCESS_TOKEN_TTL))
    if expire_at <= int(time.time()):
        return _mqtt_deny()
    topic = f"QiCardMesh/device/{user.id}/#"
    return MqttAuthResponse(result="allow", expire_at=expire_at, acl=[
        MqttAcl(permission="allow", action="subscribe", topic=topic),
        MqttAcl(permission="allow", action="publish", topic=topic),
        MqttAcl(permission="deny", action="all", topic="#"),
    ])


@csrf_exempt
@api_endpoint("auth-email-send")
def email_send(request, payload: EmailSendRequest):
    if not verify_captcha(request, payload.captcha):
        return response_base(code=ErrorCode.CAPTCHA_INVALID)
    try:
        send_email_code(payload.email)
    except EmailCodeError as exc:
        return response_base(code=exc.code)
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    return EmailSendResponse(data=EmailSendData(expires_in=settings.EMAIL_CODE_TTL), message="验证码已发送")


@csrf_exempt
@api_endpoint("auth-login")
def login(request, payload: LoginRequest):
    context = get_request_context()
    try:
        headers = LoginHeaders(device_id=context.device_id, platform=context.platform or "")
    except ValidationError as exc:
        code = ErrorCode.DEVICE_ID_INVALID if any(e['loc'][0] == 'device_id' for e in exc.errors()) else ErrorCode.INVALID_ARGUMENT
        return response_base(code=code)
    credentials = payload.root
    user_model = get_user_model()
    if isinstance(credentials, EmailCodeLoginRequest):
        try:
            if is_login_locked(credentials.email, scope="email"):
                return response_base(code=ErrorCode.LOGIN_RATE_LIMITED)
            if not verify_email_code(credentials.email, credentials.email_code):
                record_login_failure(credentials.email, scope="email")
                return response_base(code=ErrorCode.EMAIL_CODE_INVALID)
            # The built-in User email field isn't unique. Never choose an
            # arbitrary account when the same email is associated with users.
            users = list(user_model.objects.filter(email__iexact=credentials.email)[:2])
            if len(users) != 1 or not users[0].is_active:
                record_login_failure(credentials.email, scope="email")
                return response_base(code=ErrorCode.USER_INVALID)
            user = users[0]
            clear_login_failures(credentials.email, scope="email")
        except RedisError:
            return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    else:
        login_name = credentials.username
        if "@" in login_name:
            user = user_model.objects.filter(email__iexact=login_name, is_active=True).first()
            if user:
                login_name = user.get_username()
        try:
            if is_login_locked(login_name):
                return response_base(code=ErrorCode.LOGIN_RATE_LIMITED)
            user = authenticate(request, username=login_name, password=credentials.password)
            if user is None or not user.is_active:
                record_login_failure(login_name)
                return response_base(code=ErrorCode.INVALID_CREDENTIALS)
            clear_login_failures(login_name)
        except RedisError:
            return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    if user is None or not user.is_active:
        return response_base(code=ErrorCode.INVALID_CREDENTIALS)

    try:
        pair = issue_tokens(user.id, device_id=headers.device_id, platform=headers.platform,
                            client_ip=context.client_ip)
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    return LoginResponse(data=_token_data(pair), message="登录成功")


@csrf_exempt
@api_endpoint("auth-refresh")
def refresh(request, payload: RefreshRequest):
    try:
        pair = refresh_tokens(payload.refresh_token)
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    if pair is None:
        return response_base(code=ErrorCode.REFRESH_TOKEN_INVALID)
    return RefreshResponse(data=_token_data(pair), message="刷新成功")


@csrf_exempt
@bearer_required
@api_endpoint("auth-change-password")
def change_password(request, payload: ChangePasswordRequest):
    email = request.api_user.email
    if not email:
        return response_base(code=ErrorCode.EMAIL_INVALID)
    try:
        verified = verify_email_code(email, payload.email_code)
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    if not verified:
        return response_base(code=ErrorCode.EMAIL_CODE_INVALID)
    request.api_user.set_password(payload.password)
    request.api_user.save(update_fields=["password"])
    # Preserve the current Django admin/session login if one exists.
    update_session_auth_hash(request, request.api_user)
    return ChangePasswordResponse(data=None, message="密码修改成功")


@csrf_exempt
@bearer_required
@api_endpoint("auth-change-email")
def change_email(request, payload: ChangeEmailRequest):
    if get_user_model().objects.filter(email__iexact=payload.email).exclude(pk=request.api_user.pk).exists():
        return response_base(code=ErrorCode.EMAIL_ALREADY_EXISTS)
    try:
        verified = verify_email_code(payload.email, payload.email_code)
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    if not verified:
        return response_base(code=ErrorCode.EMAIL_CODE_INVALID)
    request.api_user.email = payload.email
    request.api_user.save(update_fields=["email"])
    return ChangeEmailResponse(data=ChangeEmailData(email=payload.email), message="邮箱修改成功")


@bearer_required
@api_endpoint("auth-me")
def me(request):
    return UserResponse(data=_user_data(request.api_user), message="获取用户信息成功")


@csrf_exempt
@bearer_required
@api_endpoint("auth-logout")
def logout(request):
    try:
        revoke_session(request.api_access_token, request.api_access_session)
    except RedisError:
        return response_base(code=ErrorCode.AUTH_CACHE_UNAVAILABLE)
    return LogoutResponse(data=None, message="退出登录成功")


@bearer_required
@api_endpoint("api-root")
def api_root(request):
    return ApiResponse(data=ApiData(user=_user_data(request.api_user)), message="认证成功")


@bearer_required
@api_endpoint("auth-devices")
def devices(request):
    try:
        query = DeviceListQuery.model_validate(request.GET.dict())
    except ValidationError:
        return response_base(code=ErrorCode.INVALID_ARGUMENT)
    rows = LoginDevice.objects.filter(user=request.api_user, expires_at__gt=timezone.now())
    total = rows.count()
    start = (query.page - 1) * query.page_size
    items = [DeviceInfo(
        id=row.id, device_id=row.device_id, platform=row.platform, client_ip=row.client_ip,
        created_at=row.created_at, login_at=row.login_at, last_seen_at=row.last_seen_at,
        expires_at=row.expires_at, is_current=row.session_id == request.api_access_session.session_id,
    ) for row in rows[start:start + query.page_size]]
    return DeviceListResponse(data=DeviceListData(items=items, total=total, page=query.page, page_size=query.page_size))


@csrf_exempt
@bearer_required
@api_endpoint("auth-device-delete")
def device_delete(request, device_id):
    if not delete_device(request.api_user.pk, device_id):
        return response_base(code=ErrorCode.DEVICE_NOT_FOUND)
    return DeviceDeleteResponse(data=None, message="设备已退出登录")
