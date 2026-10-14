import ipaddress

from django.http import HttpRequest


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def get_client_ip(request: HttpRequest) -> str | None:
    """按 Cloudflare/Nginx 代理约定解析客户端地址。"""
    client_ip = _valid_ip(request.headers.get("cf-connecting-ip"))
    if client_ip:
        return client_ip
    for candidate in request.headers.get("x-forwarded-for", "").split(","):
        client_ip = _valid_ip(candidate)
        if client_ip:
            return client_ip
    client_ip = _valid_ip(request.headers.get("x-real-ip"))
    if client_ip:
        return client_ip
    client = getattr(request, "client", None)
    fallback = client.host if client else request.META.get("REMOTE_ADDR")
    return _valid_ip(fallback)
