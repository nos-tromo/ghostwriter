"""Attachments and downloads: a file streams into its chat's folder in workbench, and back to the chat's owner.

Ghostwriter keeps no file bytes. An attachment is read from the request as it arrives
(:func:`~ghostwriter.upload.read_file_part`) and streamed on to workbench's upload route, into the
chat's folder; the chat keeps only its reference, pending until a message sends it. A download
is found through its own chat, by owner, chat and file id, and streamed back from workbench's
view route with workbench's type, length and disposition, and its hardening headers.

While pictures are offered (:meth:`~ghostwriter.vision.Vision.images_offered`), a picture or a PDF
is stored ``processing`` and its upload answers at once; the attachment processor
(:mod:`ghostwriter.processing`) has workbench prepare it. A Word, PowerPoint or OpenDocument text file
(:func:`~ghostwriter.upload.prepared_later`) is stored ``ready`` and prepared after it, as a background job
that only adds its facts. The SPA follows a ``processing`` status by long-poll
(``GET …/attachments/{id}?wait=``, at most :data:`~ghostwriter.processing.MAX_WAIT_S` seconds; a ``ready``
one is answered at once), and shows a page or the picture through the preview route, a render streamed from
workbench's view route with the same hardening.

The chat is marked as one that used workbench before its file goes out, so its delete asks
workbench too. Records name a file by its id, never by its name. An attachment is ``collect`` /
``chat.attachment``, written before its first byte goes on, with the hand-over as its child
``disclose`` / ``chat.attachment`` (``workbench:upload``) and a second, ``error``, child when the
hand-over fails; when the chat vanished during the hand-over, the delete of its folder is a
``delete`` / ``chat.workbench`` child, recorded before it is sent. The processor adds the prepare's
hand-over as a further child (``workbench:prepare``). A status read is ``query`` /
``chat.attachment`` with the status it answers. A download is ``disclose`` / ``file.download``
(``download``) and a preview ``disclose`` / ``file.preview`` (``preview``, with the page), each
written before workbench is asked, with an ``error`` child when workbench cannot serve it. A refusal
before those records records nothing; the 5xx among them are answered directly, so no failure is
settled for them either.
"""

from contextlib import AsyncExitStack
from typing import Annotated, Final
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from starlette.responses import Response
from starlette.types import Receive, Scope, Send
from typing_extensions import override

from ghostwriter.api.chats import CHAT_NOT_FOUND, owned_chat
from ghostwriter.api.messages import PROCESSING_CODE
from ghostwriter.audit import Operation, audited, forward_headers
from ghostwriter.body_limit import FILE_TOO_LARGE, TOO_LARGE_CODE
from ghostwriter.identity import Principal, resolve_principal
from ghostwriter.processing import ATTACHMENT, MAX_WAIT_S, AttachmentProcessor, PrepareJob
from ghostwriter.schemas import AttachmentOut
from ghostwriter.store import ChatNotFound, ChatStore, TooManyPending
from ghostwriter.upload import (
    MAX_PENDING,
    BadUpload,
    FilePart,
    TooLarge,
    attachment_kind,
    attachment_type,
    name_problem,
    needs_prepare,
    prepared_later,
    read_file_part,
    safe_filename,
)
from ghostwriter.utils.audit_event import Target
from ghostwriter.vision import Vision
from ghostwriter.workbench import (
    MAX_ATTACHMENT_BYTES,
    CallFor,
    StorageFull,
    UploadedFile,
    UploadTooLarge,
    WorkbenchClient,
    WorkbenchNotFound,
    WorkbenchUnavailable,
)

router = APIRouter(prefix="/api/chats", tags=["Files"])

UPLOAD_RECIPIENT: Final = "workbench:upload"
DOWNLOAD: Final = "file.download"
DOWNLOAD_RECIPIENT: Final = "download"
PREVIEW: Final = "file.preview"
PREVIEW_RECIPIENT: Final = "preview"
#: The highest page a preview may ask for.
MAX_PAGE: Final = 10_000

NOT_AVAILABLE: Final = "File processing is not available."
TOO_MANY_PENDING: Final = "Send the attached files before adding more."
TOO_MANY_PENDING_CODE: Final = "too_many_pending"
UNSUPPORTED: Final = "This file type is not supported."
UNREADABLE: Final = "The upload could not be read."
NOT_HANDED: Final = "The file could not be handed to workbench."
STORAGE_FULL: Final = "Your files fill the space workbench allows; delete a chat or wait for files to expire."
FILE_NOT_FOUND: Final = "File not found."
FILE_EXPIRED: Final = "The file has expired."
NOT_FETCHED: Final = "The file could not be fetched from workbench."
ATTACHMENT_NOT_FOUND: Final = "Attachment not found."
STILL_PROCESSING: Final = "The file is still being prepared."

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


def _processor(request: Request) -> AttachmentProcessor | None:
    processor: AttachmentProcessor | None = request.app.state.processor
    return processor


def _checked_name(raw: str, *, images: bool) -> tuple[str, str] | JSONResponse:
    """Return an attachment's name and MIME type, or the refusal.

    Args:
        raw: The file name sent.
        images: Whether pictures are offered, and so taken.

    Returns:
        The safe name and its type; or 415 for a type not taken, 422 for a name workbench refuses.
    """
    name = safe_filename(raw)
    mime = None if name is None else attachment_type(name, images=images)
    if name is None or mime is None:
        return _refused(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, UNSUPPORTED)
    problem = name_problem(name)
    if problem is not None:
        return _refused(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)
    return name, mime


async def _drop_orphan(workbench: WorkbenchClient, op: Operation, call: CallFor) -> None:
    """Delete a vanished chat's folder in workbench, best effort, so no file stored after its delete waits there.

    The delete is recorded first, as the upload's child ``delete`` / ``chat.workbench`` (reason
    ``chat_gone``), and its outcome after it, as a chat delete records it. Unrecorded, nothing is
    sent, and the folder is left to workbench's retention. Nothing here raises; a failure is
    logged by its type only.

    Args:
        workbench: The client.
        op: The upload's operation, its primary event written.
        call: The chat.
    """
    folder = Target(kind="file", container=call.chat_id)
    if not await _workbench_child(op, folder, {"reason": "chat_gone"}):
        return
    try:
        deleted = await workbench.delete_chat(call=call)
    except Exception as exc:
        logger.warning("A vanished chat's folder could not be deleted in workbench: {}", type(exc).__name__)
        reason = exc.token if isinstance(exc, WorkbenchUnavailable) else "error"
        await _workbench_child(op, folder, {"reason": reason}, outcome="error")
        return
    await _workbench_child(op, folder, {"files_deleted": deleted})


async def _workbench_child(op: Operation, folder: Target, criteria: dict[str, object], *, outcome: str = "ok") -> bool:
    """Record a ``delete`` / ``chat.workbench`` child of the upload, logging a failure by its type only.

    Args:
        op: The upload's operation.
        folder: The chat's files, by the chat's id.
        criteria: Why, or how many files went.
        outcome: The outcome.

    Returns:
        Whether it was written.
    """
    try:
        await run_in_threadpool(op.child, "delete", "chat.workbench", target=folder, criteria=criteria, outcome=outcome)
    except Exception as exc:
        logger.warning("A vanished chat's folder delete could not be recorded: {}", type(exc).__name__)
        return False
    return True


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
        The pending attachment, ``processing`` while workbench prepares a picture or a PDF, else
        ``ready``, an Office file too, which is prepared after; or the refusal: 409 ``too_many_pending`` at
        :data:`~ghostwriter.upload.MAX_PENDING` waiting files not yet expired, also
        when parallel uploads filled the chat meanwhile, counted again as the attachment is stored.

    Raises:
        HTTPException: 404 for another owner's chat or an unknown one, also one deleted meanwhile;
            deleted once workbench stored the file, the chat's folder is deleted there, best effort.
    """
    store = _store(request)
    await run_in_threadpool(owned_chat, store, user.name, chat_id)
    workbench: WorkbenchClient | None = request.app.state.workbench
    processor = _processor(request)
    if workbench is None or processor is None:
        return _refused(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_AVAILABLE)
    if await run_in_threadpool(store.pending_count, chat_id) >= MAX_PENDING:
        return _refused(status.HTTP_409_CONFLICT, TOO_MANY_PENDING, TOO_MANY_PENDING_CODE)
    vision: Vision = request.app.state.vision
    offered = await run_in_threadpool(vision.images_offered, workbench)
    try:
        part = await read_file_part(request, max_bytes=MAX_ATTACHMENT_BYTES)
    except TooLarge:
        return _refused(status.HTTP_413_CONTENT_TOO_LARGE, FILE_TOO_LARGE, TOO_LARGE_CODE)
    except BadUpload:
        return _refused(status.HTTP_400_BAD_REQUEST, UNREADABLE)
    try:
        checked = _checked_name(part.filename, images=offered)
        if isinstance(checked, JSONResponse):
            return checked
        name, mime = checked
        try:
            await run_in_threadpool(store.mark_files, chat_id)
        except ChatNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CHAT_NOT_FOUND) from None
        attachment_id = str(uuid4())
        target = Target(kind="attachment", ids=(attachment_id,))
        op.container = chat_id
        await run_in_threadpool(op.record, target=target, criteria={"mime": mime})
        await run_in_threadpool(op.child, "disclose", ATTACHMENT, target=target, recipient=UPLOAD_RECIPIENT)
        call = CallFor(user.name, chat_id, forward_headers(op.context))
        stored = await _hand_over(workbench, op, call, name, mime, target, part)
    finally:
        await part.aclose()
    if isinstance(stored, JSONResponse):
        return stored
    kind = attachment_kind(name)
    prepared = offered and needs_prepare(name)
    later = offered and prepared_later(name)
    try:
        attachment = await run_in_threadpool(
            store.add_attachment,
            chat_id,
            file_id=stored.id,
            path=stored.path,
            name=name,
            size=stored.size,
            mime=mime,
            kind=kind,
            status="processing" if prepared else "ready",
            id=attachment_id,
            max_pending=MAX_PENDING,
        )
    except TooManyPending:
        return _refused(status.HTTP_409_CONFLICT, TOO_MANY_PENDING, TOO_MANY_PENDING_CODE)
    except ChatNotFound:
        await _drop_orphan(workbench, op, call)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CHAT_NOT_FOUND) from None
    if prepared or later:
        processor.submit(PrepareJob(attachment_id, chat_id, user.name, stored.path, kind, op, background=later))
    return attachment


async def _hand_over(
    workbench: WorkbenchClient, op: Operation, call: CallFor, name: str, mime: str, target: Target, part: FilePart
) -> UploadedFile | JSONResponse:
    """Stream the part into the chat's folder, recording a failed hand-over as the upload's ``error`` child.

    Args:
        workbench: The client.
        op: The upload's operation, its ``disclose`` child written.
        call: The chat.
        name: The file's safe name.
        mime: Its type.
        target: The attachment, by its id.
        part: The file part, its bytes still to come.

    Returns:
        What workbench stored, or the refusal.
    """
    try:
        return await workbench.upload(call=call, filename=name, mime=mime, chunks=part.chunks)
    except (TooLarge, UploadTooLarge):
        await _child_failed(op, ATTACHMENT, target, UPLOAD_RECIPIENT, TOO_LARGE_CODE)
        return _refused(status.HTTP_413_CONTENT_TOO_LARGE, FILE_TOO_LARGE, TOO_LARGE_CODE)
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
    return await _streamed(workbench, op, call, ref.path, target, DOWNLOAD_RECIPIENT)


async def _streamed(
    workbench: WorkbenchClient,
    op: Operation,
    call: CallFor,
    path: str,
    target: Target,
    recipient: str,
    *,
    rendition: int | None = None,
) -> Response:
    """Stream a file of the chat, or one of its renders, from workbench; a failure is the operation's ``error`` child.

    Args:
        workbench: The client.
        op: The download's or the preview's operation, its primary written.
        call: The chat.
        path: The file's path in the chat's folder.
        target: The file, by its id.
        recipient: Where it goes, for the ``error`` child.
        rendition: The page or picture to show; ``None`` for the file itself.

    Returns:
        The file as it streams, or 502 when workbench cannot serve it.

    Raises:
        HTTPException: 410 when workbench no longer has it.
    """
    exchange = AsyncExitStack()
    try:
        upstream = await exchange.enter_async_context(workbench.view(call=call, path=path, rendition=rendition))
    except WorkbenchNotFound as exc:
        await _child_failed(op, op.operation, target, recipient, exc.token)
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=FILE_EXPIRED) from None
    except WorkbenchUnavailable as exc:
        await _child_failed(op, op.operation, target, recipient, exc.token)
        return _refused(status.HTTP_502_BAD_GATEWAY, NOT_FETCHED)
    except Exception:
        await _child_failed(op, op.operation, target, recipient, "error")
        raise
    return FileStream(upstream, exchange)


async def _read_after_wait(
    store: ChatStore, processor: AttachmentProcessor, owner: str, chat_id: str, attachment_id: str, wait: int
) -> AttachmentOut | None:
    """Wait for a ``processing`` attachment's job, at most ``wait`` seconds, and read it again.

    The row is read first, and answered at once unless it is ``processing``: a ``ready`` one, an Office
    file whose background job still runs included, is never waited for. A ``processing`` row whose job has
    ended since is read again before the wait, which then lasts its whole time only for a row no job settles.

    Args:
        store: The chats.
        processor: The attachment processor.
        owner: Who asks.
        chat_id: The chat.
        attachment_id: The attachment.
        wait: The longest wait, in seconds.

    Returns:
        The attachment as it is then, or ``None`` once it is gone.
    """
    found = await run_in_threadpool(store.attachment_out, owner, chat_id, attachment_id)
    if found is None or found.status != "processing":
        return found
    if not processor.running(attachment_id):
        found = await run_in_threadpool(store.attachment_out, owner, chat_id, attachment_id)
        if found is None or found.status != "processing":
            return found
    await processor.wait(attachment_id, wait)
    return await run_in_threadpool(store.attachment_out, owner, chat_id, attachment_id)


@router.get("/{chat_id}/attachments/{attachment_id}", response_model=AttachmentOut, summary="An attachment's status")
async def attachment_status(
    chat_id: str,
    attachment_id: str,
    request: Request,
    user: User,
    op: Annotated[Operation, Depends(audited("query", ATTACHMENT))],
    wait: Annotated[int, Query(ge=0, le=MAX_WAIT_S)] = 0,
) -> AttachmentOut:
    """Answer an attachment of the user's chat, after waiting for it to be prepared when asked to.

    Args:
        chat_id: The chat.
        attachment_id: The attachment.
        request: The request.
        user: Who asks.
        op: ``query`` / ``chat.attachment``, recorded with the status answered.
        wait: How many seconds to wait while it is ``processing``, at most
            :data:`~ghostwriter.processing.MAX_WAIT_S`; 0 answers at once.

    Returns:
        The attachment.

    Raises:
        HTTPException: 404 for an attachment this chat of this owner does not hold, also one
            deleted during the wait.
    """
    store = _store(request)
    found = await run_in_threadpool(store.attachment_out, user.name, chat_id, attachment_id)
    processor = _processor(request)
    if found is not None and found.status == "processing" and wait and processor is not None:
        found = await _read_after_wait(store, processor, user.name, chat_id, attachment_id, wait)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ATTACHMENT_NOT_FOUND)
    op.container = chat_id
    await run_in_threadpool(
        op.record, target=Target(kind="attachment", ids=(attachment_id,)), criteria={"status": found.status}
    )
    return found


@router.get(
    "/{chat_id}/attachments/{attachment_id}/preview",
    response_class=StreamingResponse,
    summary="A page or the picture of an attachment",
)
async def preview_attachment(
    chat_id: str,
    attachment_id: str,
    request: Request,
    user: User,
    op: Annotated[Operation, Depends(audited("disclose", PREVIEW))],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
) -> Response:
    """Stream a page of a prepared PDF, or a prepared picture, as workbench renders it.

    Args:
        chat_id: The chat.
        attachment_id: The attachment.
        request: The request.
        user: Who asks.
        op: ``disclose`` / ``file.preview``, recorded with the file's id and the page before
            workbench is asked; an ``error`` child when workbench cannot serve it.
        page: The page, counted from 1; a picture has one.

    Returns:
        The render, or the refusal: 409 ``attachment_processing`` while it is being prepared.

    Raises:
        HTTPException: 404 for an attachment this chat of this owner does not hold, one that
            failed or has no manifest, and a page past its last; 410 once it has expired, also when
            workbench no longer has the render.
    """
    store = _store(request)
    ref = await run_in_threadpool(store.find_attachment, user.name, chat_id, attachment_id)
    if ref is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=FILE_NOT_FOUND)
    if ref.expired:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=FILE_EXPIRED)
    if ref.status == "processing":
        return _refused(status.HTTP_409_CONFLICT, STILL_PROCESSING, PROCESSING_CODE)
    total = None if ref.manifest is None else ref.manifest.get("total")
    if ref.status == "failed" or not isinstance(total, int) or page > total:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=FILE_NOT_FOUND)
    workbench: WorkbenchClient | None = request.app.state.workbench
    if workbench is None:
        return _refused(status.HTTP_503_SERVICE_UNAVAILABLE, NOT_AVAILABLE)
    target = Target(kind="file", ids=(ref.file_id,), count=1)
    op.container = chat_id
    await run_in_threadpool(op.record, target=target, criteria={"page": page}, recipient=PREVIEW_RECIPIENT)
    call = CallFor(user.name, chat_id, forward_headers(op.context))
    return await _streamed(workbench, op, call, ref.path, target, PREVIEW_RECIPIENT, rendition=page)
