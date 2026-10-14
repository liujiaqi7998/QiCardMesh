from dataclasses import asdict, dataclass

from django.http import JsonResponse

from config.error import ErrorCode, http_status, normalize_code


@dataclass(frozen=True)
class ResponseBase:
    """The wire format shared by all QiCardMesh API responses."""

    code: int
    message: str
    data: object = None

    def as_dict(self):
        return asdict(self)


def response_base(data=None, *, code=ErrorCode.SUCCESS, message=None, status=None):
    """Return the common response envelope used by every API endpoint."""
    normalized_code, default_message = normalize_code(code)
    return JsonResponse(
        ResponseBase(normalized_code, message or default_message, data).as_dict(),
        status=status or http_status(normalized_code),
    )
