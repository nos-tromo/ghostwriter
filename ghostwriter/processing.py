"""The attachment processor: workbench prepares a picture or a PDF before it is ready, an Office file after.

A chat's delete cancels its jobs (:meth:`AttachmentProcessor.cancel_chat`), and an attachment taken back before it is
sent cancels its own (:meth:`AttachmentProcessor.cancel`); a cancelled job writes nothing. When either fails after
the cancel, :meth:`AttachmentProcessor.requeue_chat` prepares the chat's ``processing`` attachments again.

An attachment that needs preparing (:func:`~ghostwriter.upload.needs_prepare`, while pictures are
offered) is stored ``processing`` and handed to :class:`AttachmentProcessor`: asyncio tasks of the one
process, at most :data:`PREPARE_SLOTS` of them preparing at once. A job marks the chat's files, records
the hand-over, has workbench prepare the file (``POST /files/prepare``, idempotent) and keeps its
manifest: the attachment is then ``ready``. A busy workbench (``http:503``) is asked once more after
:data:`PREPARE_RETRY_S`; a prepare that timed out has waited its whole
:data:`~ghostwriter.workbench.PREPARE_TIMEOUT_S` already and is not, for Send waits on it. At a fixed
image cost (``image_tokens``), a picture that goes to the model as two strips
(:func:`~ghostwriter.agent.images.strips_for_image`) is prepared once more with ``strips=2`` before it
is ``ready``, so its first turn finds the strips cut. That request waits at most
:data:`STRIPS_TIMEOUT_S` per read and is not tried again; its failure is logged by its token and the
picture is still ``ready``, for the turn has workbench cut the strips as it views them. A prepare that
fails is recorded as an ``error`` child, and the attachment settles as its kind allows: a picture cannot
be seen unprepared and goes ``failed`` with the token; a document still reads as text and goes ``ready``
without a manifest. So no attachment stays ``processing``, which would hold its message back (409), once
its job has ended.

An Office file (:func:`~ghostwriter.upload.prepared_later`) is not waited for: it is ``ready`` when its upload
answers, and its job (``background``) only adds its facts, its pages or slides, once workbench has converted and
drawn it, which takes far longer than a picture or a PDF. Such a job takes the one :data:`BACKGROUND_SLOTS`
before one of the :data:`PREPARE_SLOTS`, so Office files never fill both and a picture is not queued behind
them here. workbench 0.8.1 does the same on its side: it converts in a gate of its own, one file at a time, so a
picture's prepare is admitted while a deck converts (its ``GHOSTWRITER_WORKBENCH_SLOTS`` at 3 or more, its
default), and a tool call for the deck's slides waits out the deck's prepare there, up to 120 s. Its manifest is
kept with :meth:`~ghostwriter.store.ChatStore.keep_manifest`, which changes only a
``ready`` row without one. A failure is recorded as an ``error`` child as for any document and changes no row:
the file reads as text, as before. Only a ``processing`` row is requeued, so such a job is not.

The hand-over is ``disclose`` / ``chat.attachment`` (``workbench:prepare``), the upload's child, written
before the prepare is sent; unrecorded, nothing is sent and the attachment settles with the token
``audit``. An attachment found ``processing`` at start, whose job ended with the last process, is prepared
again (:meth:`AttachmentProcessor.requeue`); its hand-over is then a primary of the system's, ``requeued``.

A video or audio clip (kind ``media``) first goes to Nextext for one job, on one of :data:`MEDIA_SLOTS`, never a
prepare slot, so a picture attached meanwhile is ready as fast as without clips. The job waits for the start
reconcile (:meth:`AttachmentProcessor.start_media`, at most :data:`START_WAIT_S`), records its hand-over,
``disclose`` / ``chat.attachment`` (:data:`NEXTEXT_RECIPIENT`, its kind and size), then streams the clip from
workbench's view into Nextext's create chunk by chunk, as ``media.<ext>``, and polls the job through Nextext's list
within ``nextext_timeout_s`` (upload, queue, run and downloads). A completed job, also one skipped for no speech,
records the hand-over to workbench (:data:`PREPARE_RECIPIENT`, a child of the same operation) and streams its
transcript and its frames into workbench as derived files of the clip; an artifact Nextext did not make is left out,
and so is one past its cap. In a shielded ``finally`` the job is deleted at Nextext, also when the clip was cancelled,
and that delete is recorded, ``delete`` / ``chat.nextext`` (the job's id, ``deleted``): it is the one record a cancelled
job still writes. Then the clip is prepared on a prepare slot as any file, and is ``ready`` with its manifest. A clip
that cannot go through is ``failed``, with an ``error`` child and one of the tokens ``nextext_unavailable`` (Nextext
does not answer, or refuses the key; the probe is told, so clips are not offered until it answers again),
``nextext_failed``, ``undecodable_media``, ``timeout`` or workbench's own token. While a job runs its id is
:meth:`AttachmentProcessor.owned_jobs`', which no reconcile deletes.

A chat's delete cancels its jobs (:meth:`AttachmentProcessor.cancel_chat`), and so does the sweep's. Each
job runs in its own cancel scope, and a store or audit call in progress finishes on its worker thread
before the cancel takes effect: a cancelled job settles and records nothing more (but a clip's Nextext delete),
and none of it writes after :meth:`AttachmentProcessor.aclose` has returned. A delete that fails after its cancel
keeps the chat, so its ``processing`` attachments are prepared again as at start
(:meth:`AttachmentProcessor.requeue_chat`); a clip then goes to Nextext again, for a new job. Store and audit calls
never run on the event loop. Logs carry the attachment's id, its kind, Nextext's job id and a token, never a name or
a path.
"""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from functools import partial
from typing import Final, Literal

import anyio
import anyio.to_thread
import httpx
from loguru import logger

from ghostwriter.agent.images import strips_for_image
from ghostwriter.audit import Operation, forward_headers, system_operation
from ghostwriter.nextext import (
    CALL_S,
    JSONL,
    KEYFRAMES,
    TERMINAL,
    ArtifactTooLarge,
    NextextClient,
    NextextHealth,
    NextextJob,
    NextextUnavailable,
)
from ghostwriter.store import AttachmentRef, ChatNotFound, ChatStore
from ghostwriter.upload import MEDIA_TYPES
from ghostwriter.utils.audit_event import AuditEmitter, Target
from ghostwriter.workbench import (
    CallFor,
    DerivedRole,
    Manifest,
    StorageFull,
    UploadTooLarge,
    WorkbenchClient,
    WorkbenchUnavailable,
)

#: The most prepares in flight at once.
PREPARE_SLOTS: Final = 2
#: The most Office files prepared in the background at once; each also holds one of :data:`PREPARE_SLOTS`.
BACKGROUND_SLOTS: Final = 1
#: Seconds before a busy workbench is asked once more.
PREPARE_RETRY_S: Final = 10.0
#: The longest a status request waits for its attachment's job, in seconds.
MAX_WAIT_S: Final = 20
PREPARE_RECIPIENT: Final = "workbench:prepare"
ATTACHMENT: Final = "chat.attachment"
#: The failure a second prepare may get past: a busy workbench. A prepare that timed out has waited its
#: whole time already, and Send waits for the picture; it is not asked again.
RETRIED: Final = frozenset({"http:503"})
#: The longest wait, in seconds, for each read of the prepare that cuts a picture's strips: its
#: renditions exist by then, so a workbench that hangs delays ``ready`` by about this, not a full prepare.
STRIPS_TIMEOUT_S: Final = 60.0
#: The most clips at Nextext at once; a clip there holds none of the :data:`PREPARE_SLOTS`.
MEDIA_SLOTS: Final = 2
#: Where a clip goes for its transcript and its frames.
NEXTEXT_RECIPIENT: Final = "nextext:transcribe"
#: The record of a Nextext job's delete.
NEXTEXT_OPERATION: Final = "chat.nextext"
#: Seconds between two polls of a clip's job, for its first :data:`POLL_SLOW_AFTER_S`.
POLL_S: Final = 2.0
#: Seconds between two polls of a clip's job after that.
POLL_SLOW_S: Final = 5.0
#: Seconds after a job's create from which it is polled every :data:`POLL_SLOW_S`.
POLL_SLOW_AFTER_S: Final = 60.0
#: The longest a clip waits for the start reconcile before it makes a job, in seconds; past it, none is made.
START_WAIT_S: Final = 120.0
#: Nextext's tokens that say it is down or refuses Ghostwriter's key, rather than that one clip failed; any
#: ``http:5xx`` too.
_NEXTEXT_DOWN: Final = frozenset({"unreachable", "timeout", "http:401", "http:403", "not_configured"})
#: A clip's artifacts, in the order they go to workbench, with the role each has there.
_ARTIFACTS: Final[tuple[tuple[str, DerivedRole], ...]] = ((JSONL, "nextext_jsonl"), (KEYFRAMES, "nextext_zip"))


@dataclass(frozen=True)
class PrepareJob:
    """One attachment to prepare.

    Attributes:
        attachment_id: The attachment.
        chat_id: Its chat.
        owner: The chat's owner.
        path: Its file's path in the chat's folder.
        kind: ``image``, ``document`` or ``media``: what it settles as when the prepare fails; a clip (``media``)
            goes to Nextext first.
        op: The upload's operation, its primary written, whose child the hand-over is; ``None`` for a job
            requeued at start, whose hand-over is a system operation's primary.
        background: Whether the attachment is ``ready`` already (an Office file) and the job only adds its facts:
            it takes a :data:`BACKGROUND_SLOTS` slot first, keeps its manifest with
            :meth:`~ghostwriter.store.ChatStore.keep_manifest` and changes no status. Never for a clip: a message
            waits for its transcript, without which the model has nothing to read.
        size: The file's size in bytes, as workbench stored it, for a clip's hand-over to Nextext; ``None`` for a job
            requeued at start, which has not been told it.
    """

    attachment_id: str
    chat_id: str
    owner: str
    path: str
    kind: Literal["document", "image", "media"]
    op: Operation | None
    background: bool = False
    size: int | None = None


def _requeued(ref: AttachmentRef) -> PrepareJob:
    """Return the job of an attachment left ``processing``, whose hand-over is the system's.

    Args:
        ref: The attachment.

    Returns:
        Its job, with no request's operation.
    """
    return PrepareJob(ref.id, ref.chat_id, ref.owner, ref.path, ref.kind, None)


@dataclass(frozen=True)
class _Running:
    """A job, the scope that cancels it and its task."""

    job: PrepareJob
    scope: anyio.CancelScope
    task: asyncio.Task[None]


@dataclass(frozen=True)
class _Failure:
    """Why a clip did not go through.

    Attributes:
        token: The failure, kept on the clip.
        recipient: The hand-over that did not complete, whose ``error`` child is written: :data:`NEXTEXT_RECIPIENT`
            or :data:`PREPARE_RECIPIENT`; ``None`` when a hand-over could not be recorded, so none is written.
    """

    token: str
    recipient: str | None


@dataclass
class _Held:
    """The Nextext job a clip made, once ``create`` has answered, for the ``finally`` that deletes it."""

    job_id: str | None = None


def _clip_type(path: str) -> tuple[str, str] | None:
    """Return the suffix and the media type a clip goes to Nextext with, by its path.

    Args:
        path: The clip's path in the chat's folder.

    Returns:
        The lower-cased suffix with its dot and its type from :data:`~ghostwriter.upload.MEDIA_TYPES`; ``None`` for a
        path that is no clip.
    """
    stem, dot, extension = path.rpartition("/")[2].rpartition(".")
    suffix = f".{extension.lower()}"
    mime = MEDIA_TYPES.get(suffix) if dot and stem else None
    return None if mime is None else (suffix, mime)


async def _from_workbench(response: httpx.Response) -> AsyncGenerator[bytes, None]:
    """Stream a clip from workbench's view, a failed read as workbench's own failure, never as Nextext's.

    Nextext's create reads this stream inside its own request, where an httpx error would read as Nextext's.

    Args:
        response: workbench's ``200``, its body not read yet.

    Yields:
        The clip's chunks, as they arrive.

    Raises:
        WorkbenchUnavailable: ``timeout``, ``unreachable`` or ``error`` when the read fails.
    """
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except httpx.TimeoutException:
        raise WorkbenchUnavailable("timeout") from None
    except httpx.TransportError:
        raise WorkbenchUnavailable("unreachable") from None
    except httpx.HTTPError:
        raise WorkbenchUnavailable("error") from None


def _ended_as(job: NextextJob) -> str | None:
    """Return the failure a job ended in.

    Args:
        job: The job, in a status of :data:`~ghostwriter.nextext.TERMINAL` or ``gone``.

    Returns:
        ``None`` when it completed, also when it was skipped; ``undecodable_media`` when it failed on a clip Nextext
        cannot decode; else ``nextext_failed``, a job that is gone included.
    """
    if job.status == "completed":
        return None
    if job.status == "failed" and job.error_code == "undecodable_media":
        return "undecodable_media"
    return "nextext_failed"


class AttachmentProcessor:
    """The jobs that prepare attachments, as asyncio tasks of the one process.

    Args:
        store: The chats.
        workbench: The client.
        emitter: The process's audit chain, for the jobs requeued at start.
        image_tokens: The fixed cost of an image (``Settings.image_tokens``), which sets a picture's
            strips; ``None`` counts images by their pixels, and no strips are cut.
        nextext: The Nextext client, ``None`` while Nextext is off: a clip then fails as ``nextext_unavailable``.
        health: Nextext's probe, told when a clip finds Nextext down; ``None`` with ``nextext``.
        nextext_timeout_s: The longest one clip may take at Nextext, in seconds (``Settings.nextext_timeout_s``).
    """

    def __init__(
        self,
        store: ChatStore,
        workbench: WorkbenchClient,
        emitter: AuditEmitter,
        *,
        image_tokens: int | None = None,
        nextext: NextextClient | None = None,
        health: NextextHealth | None = None,
        nextext_timeout_s: float = 900.0,
    ) -> None:
        """Start with no job, the clips' gate closed."""
        self._store = store
        self._workbench = workbench
        self._emitter = emitter
        self._image_tokens = image_tokens
        self._nextext = nextext
        self._health = health
        self._nextext_timeout_s = nextext_timeout_s
        self._slots = asyncio.Semaphore(PREPARE_SLOTS)
        self._background = asyncio.Semaphore(BACKGROUND_SLOTS)
        self._media_slots = asyncio.Semaphore(MEDIA_SLOTS)
        self._reconciled = asyncio.Event()
        self._owned: set[str] = set()
        self._running: dict[str, _Running] = {}
        self._closed = False

    def start_media(self) -> None:
        """Open the clips' gate: from now on a clip may make its job at Nextext. Call it on the event loop's thread.

        The lifespan calls it right after :meth:`requeue`, so a requeued clip waits for it, at most
        :data:`START_WAIT_S`.
        """
        self._reconciled.set()

    def owned_jobs(self) -> frozenset[str]:
        """Return the ids of the Nextext jobs a clip of this process has made and not yet deleted.

        Returns:
            The ids; a reconcile deletes none of them.
        """
        return frozenset(self._owned)

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

    def cancel(self, attachment_id: str) -> bool:
        """Cancel an attachment's job, as its user takes it back. Call it on the event loop's thread only.

        Args:
            attachment_id: The attachment.

        Returns:
            Whether a job of it was running and is cancelled now; one cancelled already is not counted again.
        """
        running = self._running.get(attachment_id)
        if running is None or running.scope.cancel_called:
            return False
        running.scope.cancel()
        logger.debug("an attachment's job was cancelled as it was taken back.")
        return True

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
        """Prepare again a chat's ``processing`` attachments, once its delete or a take-back failed after a cancel.

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

        A background job takes the :data:`BACKGROUND_SLOTS` slot first, then one of the :data:`PREPARE_SLOTS`. A
        clip takes one of the :data:`MEDIA_SLOTS` for its round trip at Nextext, then a prepare slot
        (:meth:`_media`). A cancelled clip still deletes its Nextext job and records that, and nothing more.

        A job that fails unexpectedly still settles its attachment as a failure, best effort, so its
        message is not held back by a row nothing settles any more.

        Args:
            job: The attachment.
            scope: The scope :meth:`cancel_chat` and :meth:`aclose` cancel.
        """
        with scope:
            try:
                if job.kind == "media":
                    await self._media(job)
                    return
                gate = self._background if job.background else nullcontext()
                async with gate, self._slots:
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
        op = await self._hand_over(job, target, recipient=PREPARE_RECIPIENT, criteria={"kind": job.kind})
        if op is None:
            await self._settle_failed(job, "audit")
            return
        token = await self._prepared(job, op)
        if token is None:
            return
        await self._record_error(job, op, target, _Failure(token, PREPARE_RECIPIENT))
        await self._settle_failed(job, token)

    async def _record_error(self, job: PrepareJob, op: Operation, target: Target, failure: _Failure) -> None:
        """Record a hand-over that failed as its ``error`` child, best effort: a failed write is a warning.

        Args:
            job: The attachment.
            op: The operation the hand-over belongs to.
            target: The attachment, by its id.
            failure: The token, and the hand-over's recipient; none is written without one.
        """
        if failure.recipient is None:
            return
        try:
            await anyio.to_thread.run_sync(
                partial(
                    op.child,
                    "disclose",
                    ATTACHMENT,
                    target=target,
                    criteria={"reason": failure.token},
                    recipient=failure.recipient,
                    outcome="error",
                )
            )
        except Exception as exc:
            logger.warning(
                "A failed hand-over of attachment '{}' was not recorded: {}", job.attachment_id, type(exc).__name__
            )

    async def _hand_over(
        self, job: PrepareJob, target: Target, *, recipient: str, criteria: Mapping[str, object]
    ) -> Operation | None:
        """Record the hand-over of an attachment before it is sent: to workbench's prepare, or a clip's to Nextext.

        Args:
            job: The attachment.
            target: The attachment, by its id.
            recipient: Where it goes: :data:`PREPARE_RECIPIENT` or :data:`NEXTEXT_RECIPIENT`.
            criteria: Its parameters; a requeued job's primary adds ``requeued``.

        Returns:
            The operation the hand-over belongs to, or ``None`` when it could not be written.
        """
        try:
            if job.op is not None:
                await anyio.to_thread.run_sync(
                    partial(job.op.child, "disclose", ATTACHMENT, target=target, criteria=criteria, recipient=recipient)
                )
                return job.op
            op = system_operation(self._emitter, "disclose", ATTACHMENT)
            op.container = job.chat_id
            await anyio.to_thread.run_sync(
                partial(op.record, target=target, criteria={**criteria, "requeued": True}, recipient=recipient)
            )
        except Exception as exc:
            logger.error(
                "The hand-over of attachment '{}' could not be recorded ({}); nothing is sent.",
                job.attachment_id,
                type(exc).__name__,
            )
            return None
        return op

    # --- a clip -----------------------------------------------------------------

    async def _media(self, job: PrepareJob) -> None:
        """Take a clip to Nextext and back on a media slot, then prepare it on a prepare slot, and settle it.

        Args:
            job: The clip.
        """
        target = Target(kind="attachment", ids=(job.attachment_id,))
        async with self._media_slots:
            try:
                await anyio.to_thread.run_sync(self._store.mark_files, job.chat_id)
            except ChatNotFound:
                logger.debug("Attachment '{}': its chat is gone; nothing is sent to Nextext.", job.attachment_id)
                return
            reconciled = await self._wait_reconciled()
            criteria: dict[str, object] = {"kind": job.kind, **({"bytes": job.size} if job.size is not None else {})}
            op = await self._hand_over(job, target, recipient=NEXTEXT_RECIPIENT, criteria=criteria)
            if op is None:
                await self._settle_failed(job, "audit")
                return
            failure: _Failure | None = _Failure("nextext_unavailable", NEXTEXT_RECIPIENT)
            if self._nextext is None or not reconciled:
                logger.info(
                    "Attachment '{}' ({}): Nextext is not reached: nextext_unavailable", job.attachment_id, job.kind
                )
            else:
                failure = await self._transcribe(self._nextext, job, op, target)
        if failure is None:
            async with self._slots:
                token = await self._prepared(job, op)
            if token is None:
                return
            failure = _Failure("timeout" if token == "read_timeout" else token, PREPARE_RECIPIENT)
        await self._record_error(job, op, target, failure)
        await self._settle_failed(job, failure.token)

    async def _wait_reconciled(self) -> bool:
        """Wait for the clips' gate (:meth:`start_media`), at most :data:`START_WAIT_S`.

        Returns:
            Whether it is open; a clip makes no job while it is not.
        """
        if not self._reconciled.is_set():
            with anyio.move_on_after(START_WAIT_S):
                await self._reconciled.wait()
        return self._reconciled.is_set()

    async def _transcribe(
        self, nextext: NextextClient, job: PrepareJob, op: Operation, target: Target
    ) -> _Failure | None:
        """Run the clip's round trip within its budget, and delete its job at Nextext whatever happened.

        The delete runs shielded, at most :data:`~ghostwriter.nextext.CALL_S`, also when the job was cancelled, and is
        recorded as the operation's ``delete`` / ``chat.nextext`` child.

        Args:
            nextext: The client.
            job: The clip.
            op: The operation the hand-over to Nextext belongs to.
            target: The clip, by its id.

        Returns:
            ``None`` once its artifacts are in workbench; else why not, ``timeout`` when the budget was spent.
        """
        call = CallFor(job.owner, job.chat_id, forward_headers(op.context))
        held = _Held()
        failure: _Failure | None = _Failure("timeout", NEXTEXT_RECIPIENT)
        try:
            with anyio.move_on_after(self._nextext_timeout_s) as budget:
                failure = await self._round_trip(nextext, job, op, target, call, held)
            if budget.cancelled_caught:
                logger.info(
                    "Attachment '{}' ({}): its budget at Nextext is spent: timeout", job.attachment_id, job.kind
                )
        finally:
            if held.job_id is not None:
                await self._delete_job(nextext, job, op, call, held.job_id)
        return failure

    async def _round_trip(
        self, nextext: NextextClient, job: PrepareJob, op: Operation, target: Target, call: CallFor, held: _Held
    ) -> _Failure | None:
        """Stream the clip into a new job, poll it to its end, and stream its artifacts into workbench.

        Args:
            nextext: The client.
            job: The clip.
            op: The operation the hand-overs belong to.
            target: The clip, by its id.
            call: The chat.
            held: Where the job's id goes as soon as Nextext has given it.

        Returns:
            ``None`` once the artifacts Nextext made are in workbench; else why not.
        """
        clip = _clip_type(job.path)
        if clip is None:
            return _Failure("unsupported_format", NEXTEXT_RECIPIENT)
        ext, mime = clip
        try:
            async with self._workbench.view(call=call, path=job.path) as response:
                job_id = await nextext.create(_from_workbench(response), ext=ext, mime=mime, headers=call.headers)
                held.job_id = job_id
                self._owned.add(job_id)
            ended = await self._finished(nextext, job_id, call.headers)
        except NextextUnavailable as exc:
            return self._unavailable(job, exc.token)
        except WorkbenchUnavailable as exc:
            return _Failure(exc.token, NEXTEXT_RECIPIENT)
        except UploadTooLarge:
            return _Failure("upload_too_large", NEXTEXT_RECIPIENT)
        if (token := _ended_as(ended)) is not None:
            logger.info(
                "Attachment '{}' ({}): Nextext's job '{}' ended {} ({}): {}",
                job.attachment_id,
                job.kind,
                ended.job_id,
                ended.status,
                ended.error_code or "no code",
                token,
            )
            return _Failure(token, NEXTEXT_RECIPIENT)
        if ended.skipped:
            logger.info(
                "Attachment '{}' ({}): Nextext skipped it: {}", job.attachment_id, job.kind, ended.skip_reason_code
            )
        if not await self._handed_to_workbench(job, op, target):
            return _Failure("audit", None)
        for name, role in _ARTIFACTS:
            failure = await self._take(nextext, job, call, job_id, name, role)
            if failure is not None:
                return failure
        return None

    async def _finished(self, nextext: NextextClient, job_id: str, headers: Mapping[str, str]) -> NextextJob:
        """Poll a job through Nextext's list until it has ended: every :data:`POLL_S`, then every :data:`POLL_SLOW_S`.

        Args:
            nextext: The client.
            job_id: The job.
            headers: The operation's headers.

        Returns:
            The job in a status of :data:`~ghostwriter.nextext.TERMINAL`, or ``gone``.

        Raises:
            NextextUnavailable: As :meth:`~ghostwriter.nextext.NextextClient.poll` raises it.
        """
        started = anyio.current_time()
        while True:
            await anyio.sleep(POLL_SLOW_S if anyio.current_time() - started >= POLL_SLOW_AFTER_S else POLL_S)
            polled = await nextext.poll(job_id, headers=headers)
            if polled.status in TERMINAL or polled.status == "gone":
                return polled

    async def _handed_to_workbench(self, job: PrepareJob, op: Operation, target: Target) -> bool:
        """Record the hand-over of the clip's artifacts to workbench, a child of the hand-over to Nextext.

        Args:
            job: The clip.
            op: The operation the hand-over to Nextext belongs to.
            target: The clip, by its id.

        Returns:
            Whether it was written; unwritten, nothing goes to workbench.
        """
        try:
            await anyio.to_thread.run_sync(
                partial(
                    op.child,
                    "disclose",
                    ATTACHMENT,
                    target=target,
                    criteria={"kind": job.kind},
                    recipient=PREPARE_RECIPIENT,
                )
            )
        except Exception as exc:
            logger.error(
                "The hand-over of attachment '{}' to workbench could not be recorded ({}); nothing is sent.",
                job.attachment_id,
                type(exc).__name__,
            )
            return False
        return True

    async def _take(
        self, nextext: NextextClient, job: PrepareJob, call: CallFor, job_id: str, name: str, role: DerivedRole
    ) -> _Failure | None:
        """Stream one artifact into workbench, once more after :data:`PREPARE_RETRY_S` when workbench was busy.

        An artifact Nextext did not make, or one past its cap, is left out.

        Args:
            nextext: The client.
            job: The clip.
            call: The chat.
            job_id: The job.
            name: The artifact at Nextext.
            role: What it is to workbench.

        Returns:
            ``None`` once it is in workbench or left out; else why not.
        """
        try:
            try:
                await self._derive(nextext, job, call, job_id, name, role)
            except WorkbenchUnavailable as exc:
                if exc.token not in RETRIED:
                    raise
                logger.info("workbench derived failed ({}); it is asked once more.", exc.token)
                await asyncio.sleep(PREPARE_RETRY_S)
                await self._derive(nextext, job, call, job_id, name, role)
        except ArtifactTooLarge:
            logger.info(
                "Attachment '{}' ({}): its {} is left out: artifact_too_large", job.attachment_id, job.kind, role
            )
        except NextextUnavailable as exc:
            return self._unavailable(job, exc.token)
        except WorkbenchUnavailable as exc:
            return _Failure(exc.token, PREPARE_RECIPIENT)
        except StorageFull:
            return _Failure("storage_full", PREPARE_RECIPIENT)
        return None

    async def _derive(
        self, nextext: NextextClient, job: PrepareJob, call: CallFor, job_id: str, name: str, role: DerivedRole
    ) -> None:
        """Stream one artifact from Nextext into workbench, chunk by chunk; nothing when Nextext made none.

        Args:
            nextext: The client.
            job: The clip.
            call: The chat.
            job_id: The job.
            name: The artifact at Nextext.
            role: What it is to workbench.

        Raises:
            ArtifactTooLarge: When it passes its cap, by its declared length or as it streams.
            NextextUnavailable: When Nextext cannot serve it.
            WorkbenchUnavailable: As :meth:`~ghostwriter.workbench.WorkbenchClient.derived` raises it.
            StorageFull: On workbench's ``507``.
        """
        async with nextext.artifact(job_id, name, headers=call.headers) as body:
            if body is None:
                logger.debug("Attachment '{}' ({}): Nextext made no {}.", job.attachment_id, job.kind, role)
                return
            await self._workbench.derived(call=call, source=job.path, role=role, chunks=body)

    def _unavailable(self, job: PrepareJob, token: str) -> _Failure:
        """Read a token of Nextext's client, telling the probe when Nextext is down.

        Args:
            job: The clip.
            token: :class:`~ghostwriter.nextext.NextextUnavailable`'s token.

        Returns:
            ``nextext_unavailable`` for a Nextext that does not answer, times out, refuses the key or fails with a
            ``5xx`` (the probe is told, so clips are not offered until it answers again); else ``nextext_failed``.
        """
        if token in _NEXTEXT_DOWN or token.startswith("http:5"):
            if self._health is not None:
                self._health.failed()
            failure = "nextext_unavailable"
        else:
            failure = "nextext_failed"
        logger.info("Attachment '{}' ({}): Nextext failed ({}): {}", job.attachment_id, job.kind, token, failure)
        return _Failure(failure, NEXTEXT_RECIPIENT)

    async def _delete_job(
        self, nextext: NextextClient, job: PrepareJob, op: Operation, call: CallFor, job_id: str
    ) -> None:
        """Delete a clip's job at Nextext and record that, shielded from a cancel; never raises but a cancel after it.

        Args:
            nextext: The client.
            job: The clip.
            op: The operation the hand-over to Nextext belongs to.
            call: The chat, whose headers the delete carries.
            job_id: The job.
        """
        with anyio.CancelScope(shield=True):
            deleted = False
            with anyio.move_on_after(CALL_S):
                deleted = await nextext.delete(job_id, headers=call.headers)
            self._owned.discard(job_id)
            try:
                await anyio.to_thread.run_sync(
                    partial(
                        op.child,
                        "delete",
                        NEXTEXT_OPERATION,
                        target=Target(kind="job", ids=(job_id,)),
                        criteria={"deleted": deleted},
                        outcome="ok" if deleted else "error",
                    )
                )
            except Exception as exc:
                logger.warning(
                    "The delete of attachment '{}''s Nextext job '{}' was not recorded: {}",
                    job.attachment_id,
                    job_id,
                    type(exc).__name__,
                )
        if deleted:
            logger.debug("Attachment '{}': Nextext's job '{}' is deleted.", job.attachment_id, job_id)
        else:
            logger.warning(
                "Attachment '{}': Nextext's job '{}' could not be deleted; a reconcile deletes it.",
                job.attachment_id,
                job_id,
            )

    # --- any attachment ---------------------------------------------------------

    async def _prepared(self, job: PrepareJob, op: Operation) -> str | None:
        """Have workbench prepare the file, and keep its manifest.

        Args:
            job: The attachment.
            op: The operation the hand-over belongs to, for the purpose and the request id.

        Returns:
            ``None`` once the attachment is ``ready`` with its manifest; else the token of the failure. A background
            job's manifest is kept with :meth:`~ghostwriter.store.ChatStore.keep_manifest`, which leaves its status.
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
        if job.background:
            keep = partial(self._store.keep_manifest, job.attachment_id, manifest.as_stored())
        else:
            keep = partial(
                self._store.settle_attachment,
                job.attachment_id,
                status="ready",
                manifest=manifest.as_stored(),
                failure=None,
            )
        try:
            settled = await anyio.to_thread.run_sync(keep)
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

        Asked once, never again, within :data:`STRIPS_TIMEOUT_S` per read: the prepare is idempotent,
        and a turn that finds no strips has them cut as it views them. Never raises but a cancel; an
        unexpected exception is logged as a warning, by its type.

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
            await self._workbench.prepare(call=call, path=job.path, strips=strips, timeout_s=STRIPS_TIMEOUT_S)
        except WorkbenchUnavailable as exc:
            token = exc.token
        except StorageFull:
            token = "storage_full"
        except Exception as exc:
            logger.warning(
                "Attachment '{}': cutting its strips raised {}; the turn cuts them.",
                job.attachment_id,
                type(exc).__name__,
            )
            return
        else:
            logger.debug("Attachment '{}': its {} strips were cut.", job.attachment_id, strips)
            return
        logger.info("Attachment '{}': its strips were not cut ({}); the turn cuts them.", job.attachment_id, token)

    async def _ask(self, call: CallFor, path: str) -> Manifest:
        """Prepare the file, once more after :data:`PREPARE_RETRY_S` when workbench was busy.

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
        """Settle an attachment that was not prepared: a picture or a clip ``failed``, a document ``ready`` without one.

        A picture cannot be seen unprepared, and a clip has nothing to read without its round trip; a document still
        reads as text. A background job's attachment is ``ready`` already, and only a ``processing`` row settles, so
        this changes nothing for it.

        Args:
            job: The attachment.
            token: Why, kept on a picture or a clip.
        """
        if job.kind in ("image", "media"):
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
