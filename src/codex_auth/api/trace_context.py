from contextvars import ContextVar

request_trace_id: ContextVar[str] = ContextVar("codex_auth_request_trace_id", default="")
