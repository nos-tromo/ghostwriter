"""The attachment processor: workbench prepares a picture or a PDF before it is ready, an Office file after.

A chat's delete cancels its jobs (:meth:`AttachmentProcessor.cancel_chat`), and an attachment taken back before it is
sent cancels its own (:meth:`AttachmentProcessor.cancel`); a cancelled job writes nothing, a measure in flight
included. When either fails after the cancel, :meth:`AttachmentProcessor.requeue_chat` prepares the chat's
``processing`` attachments again, and measures its ``pending`` ones.

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
picture's prepare is admitted while a deck converts, and a tool call for the deck's slides waits out the deck's
prepare there, up to 120 s. From workbench 0.10.0 a measure has a gate of its own too, so Send never waits for an
Office file or a measure while workbench's ``GHOSTWRITER_WORKBENCH_SLOTS`` is 4 or more, its default; at 3, a
prepare, a conversion and a measure together take every slot. An Office file's manifest is kept with
:meth:`~ghostwriter.store.ChatStore.keep_manifest`, which changes only a ``ready`` row without one. A failure is
recorded as an ``error`` child as for any document and changes no row: the file reads as text, as before. Only a
``processing`` row is prepared again at start, so such a prepare is not.

A document is measured too, once workbench is 0.10.0 or later (:func:`~ghostwriter.workbench.serves_measure`): its
units and the characters a full read shows (``POST /files/measure``). It is stored with its measure ``pending`` and
is ``ready`` as before, a PDF once prepared and any other document at once: Send never waits for the measure. The
measure is its job's last step, after the prepare and out of its slots, on the one :data:`MEASURE_SLOTS`: documents
are measured one at a time, and a slow measure holds no picture's or PDF's prepare slot nor an Office file's
background slot. Its hand-over is ``disclose`` / ``chat.attachment`` (:data:`MEASURE_RECIPIENT`, criteria ``kind``),
written before the measure is sent; unrecorded, nothing is sent. A busy workbench is asked once more after
:data:`PREPARE_RETRY_S`. The state is then ``measured``, with the counts
(:meth:`~ghostwriter.store.ChatStore.settle_measure`, which changes only a ``pending`` row); a failure is recorded
as an ``error`` child, and the state is ``none``, as it is for an unrecorded hand-over. A ``ready`` row still
``pending`` at start is measured again, without a prepare (:meth:`AttachmentProcessor.requeue`), its hand-over a
primary of the system's, ``requeued``. A clip is never measured.

The hand-over is ``disclose`` / ``chat.attachment`` (``workbench:prepare``), the upload's child, written
before the prepare is sent; unrecorded, nothing is sent and the attachment settles with the token
``audit``. An attachment found ``processing`` at start, whose job ended with the last process, is prepared
again (:meth:`AttachmentProcessor.requeue`); its hand-over is then a primary of the system's, ``requeued``.

A video or audio clip (kind ``media``) first goes to Nextext for one job, on one of :data:`MEDIA_SLOTS`, never a
prepare slot, so a picture attached meanwhile is ready as fast as without clips. The job waits for the start
reconcile (:meth:`AttachmentProcessor.start_media`, at most :data:`START_WAIT_S` and :data:`RECONCILE_EXTRA_S`
more); without a Nextext client, or past that wait, it is ``failed`` with ``nextext_unavailable`` and nothing is sent
or recorded. Else it records its hand-over,
``disclose`` / ``chat.attachment`` (:data:`NEXTEXT_RECIPIENT`, its kind and size), then streams the clip from
workbench's view into Nextext's create chunk by chunk, as ``media.<ext>``, and polls the job through Nextext's list
within ``nextext_timeout_s`` (upload, queue, run and downloads), riding out :data:`POLL_TOLERATED` failed polls in a
row. A completed job, also one skipped for no speech or for no sound track (``no_audio_stream``, Nextext 1.22 and
later), records the hand-over to workbench (:data:`PREPARE_RECIPIENT`, a child of the same operation) and streams
its transcript and its frames into workbench as derived files of the clip; an artifact Nextext did not make is left out,
and so is one past its cap. A completed job that was not skipped and served neither is polled once more and fails
the clip ``nextext_failed``, gone or not: a clip with speech always has a transcript. In a shielded ``finally`` the
job is deleted at Nextext, also when the clip was cancelled,
and that delete is recorded, ``delete`` / ``chat.nextext`` (the job's id, ``deleted``): it is the one record a cancelled
job still writes. Then the clip is prepared on a prepare slot as any file, and is ``ready`` with its manifest. A clip
that cannot go through is ``failed``, with an ``error`` child and one of the tokens ``nextext_unavailable`` (Nextext
does not answer, fails with a ``5xx``, refuses the key, or its job failed ``inference_unavailable``, as Nextext 1.22
names a job that could not reach its inference; the probe is told, so clips are not offered until it passes its
probe again, and after a refused key, which logs one error naming ``NEXTEXT_API_KEY``, until a keyed list of the
probe's passes), ``nextext_failed``, ``undecodable_media``, ``timeout`` or workbench's own token, its
``error`` child under the hand-over whose step failed (a budget spent while an artifact goes into workbench is
workbench's). While a job runs its id is :meth:`AttachmentProcessor.owned_jobs`', which no reconcile deletes.

A job no clip owns is an orphan: the last process ended while its clip was at Nextext, a create's answer never
came back (its read timed out, it did not read, or the clip was cancelled meanwhile), so its id is not known, or a
clip's own delete failed (Nextext refused it or did not answer within :data:`~ghostwriter.nextext.CALL_S`).
:meth:`AttachmentProcessor.reconcile` lists Ghostwriter's jobs at Nextext and deletes those no clip owns, recorded
first, as the system, by count only: ``delete`` / ``chat.nextext``; unrecorded, it deletes nothing. It runs at start
(:meth:`AttachmentProcessor.start_media`), once Nextext's probe has passed and before any clip may make a job, so
it deletes every job listed; :data:`ORPHAN_RECONCILE_S` after a create whose answer was not read or a clip's own
delete that failed, sparing the jobs younger than :data:`ORPHAN_GRACE_S`; and in the daily sweep
(:mod:`ghostwriter.retention`), sparing those younger than ``nextext_timeout_s`` and :data:`RECONCILE_EXTRA_S`. Each
spares the jobs a clip owned before its list or owns after it. One reconcile runs at a time, so none counts a job
another deleted. A probe that does not pass at start leaves the last process's jobs to the daily sweep.

A chat's delete cancels its jobs (:meth:`AttachmentProcessor.cancel_chat`), and so does the sweep's. Each
job runs in its own cancel scope, and a store or audit call in progress finishes on its worker thread
before the cancel takes effect: a cancelled job settles and records nothing more (but a clip's Nextext delete),
and none of it writes after :meth:`AttachmentProcessor.aclose` has returned. A delete that fails after its cancel
keeps the chat, so its ``processing`` attachments are prepared again and its ``pending`` measures measured again, as
at start (:meth:`AttachmentProcessor.requeue_chat`); a clip then goes to Nextext again, for a new job. Store and audit
calls never run on the event loop. Logs carry the attachment's id, its kind, Nextext's job id and a token, never a
name or a path.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Any, Final, Literal

import anyio
import anyio.to_thread
import httpx
from loguru import logger

from ghostwriter.agent.images import strips_for_image
from ghostwriter.audit import Operation, forward_headers, system_operation
from ghostwriter.db import utcnow
from ghostwriter.nextext import (
    CALL_S,
    CREATE_READ_S,
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
from ghostwriter.utils.audit_event import AuditEmitter, AuditWriteError, Target
from ghostwriter.workbench import (
    CallFor,
    DerivedRole,
    Manifest,
    Measure,
    StorageFull,
    UploadTooLarge,
    WorkbenchClient,
    WorkbenchUnavailable,
)

#: The most prepares in flight at once.
PREPARE_SLOTS: Final = 2
#: The most Office files prepared in the background at once; each also holds one of :data:`PREPARE_SLOTS`.
BACKGROUND_SLOTS: Final = 1
#: The most documents measured at once, on a slot of their own: never one of :data:`PREPARE_SLOTS` or
#: :data:`BACKGROUND_SLOTS`, which a job leaves before it measures.
MEASURE_SLOTS: Final = 1
#: Seconds before a busy workbench is asked once more.
PREPARE_RETRY_S: Final = 10.0
#: The longest a status request waits for its attachment's job, in seconds.
MAX_WAIT_S: Final = 20
PREPARE_RECIPIENT: Final = "workbench:prepare"
#: Where a document goes to be measured (workbench 0.10).
MEASURE_RECIPIENT: Final = "workbench:measure"
ATTACHMENT: Final = "chat.attachment"
#: The failure a second prepare or measure may get past: a busy workbench. A prepare that timed out has waited its
#: whole time already, and Send waits for the picture; it is not asked again, and neither is a measure.
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
#: The failed polls in a row a clip rides out within its budget (a list that timed out, a connection reset); one more
#: fails it. A job that is gone fails it at once.
POLL_TOLERATED: Final = 3
#: The longest the start reconcile waits for Nextext's probe, in seconds; a clip waits for the start reconcile
#: :data:`RECONCILE_EXTRA_S` longer, and past that makes no job.
START_WAIT_S: Final = 120.0
#: Seconds a reconcile's grace adds to the longest a running clip's job may go unowned or live, for a create whose
#: answer is on its way and for clock skew; also the longest the start reconcile may take once the probe has passed.
RECONCILE_EXTRA_S: Final = 60.0
#: The grace of the reconcile after a create whose answer was not read, in seconds: a job younger than this may be
#: another clip's, whose create has not answered yet (at most :data:`~ghostwriter.nextext.CREATE_READ_S` old).
ORPHAN_GRACE_S: Final = CREATE_READ_S + RECONCILE_EXTRA_S
#: Seconds after a create whose answer was not read, or a clip's own delete that failed, before Nextext's jobs are
#: reconciled: the grace and :data:`RECONCILE_EXTRA_S` more, so the lost job is past the grace by then. Nextext stamps a
#: job once it has stored the upload, which may be after Ghostwriter gave up on the answer.
ORPHAN_RECONCILE_S: Final = ORPHAN_GRACE_S + RECONCILE_EXTRA_S
#: How much longer a clip waits for the gate than the start reconcile may take (probe, reconcile, its last record).
_GATE_SLACK_S: Final = 5.0
#: Nextext's tokens that say it is down, rather than that one clip failed; any ``http:5xx`` too.
_NEXTEXT_DOWN: Final = frozenset({"unreachable", "timeout", "not_configured"})
#: Nextext's tokens that say it refuses Ghostwriter's key: :attr:`~ghostwriter.nextext.NextextHealth.key_refused`.
_KEY_REFUSED: Final = frozenset({"http:401", "http:403"})
#: The tokens of a create whose answer was not read, after which Nextext may hold a job whose id never came back.
_UNREAD: Final = frozenset({"timeout", "unreachable", "bad_answer"})
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
        prepare: Whether workbench prepares the file first: a ``processing`` row, or an Office file's background
            prepare; not for a document that is only measured.
        measure: Whether workbench measures the document after that (workbench 0.10), on :data:`MEASURE_SLOTS`; never
            for a picture or a clip.
    """

    attachment_id: str
    chat_id: str
    owner: str
    path: str
    kind: Literal["document", "image", "media"]
    op: Operation | None
    background: bool = False
    size: int | None = None
    prepare: bool = True
    measure: bool = False


def _requeued(ref: AttachmentRef) -> PrepareJob:
    """Return the job of an attachment left ``processing`` or its measure ``pending``; its hand-over is the system's.

    Args:
        ref: The attachment.

    Returns:
        Its job, with no request's operation: it prepares a ``processing`` row, and measures a ``pending`` one.
    """
    return PrepareJob(
        ref.id,
        ref.chat_id,
        ref.owner,
        ref.path,
        ref.kind,
        None,
        prepare=ref.status == "processing",
        measure=ref.measure_state == "pending",
    )


def _once(*listed: Sequence[AttachmentRef]) -> list[AttachmentRef]:
    """Return the attachments of several lists, each one once, in their order.

    Args:
        listed: The lists, the first one's entry kept for an attachment in more than one.

    Returns:
        The attachments.
    """
    kept: dict[str, AttachmentRef] = {}
    for refs in listed:
        for ref in refs:
            kept.setdefault(ref.id, ref)
    return list(kept.values())


@dataclass(frozen=True)
class _Running:
    """A job, the scope that cancels it, its task, and the event set once it no longer prepares.

    Attributes:
        job: The job.
        scope: The scope that cancels it.
        task: Its task.
        prepared: Set once the job has left its prepare (at once for one that only measures), so a status request
            for a ``processing`` row waits for no measure after it.
    """

    job: PrepareJob
    scope: anyio.CancelScope
    task: asyncio.Task[None]
    prepared: asyncio.Event


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


@dataclass(frozen=True)
class _Chore:
    """A task of the processor's own, no attachment's (a reconcile), and the scope that cancels it."""

    scope: anyio.CancelScope
    task: asyncio.Task[None]


@dataclass
class _Held:
    """Where a clip is at Nextext, for the ``finally`` that deletes its job and for a budget spent midway.

    Attributes:
        job_id: The job the clip made, once ``create`` has answered.
        step: The hand-over in progress, under which a spent budget's ``error`` child goes: :data:`NEXTEXT_RECIPIENT`,
            or :data:`PREPARE_RECIPIENT` while an artifact goes into workbench.
        served: The artifacts Nextext served, also one left out past its cap; not one it answered ``404`` for.
    """

    job_id: str | None = None
    step: str = NEXTEXT_RECIPIENT
    served: set[str] = field(default_factory=set)


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
        cannot decode; ``nextext_unavailable`` when it failed because Nextext could not reach its inference
        (``inference_unavailable``, Nextext 1.22 and later); else ``nextext_failed``, a job that is gone included.
    """
    if job.status == "completed":
        return None
    if job.status == "failed" and job.error_code == "undecodable_media":
        return "undecodable_media"
    if job.status == "failed" and job.error_code == "inference_unavailable":
        return "nextext_unavailable"
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
        self._measuring = asyncio.Semaphore(MEASURE_SLOTS)
        self._media_slots = asyncio.Semaphore(MEDIA_SLOTS)
        self._reconciled = asyncio.Event()
        self._owned: set[str] = set()
        self._running: dict[str, _Running] = {}
        self._start: _Chore | None = None
        self._orphans: _Chore | None = None
        self._orphans_due: float | None = None
        self._reconciling = asyncio.Lock()
        self._closed = False

    def start_media(self) -> None:
        """Start the start reconcile, which opens the clips' gate once it ends. Call it once, on the event loop.

        Without Nextext the gate opens at once. Otherwise, in the background: wait for Nextext's probe, at most
        :data:`START_WAIT_S`; once it passes, delete every job Nextext lists (:meth:`reconcile` without a grace), within
        :data:`RECONCILE_EXTRA_S`; then open the gate, also when the probe did not pass, the record could not be
        written (logged), or the reconcile was cancelled. Until then no clip makes a job, so none of this process's
        is listed. The lifespan calls it right after :meth:`requeue`, so a requeued clip waits for it.
        """
        if self._closed or self._start is not None or self._reconciled.is_set():
            return
        if self._nextext is None:
            self._reconciled.set()
            return
        self._start = self._chore(self._start_reconcile)

    async def _start_reconcile(self) -> None:
        """Wait for Nextext's probe, delete every job it lists, and open the clips' gate whatever happened."""
        try:
            if self._health is not None and not await self._health.wait_healthy(START_WAIT_S):
                logger.warning(
                    "Nextext did not pass its probe within {} s of start; its jobs are reconciled by the sweep.",
                    START_WAIT_S,
                )
                return
            with anyio.move_on_after(RECONCILE_EXTRA_S) as bound:
                await self.reconcile(grace_s=None)
            if bound.cancelled_caught:
                logger.warning(
                    "The start reconcile took more than {} s; the sweep deletes what it left.", RECONCILE_EXTRA_S
                )
        except AuditWriteError:
            logger.error("The start reconcile could not be recorded; no job was deleted at Nextext.")
        finally:
            self._reconciled.set()

    async def reconcile(self, *, grace_s: float | None) -> int:
        """Delete Ghostwriter's jobs at Nextext that no clip of this process owns, recorded first.

        Nextext lists only the jobs of Ghostwriter's token. A job a clip owns (:meth:`owned_jobs`, read before the list
        and after it, so a clip that deletes its own job meanwhile has it spared) is spared, and with a grace so is
        every job Nextext made less than ``grace_s`` ago: a create whose answer has not come back may have made it. A
        list that refuses the key tells the probe (:meth:`~ghostwriter.nextext.NextextHealth.refused`). The deletes
        are recorded first, as the system, by count only: ``delete`` / ``chat.nextext``, target ``job`` (count),
        criteria ``listed`` and ``at_start``; the jobs Nextext did not delete, also those a cancel left, get an
        ``error`` child (count), best effort. One reconcile runs at a time; another waits for it, then lists anew. On
        the event loop.

        Args:
            grace_s: Spare the jobs younger than this, in seconds; ``None`` at start, where no clip of this process
                can own a job (each waits for the start reconcile), so every job listed is deleted, whatever Nextext's
                clock says.

        Returns:
            How many jobs were deleted; 0 without Nextext, without a job to delete, or when Nextext cannot list them
            (logged by its token).

        Raises:
            AuditWriteError: When the record cannot be written; nothing is deleted.
        """
        nextext = self._nextext
        if nextext is None:
            return 0
        async with self._reconciling:
            return await self._reconcile(nextext, grace_s)

    async def _reconcile(self, nextext: NextextClient, grace_s: float | None) -> int:
        """Run one reconcile (:meth:`reconcile`), holding the processor's one reconcile lock.

        Args:
            nextext: The client.
            grace_s: As for :meth:`reconcile`.

        Returns:
            How many jobs were deleted.

        Raises:
            AuditWriteError: When the record cannot be written; nothing is deleted.
        """
        op = system_operation(self._emitter, "delete", NEXTEXT_OPERATION)
        headers = forward_headers(op.context)
        owned_before = self.owned_jobs()
        try:
            jobs = await nextext.list_jobs(headers=headers)
        except NextextUnavailable as exc:
            if exc.token in _KEY_REFUSED:
                self._refused_key(exc.token)
            logger.info("Nextext's jobs were not reconciled: {}", exc.token)
            return 0
        owned = owned_before | self.owned_jobs()
        cutoff = None if grace_s is None else utcnow() - timedelta(seconds=grace_s)
        stale = [j.job_id for j in jobs if j.job_id not in owned and (cutoff is None or j.created_at <= cutoff)]
        if not stale:
            logger.debug("Nextext lists {} jobs; none to delete.", len(jobs))
            return 0
        await anyio.to_thread.run_sync(
            partial(
                op.record,
                target=Target(kind="job", count=len(stale)),
                criteria={"listed": len(jobs), "at_start": grace_s is None},
            )
        )
        deleted = 0
        try:
            for job_id in stale:
                if await nextext.delete(job_id, headers=headers):
                    deleted += 1
        finally:
            if deleted < len(stale):
                with anyio.CancelScope(shield=True):
                    await self._undeleted(op, len(stale) - deleted)
        logger.info("Nextext: {} of {} jobs no clip owns were deleted.", deleted, len(stale))
        return deleted

    async def _undeleted(self, op: Operation, count: int) -> None:
        """Record the jobs a reconcile did not delete as its ``error`` child, best effort: a failed write is a warning.

        Args:
            op: The reconcile's operation, its primary written.
            count: How many.
        """
        logger.warning("Nextext: {} jobs no clip owns were not deleted; the next reconcile tries again.", count)
        try:
            await anyio.to_thread.run_sync(
                partial(op.child, "delete", NEXTEXT_OPERATION, target=Target(kind="job", count=count), outcome="error")
            )
        except Exception as exc:
            logger.warning("A reconcile's undeleted jobs were not recorded: {}", type(exc).__name__)

    def _reconcile_later(self) -> None:
        """Have Nextext's jobs reconciled :data:`ORPHAN_RECONCILE_S` from now, after a lost create or a failed delete.

        A create's answer went unread, or a clip's own delete failed. The lost job is then older than
        :data:`ORPHAN_GRACE_S` by :data:`RECONCILE_EXTRA_S`, also when Nextext made it a little after the create gave
        up, or its clock runs that much ahead; a job whose own delete failed is older still, and no clip owns it any
        more.

        At most one such reconcile waits at a time. A create or a delete that fails while it does moves the next one:
        once a reconcile has run, another follows the latest failure's wait, so each orphan is deleted within two waits.
        """
        if self._closed:
            return
        self._orphans_due = anyio.current_time() + ORPHAN_RECONCILE_S
        if self._orphans is None or self._orphans.task.done():
            self._orphans = self._chore(self._reconcile_orphans)

    async def _reconcile_orphans(self) -> None:
        """Reconcile once each wait after a lost job is over, sparing jobs younger than :data:`ORPHAN_GRACE_S`."""
        ran: float | None = None
        while (due := self._orphans_due) is not None and due != ran:
            await anyio.sleep(max(0.0, due - anyio.current_time()))
            try:
                await self.reconcile(grace_s=ORPHAN_GRACE_S)
            except AuditWriteError:
                logger.error("A reconcile after a lost job could not be recorded; no job was deleted at Nextext.")
            ran = due

    def _chore(self, work: Callable[[], Awaitable[None]]) -> _Chore:
        """Run one of the processor's own tasks in its cancel scope, which :meth:`aclose` cancels.

        Args:
            work: The task's work; an unexpected exception is logged by its type.

        Returns:
            The task and its scope.
        """
        scope = anyio.CancelScope()

        async def run() -> None:
            with scope:
                try:
                    await work()
                except Exception as exc:
                    logger.error("A reconcile of Nextext's jobs failed: {}", type(exc).__name__)

        return _Chore(scope, asyncio.get_running_loop().create_task(run()))

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
        prepared = asyncio.Event()
        if not job.prepare and job.kind != "media":
            prepared.set()
        task = asyncio.get_running_loop().create_task(self._run(job, scope, prepared))
        self._running[job.attachment_id] = _Running(job, scope, task, prepared)
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

    async def wait(self, attachment_id: str, timeout_s: float, *, prepare_only: bool = False) -> None:
        """Wait until the attachment's job has settled what its status request follows, at most ``timeout_s``.

        While the job prepares, that is the prepare: a ``processing`` row is settled then, and a request for it is
        answered without waiting for the measure after it. After that, it is the job's end, its measure included;
        but a request that read the row ``processing`` follows its prepare alone, and returns at once once the job
        has left it, as the stage may have moved on between its read and its wait. Without a job the status cannot
        change, so this sleeps ``timeout_s``: a client that follows it asks again no sooner.

        Args:
            attachment_id: The attachment.
            timeout_s: The longest wait, in seconds.
            prepare_only: Whether the request read the row ``processing``: its wait ends with the prepare, never with
                the measure after it.
        """
        running = self._running.get(attachment_id)
        if running is None:
            await asyncio.sleep(timeout_s)
            return
        if running.prepared.is_set():
            if not prepare_only:
                await asyncio.wait({running.task}, timeout=timeout_s)
            return
        prepared = asyncio.get_running_loop().create_task(running.prepared.wait())
        try:
            await asyncio.wait({running.task, prepared}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            prepared.cancel()

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

    def _stranded(self) -> list[AttachmentRef]:
        """Return every attachment a job of this process must take up: ``processing``, or ``ready`` and unmeasured.

        Returns:
            The rows of :meth:`~ghostwriter.store.ChatStore.processing_attachments` and of
            :meth:`~ghostwriter.store.ChatStore.pending_measures`, each attachment once.
        """
        return _once(self._store.processing_attachments(), self._store.pending_measures())

    def requeue(self) -> int:
        """Prepare again every attachment left ``processing``, and measure every one left ``pending``; at start.

        On the event loop. A ``processing`` document whose measure is pending is one job, which does both.

        Returns:
            How many jobs were started.
        """
        stranded = self._stranded()
        for ref in stranded:
            self.submit(_requeued(ref))
        if stranded:
            logger.info("{} attachments left processing or unmeasured are taken up again.", len(stranded))
        return len(stranded)

    async def requeue_chat(self, chat_id: str) -> int:
        """Take up again a chat's ``processing`` and ``pending`` attachments, once its delete or a take-back failed.

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
            stranded = await anyio.to_thread.run_sync(self._stranded)
        except Exception as exc:
            logger.warning("A chat's attachments could not be prepared again: {}", type(exc).__name__)
            return 0
        started = [ref for ref in stranded if ref.chat_id == chat_id and not self.running(ref.id)]
        for ref in started:
            self.submit(_requeued(ref))
        if started:
            logger.info("{} attachments of a chat whose delete failed are taken up again.", len(started))
        return len(started)

    async def aclose(self) -> None:
        """Cancel every job and reconcile, and wait until each has ended, its store or audit call in progress too."""
        self._closed = True
        for running in self._running.values():
            running.scope.cancel()
        chores = [chore for chore in (self._start, self._orphans) if chore is not None]
        for chore in chores:
            chore.scope.cancel()
        tasks = [running.task for running in self._running.values()] + [chore.task for chore in chores]
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

    async def _run(self, job: PrepareJob, scope: anyio.CancelScope, prepared: asyncio.Event) -> None:
        """Run one job in its cancel scope, under a slot; a cancel ends it with nothing more written.

        A background job takes the :data:`BACKGROUND_SLOTS` slot first, then one of the :data:`PREPARE_SLOTS`. A
        clip takes one of the :data:`MEDIA_SLOTS` for its round trip at Nextext, then a prepare slot
        (:meth:`_media`). A cancelled clip still deletes its Nextext job and records that, and nothing more. A
        document's measure comes last, once the job has left the prepare's slots (never inside them: an Office file
        holds the one background slot while it prepares), on one of the :data:`MEASURE_SLOTS` and no other.

        A job that fails unexpectedly still settles its attachment as a failure, best effort, so its
        message is not held back by a row nothing settles any more, and a measure it had not settled as ``none``.

        Args:
            job: The attachment.
            scope: The scope :meth:`cancel_chat` and :meth:`aclose` cancel.
            prepared: Set once the job has left its prepare.
        """
        with scope:
            try:
                if job.kind == "media":
                    await self._media(job)
                    return
                if job.prepare:
                    gate = self._background if job.background else nullcontext()
                    async with gate, self._slots:
                        await self._prepare(job)
                prepared.set()
                if job.measure:
                    async with self._measuring:
                        await self._measure(job)
            except Exception as exc:
                logger.error(
                    "Attachment '{}' ({}) could not be processed: {}", job.attachment_id, job.kind, type(exc).__name__
                )
                with suppress(Exception):
                    await self._settle_failed(job, "error")
                if job.measure:
                    with suppress(Exception):
                        await self._settle_measure(job, None, "error")

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

    async def _measure(self, job: PrepareJob) -> None:
        """Mark the chat's files, record the hand-over, have workbench measure the document and settle its measure.

        Unrecorded, nothing is sent and the measure is ``none``. A failure is recorded as the hand-over's ``error``
        child, and the measure is ``none``. A cancel (the attachment taken back, its chat deleted) settles nothing.

        Args:
            job: The document.
        """
        try:
            await anyio.to_thread.run_sync(self._store.mark_files, job.chat_id)
        except ChatNotFound:
            logger.debug("Attachment '{}': its chat is gone; nothing is measured.", job.attachment_id)
            return
        target = Target(kind="attachment", ids=(job.attachment_id,))
        op = await self._hand_over(job, target, recipient=MEASURE_RECIPIENT, criteria={"kind": job.kind})
        if op is None:
            await self._settle_measure(job, None, "audit")
            return
        call = CallFor(job.owner, job.chat_id, forward_headers(op.context))
        try:
            measure = await self._ask_measure(call, job.path)
        except WorkbenchUnavailable as exc:
            token = exc.token
        except StorageFull:
            token = "storage_full"
        except Exception as exc:
            logger.warning(
                "Attachment '{}' ({}): the measure raised {}", job.attachment_id, job.kind, type(exc).__name__
            )
            token = "error"
        else:
            await self._settle_measure(job, measure.as_stored(), None)
            return
        await self._record_error(job, op, target, _Failure(token, MEASURE_RECIPIENT))
        await self._settle_measure(job, None, token)

    async def _ask_measure(self, call: CallFor, path: str) -> Measure:
        """Measure the document, once more after :data:`PREPARE_RETRY_S` when workbench was busy.

        Args:
            call: The chat.
            path: The file's path in the chat's folder.

        Returns:
            The measure.

        Raises:
            WorkbenchUnavailable: As :meth:`~ghostwriter.workbench.WorkbenchClient.measure` raises it.
            StorageFull: On workbench's ``507``.
        """
        try:
            return await self._workbench.measure(call=call, path=path)
        except WorkbenchUnavailable as exc:
            if exc.token not in RETRIED:
                raise
            logger.info("workbench measure failed ({}); it is asked once more.", exc.token)
        await asyncio.sleep(PREPARE_RETRY_S)
        return await self._workbench.measure(call=call, path=path)

    async def _settle_measure(self, job: PrepareJob, measure: Mapping[str, Any] | None, token: str | None) -> None:
        """Settle the document's measure: ``measured`` with its counts, or ``none``.

        Args:
            job: The document.
            measure: What workbench measured; ``None`` when it could not be measured.
            token: Why not, for the log.
        """
        settled = await anyio.to_thread.run_sync(partial(self._store.settle_measure, job.attachment_id, measure))
        if not settled:
            logger.debug(
                "Attachment '{}' ({}): its measure is not kept, its row gone or settled.", job.attachment_id, job.kind
            )
        elif measure is None:
            logger.info("Attachment '{}' ({}) was not measured: {}", job.attachment_id, job.kind, token)
        else:
            logger.debug("Attachment '{}' ({}) is measured.", job.attachment_id, job.kind)

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
        """Record the hand-over of an attachment before it is sent: to workbench's prepare or measure, or to Nextext.

        Args:
            job: The attachment.
            target: The attachment, by its id.
            recipient: Where it goes: :data:`PREPARE_RECIPIENT`, :data:`MEASURE_RECIPIENT` or :data:`NEXTEXT_RECIPIENT`.
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

        Without a Nextext client, or when the clips' gate never opened, the clip is ``failed`` with
        ``nextext_unavailable`` and no hand-over is recorded: nothing is sent.

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
            nextext = self._nextext
            if nextext is None or not await self._wait_reconciled():
                logger.info(
                    "Attachment '{}' ({}): Nextext is not reached; nothing is sent or recorded.",
                    job.attachment_id,
                    job.kind,
                )
                await self._settle_failed(job, "nextext_unavailable")
                return
            criteria: dict[str, object] = {"kind": job.kind, **({"bytes": job.size} if job.size is not None else {})}
            op = await self._hand_over(job, target, recipient=NEXTEXT_RECIPIENT, criteria=criteria)
            if op is None:
                await self._settle_failed(job, "audit")
                return
            failure = await self._transcribe(nextext, job, op, target)
        if failure is None:
            async with self._slots:
                token = await self._prepared(job, op)
            if token is None:
                return
            failure = _Failure("timeout" if token == "read_timeout" else token, PREPARE_RECIPIENT)
        await self._record_error(job, op, target, failure)
        await self._settle_failed(job, failure.token)

    async def _wait_reconciled(self) -> bool:
        """Wait for the clips' gate (:meth:`start_media`), longer than the start reconcile may take.

        That is :data:`START_WAIT_S` for the probe, :data:`RECONCILE_EXTRA_S` for the reconcile, and
        :data:`_GATE_SLACK_S` for its last record, so a clip waiting at start is failed only when the gate never
        opened.

        Returns:
            Whether it is open; a clip makes no job while it is not.
        """
        if not self._reconciled.is_set():
            with anyio.move_on_after(START_WAIT_S + RECONCILE_EXTRA_S + _GATE_SLACK_S):
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
            ``None`` once its artifacts are in workbench; else why not, ``timeout`` when the budget was spent, under the
            hand-over whose step it was spent in.
        """
        call = CallFor(job.owner, job.chat_id, forward_headers(op.context))
        held = _Held()
        failure: _Failure | None = None
        try:
            with anyio.move_on_after(self._nextext_timeout_s) as budget:
                failure = await self._round_trip(nextext, job, op, target, call, held)
            if budget.cancelled_caught:
                failure = _Failure("timeout", held.step)
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
            held: Where the job's id goes as soon as Nextext has given it, and the step in progress.

        Returns:
            ``None`` once the artifacts Nextext made are in workbench; else why not.
        """
        clip = _clip_type(job.path)
        if clip is None:
            return _Failure("unsupported_format", NEXTEXT_RECIPIENT)
        ext, mime = clip
        try:
            async with self._workbench.view(call=call, path=job.path) as response:
                job_id = await self._create(nextext, response, ext, mime, call.headers)
                held.job_id = job_id
                self._owned.add(job_id)
            ended = await self._finished(nextext, job, job_id, call.headers)
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
            if token == "nextext_unavailable" and self._health is not None:
                self._health.failed()
            return _Failure(token, NEXTEXT_RECIPIENT)
        if ended.skipped:
            logger.info(
                "Attachment '{}' ({}): Nextext skipped it: {}", job.attachment_id, job.kind, ended.skip_reason_code
            )
        if not await self._handed_to_workbench(job, op, target):
            return _Failure("audit", None)
        for name, role in _ARTIFACTS:
            failure = await self._take(nextext, job, call, job_id, held, name, role)
            if failure is not None:
                return failure
        if not held.served and not ended.skipped:
            return await self._served_nothing(nextext, job, job_id, call.headers)
        return None

    async def _create(
        self, nextext: NextextClient, response: httpx.Response, ext: str, mime: str, headers: Mapping[str, str]
    ) -> str:
        """Make the clip's job from workbench's view; after a create whose answer was not read, reconcile later.

        Such a create (its token in :data:`_UNREAD`, or a cancel meanwhile) may have left a job at Nextext whose id
        never came back, so no clip owns it: :meth:`_reconcile_later`. A clip whose read from workbench failed, or
        that passed the cap, was cut off before Nextext had all of it, and made no job.

        Args:
            nextext: The client.
            response: workbench's view of the clip, its body not read yet.
            ext: The clip's suffix, with its dot.
            mime: Its media type.
            headers: The operation's headers.

        Returns:
            Nextext's id for the job.

        Raises:
            NextextUnavailable: As :meth:`~ghostwriter.nextext.NextextClient.create` raises it.
            WorkbenchUnavailable: When the read from workbench fails.
            UploadTooLarge: Past the cap.
        """
        unread = True
        try:
            job_id = await nextext.create(_from_workbench(response), ext=ext, mime=mime, headers=headers)
        except NextextUnavailable as exc:
            unread = exc.token in _UNREAD
            raise
        except (WorkbenchUnavailable, UploadTooLarge):
            unread = False
            raise
        else:
            unread = False
            return job_id
        finally:
            if unread:
                logger.info(
                    "A create's answer was not read; Nextext's jobs are reconciled in {} s.",
                    ORPHAN_RECONCILE_S,
                )
                self._reconcile_later()

    async def _finished(
        self, nextext: NextextClient, job: PrepareJob, job_id: str, headers: Mapping[str, str]
    ) -> NextextJob:
        """Poll a job through Nextext's list until it has ended: every :data:`POLL_S`, then every :data:`POLL_SLOW_S`.

        A poll that fails is ridden out, at most :data:`POLL_TOLERATED` in a row; the next one that fails raises.

        Args:
            nextext: The client.
            job: The clip, for the log.
            job_id: The job.
            headers: The operation's headers.

        Returns:
            The job in a status of :data:`~ghostwriter.nextext.TERMINAL`, or ``gone``.

        Raises:
            NextextUnavailable: As :meth:`~ghostwriter.nextext.NextextClient.poll` raises it, once more than
                :data:`POLL_TOLERATED` polls in a row have failed.
        """
        started = anyio.current_time()
        failed = 0
        while True:
            await anyio.sleep(POLL_SLOW_S if anyio.current_time() - started >= POLL_SLOW_AFTER_S else POLL_S)
            try:
                polled = await nextext.poll(job_id, headers=headers)
            except NextextUnavailable as exc:
                failed += 1
                if failed > POLL_TOLERATED:
                    raise
                logger.info(
                    "Attachment '{}' ({}): a poll of Nextext's job '{}' failed ({}), {} of {} in a row ridden out.",
                    job.attachment_id,
                    job.kind,
                    job_id,
                    exc.token,
                    failed,
                    POLL_TOLERATED,
                )
                continue
            failed = 0
            if polled.status in TERMINAL or polled.status == "gone":
                return polled

    async def _served_nothing(
        self, nextext: NextextClient, job: PrepareJob, job_id: str, headers: Mapping[str, str]
    ) -> _Failure:
        """Read a completed job, not skipped, that served neither artifact: polled once more, it failed either way.

        A clip with speech always has a transcript, so a job that was not skipped for no speech and served none made
        none, or is gone since its last poll (Nextext restarted before the downloads): ``nextext_failed``. The poll
        tells the two apart in the log; a poll that fails is read as any failure of Nextext's.

        Args:
            nextext: The client.
            job: The clip.
            job_id: The job.
            headers: The operation's headers.

        Returns:
            ``nextext_failed``; or ``nextext_unavailable`` when the poll finds Nextext down.
        """
        try:
            polled = await nextext.poll(job_id, headers=headers)
        except NextextUnavailable as exc:
            return self._unavailable(job, exc.token)
        logger.info(
            "Attachment '{}' ({}): Nextext's job '{}' ({}) served no transcript and no frames: nextext_failed",
            job.attachment_id,
            job.kind,
            job_id,
            polled.status,
        )
        return _Failure("nextext_failed", NEXTEXT_RECIPIENT)

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
        self,
        nextext: NextextClient,
        job: PrepareJob,
        call: CallFor,
        job_id: str,
        held: _Held,
        name: str,
        role: DerivedRole,
    ) -> _Failure | None:
        """Stream one artifact into workbench, once more after :data:`PREPARE_RETRY_S` when workbench was busy.

        An artifact Nextext did not make, or one past its cap, is left out.

        Args:
            nextext: The client.
            job: The clip.
            call: The chat.
            job_id: The job.
            held: The clip's step, and the artifacts served, to which this adds one left out past its cap.
            name: The artifact at Nextext.
            role: What it is to workbench.

        Returns:
            ``None`` once it is in workbench or left out; else why not.
        """
        try:
            try:
                await self._derive(nextext, job, call, job_id, held, name, role)
            except WorkbenchUnavailable as exc:
                if exc.token not in RETRIED:
                    raise
                logger.info("workbench derived failed ({}); it is asked once more.", exc.token)
                await asyncio.sleep(PREPARE_RETRY_S)
                await self._derive(nextext, job, call, job_id, held, name, role)
        except ArtifactTooLarge:
            held.served.add(name)
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
        self,
        nextext: NextextClient,
        job: PrepareJob,
        call: CallFor,
        job_id: str,
        held: _Held,
        name: str,
        role: DerivedRole,
    ) -> None:
        """Stream one artifact from Nextext into workbench, chunk by chunk; nothing when Nextext made none.

        The clip's step is Nextext's while the artifact is asked for, and workbench's once it streams into workbench.

        Args:
            nextext: The client.
            job: The clip.
            call: The chat.
            job_id: The job.
            held: The clip's step, which this sets, and the artifacts served, to which this adds ``name`` when Nextext
                serves it.
            name: The artifact at Nextext.
            role: What it is to workbench.

        Raises:
            ArtifactTooLarge: When it passes its cap, by its declared length or as it streams.
            NextextUnavailable: When Nextext cannot serve it.
            WorkbenchUnavailable: As :meth:`~ghostwriter.workbench.WorkbenchClient.derived` raises it.
            StorageFull: On workbench's ``507``.
        """
        held.step = NEXTEXT_RECIPIENT
        async with nextext.artifact(job_id, name, headers=call.headers) as body:
            if body is None:
                logger.debug("Attachment '{}' ({}): Nextext made no {}.", job.attachment_id, job.kind, role)
                return
            held.served.add(name)
            held.step = PREPARE_RECIPIENT
            await self._workbench.derived(call=call, source=job.path, role=role, chunks=body)

    def _unavailable(self, job: PrepareJob, token: str) -> _Failure:
        """Read a token of Nextext's client, telling the probe when Nextext is down.

        Args:
            job: The clip.
            token: :class:`~ghostwriter.nextext.NextextUnavailable`'s token.

        Returns:
            ``nextext_unavailable`` for a Nextext that does not answer, times out or fails with a ``5xx`` (the probe is
            told, so clips are not offered until it answers its probe again), or that refuses the key (one error
            names ``NEXTEXT_API_KEY``, and clips are not offered until a keyed list of the probe's passes); else
            ``nextext_failed``.
        """
        if token in _KEY_REFUSED:
            self._refused_key(token)
            failure = "nextext_unavailable"
        elif token in _NEXTEXT_DOWN or token.startswith("http:5"):
            if self._health is not None:
                self._health.failed()
            failure = "nextext_unavailable"
        else:
            failure = "nextext_failed"
        logger.info("Attachment '{}' ({}): Nextext failed ({}): {}", job.attachment_id, job.kind, token, failure)
        return _Failure(failure, NEXTEXT_RECIPIENT)

    def _refused_key(self, token: str) -> None:
        """Tell the probe that Nextext refuses the key, which logs one error naming the setting; on the event loop.

        Args:
            token: ``http:401`` or ``http:403``.
        """
        if self._health is not None:
            self._health.refused(token)
        else:
            logger.error("Nextext refuses NEXTEXT_API_KEY ({}).", token)

    async def _delete_job(
        self, nextext: NextextClient, job: PrepareJob, op: Operation, call: CallFor, job_id: str
    ) -> None:
        """Delete a clip's job at Nextext and record that, shielded from a cancel; never raises but a cancel after it.

        A delete Nextext refuses, or does not answer within :data:`~ghostwriter.nextext.CALL_S`, leaves a job no clip
        owns: it is reconciled :data:`ORPHAN_RECONCILE_S` later (:meth:`_reconcile_later`), as after a lost create.

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
            return
        logger.warning(
            "Attachment '{}': Nextext's job '{}' could not be deleted; Nextext's jobs are reconciled in {} s.",
            job.attachment_id,
            job_id,
            ORPHAN_RECONCILE_S,
        )
        self._reconcile_later()

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
