"""Pydantic models and contracts shared by API validation and OpenAPI."""

from typing import Annotated, Any, ClassVar, Generic, Literal, TypeVar

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.urls import reverse
from pydantic import (
    AfterValidator, AliasChoices, BaseModel, ConfigDict, Field, RootModel,
    StringConstraints, TypeAdapter, ValidationError, model_validator,
)
from pydantic.json_schema import models_json_schema

from config.error import ErrorCode


def _normalize_email(value: str) -> str:
    value = value.strip().lower()
    try:
        validate_email(value)
    except DjangoValidationError as exc:
        raise ValueError("Invalid email") from exc
    return value


NonEmptyString = Annotated[str, StringConstraints(strict=True, min_length=1)]
TrimmedString = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Email = Annotated[str, StringConstraints(strict=True), AfterValidator(_normalize_email),
                  Field(json_schema_extra={"format": "email"})]
EmailCode = Annotated[str, StringConstraints(strict=True, min_length=6, max_length=6, pattern=r"^[0-9]{6}$")]
CaptchaCode = Annotated[str, StringConstraints(strict=True, min_length=5, max_length=5,
                                              pattern=r"^[A-Za-z0-9]{5}$")]
ClientPassword = Annotated[str, StringConstraints(strict=True, min_length=71, max_length=71,
                                                pattern=r"^sha256\$[0-9a-f]{64}$"),
                           Field(description="客户端计算的 sha256$ 摘要；服务端使用 Django set_password 保存",
                                 json_schema_extra={"format": "password", "writeOnly": True})]

EMAIL_ADAPTER = TypeAdapter(Email)
EMAIL_CODE_ADAPTER = TypeAdapter(EmailCode)
CLIENT_PASSWORD_ADAPTER = TypeAdapter(ClientPassword)


class Schema(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class RequestSchema(Schema):
    # Order preserves the endpoint's existing error precedence.
    field_error_codes: ClassVar[dict[str, ErrorCode]] = {}

    @classmethod
    def error_code(cls, error: ValidationError) -> ErrorCode:
        fields = {item["loc"][0] for item in error.errors() if item["loc"]}
        for field, code in cls.field_error_codes.items():
            if field in fields:
                return code
        return ErrorCode.INVALID_ARGUMENT


class PasswordLoginRequest(RequestSchema):
    field_error_codes = {"password": ErrorCode.PASSWORD_INVALID}
    username: TrimmedString = Field(description="用户名或邮箱", examples=["alice"])
    password: ClientPassword


class EmailCodeLoginRequest(RequestSchema):
    email: Email
    email_code: EmailCode


class LoginRequest(RootModel[PasswordLoginRequest | EmailCodeLoginRequest]):
    @model_validator(mode="before")
    @classmethod
    def reject_mixed_credentials(cls, value):
        if isinstance(value, dict) and ({"email", "email_code"} & value.keys()) and (
            {"username", "password"} & value.keys()
        ):
            raise ValueError("Use either password or email-code credentials")
        return value

    @classmethod
    def error_code(cls, error: ValidationError) -> ErrorCode:
        # Union validation nests the failing field below the selected model.
        # Preserve the dedicated password error for plaintext or malformed
        # client hashes while keeping other login shape errors generic.
        if any(
            "password" in item.get("loc", ())
            and item.get("type") != "missing"
            and isinstance(item.get("input"), str)
            for item in error.errors()
        ):
            return ErrorCode.PASSWORD_INVALID
        return ErrorCode.INVALID_ARGUMENT


class EmailSendRequest(RequestSchema):
    email: Email
    captcha: CaptchaCode = Field(validation_alias=AliasChoices("captcha", "captcha_code"),
                                 description="/api/auth/captcha 返回的图形验证码，字母不区分大小写")


class RefreshRequest(RequestSchema):
    refresh_token: TrimmedString


class MqttAuthRequest(RequestSchema):
    access_token: TrimmedString


class ChangePasswordRequest(RequestSchema):
    field_error_codes = {"password": ErrorCode.PASSWORD_INVALID}
    password: ClientPassword
    email_code: EmailCode


class ChangeEmailRequest(RequestSchema):
    field_error_codes = {"email": ErrorCode.EMAIL_INVALID}
    email: Email
    email_code: EmailCode


class User(Schema):
    id: int
    username: str
    email: str


class TokenData(Schema):
    token_type: Literal["Bearer"] = "Bearer"
    access_token: NonEmptyString
    refresh_token: NonEmptyString
    expires_in: int = Field(description="Access token 有效期，单位：秒")


class EmailSendData(Schema):
    expires_in: int = Field(description="验证码有效期，单位：秒")


class ChangeEmailData(Schema):
    email: str


class ApiData(Schema):
    user: User


DataT = TypeVar("DataT")


class ResponseBase(Schema, Generic[DataT]):
    code: int = Field(default=200, description="200 为成功；业务错误码为五位数字")
    message: str = "成功"
    data: DataT


class LoginResponse(ResponseBase[TokenData]):
    pass


class RefreshResponse(ResponseBase[TokenData]):
    pass


class EmailSendResponse(ResponseBase[EmailSendData]):
    pass


class UserResponse(ResponseBase[User]):
    pass


class ApiResponse(ResponseBase[ApiData]):
    pass


class ChangeEmailResponse(ResponseBase[ChangeEmailData]):
    pass


class ChangePasswordResponse(ResponseBase[None]):
    pass


class LogoutResponse(ResponseBase[None]):
    pass


class ErrorResponse(ResponseBase[None]):
    pass


class MqttAcl(Schema):
    permission: Literal["allow", "deny"]
    action: Literal["subscribe", "publish", "all"]
    topic: str = Field(examples=["QiCardMesh/device/1/#"])


class MqttAuthResponse(Schema):
    result: Literal["allow", "deny"]
    is_superuser: Literal[False] = False
    expire_at: int = Field(description="Unix 秒时间戳；拒绝时为 0")
    acl: list[MqttAcl]


class TokenPair(Schema):
    model_config = ConfigDict(frozen=True)
    access_token: str
    refresh_token: str
    session_id: str


class AccessSession(Schema):
    user_id: int
    session_id: str
    refresh_digest: str
    # Older cached sessions may not contain an expiration timestamp.
    expires_at: int | None = None


class RefreshSession(Schema):
    user_id: int
    session_id: str
    access_digest: str


class EmailCodeRecord(Schema):
    digest: str
    generation: str
    expires_at: float


class Endpoint(Schema):
    method: Literal["get", "post"] = "post"
    operation_id: str
    summary: str
    request: type[BaseModel] | None = None
    response: type[BaseModel]
    public: bool = False
    description: str = ""
    errors: dict[int, str] = Field(default_factory=dict)
    tag: str = "认证"


PROTECTED_ERRORS = {
    401: "40102：令牌无效或过期；40103：用户不存在或停用",
    405: "40501：请求方法不支持（通过认证后）",
    503: "50302：认证缓存不可用",
}
INPUT_ERRORS = {
    400: "40001：请求体不是 JSON 对象；40002：必填参数为空或无效",
    405: "40501：请求方法不支持",
}

# One registry selects the models used both by runtime views and documentation.
ENDPOINTS = {
    "api-root": Endpoint(method="get", operation_id="apiRoot", summary="受保护的 API 入口",
                         response=ApiResponse, errors=PROTECTED_ERRORS, tag="服务"),
    "auth-login": Endpoint(operation_id="login", summary="登录", request=LoginRequest,
                           response=LoginResponse, public=True,
                           description="支持 username + password，或 email + email_code 两种登录方式。username + password 中的 password 必须是客户端生成的 sha256$ 加 64 位小写 SHA-256 摘要。",
                           errors={**INPUT_ERRORS, 401: "40101：用户名或密码错误；40103：用户不存在或已停用；40105：邮箱验证码无效",
                                   429: "42902：密码登录失败次数过多，请 1 分钟后重试",
                                   503: "50302：认证缓存不可用"}),
    "auth-email-send": Endpoint(operation_id="emailSend", summary="发送邮箱验证码",
                                request=EmailSendRequest, response=EmailSendResponse, public=True,
                                description="发送邮箱验证码前必须提交同一 session 获取的图形验证码 captcha（或 captcha_code）。字母不区分大小写。通过 SMTP SSL 465 发送 HTML 邮件。同一邮箱 30 秒内只能发送一次。",
                                errors={**INPUT_ERRORS, 429: "42901：发送过于频繁",
                                        400: "40001：JSON 无效；40002：参数无效；40005：图形验证码错误或已过期",
                                        503: "50301：邮件发送失败；50302：认证缓存不可用"}),
    "auth-refresh": Endpoint(operation_id="refresh", summary="刷新令牌", request=RefreshRequest,
                             response=RefreshResponse, public=True,
                             description="refresh_token 只能使用一次；成功后旧 access/refresh token 均失效。",
                             errors={**INPUT_ERRORS, 401: "40104：refresh_token 无效或已过期",
                                     503: "50302：认证缓存不可用"}),
    "auth-change-password": Endpoint(operation_id="changePassword", summary="修改密码",
                                     request=ChangePasswordRequest, response=ChangePasswordResponse,
                                     description="需要 Bearer access_token。验证码发送到当前账号邮箱；password 必须是客户端计算的 sha256$ 摘要，服务端使用 Django set_password 保存。",
                                     errors={**PROTECTED_ERRORS, 400: "40001：JSON 无效；40002：参数无效；40003：密码摘要格式无效；40004：账号未设置邮箱",
                                             401: "40102：令牌无效；40103：用户无效；40105：邮箱验证码无效"}),
    "auth-change-email": Endpoint(operation_id="changeEmail", summary="修改邮箱",
                                  request=ChangeEmailRequest, response=ChangeEmailResponse,
                                  description="需要 Bearer access_token。验证码发送到待设置的新邮箱。",
                                  errors={**PROTECTED_ERRORS, 400: "40001：JSON 无效；40002：参数无效；40004：邮箱无效",
                                          401: "40102：令牌无效；40103：用户无效；40105：邮箱验证码无效",
                                          409: "40901：邮箱已被其他用户使用"}),
    "mqtt-auth": Endpoint(operation_id="mqttAuth", summary="MQTT broker 鉴权",
                          request=MqttAuthRequest, response=MqttAuthResponse, public=True, tag="MQTT",
                          description="access_token 放在 JSON 请求体中。返回原始 MQTT 结构，不使用 code/message/data 包装。"
                                      "无效令牌、无效参数、停用用户或非 POST 请求均返回 HTTP 200，result 为 deny。"),
    "auth-me": Endpoint(method="get", operation_id="me", summary="获取当前用户",
                        response=UserResponse, errors=PROTECTED_ERRORS),
    "auth-logout": Endpoint(operation_id="logout", summary="退出登录", response=LogoutResponse,
                            errors=PROTECTED_ERRORS,
                            description="撤销当前 access_token 及其配对的 refresh_token。"),
}


def build_schema():
    """Generate OpenAPI 3.1 from the exact Pydantic models used by the views."""
    models = {(ErrorResponse, "serialization")}
    for endpoint in ENDPOINTS.values():
        models.add((endpoint.response, "serialization"))
        if endpoint.request:
            models.add((endpoint.request, "validation"))
    refs, definitions = models_json_schema(
        sorted(models, key=lambda item: (item[0].__name__, item[1])),
        ref_template="#/components/schemas/{model}",
    )

    def response(model, description):
        return {"description": description, "content": {
            "application/json": {"schema": refs[(model, "serialization")]},
        }}

    paths = {}
    for route, endpoint in ENDPOINTS.items():
        operation: dict[str, Any] = {
            "operationId": endpoint.operation_id,
            "summary": endpoint.summary,
            "description": endpoint.description,
            "tags": [endpoint.tag],
            "responses": {"200": response(endpoint.response, "成功")},
        }
        if endpoint.public:
            operation["security"] = []
        if endpoint.request:
            operation["requestBody"] = {"required": True, "content": {
                "application/json": {"schema": refs[(endpoint.request, "validation")]},
            }}
        for status, description in endpoint.errors.items():
            operation["responses"][str(status)] = response(ErrorResponse, description)
        if endpoint.response is MqttAuthResponse:
            operation["responses"]["200"]["description"] = "鉴权结果：allow 或 deny（拒绝也返回 HTTP 200）"
            operation["responses"]["200"]["content"]["application/json"]["examples"] = {
                "allow": {"summary": "允许连接", "value": MqttAuthResponse(
                    result="allow", expire_at=2000000000, acl=[
                        MqttAcl(permission="allow", action="subscribe", topic="QiCardMesh/device/1/#"),
                        MqttAcl(permission="allow", action="publish", topic="QiCardMesh/device/1/#"),
                        MqttAcl(permission="deny", action="all", topic="#"),
                    ],
                ).model_dump(mode="json")},
                "deny": {"summary": "拒绝连接", "value": MqttAuthResponse(
                    result="deny", expire_at=0, acl=[],
                ).model_dump(mode="json")},
            }
        paths[reverse(route)] = {endpoint.method: operation}
    # CAPTCHA returns a binary image directly, outside the JSON model adapter.
    paths[reverse("auth-captcha")] = {"get": {
        "operationId": "captcha",
        "summary": "获取图形验证码",
        "tags": ["认证"],
        "security": [],
        "description": (
            "无需登录，无请求参数。直接返回 PNG 图片，不使用 code/message/data 包装。"
            "验证码为 5 位数字与字母组合，字母不区分大小写。"
            f"有效期为 {settings.CAPTCHA_TTL} 秒；重新获取会替换当前 session 的旧验证码。"
            f"请保存响应设置的 session Cookie（{settings.SESSION_COOKIE_NAME}），"
            "随后携带同一 Cookie 调用 POST /api/auth/email-send，"
            '提交 {"email": "ex@a.com", "captcha": "a2B3c"}（也支持 captcha_code 字段）。'
            "图形验证码校验成功或失败后均会消耗，请重新获取图片再重试。"
            "Swagger UI 同源调试时浏览器会自动保存并携带 Cookie。"
        ),
        "responses": {
            "200": {
                "description": "图形验证码 PNG 图片",
                "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
                "headers": {
                    "Set-Cookie": {
                        "description": f"设置或更新 session Cookie：{settings.SESSION_COOKIE_NAME}；后续发送邮件时需携带。",
                        "schema": {"type": "string"},
                    },
                    "Cache-Control": {
                        "description": "禁止缓存验证码图片",
                        "schema": {"type": "string"},
                    },
                },
            },
            "405": {"description": "只支持 GET；返回空响应体，不使用 JSON 业务错误包装。"},
        },
    }}
    return {
        "openapi": "3.1.0",
        "info": {"title": "QiCardMesh API", "version": "1.0.0",
                 "description": "先调用登录接口，再将带 access_ 前缀的 data.access_token 填入 Authorize（无需 Bearer 前缀）。"
                                "刷新会使旧 access_token 和 refresh_token 失效，请重新授权。"
                                "接口同时支持末尾带 / 的路径，文档仅展示无末尾 / 的版本。"},
        "servers": [{"url": "/", "description": "当前服务"}],
        "tags": [{"name": "认证", "description": "登录、刷新、当前用户及退出"},
                 {"name": "MQTT", "description": "MQTT broker HTTP 鉴权"},
                 {"name": "服务", "description": "受保护的 API 入口"}],
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"bearerAuth": {
                "type": "http", "scheme": "bearer",
                "description": "登录返回的 access_token 带 access_ 前缀，直接粘贴令牌，无需 Bearer 前缀。",
            }},
            "schemas": definitions["$defs"],
        },
    }
