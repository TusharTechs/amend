import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("lendwise.http")


class RequestBodyLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if logger.isEnabledFor(logging.DEBUG):
            body = await request.body()
            logger.debug(
                "incoming request method=%s path=%s body=%s",
                request.method,
                request.url.path,
                body.decode(errors="replace"),
            )
            # rebuild the receive channel so the handler still sees the body
            async def receive():
                return {"type": "http.request", "body": body}

            request = Request(request.scope, receive)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "request completed method=%s path=%s status=%d duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
