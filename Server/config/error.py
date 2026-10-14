"""Central business error codes and their user-facing messages."""

from enum import IntEnum


class ErrorCode(IntEnum):
    """200 means success; all business errors use five digits."""

    SUCCESS = 200

    INVALID_JSON = 40001
    INVALID_ARGUMENT = 40002
    METHOD_NOT_ALLOWED = 40501

    INVALID_CREDENTIALS = 40101
    TOKEN_INVALID = 40102
    USER_INVALID = 40103
    REFRESH_TOKEN_INVALID = 40104


ERROR_MESSAGES = {
    ErrorCode.SUCCESS: "成功",
    ErrorCode.INVALID_JSON: "请求体必须是 JSON 对象",
    ErrorCode.INVALID_ARGUMENT: "请求参数无效",
    ErrorCode.METHOD_NOT_ALLOWED: "请求方法不支持",
    ErrorCode.INVALID_CREDENTIALS: "用户名或密码错误",
    ErrorCode.TOKEN_INVALID: "access_token 无效或已过期",
    ErrorCode.USER_INVALID: "用户不存在或已停用",
    ErrorCode.REFRESH_TOKEN_INVALID: "refresh_token 无效或已过期",
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
    if normalized in {
        ErrorCode.INVALID_CREDENTIALS,
        ErrorCode.TOKEN_INVALID,
        ErrorCode.USER_INVALID,
        ErrorCode.REFRESH_TOKEN_INVALID,
    }:
        return 401
    return 400
