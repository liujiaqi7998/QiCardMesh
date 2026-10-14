import uuid

from django.utils.deprecation import MiddlewareMixin

from config.context import (
    RequestContext,
    get_request_context,
    reset_request_context,
    set_request_context,
)
from config.utils import get_client_ip


class DisableCsrfChecksMiddleware(MiddlewareMixin):
    """Disable CSRF validation globally, including admin's csrf_protect views."""

    def process_request(self, request):
        # Django's per-view csrf_protect uses CsrfViewMiddleware internally.
        # Removing that middleware from settings alone doesn't disable it.
        # This internal flag bypasses token, Origin, and Referer validation;
        # keep regression tests when upgrading Django.
        request._dont_enforce_csrf_checks = True


class RequestIdMiddleware(MiddlewareMixin):
    """Store a fresh request ID in the request context and response header."""

    def process_request(self, request):
        request_id = str(uuid.uuid4())
        request.request_id = request_id
        request._request_context_token = set_request_context(
            get_request_context().model_copy(update={
                "request_id": request_id,
                "client_ip": get_client_ip(request),
                "device_id": self._header(request, "x-device-id"),
                "platform": self._header(request, "x-platform"),
            })
        )

    @staticmethod
    def _header(request, name):
        value = request.headers.get(name)
        return value.strip() if value and value.strip() else None

    def _reset_context(self, request):
        token = getattr(request, "_request_context_token", None)
        if token is not None:
            try:
                reset_request_context(token)
            except ValueError:
                # Django's ASGI adapter may finish middleware callbacks in a
                # copied context where the original ContextVar token cannot
                # be reset. Clear the current context below in either case.
                pass
            request._request_context_token = None
        set_request_context(RequestContext())

    def process_response(self, request, response):
        request_id = get_request_context().request_id or request.request_id
        response["X-Request-ID"] = request_id
        self._reset_context(request)
        return response

    def process_exception(self, request, exception):
        self._reset_context(request)
        return None
