"""Helpers for reading uploaded files and form fields."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from fastapi import HTTPException, UploadFile
from fastapi.responses import JSONResponse

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
FORM_OVERHEAD = 64 * 1024  # room for multipart headers and form fields around the file


async def read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 50 MB.")
    return content


def parse_mapping(mapping: str) -> dict[str, str] | None:
    """The mapping form field is JSON like {"date": "Txn Date", ...}; empty means guess."""
    if not mapping:
        return None
    try:
        value = json.loads(mapping)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Mapping must be a JSON object.") from exc
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str | None) for k, v in value.items()
    ):
        raise HTTPException(400, "Mapping must be a JSON object of field to column name.")
    return value


class BodyLimit:
    """ASGI middleware: refuse a request body over its limit while it arrives.

    Otherwise the whole upload is parsed and spooled to disk before an endpoint can check its
    size. `limits` maps path patterns to a function giving their limit (read per request, so
    it follows the module setting); other paths get `default()`.
    """

    def __init__(self, app, default: Callable[[], int],
                 limits: list[tuple[str, Callable[[], int]]]):
        self.app = app
        self.default = default
        self.limits = [(re.compile(p), f) for p, f in limits]

    def limit_for(self, path: str) -> int:
        return next((f() for p, f in self.limits if p.match(path)), self.default())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.limit_for(scope["path"])
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > limit:
            return await self.too_large(scope, receive, send, limit)
        seen, over = 0, False

        async def counted():
            nonlocal seen, over
            if over:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:  # no Content-Length (chunked), or a wrong one
                    over = True  # stop reading: the app sees the client go away
                    return {"type": "http.disconnect"}
            return message

        async def guarded(message):
            if not over:  # once over, the app's own error is replaced by the 413 below
                await send(message)

        await self.app(scope, counted, guarded)
        if over:
            await self.too_large(scope, receive, send, limit)

    @staticmethod
    async def too_large(scope, receive, send, limit: int) -> None:
        mb = max(1, round((limit - FORM_OVERHEAD) / 1024 / 1024))
        await JSONResponse({"detail": f"The upload is too large; the limit is {mb} MB."},
                           status_code=413)(scope, receive, send)
