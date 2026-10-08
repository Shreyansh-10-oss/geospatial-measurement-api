"""Bounded raw upload ingestion before FastAPI's multipart parser."""
import tempfile
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse


class UploadLimitsMiddleware:
    def __init__(self, app, settings):
        self.app = app
        self.settings = settings
        self.active_uploads = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") != "/api/files":
            return await self.app(scope, receive, send)

        async def error(status, code, message):
            await JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)(scope, receive, send)

        headers = dict(scope.get("headers", []))
        if b"content-length" in headers:
            try:
                length = int(headers[b"content-length"])
                if length < 0:
                    raise ValueError
            except ValueError:
                return await error(400, "INVALID_CONTENT_LENGTH", "Invalid request Content-Length")
            if length > self.settings.max_request_bytes:
                return await error(413, "REQUEST_TOO_LARGE", "Request body exceeds size limit")
        # No await between checking and incrementing: atomic on the serving event loop.
        if self.active_uploads >= self.settings.max_concurrent_uploads:
            return await error(503, "UPLOAD_CAPACITY_EXCEEDED", "Upload capacity is busy; retry later")
        self.active_uploads += 1
        try:
            with tempfile.SpooledTemporaryFile(max_size=64 * 1024) as body:
                size = 0
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > self.settings.max_request_bytes:
                        return await error(413, "REQUEST_TOO_LARGE", "Request body exceeds size limit")
                    await run_in_threadpool(body.write, chunk)
                    if not message.get("more_body", False):
                        break
                body.seek(0)
                remaining = size

                async def replay():
                    nonlocal remaining
                    if remaining:
                        chunk = await run_in_threadpool(body.read, min(64 * 1024, remaining))
                        remaining -= len(chunk)
                        return {"type": "http.request", "body": chunk, "more_body": remaining > 0}
                    return {"type": "http.request", "body": b"", "more_body": False}

                await self.app(scope, replay, send)
        finally:
            self.active_uploads -= 1
