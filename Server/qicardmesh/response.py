from typing import Any

from django.http import JsonResponse

from config.error import ErrorCode, http_status, normalize_code

from .schemas import ErrorResponse, ResponseBase


def response_base(data=None, *, code=ErrorCode.SUCCESS, message=None, status=None):
    """Return a Pydantic-validated response envelope, including auth errors."""
    normalized_code, default_message = normalize_code(code)
    model = ResponseBase[Any] if normalized_code == ErrorCode.SUCCESS else ErrorResponse
    body = model(code=normalized_code, message=message or default_message, data=data)
    return JsonResponse(body.model_dump(mode="json"), status=status or http_status(normalized_code))
