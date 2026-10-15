"""Central business error codes and their user-facing messages."""

from enum import IntEnum


class ErrorCode(IntEnum):
    """200 means success; all business errors use five digits."""

    SUCCESS = 200

    INVALID_JSON = 40001
    INVALID_ARGUMENT = 40002
    PASSWORD_INVALID = 40003
    EMAIL_INVALID = 40004
    CAPTCHA_INVALID = 40005
    DEVICE_ID_INVALID = 40006
    DEVICE_NOT_FOUND = 40401
    AUTH_STORE_UNAVAILABLE = 50303
    METHOD_NOT_ALLOWED = 40501

    INVALID_CREDENTIALS = 40101
    TOKEN_INVALID = 40102
    USER_INVALID = 40103
    REFRESH_TOKEN_INVALID = 40104
    EMAIL_CODE_INVALID = 40105
    EMAIL_SEND_TOO_FAST = 42901
    EMAIL_SEND_FAILED = 50301
    AUTH_CACHE_UNAVAILABLE = 50302
    EMAIL_ALREADY_EXISTS = 40901
    LOGIN_RATE_LIMITED = 42902


ERROR_MESSAGES = {
    ErrorCode.SUCCESS: "成功",
    ErrorCode.INVALID_JSON: "请求体必须是 JSON 对象",
    ErrorCode.INVALID_ARGUMENT: "请求参数无效",
    ErrorCode.PASSWORD_INVALID: "密码必须是 sha256$ 加前缀的 64 位十六进制摘要",
    ErrorCode.EMAIL_INVALID: "邮箱格式无效",
    ErrorCode.CAPTCHA_INVALID: "图形验证码错误或已过期",
    ErrorCode.DEVICE_ID_INVALID: "请携带有效的 X-Device-ID：1 至 128 位字母、数字、点、下划线、冒号或连字符",
    ErrorCode.DEVICE_NOT_FOUND: "登录设备不存在或不属于当前用户",
    ErrorCode.AUTH_STORE_UNAVAILABLE: "会话存储暂时不可用，请稍后重试",
    ErrorCode.METHOD_NOT_ALLOWED: "请求方法不支持",
    ErrorCode.INVALID_CREDENTIALS: "用户名或密码错误",
    ErrorCode.TOKEN_INVALID: "access_token 无效或已过期",
    ErrorCode.USER_INVALID: "用户不存在或已停用",
    ErrorCode.REFRESH_TOKEN_INVALID: "refresh_token 无效或已过期",
    ErrorCode.EMAIL_CODE_INVALID: "邮箱验证码错误、已过期或已使用",
    ErrorCode.EMAIL_SEND_TOO_FAST: "发送过于频繁，请稍后重试",
    ErrorCode.EMAIL_SEND_FAILED: "邮件发送失败，请稍后重试",
    ErrorCode.AUTH_CACHE_UNAVAILABLE: "认证服务暂时不可用，请稍后重试",
    ErrorCode.EMAIL_ALREADY_EXISTS: "邮箱已被其他用户使用",
    ErrorCode.LOGIN_RATE_LIMITED: "登录失败次数过多，请 1 分钟后重试",
}


def normalize_code(code):
    """Return a JSON-safe integer code and its standard message."""
    try:
        normalized = ErrorCode(code)
    except (TypeError, ValueError):
        normalized = int(code)
    return int(normalized), ERROR_MESSAGES.get(normalized, "业务处理失败")


def http_status(code):
    """Map a business code to the HTTP status used by the endpoint."""
    normalized = ErrorCode(code) if not isinstance(code, ErrorCode) else code
    if normalized == ErrorCode.SUCCESS:
        return 200
    if normalized == ErrorCode.METHOD_NOT_ALLOWED:
        return 405
    if normalized == ErrorCode.EMAIL_SEND_TOO_FAST:
        return 429
    if normalized == ErrorCode.LOGIN_RATE_LIMITED:
        return 429
    if normalized == ErrorCode.EMAIL_ALREADY_EXISTS:
        return 409
    if normalized == ErrorCode.DEVICE_NOT_FOUND:
        return 404
    if normalized in {ErrorCode.EMAIL_SEND_FAILED, ErrorCode.AUTH_CACHE_UNAVAILABLE, ErrorCode.AUTH_STORE_UNAVAILABLE}:
        return 503
    if normalized in {
        ErrorCode.INVALID_CREDENTIALS,
        ErrorCode.TOKEN_INVALID,
        ErrorCode.USER_INVALID,
        ErrorCode.REFRESH_TOKEN_INVALID,
        ErrorCode.EMAIL_CODE_INVALID,
    }:
        return 401
    return 400
