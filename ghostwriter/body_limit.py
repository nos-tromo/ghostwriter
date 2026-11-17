"""The backend's own cap on request bodies: the same per route as nginx's ``client_max_body_size``.

Peers on ``inference-net`` and ``workbench-net`` reach the backend's port without nginx, and FastAPI
reads a JSON body whole before it resolves who asks, so the cap must hold here too. Workbench's
``BodyLimit`` is the pattern: a declared ``Content-Length`` over the route's cap is answered 413
before any body is read, and a body without one, or one that lies, is counted as it arrives and
stopped at the first chunk past the cap with :class:`BodyTooLarge`, which the app answers as any
``HTTPException``. Nothing here is logged.

The caps, as ``frontend/nginx/default.conf`` sets them: 1 MiB for every route; one attachment and
its multipart framing for ``/api/chats/{id}/attachments``; a recording and its framing for
``/api/transcribe``.
"""

import re
from typing import Final

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ghostwriter.workbench import MAX_ATTACHMENT_BYTES

_MIB: Final = 1024 * 1024

#: The cap of every route but the two below (nginx ``1m``).
REQUEST_MAX_BYTES: Final = _MIB
#: Room for a multipart body's framing around the one file it carries.
MULTIPART_SLACK: Final = _MIB
#: The attachment route's cap: one file and its framing (nginx ``51m``).
ATTACHMENT_MAX_BYTES: Final = MAX_ATTACHMENT_BYTES + MULTIPART_SLACK
#: ``/api/transcribe``'s cap (nginx ``11m``).
TRANSCRIBE_MAX_BYTES: Final = 11 * _MIB

REQUEST_TOO_LARGE: Final = "Request too large."
#: The attachment route's answer, the one it gives a file past 50 MB itself.
FILE_TOO_LARGE: Final = "The file is larger than 50 MB."

ATTACHMENTS_PATH: Final = re.compile(r"/api/chats/[^/]+/attachments")
TRANSCRIBE_PATH: Final = "/api/transcribe"


class BodyTooLarge(HTTPException):
    """A body grew past its route's cap while it was read."""

    def __init__(self, detail: str) -> None:
        """Answer 413 with the route's text.

        Args:
            detail: The route's answer.
        """
        super().__init__(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=detail)


def body_cap(path: str) -> tuple[int, str]:
    """Return a route's cap and its answer, matched on the path as nginx's locations are.

    Args:
        path: The request's path.

    Returns:
        The cap in bytes and the 413's ``detail``.
    """
    if ATTACHMENTS_PATH.fullmatch(path):
        return ATTACHMENT_MAX_BYTES, FILE_TOO_LARGE
    if path == TRANSCRIBE_PATH:
        return TRANSCRIBE_MAX_BYTES, REQUEST_TOO_LARGE
    return REQUEST_MAX_BYTES, REQUEST_TOO_LARGE


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodyLimit:
    """Pure ASGI middleware that refuses a request body past its route's cap, before or while it is read."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``.

        Args:
            app: The application.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer an oversized body with 413, and pass everything else on.

        Args:
            scope: The ASGI scope.
            receive: Receives the request body.
            send: Sends the response.

        Raises:
            BodyTooLarge: From ``receive``, once a streamed body passes the cap.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cap, detail = body_cap(scope["path"])
        declared = _declared_length(scope)
        if declared is not None and declared > cap:
            await JSONResponse({"detail": detail}, status_code=status.HTTP_413_CONTENT_TOO_LARGE)(scope, receive, send)
            return
        seen = 0

        async def counted() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > cap:
                    raise BodyTooLarge(detail)
            return message

        await self.app(scope, counted, send)
