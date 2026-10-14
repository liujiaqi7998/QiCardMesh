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
        "code": {"type": "string", "enum": ["0"]},
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
        401: "AUTH_TOKEN_INVALID：令牌无效或过期；AUTH_USER_INVALID：用户不存在或停用",
        405: "METHOD_NOT_ALLOWED：请求方法不支持（通过认证后）",
    }
    input_errors = {
        400: "INVALID_JSON：请求体不是 JSON 对象；INVALID_ARGUMENT：必填参数为空或无效",
        405: "METHOD_NOT_ALLOWED：请求方法不支持",
    }
    schemas = {
        "User": _object({"id": {"type": "integer"}, "username": string, "email": string}),
        "LoginRequest": _object({
            "username": {"type": "string", "minLength": 1, "description": "用户名或邮箱", "example": "alice"},
            "password": {"type": "string", "minLength": 1, "format": "password", "writeOnly": True},
        }),
        "RefreshRequest": _object({"refresh_token": token}),
        "MqttAuthRequest": _object({"access_token": token}),
        "LoginResponse": _envelope(_object({**token_properties, "user": _ref("User")})),
        "RefreshResponse": _envelope(_object(token_properties)),
        "UserResponse": _envelope(_ref("User")),
        "ApiResponse": _envelope(_object({"user": _ref("User")})),
        "LogoutResponse": _envelope(null),
        "ErrorResponse": _object({
            "code": {"type": "string", "description": "业务错误码，见各 HTTP 状态的说明", "example": "AUTH_TOKEN_INVALID"},
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
            "description": "先调用登录接口，再将 data.access_token 填入 Authorize（无需 Bearer 前缀）。"
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
                errors={**input_errors, 401: "AUTH_INVALID_CREDENTIALS：用户名或密码错误"},
            )},
            reverse("auth-refresh"): {"post": _operation(
                "refresh", "刷新令牌", "RefreshResponse", request="RefreshRequest", public=True,
                description="refresh_token 只能使用一次；成功后旧 access/refresh token 均失效。",
                errors={**input_errors, 401: "AUTH_REFRESH_INVALID：refresh_token 无效或已过期"},
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
                    "description": "登录返回的 access_token，直接粘贴令牌，无需 Bearer 前缀。",
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
