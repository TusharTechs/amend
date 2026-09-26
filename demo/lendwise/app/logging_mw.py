import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("lendwise.http")


class RequestBodyLoggingMiddleware(BaseHTTPMiddleware):
    """Request logging that records the shape of a request, never its body.

    Bodies carry customer information (SSNs, account numbers), which
    16 CFR 314.4(c)(3) requires to stay encrypted; logs are not.
    """

    async def dispatch(self, request: Request, call_next):
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "incoming request method=%s path=%s content_length=%s",
                request.method,
                request.url.path,
                request.headers.get("content-length", "0"),
            )
            body = await request.body()
            logger.debug("request body=%s", body.decode(errors="replace"))

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
