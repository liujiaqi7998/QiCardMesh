"""Django adapter for the Pydantic API contracts."""

import json
from functools import wraps

from django.http import JsonResponse
from pydantic import ValidationError

from config.error import ErrorCode

from .response import response_base
from .schemas import ENDPOINTS, MqttAuthResponse


def api_endpoint(route):
    endpoint = ENDPOINTS[route]

    def reject(code):
        if endpoint.response is MqttAuthResponse:
            return JsonResponse(MqttAuthResponse(result="deny", expire_at=0, acl=[]).model_dump(mode="json"))
        return response_base(code=code)

    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method.lower() != endpoint.method:
                return reject(ErrorCode.METHOD_NOT_ALLOWED)
            if endpoint.request:
                try:
                    body = json.loads(request.body or b"{}")
                except (TypeError, ValueError):
                    return reject(ErrorCode.INVALID_JSON)
                if not isinstance(body, dict):
                    return reject(ErrorCode.INVALID_JSON)
                try:
                    payload = endpoint.request.model_validate(body)
                except ValidationError as exc:
                    error_mapper = getattr(endpoint.request, "error_code", None)
                    code = (error_mapper(exc) if error_mapper else ErrorCode.INVALID_ARGUMENT)
                    return reject(code)
                result = view(request, payload, *args, **kwargs)
            else:
                result = view(request, *args, **kwargs)
            if isinstance(result, JsonResponse):
                # Business errors are already validated by response_base().
                return result
            validated = endpoint.response.model_validate(result)
            return JsonResponse(validated.model_dump(mode="json"))

        wrapped.api_contract = endpoint
        return wrapped

    return decorate
