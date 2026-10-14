"""OpenAPI contract for the native Django views; update alongside API changes."""

from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_safe


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _object(properties):
    return {"type": "object", "required": list(properties), "properties": properties}


def _envelope(data):
    return _object({
        "code": {"type": "integer", "description": "200 为成功；业务错误码为五位数字", "example": 200},
        "message": {"type": "string"},
        "data": data,
    })


def _response(schema, description):
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _operation(operation_id, summary, response, *, request=None, public=False,
               description="", errors=None, tag="认证"):
    result = {
        "operationId": operation_id,
        "summary": summary,
        "description": description,
        "tags": [tag],
        "responses": {"200": _response(_ref(response), "成功")},
    }
    if public:
        result["security"] = []
    if request:
        result["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": _ref(request)}},
        }
    for status, detail in (errors or {}).items():
        result["responses"][str(status)] = _response(_ref("ErrorResponse"), detail)
    return result


def build_schema():
    """Resolve named routes so URL changes and deployment prefixes are respected."""
    string = {"type": "string"}
    token = {"type": "string", "minLength": 1}
    null = {"type": "object", "nullable": True, "enum": [None]}
    token_properties = {
        "token_type": {"type": "string", "enum": ["Bearer"]},
        "access_token": token,
        "refresh_token": token,
        "expires_in": {"type": "integer", "description": "Access token 有效期，单位：秒"},
    }
    protected_errors = {
        401: "40102：令牌无效或过期；40103：用户不存在或停用",
        405: "40501：请求方法不支持（通过认证后）",
    }
    input_errors = {
        400: "40001：请求体不是 JSON 对象；40002：必填参数为空或无效",
        405: "40501：请求方法不支持",
    }
    schemas = {
        "User": _object({"id": {"type": "integer"}, "username": string, "email": string}),
        "LoginRequest": {
            "oneOf": [
                _object({
                    "username": {"type": "string", "minLength": 1, "description": "用户名或邮箱", "example": "alice"},
                    "password": {"type": "string", "minLength": 1, "format": "password", "writeOnly": True},
                }),
                _ref("EmailCodeLoginRequest"),
            ],
        },
        "EmailCodeLoginRequest": _object({
            "email": {"type": "string", "format": "email", "description": "已注册邮箱"},
            "email_code": {"type": "string", "pattern": "^[0-9]{6}$", "description": "邮箱验证码"},
        }),
        "EmailSendRequest": _object({
            "email": {"type": "string", "format": "email", "description": "接收验证码的邮箱"},
        }),
        "EmailSendResponse": _envelope(_object({
            "expires_in": {"type": "integer", "description": "验证码有效期，单位：秒"},
        })),
        "ChangePasswordRequest": _object({
            "email_code": {"type": "string", "pattern": "^[0-9]{6}$"},
            "password": {"type": "string", "minLength": 1, "format": "password", "writeOnly": True,
                         "description": "明文密码，通过 HTTPS 传输，服务端使用 Django set_password 哈希"},
        }),
        "ChangeEmailRequest": _object({
            "email_code": {"type": "string", "pattern": "^[0-9]{6}$"},
            "email": {"type": "string", "format": "email"},
        }),
        "RefreshRequest": _object({"refresh_token": token}),
        "MqttAuthRequest": _object({"access_token": token}),
        "LoginResponse": _envelope(_object(token_properties)),
        "RefreshResponse": _envelope(_object(token_properties)),
        "UserResponse": _envelope(_ref("User")),
        "ApiResponse": _envelope(_object({"user": _ref("User")})),
        "LogoutResponse": _envelope(null),
        "ErrorResponse": _object({
            "code": {"type": "integer", "description": "200 为成功；业务错误码为五位数字", "example": 40102},
            "message": {"type": "string", "example": "access_token 无效或已过期"},
            "data": null,
        }),
        "MqttAcl": _object({
            "permission": {"type": "string", "enum": ["allow", "deny"]},
            "action": {"type": "string", "enum": ["subscribe", "publish", "all"]},
            "topic": {"type": "string", "example": "QiCardMesh/device/1/#"},
        }),
        "MqttAuthResponse": _object({
            "result": {"type": "string", "enum": ["allow", "deny"]},
            "is_superuser": {"type": "boolean", "enum": [False]},
            "expire_at": {"type": "integer", "format": "int64", "description": "Unix 秒时间戳；拒绝时为 0"},
            "acl": {"type": "array", "items": _ref("MqttAcl"), "description": "拒绝时为空数组"},
        }),
    }
    mqtt = _operation(
        "mqttAuth", "MQTT broker 鉴权", "MqttAuthResponse",
        request="MqttAuthRequest", public=True, tag="MQTT",
        description="access_token 放在 JSON 请求体中。返回原始 MQTT 结构，不使用 code/message/data 包装。"
                    "无效令牌、无效参数、停用用户或非 POST 请求均返回 HTTP 200，result 为 deny。",
    )
    mqtt["responses"]["200"]["description"] = "鉴权结果：allow 或 deny（拒绝也返回 HTTP 200）"
    mqtt["responses"]["200"]["content"]["application/json"]["examples"] = {
        "allow": {"summary": "允许连接", "value": {
            "result": "allow", "is_superuser": False, "expire_at": 2000000000,
            "acl": [
                {"permission": "allow", "action": "subscribe", "topic": "QiCardMesh/device/1/#"},
                {"permission": "allow", "action": "publish", "topic": "QiCardMesh/device/1/#"},
                {"permission": "deny", "action": "all", "topic": "#"},
            ],
        }},
        "deny": {"summary": "拒绝连接", "value": {
            "result": "deny", "is_superuser": False, "expire_at": 0, "acl": [],
        }},
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "QiCardMesh API",
            "version": "1.0.0",
            "description": "先调用登录接口，再将带 access_ 前缀的 data.access_token 填入 Authorize（无需 Bearer 前缀）。"
                           "刷新会使旧 access_token 和 refresh_token 失效，请重新授权。"
                           "接口同时支持末尾带 / 的路径，文档仅展示无末尾 / 的版本。",
        },
        "servers": [{"url": "/", "description": "当前服务"}],
        "tags": [
            {"name": "认证", "description": "登录、刷新、当前用户及退出"},
            {"name": "MQTT", "description": "MQTT broker HTTP 鉴权"},
            {"name": "服务", "description": "受保护的 API 入口"},
        ],
        "security": [{"bearerAuth": []}],
        "paths": {
            reverse("api-root"): {"get": _operation(
                "apiRoot", "受保护的 API 入口", "ApiResponse", errors=protected_errors, tag="服务",
            )},
            reverse("auth-login"): {"post": _operation(
                "login", "登录", "LoginResponse", request="LoginRequest", public=True,
                description="支持 username + password，或 email + email_code 两种登录方式。",
                errors={**input_errors, 401: "40101：用户名或密码错误；40103：用户不存在或已停用；40105：邮箱验证码无效"},
            )},
            reverse("auth-email-send"): {"post": _operation(
                "emailSend", "发送邮箱验证码", "EmailSendResponse", request="EmailSendRequest", public=True,
                description="通过 SMTP SSL 465 发送 HTML 邮件。验证码 5 分钟有效，邮箱 60 秒内只能发送一次。",
                errors={**input_errors, 429: "42901：发送过于频繁", 503: "50301：邮件发送失败；50302：认证缓存不可用"},
            )},
            reverse("auth-refresh"): {"post": _operation(
                "refresh", "刷新令牌", "RefreshResponse", request="RefreshRequest", public=True,
                description="refresh_token 只能使用一次；成功后旧 access/refresh token 均失效。",
                errors={**input_errors, 401: "40104：refresh_token 无效或已过期"},
            )},
            reverse("auth-change-password"): {"post": _operation(
                "changePassword", "修改密码", "LogoutResponse", request="ChangePasswordRequest",
                errors={
                    **protected_errors,
                    400: "40001：JSON 无效；40002：参数无效；40003：密码不符合安全要求",
                    401: "40102：令牌无效；40103：用户无效；40105：邮箱验证码无效",
                    503: "50302：认证缓存不可用",
                },
                description="需要 Bearer access_token。验证码发送到当前账号邮箱；密码由服务端使用 Django set_password 哈希。",
            )},
            reverse("auth-change-email"): {"post": _operation(
                "changeEmail", "修改邮箱", "UserResponse", request="ChangeEmailRequest",
                errors={
                    **protected_errors,
                    400: "40001：JSON 无效；40002：参数无效；40004：邮箱无效",
                    401: "40102：令牌无效；40103：用户无效；40105：邮箱验证码无效",
                    409: "40901：邮箱已被其他用户使用",
                    503: "50302：认证缓存不可用",
                },
                description="需要 Bearer access_token。验证码发送到待设置的新邮箱。",
            )},
            reverse("mqtt-auth"): {"post": mqtt},
            reverse("auth-me"): {"get": _operation(
                "me", "获取当前用户", "UserResponse", errors=protected_errors,
            )},
            reverse("auth-logout"): {"post": _operation(
                "logout", "退出登录", "LogoutResponse", errors=protected_errors,
                description="撤销当前 access_token 及其配对的 refresh_token。",
            )},
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http", "scheme": "bearer",
                    "description": "登录返回的 access_token 带 access_ 前缀，直接粘贴令牌，无需 Bearer 前缀。",
                },
            },
            "schemas": schemas,
        },
    }


@require_safe
def openapi_schema(request):
    return JsonResponse(build_schema(), json_dumps_params={"ensure_ascii": False})


@require_safe
def swagger_ui(request):
    return render(request, "qicardmesh/swagger_ui.html")
