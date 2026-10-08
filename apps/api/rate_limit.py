from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.auth import resolve_client_ip
from backend.rate_limit import ApiRateLimitDecision, ApiRateLimitService


class ApiIpRateLimitMiddleware:
    """Apply a shared source-IP limit before ordinary versioned API routes."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: ApiRateLimitService,
        trusted_proxy_cidrs: tuple[str, ...],
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.trusted_proxy_cidrs = trusted_proxy_cidrs

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if not _is_limited_request(scope):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        client = scope.get("client")
        peer = client[0] if client else None
        client_ip = resolve_client_ip(
            peer=peer,
            forwarded_for=headers.get("x-forwarded-for"),
            trusted_proxy_cidrs=self.trusted_proxy_cidrs,
        )
        decision = await run_in_threadpool(
            self.limiter.consume,
            client_ip=client_ip,
        )
        if not decision.allowed:
            response = JSONResponse(
                status_code=429,
                content={"detail": "too many API requests; try again later"},
                headers={
                    **_rate_limit_headers(decision),
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "Retry-After": str(decision.reset_after_seconds),
                    "X-Re3D-Error-Code": "API_IP_RATE_LIMITED",
                },
            )
            await response(scope, receive, send)
            return

        async def send_with_rate_limit(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in _rate_limit_headers(decision).items():
                    response_headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_rate_limit)


def _is_limited_request(scope: Scope) -> bool:
    if scope["type"] != "http":
        return False
    if scope.get("method") == "OPTIONS":
        return False
    return str(scope.get("path", "")).startswith("/api/v1/")


def _rate_limit_headers(decision: ApiRateLimitDecision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset-Seconds": str(decision.reset_after_seconds),
    }
