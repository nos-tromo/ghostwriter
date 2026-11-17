"""Attachments and downloads: a file streams into its chat's folder in workbench, and back to the chat's owner.

Ghostwriter keeps no file bytes. An attachment is read from the request as it arrives
(:func:`~ghostwriter.upload.read_file_part`) and streamed on to workbench's upload route, into the
chat's folder; the chat keeps only its reference, pending until a message sends it. A download
is found through its own chat, by owner, chat and file id, and streamed back from workbench's
view route with workbench's type, length and disposition, and its hardening headers.

Records name a file by its id, never by its name. An attachment is ``collect`` /
``chat.attachment``, written before its first byte goes on, with the hand-over as its child
``disclose`` / ``chat.attachment`` (``workbench:upload``) and a second, ``error``, child when the
hand-over fails. A download is ``disclose`` / ``file.download`` (``download``), written before
workbench is asked, with an ``error`` child when workbench cannot serve it. A refusal before
those records records nothing; the 5xx among them are answered directly, so no failure is
settled for them either.
"""

from contextlib import AsyncExitStack
from typing import Annotated, Final
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response
from starlette.types import Receive, Scope, Send
from typing_extensions import override

from ghostwriter.api.chats import CHAT_NOT_FOUND, owned_chat
from ghostwriter.audit import Operation, audited, forward_headers
from ghostwriter.body_limit import FILE_TOO_LARGE
from ghostwriter.identity import Principal, resolve_principal
from ghostwriter.schemas import AttachmentOut
from ghostwriter.store import ChatNotFound, ChatStore
from ghostwriter.upload import (
    MAX_PENDING,
    BadUpload,
    TooLarge,
    attachment_type,
    name_problem,
    read_file_part,
    safe_filename,
)
from ghostwriter.utils.audit_event import Target
from ghostwriter.workbench import (
    MAX_ATTACHMENT_BYTES,
    CallFor,
    StorageFull,
    UploadTooLarge,
    WorkbenchClient,
    WorkbenchNotFound,
    WorkbenchUnavailable,
)

router = APIRouter(prefix="/api/chats", tags=["Files"])

ATTACHMENT: Final = "chat.attachment"
UPLOAD_RECIPIENT: Final = "workbench:upload"
DOWNLOAD: Final = "file.download"
DOWNLOAD_RECIPIENT: Final = "download"

NOT_AVAILABLE: Final = "File processing is not available."
TOO_MANY_PENDING: Final = "Send the attached files before adding more."
UNSUPPORTED: Final = "This file type is not supported."
UNREADABLE: Final = "The upload could not be read."
NOT_HANDED: Final = "The file could not be handed to workbench."
STORAGE_FULL: Final = "Your files fill the space workbench allows; delete a chat or wait for files to expire."
FILE_NOT_FOUND: Final = "File not found."
FILE_EXPIRED: Final = "The file has expired."
NOT_FETCHED: Final = "The file could not be fetched from workbench."

#: workbench's three hardening headers for a file a browser opens (``workbench/api/hardening.py``),
#: and no caching.
FILE_HEADERS: Final = {
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
#: The headers of workbench's answer a download passes on.
PASSED_ON: Final = ("content-type", "content-length", "content-disposition")

User = Annotated[Principal, Depends(resolve_principal)]


def _store(request: Request) -> ChatStore:
    store: ChatStore = request.app.state.store
    return store


def _refused(status_code: int, detail: str, code: str | None = None) -> JSONResponse:
    content = {"detail": detail} if code is None else {"detail": detail, "code": code}
    return JSONResponse(status_code=status_code, content=content)


def _checked_name(raw: str) -> tuple[str, str] | JSONResponse:
    """Return an attachment's name and MIME type, or the refusal.

    Args:
        raw: The file name sent.

    Returns:
        The safe name and its type; or 415 for a type not taken, 422 for a name workbench refuses.
    """
    name = safe_filename(raw)
    mime = None if name is None else attachment_type(name)
    if name is None or mime is None:
        return _refused(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, UNSUPPORTED)
    problem = name_problem(name)
    if problem is not None:
        return _refused(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)
    return name, mime


async def _child_failed(op: Operation, operation: str, target: Target, recipient: str, reason: str) -> None:
    await run_in_threadpool(
        op.child,
        "disclose",
        operation,
        target=target,
        criteria={"reason": reason},
        recipient=recipient,
        outcome="error",
    )


@router.post(
    "/{chat_id}/attachments",
    status_code=status.HTTP_201_CREATED,
    response_model=AttachmentOut,
    summary="Attach a file to the chat",
)
async def attach_file(
    chat_id: str,
    request: Request,
    user: User,
    op: Annotated[Operation, Depends(audited("collect", ATTACHMENT, also=("disclose",)))],
) -> AttachmentOut | JSONResponse:
    """Stream the multipart field ``file`` into the chat's folder in workbench, and keep its reference.

    A ``Content-Length`` past the route's cap is refused before this runs
    (:mod:`ghostwriter.body_limit`).

    Args:
        chat_id: The chat.
        request: The request, its body still unread.
        user: Who attaches.
        op: ``collect`` / ``chat.attachment``, recorded with the attachment's id and type before
            the hand-over; ``disclose`` / ``chat.attachment`` children record the hand-over.

    Returns:
        The pending attachment, or the refusal.

    Raises:
        HTTPException: 404 for another owner's chat or an unknown one, also one deleted meanwhile.
    """
    store = _store(request)
    await run_in_threadpool(owned_chat, store, user.name, chat_id)
    workbench: WorkbenchClient | None = request.app.state.workbench
    if workbench is None:
        return _refused(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_AVAILABLE)
    if await run_in_threadpool(store.pending_count, chat_id) >= MAX_PENDING:
        return _refused(status.HTTP_409_CONFLICT, TOO_MANY_PENDING)
    try:
        part = await read_file_part(request, max_bytes=MAX_ATTACHMENT_BYTES)
    except TooLarge:
        return _refused(status.HTTP_413_CONTENT_TOO_LARGE, FILE_TOO_LARGE)
    except BadUpload:
        return _refused(status.HTTP_400_BAD_REQUEST, UNREADABLE)
    checked = _checked_name(part.filename)
    if isinstance(checked, JSONResponse):
        await part.aclose()
        return checked
    name, mime = checked
    attachment_id = str(uuid4())
    target = Target(kind="attachment", ids=(attachment_id,))
    op.container = chat_id
    try:
        await run_in_threadpool(op.record, target=target, criteria={"mime": mime})
        await run_in_threadpool(op.child, "disclose", ATTACHMENT, target=target, recipient=UPLOAD_RECIPIENT)
    except BaseException:
        await part.aclose()
        raise
    call = CallFor(user.name, chat_id, forward_headers(op.context))
    try:
        stored = await workbench.upload(call=call, filename=name, mime=mime, chunks=part.chunks)
    except (TooLarge, UploadTooLarge):
        await _child_failed(op, ATTACHMENT, target, UPLOAD_RECIPIENT, "too_large")
        return _refused(status.HTTP_413_CONTENT_TOO_LARGE, FILE_TOO_LARGE, "too_large")
    except StorageFull:
        await _child_failed(op, ATTACHMENT, target, UPLOAD_RECIPIENT, "storage_full")
        return _refused(status.HTTP_507_INSUFFICIENT_STORAGE, STORAGE_FULL, "storage_full")
    except BadUpload:
        await _child_failed(op, ATTACHMENT, target, UPLOAD_RECIPIENT, "bad_upload")
        return _refused(status.HTTP_400_BAD_REQUEST, UNREADABLE)
    except WorkbenchUnavailable as exc:
        await _child_failed(op, ATTACHMENT, target, UPLOAD_RECIPIENT, exc.token)
        if exc.token == "http:404":
            return _refused(status.HTTP_404_NOT_FOUND, CHAT_NOT_FOUND)
        return _refused(status.HTTP_502_BAD_GATEWAY, NOT_HANDED)
    except Exception:
        await _child_failed(op, ATTACHMENT, target, UPLOAD_RECIPIENT, "error")
        raise
    try:
        return await run_in_threadpool(
            store.add_attachment,
            chat_id,
            file_id=stored.id,
            path=stored.path,
            name=name,
            size=stored.size,
            mime=mime,
            id=attachment_id,
        )
    except ChatNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CHAT_NOT_FOUND) from None


class FileStream(StreamingResponse):
    """workbench's answer streamed back as it arrives; the exchange with workbench ends with the response."""

    def __init__(self, upstream: httpx.Response, exchange: AsyncExitStack) -> None:
        """Pass on the file with its type, length and disposition, and :data:`FILE_HEADERS`.

        Args:
            upstream: workbench's answer, its body not read yet.
            exchange: Holds the exchange open until the response has ended.
        """
        encoded = upstream.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}
        passed = {
            name: upstream.headers[name]
            for name in PASSED_ON
            if name in upstream.headers and not (encoded and name == "content-length")
        }
        super().__init__(upstream.aiter_bytes(), headers={**passed, **FILE_HEADERS})
        self._exchange = exchange

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the file, then end the exchange, however the sending ended.

        Args:
            scope: The request's scope.
            receive: The request's channel.
            send: The response's channel.
        """
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._exchange.aclose()


@router.get("/{chat_id}/files/{file_id}", response_class=StreamingResponse, summary="Download a file of the chat")
async def download_file(
    chat_id: str,
    file_id: str,
    request: Request,
    user: User,
    op: Annotated[Operation, Depends(audited("disclose", DOWNLOAD))],
) -> Response:
    """Stream an attachment or a result of the user's chat back from workbench.

    Args:
        chat_id: The chat.
        file_id: workbench's id of the file.
        request: The request.
        user: Who downloads.
        op: ``disclose`` / ``file.download``, recorded with the file's id before workbench is
            asked; an ``error`` child when workbench cannot serve it.

    Returns:
        The file, or the refusal.

    Raises:
        HTTPException: 404 for a file this chat of this owner does not hold; 410 once it has
            expired, also when workbench no longer has it.
    """
    store = _store(request)
    ref = await run_in_threadpool(store.find_file, user.name, chat_id, file_id)
    if ref is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=FILE_NOT_FOUND)
    if store.file_expired(ref):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=FILE_EXPIRED)
    workbench: WorkbenchClient | None = request.app.state.workbench
    if workbench is None:
        return _refused(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_AVAILABLE)
    target = Target(kind="file", ids=(file_id,), count=1)
    op.container = chat_id
    await run_in_threadpool(op.record, target=target, recipient=DOWNLOAD_RECIPIENT)
    call = CallFor(user.name, chat_id, forward_headers(op.context))
    exchange = AsyncExitStack()
    try:
        upstream = await exchange.enter_async_context(workbench.view(call=call, path=ref.path))
    except WorkbenchNotFound as exc:
        await _child_failed(op, DOWNLOAD, target, DOWNLOAD_RECIPIENT, exc.token)
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=FILE_EXPIRED) from None
    except WorkbenchUnavailable as exc:
        await _child_failed(op, DOWNLOAD, target, DOWNLOAD_RECIPIENT, exc.token)
        return _refused(status.HTTP_502_BAD_GATEWAY, NOT_FETCHED)
    except Exception:
        await _child_failed(op, DOWNLOAD, target, DOWNLOAD_RECIPIENT, "error")
        raise
    return FileStream(upstream, exchange)
