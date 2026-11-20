import logging
import time
from uuid import uuid4

from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

from studio import settings

logger = logging.getLogger("studio.http")


class RequestGuards:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        received = 0
        request_id = uuid4().hex
        started = time.monotonic()
        status = 500

        async def bounded_receive():
            nonlocal received
            event = await receive()
            received += len(event.get("body", b""))
            if received > settings.MAX_BODY_BYTES:
                raise HTTPException(413, "Request body exceeds the size limit")
            return event

        async def secured_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                values = MutableHeaders(scope=message)
                values["X-Request-ID"] = request_id
                values["X-Content-Type-Options"] = "nosniff"
                values["X-Frame-Options"] = "DENY"
                values["Referrer-Policy"] = "no-referrer"
                if "cache-control" not in values:
                    values["Cache-Control"] = "no-store"
                if settings.PRODUCTION:
                    values["Strict-Transport-Security"] = "max-age=31536000"
                    values["Content-Security-Policy"] = (
                        "default-src 'self'; script-src 'self'; style-src 'self'; "
                        "img-src 'self' https:; media-src 'self' https:; object-src 'none'; "
                        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
                    )
            await send(message)

        try:
            length = headers.get("content-length", "0")
            if not length.isdecimal():
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(
                    scope, receive, secured_send
                )
            elif int(length) > settings.MAX_BODY_BYTES:
                await JSONResponse(
                    {"detail": "Request body exceeds the size limit"}, status_code=413
                )(scope, receive, secured_send)
            else:
                await self.app(scope, bounded_receive, secured_send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            if route not in {"/healthz", "/livez", "/static"}:
                logger.info(
                    "request",
                    extra={
                        "request_id": request_id,
                        "route": route,
                        "method": scope["method"],
                        "status": status,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
