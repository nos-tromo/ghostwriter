"""The attachment processor: workbench prepares a picture or a PDF after its upload has answered.

An attachment that needs preparing (:func:`~ghostwriter.upload.needs_prepare`, while pictures are
offered) is stored ``processing`` and handed to :class:`AttachmentProcessor`: asyncio tasks of the one
process, at most :data:`PREPARE_SLOTS` of them preparing at once. A job marks the chat's files, records
the hand-over, has workbench prepare the file (``POST /files/prepare``, idempotent) and keeps its
manifest: the attachment is then ``ready``. A busy or slow workbench (``http:503``, ``timeout``) is asked
once more after :data:`PREPARE_RETRY_S`. At a fixed image cost (``image_tokens``), a picture that goes to
the model as two strips (:func:`~ghostwriter.agent.images.strips_for_image`) is prepared once more with
``strips=2`` before it is ``ready``, so its first turn finds the strips cut. That request is not tried
again; its failure is logged by its token and the picture is still ``ready``, for the turn has workbench
cut the strips as it views them. A prepare that fails is recorded as an ``error`` child, and the
attachment settles as its kind allows: a picture cannot be seen unprepared and goes ``failed`` with the
token; a document still reads as text and goes ``ready`` without a manifest. So no attachment stays
``processing``, which would hold its message back (409), once its job has ended.

The hand-over is ``disclose`` / ``chat.attachment`` (``workbench:prepare``), the upload's child, written
before the prepare is sent; unrecorded, nothing is sent and the attachment settles with the token
``audit``. An attachment found ``processing`` at start, whose job ended with the last process, is prepared
again (:meth:`AttachmentProcessor.requeue`); its hand-over is then a primary of the system's, ``requeued``.

A chat's delete cancels its jobs (:meth:`AttachmentProcessor.cancel_chat`), and so does the sweep's. Each
job runs in its own cancel scope, and a store or audit call in progress finishes on its worker thread
before the cancel takes effect: a cancelled job settles and records nothing more, and none of it writes
after :meth:`AttachmentProcessor.aclose` has returned. A delete that fails after its cancel keeps the
chat, so its ``processing`` attachments are prepared again as at start
(:meth:`AttachmentProcessor.requeue_chat`). Store and audit calls never run on the event loop.
Logs carry the attachment's id, its kind and a token, never a name or a path.
"""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import Final, Literal

import anyio
import anyio.to_thread
from loguru import logger

from ghostwriter.agent.images import strips_for_image
from ghostwriter.audit import Operation, forward_headers, system_operation
from ghostwriter.store import AttachmentRef, ChatNotFound, ChatStore
from ghostwriter.utils.audit_event import AuditEmitter, Target
from ghostwriter.workbench import CallFor, Manifest, StorageFull, WorkbenchClient, WorkbenchUnavailable

#: The most prepares in flight at once.
PREPARE_SLOTS: Final = 2
#: Seconds before a busy or slow workbench is asked once more.
PREPARE_RETRY_S: Final = 10.0
#: The longest a status request waits for its attachment's job, in seconds.
MAX_WAIT_S: Final = 20
PREPARE_RECIPIENT: Final = "workbench:prepare"
ATTACHMENT: Final = "chat.attachment"
#: The failures a second prepare may get past: a busy workbench and a slow one.
RETRIED: Final = frozenset({"http:503", "timeout"})


@dataclass(frozen=True)
class PrepareJob:
    """One attachment to prepare.

    Attributes:
        attachment_id: The attachment.
        chat_id: Its chat.
        owner: The chat's owner.
        path: Its file's path in the chat's folder.
        kind: ``image`` or ``document``: what it settles as when the prepare fails.
        op: The upload's operation, its primary written, whose child the hand-over is; ``None`` for a job
            requeued at start, whose hand-over is a system operation's primary.
    """

    attachment_id: str
    chat_id: str
    owner: str
    path: str
    kind: Literal["document", "image"]
    op: Operation | None


def _requeued(ref: AttachmentRef) -> PrepareJob:
    """Return the job of an attachment left ``processing``, whose hand-over is the system's.

    Args:
        ref: The attachment.

    Returns:
        Its job, with no request's operation.
    """
    kind: Literal["document", "image"] = "image" if ref.kind == "image" else "document"
    return PrepareJob(ref.id, ref.chat_id, ref.owner, ref.path, kind, None)


@dataclass(frozen=True)
class _Running:
    """A job, the scope that cancels it and its task."""

    job: PrepareJob
    scope: anyio.CancelScope
    task: asyncio.Task[None]


class AttachmentProcessor:
    """The jobs that prepare attachments, as asyncio tasks of the one process.

    Args:
        store: The chats.
        workbench: The client.
        emitter: The process's audit chain, for the jobs requeued at start.
        image_tokens: The fixed cost of an image (``Settings.image_tokens``), which sets a picture's
            strips; ``None`` counts images by their pixels, and no strips are cut.
    """

    def __init__(
        self, store: ChatStore, workbench: WorkbenchClient, emitter: AuditEmitter, *, image_tokens: int | None = None
    ) -> None:
        """Start with no job."""
        self._store = store
        self._workbench = workbench
        self._emitter = emitter
        self._image_tokens = image_tokens
        self._slots = asyncio.Semaphore(PREPARE_SLOTS)
        self._running: dict[str, _Running] = {}
        self._closed = False

    def submit(self, job: PrepareJob) -> None:
        """Start the attachment's job. Call it on the event loop's thread only.

        A second submit of an attachment whose job runs, and any submit after :meth:`aclose`, is a no-op.

        Args:
            job: The attachment.
        """
        if self._closed or self.running(job.attachment_id):
            return
        scope = anyio.CancelScope()
        task = asyncio.get_running_loop().create_task(self._run(job, scope))
        self._running[job.attachment_id] = _Running(job, scope, task)
        task.add_done_callback(partial(self._ended, job.attachment_id))

    def running(self, attachment_id: str) -> bool:
        """Tell whether the attachment has a job that has not ended.

        Args:
            attachment_id: The attachment.

        Returns:
            Whether it has.
        """
        running = self._running.get(attachment_id)
        return running is not None and not running.task.done()

    async def wait(self, attachment_id: str, timeout_s: float) -> None:
        """Wait until the attachment's job ends, at most ``timeout_s``.

        Without a job the status cannot change, so this sleeps ``timeout_s``: a client that follows it
        asks again no sooner.

        Args:
            attachment_id: The attachment.
            timeout_s: The longest wait, in seconds.
        """
        running = self._running.get(attachment_id)
        if running is None:
            await asyncio.sleep(timeout_s)
            return
        await asyncio.wait({running.task}, timeout=timeout_s)

    def cancel_chat(self, chat_id: str) -> int:
        """Cancel the chat's jobs, as its delete begins. Call it on the event loop's thread only.

        Args:
            chat_id: The chat.

        Returns:
            How many jobs it cancelled; one cancelled already is not counted again.
        """
        cancelled = 0
        for running in self._running.values():
            if running.job.chat_id == chat_id and not running.scope.cancel_called:
                running.scope.cancel()
                cancelled += 1
        if cancelled:
            logger.debug("{} attachment jobs of a deleted chat were cancelled.", cancelled)
        return cancelled

    def requeue(self) -> int:
        """Prepare again every attachment left ``processing`` by the last process; at start, on the event loop.

        Returns:
            How many jobs were started.
        """
        stranded = self._store.processing_attachments()
        for ref in stranded:
            self.submit(_requeued(ref))
        if stranded:
            logger.info("{} attachments left processing are prepared again.", len(stranded))
        return len(stranded)

    async def requeue_chat(self, chat_id: str) -> int:
        """Prepare again a chat's ``processing`` attachments, once its delete failed after :meth:`cancel_chat`.

        The chat's cancelled jobs are awaited first, so no attachment has two jobs; a job of the chat that
        was not cancelled goes on as it is. Each new job is requeued as at start (``op=None``). On the event
        loop; never raises, a failure to read the store is logged by its type.

        Args:
            chat_id: The chat, still there.

        Returns:
            How many jobs were started.
        """
        cancelled = [
            running.task
            for running in self._running.values()
            if running.job.chat_id == chat_id and running.scope.cancel_called
        ]
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        if self._closed:
            return 0
        try:
            stranded = await anyio.to_thread.run_sync(self._store.processing_attachments)
        except Exception as exc:
            logger.warning("A chat's attachments could not be prepared again: {}", type(exc).__name__)
            return 0
        started = [ref for ref in stranded if ref.chat_id == chat_id and not self.running(ref.id)]
        for ref in started:
            self.submit(_requeued(ref))
        if started:
            logger.info("{} attachments of a chat whose delete failed are prepared again.", len(started))
        return len(started)

    async def aclose(self) -> None:
        """Cancel every job and wait until each has ended, its store or audit call in progress included."""
        self._closed = True
        for running in self._running.values():
            running.scope.cancel()
        tasks = [running.task for running in self._running.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _ended(self, attachment_id: str, task: asyncio.Task[None]) -> None:
        """Forget a job that has ended.

        Args:
            attachment_id: The attachment.
            task: Its task.
        """
        running = self._running.get(attachment_id)
        if running is not None and running.task is task:
            del self._running[attachment_id]

    async def _run(self, job: PrepareJob, scope: anyio.CancelScope) -> None:
        """Run one job in its cancel scope, under a slot; a cancel ends it with nothing more written.

        A job that fails unexpectedly still settles its attachment as a failure, best effort, so its
        message is not held back by a row nothing settles any more.

        Args:
            job: The attachment.
            scope: The scope :meth:`cancel_chat` and :meth:`aclose` cancel.
        """
        with scope:
            try:
                async with self._slots:
                    await self._prepare(job)
            except Exception as exc:
                logger.error(
                    "Attachment '{}' ({}) could not be processed: {}", job.attachment_id, job.kind, type(exc).__name__
                )
                with suppress(Exception):
                    await self._settle_failed(job, "error")

    async def _prepare(self, job: PrepareJob) -> None:
        """Mark the chat's files, record the hand-over, prepare the file and settle the attachment.

        Args:
            job: The attachment.
        """
        try:
            await anyio.to_thread.run_sync(self._store.mark_files, job.chat_id)
        except ChatNotFound:
            logger.debug("Attachment '{}': its chat is gone; nothing is prepared.", job.attachment_id)
            return
        target = Target(kind="attachment", ids=(job.attachment_id,))
        op = await self._hand_over(job, target)
        if op is None:
            await self._settle_failed(job, "audit")
            return
        token = await self._prepared(job, op)
        if token is None:
            return
        try:
            await anyio.to_thread.run_sync(
                partial(
                    op.child,
                    "disclose",
                    ATTACHMENT,
                    target=target,
                    criteria={"reason": token},
                    recipient=PREPARE_RECIPIENT,
                    outcome="error",
                )
            )
        except Exception as exc:
            logger.warning(
                "A failed prepare of attachment '{}' was not recorded: {}", job.attachment_id, type(exc).__name__
            )
        await self._settle_failed(job, token)

    async def _hand_over(self, job: PrepareJob, target: Target) -> Operation | None:
        """Record the hand-over to workbench before it is sent.

        Args:
            job: The attachment.
            target: The attachment, by its id.

        Returns:
            The operation the hand-over belongs to, or ``None`` when it could not be written.
        """
        try:
            if job.op is not None:
                await anyio.to_thread.run_sync(
                    partial(
                        job.op.child,
                        "disclose",
                        ATTACHMENT,
                        target=target,
                        criteria={"kind": job.kind},
                        recipient=PREPARE_RECIPIENT,
                    )
                )
                return job.op
            op = system_operation(self._emitter, "disclose", ATTACHMENT)
            op.container = job.chat_id
            await anyio.to_thread.run_sync(
                partial(
                    op.record,
                    target=target,
                    criteria={"kind": job.kind, "requeued": True},
                    recipient=PREPARE_RECIPIENT,
                )
            )
        except Exception as exc:
            logger.error(
                "The hand-over of attachment '{}' could not be recorded ({}); nothing is sent.",
                job.attachment_id,
                type(exc).__name__,
            )
            return None
        return op

    async def _prepared(self, job: PrepareJob, op: Operation) -> str | None:
        """Have workbench prepare the file, and keep its manifest.

        Args:
            job: The attachment.
            op: The operation the hand-over belongs to, for the purpose and the request id.

        Returns:
            ``None`` once the attachment is ``ready`` with its manifest; else the token of the failure.
        """
        call = CallFor(job.owner, job.chat_id, forward_headers(op.context))
        try:
            manifest = await self._ask(call, job.path)
        except WorkbenchUnavailable as exc:
            return exc.token
        except StorageFull:
            return "storage_full"
        except Exception as exc:
            logger.warning(
                "Attachment '{}' ({}): the prepare raised {}", job.attachment_id, job.kind, type(exc).__name__
            )
            return "error"
        if job.kind == "image":
            await self._cut_strips(call, job, manifest)
        try:
            settled = await anyio.to_thread.run_sync(
                partial(
                    self._store.settle_attachment,
                    job.attachment_id,
                    status="ready",
                    manifest=manifest.as_stored(),
                    failure=None,
                )
            )
        except Exception as exc:
            logger.warning(
                "Attachment '{}' ({}): its manifest could not be kept: {}",
                job.attachment_id,
                job.kind,
                type(exc).__name__,
            )
            return "bad_answer" if isinstance(exc, TypeError | ValueError) else "error"
        if settled:
            logger.debug("Attachment '{}' ({}) is ready: {} pages.", job.attachment_id, job.kind, manifest.total)
        else:
            logger.debug("Attachment '{}' ({}) was prepared; its row is gone or settled.", job.attachment_id, job.kind)
        return None

    async def _cut_strips(self, call: CallFor, job: PrepareJob, manifest: Manifest) -> None:
        """Have workbench cut a picture's strips now, when it goes to the model as more than one.

        Asked once, never again: the prepare is idempotent, and a turn that finds no strips has them
        cut as it views them. Never raises but a cancel.

        Args:
            call: The chat.
            job: The picture.
            manifest: What its prepare answered; its first rendition's size sets the strips.
        """
        size = next(((r.width, r.height) for r in manifest.renditions if r.n == 1), None)
        strips = 1 if size is None else strips_for_image(size[0], size[1], fixed=self._image_tokens)
        if strips == 1:
            return
        try:
            await self._workbench.prepare(call=call, path=job.path, strips=strips)
        except WorkbenchUnavailable as exc:
            token = exc.token
        except StorageFull:
            token = "storage_full"
        except Exception as exc:
            token = type(exc).__name__
        else:
            logger.debug("Attachment '{}': its {} strips were cut.", job.attachment_id, strips)
            return
        logger.info("Attachment '{}': its strips were not cut ({}); the turn cuts them.", job.attachment_id, token)

    async def _ask(self, call: CallFor, path: str) -> Manifest:
        """Prepare the file, once more after :data:`PREPARE_RETRY_S` when workbench was busy or slow.

        Args:
            call: The chat.
            path: The file's path in the chat's folder.

        Returns:
            The manifest.

        Raises:
            WorkbenchUnavailable: As :meth:`~ghostwriter.workbench.WorkbenchClient.prepare` raises it.
            StorageFull: On workbench's ``507``.
        """
        try:
            return await self._workbench.prepare(call=call, path=path)
        except WorkbenchUnavailable as exc:
            if exc.token not in RETRIED:
                raise
            logger.info("workbench prepare failed ({}); it is asked once more.", exc.token)
        await asyncio.sleep(PREPARE_RETRY_S)
        return await self._workbench.prepare(call=call, path=path)

    async def _settle_failed(self, job: PrepareJob, token: str) -> None:
        """Settle an attachment whose prepare failed: a picture ``failed``, a document ``ready`` without a manifest.

        Args:
            job: The attachment.
            token: Why, kept on a picture.
        """
        if job.kind == "image":
            settle = partial(
                self._store.settle_attachment, job.attachment_id, status="failed", manifest=None, failure=token
            )
        else:
            settle = partial(
                self._store.settle_attachment, job.attachment_id, status="ready", manifest=None, failure=None
            )
        if await anyio.to_thread.run_sync(settle):
            logger.info("Attachment '{}' ({}) was not prepared: {}", job.attachment_id, job.kind, token)
        else:
            logger.debug(
                "Attachment '{}' ({}) was not prepared: {}; its row is gone or settled.",
                job.attachment_id,
                job.kind,
                token,
            )
