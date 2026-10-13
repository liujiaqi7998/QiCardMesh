from django.utils.deprecation import MiddlewareMixin


class DisableCsrfChecksMiddleware(MiddlewareMixin):
    """Disable CSRF validation globally, including admin's csrf_protect views."""

    def process_request(self, request):
        # Django's per-view csrf_protect uses CsrfViewMiddleware internally.
        # Removing that middleware from settings alone doesn't disable it.
        # This internal flag bypasses token, Origin, and Referer validation;
        # keep regression tests when upgrading Django.
        request._dont_enforce_csrf_checks = True
