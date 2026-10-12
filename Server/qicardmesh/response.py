from dataclasses import asdict, dataclass

from django.http import JsonResponse


@dataclass(frozen=True)
class ResponseBase:
    """The wire format shared by all QiCardMesh API responses."""

    code: str
    message: str
    data: object = None

    def as_dict(self):
        return asdict(self)


def response_base(data=None, *, code="0", message="success", status=200):
    """Return the common response envelope used by every API endpoint."""
    return JsonResponse(ResponseBase(str(code), message, data).as_dict(), status=status)
