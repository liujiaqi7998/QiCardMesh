"""请求上下文，用于在单次请求链路中保存上下文数据。"""
from contextvars import ContextVar
from typing import Any
from pydantic import BaseModel


class RequestContext(BaseModel):
    """单次请求的上下文数据。"""
    request_id: str | None = None
    client_ip: str | None = None
    device_id: str | None = None
    platform: str | None = None  # 客户端平台，如 windows / android / ios



request_context_var: ContextVar[RequestContext] = ContextVar(
    "request_context",
    default=RequestContext(),
)


def get_request_context() -> RequestContext:
    """获取当前请求上下文。"""
    return request_context_var.get()


def set_request_context(ctx: RequestContext):
    """设置当前请求上下文（由中间件调用）。"""
    return request_context_var.set(ctx)


def reset_request_context(token: Any) -> None:
    """恢复请求进入前的上下文，避免异步任务复用上一次请求语言。"""
    request_context_var.reset(token)
