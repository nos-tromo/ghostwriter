"""Ghostwriter's client for Nextext: one job per clip, streamed in, polled through the list, streamed out.

Nextext's API (``/api/v1`` under :attr:`~ghostwriter.config.Settings.nextext_api_base`) is reached with
Ghostwriter's service token, ``Authorization: Bearer <NEXTEXT_API_KEY>``, and the purpose and request id of the
operation (``X-Audit-Purpose``, ``X-Request-ID``: :func:`ghostwriter.audit.forward_headers`). Each call is made
over a fresh ``httpx.AsyncClient`` that takes no proxy from the environment, on the caller's event loop.

One job is made for one clip (:meth:`NextextClient.create`): the clip streams chunk by chunk, counted against
:data:`~ghostwriter.workbench.MAX_ATTACHMENT_BYTES`, as the multipart field ``file`` named ``media.<ext>``, never
under the user's own file name, with the field ``options`` (:data:`JOB_OPTIONS`) beside it. The job is read
through ``GET /jobs`` (:meth:`NextextClient.poll`), never through ``GET /jobs/{id}``, whose answer holds the whole
transcript, which Ghostwriter must not hold. A job that the list no longer names is ``gone``. Each of the two
artifacts, the transcript (:data:`JSONL`) and the frames (:data:`KEYFRAMES`), streams back within its cap
(:data:`ARTIFACT_CAPS`), checked on the declared length first and then on the bytes. A job is deleted as soon as
its artifacts are taken (:meth:`NextextClient.delete`), which never raises.

Clips are offered only while Nextext is up and workbench takes them (:class:`NextextHealth`): a background probe of
``GET /health`` that start-up never waits for, and the version workbench names itself (:func:`serves_media`).

The log holds statuses, counts and tokens, never a file name, a transcript line, Nextext's error text or the key.
"""

import asyncio
import contextlib
import json
import re
import secrets
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx
from loguru import logger

from ghostwriter.config import Settings
from ghostwriter.utils.audit_runtime import PURPOSE_HEADER, REQUEST_ID_HEADER
from ghostwriter.vision import system_call
from ghostwriter.workbench import MAX_ATTACHMENT_BYTES, UploadTooLarge, WorkbenchClient

#: The name a clip goes under at Nextext, before its suffix.
MEDIA_NAME: Final = "media"
#: The options of every job: frames at 20 a minute, at most 19 (workbench keeps 19), no captions, no summary.
JOB_OPTIONS: Final[dict[str, bool | int]] = {
    "keyframes": True,
    "visual_context": False,
    "summarization": False,
    "keyframes_max": 19,
    "keyframes_per_minute": 20,
}
#: The transcript, as one JSON line per sentence.
JSONL: Final = "docint.jsonl"
#: The frames, as a zip of pictures with a manifest.
KEYFRAMES: Final = "keyframes.zip"
#: The most bytes each artifact may have.
ARTIFACT_CAPS: Final = {JSONL: 8 * 1024 * 1024, KEYFRAMES: 32 * 1024 * 1024}
#: The statuses after which a job does not change.
TERMINAL: Final = frozenset({"completed", "failed", "cancelled", "interrupted"})
#: How long a create waits for Nextext's answer once the clip is sent, in seconds.
CREATE_READ_S: Final = 120.0
#: The deadline of a list and of a delete, in seconds.
CALL_S: Final = 10.0
#: How long one read of an artifact may wait, in seconds.
ARTIFACT_READ_S: Final = 60.0
#: The deadline of the health probe, in seconds.
HEALTH_S: Final = 5.0
#: The most bytes the job list may have.
LIST_MAX_BYTES: Final = 1024 * 1024
#: The first workbench that takes a clip, as ``(major, minor)``: 0.9.
MEDIA_WORKBENCH: Final = (0, 9)
#: The wait after the first failed probe, in seconds; it doubles with each failure.
PROBE_FIRST_S: Final = 5.0
#: The longest wait between two probes of an unhealthy Nextext, in seconds.
PROBE_MAX_S: Final = 300.0

_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_EXT = re.compile(r"\.[a-z0-9]{1,8}")
# Mirrors workbench.py's pattern of the same name.
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}")
# A declared Content-Length: ASCII digits only, so no non-ASCII digit or oversized number reaches int().
_LENGTH = re.compile(r"[0-9]{1,15}")
# A version workbench names itself as offered: dotted numbers, at least two, of ASCII digits.
_RELEASE = re.compile(r"([0-9]{1,6})\.([0-9]{1,6})(?:\.[0-9]{1,6})*")
_STATUSES = frozenset({"queued", "running"} | TERMINAL)
_FORWARDED = frozenset({PURPOSE_HEADER.lower(), REQUEST_ID_HEADER.lower()})
_ANSWER_MAX_BYTES = 64 * 1024
_CONNECT_S = 3.0
_WRITE_S = 10.0
_POOL_S = 3.0


class NextextUnavailable(Exception):
    """Nextext could not do what was asked; ``token`` says why."""

    def __init__(self, token: str) -> None:
        """Name the failure.

        Args:
            token: Why, as a token safe to log: ``unreachable``, ``timeout``, ``http:<status>``, ``bad_answer``
                for an answer that cannot be read, or ``not_configured`` without a key or a base.
        """
        super().__init__(token)
        self.token = token


class ArtifactTooLarge(Exception):
    """An artifact is longer than its cap in :data:`ARTIFACT_CAPS`."""


@dataclass(frozen=True)
class NextextJob:
    """One job, as Nextext's list shows it.

    Attributes:
        job_id: Nextext's id for the job.
        status: ``queued``, ``running``, ``completed``, ``failed``, ``cancelled`` or ``interrupted``; or ``gone``,
            for a job the list does not name.
        error_code: The typed cause of a failure, such as ``undecodable_media``; never Nextext's error text.
        skipped: Whether the job produced nothing on purpose.
        skip_reason_code: Why, such as ``vad_no_speech``.
        created_at: When Nextext made the job (aware, UTC); for a job that is gone, when it was noticed.
    """

    job_id: str
    status: str
    error_code: str | None
    skipped: bool
    skip_reason_code: str | None
    created_at: datetime


def _created_at(value: object) -> datetime | None:
    """Read a time Nextext wrote, such as ``2026-11-22T22:54:00.912174Z``.

    Args:
        value: The item's ``created_at``.

    Returns:
        The time in UTC (one with no offset is read as UTC); ``None`` when it is no ISO 8601 time.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _code(value: object) -> str | None:
    """Return a typed code, or ``None`` for none or anything that is no token.

    Args:
        value: An item's ``error_code`` or ``skip_reason_code``.

    Returns:
        The code, or ``None``.
    """
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _job(entry: object) -> NextextJob | None:
    """Read one item of ``GET /jobs``.

    Args:
        entry: The item.

    Returns:
        The job; ``None`` when the item has no valid id, no known status, no ``skipped`` flag or no valid time.
    """
    if not isinstance(entry, dict):
        return None
    job_id, status, skipped = entry.get("job_id"), entry.get("status"), entry.get("skipped", False)
    created = _created_at(entry.get("created_at"))
    if (
        not isinstance(job_id, str)
        or not _JOB_ID.fullmatch(job_id)
        or not isinstance(status, str)
        or status not in _STATUSES
        or not isinstance(skipped, bool)
        or created is None
    ):
        return None
    return NextextJob(
        job_id, status, _code(entry.get("error_code")), skipped, _code(entry.get("skip_reason_code")), created
    )


async def _job_body(boundary: str, ext: str, mime: str, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Stream the body of a create: the options, then the clip as the field ``file``, cut off past the cap.

    Args:
        boundary: The body's boundary.
        ext: The clip's suffix, lower-cased, with its dot.
        mime: Its media type.
        chunks: Its bytes.

    Yields:
        The options part, the file part's headers, the bytes, then the closing boundary.

    Raises:
        UploadTooLarge: Past :data:`~ghostwriter.workbench.MAX_ATTACHMENT_BYTES`, before the chunk that passes it.
    """
    options = json.dumps(JOB_OPTIONS, separators=(",", ":"))
    yield (
        f'--{boundary}\r\nContent-Disposition: form-data; name="options"\r\n'
        f"Content-Type: application/json\r\n\r\n{options}\r\n"
    ).encode()
    yield (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{MEDIA_NAME}{ext}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode()
    sent = 0
    async for chunk in chunks:
        sent += len(chunk)
        if sent > MAX_ATTACHMENT_BYTES:
            raise UploadTooLarge
        if chunk:
            yield chunk
    yield f"\r\n--{boundary}--\r\n".encode()


async def _json_answer(response: httpx.Response, limit: int) -> object:
    """Read an answer's body as one JSON document of at most ``limit`` bytes.

    Mirrors ``workbench._json_answer``, for the answers of this client: any status is the caller's to check.

    Args:
        response: The streamed answer.
        limit: The most bytes the body may have.

    Returns:
        The document.

    Raises:
        NextextUnavailable: ``bad_answer`` for a body that is longer, or no JSON.
    """
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw += chunk
        if len(raw) > limit:
            raise NextextUnavailable("bad_answer")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise NextextUnavailable("bad_answer") from None


@contextlib.contextmanager
def _errors(operation: str) -> Iterator[None]:
    """Turn httpx's errors into :class:`NextextUnavailable`, and log each failure by its token.

    Args:
        operation: What is being done, for the log.

    Yields:
        Nothing.

    Raises:
        NextextUnavailable: ``timeout`` or ``unreachable`` for httpx's errors, ``bad_answer`` for the others,
            or as raised inside.
        UploadTooLarge: As raised inside.
        ArtifactTooLarge: As raised inside.
    """
    try:
        yield
    except httpx.TimeoutException:
        token = "timeout"
    except httpx.TransportError:
        token = "unreachable"
    except httpx.HTTPError:
        token = "bad_answer"
    except NextextUnavailable as exc:
        logger.warning("nextext {} failed: {}", operation, exc.token)
        raise
    except (UploadTooLarge, ArtifactTooLarge) as exc:
        logger.info(
            "nextext {} refused: {}",
            operation,
            "upload_too_large" if isinstance(exc, UploadTooLarge) else "artifact_too_large",
        )
        raise
    else:
        return
    logger.warning("nextext {} failed: {}", operation, token)
    raise NextextUnavailable(token) from None


async def _capped(response: httpx.Response, cap: int) -> AsyncGenerator[bytes, None]:
    """Stream an artifact's body, cut off before the chunk that passes ``cap``.

    Args:
        response: The ``200`` answer, its body not read yet.
        cap: The most bytes it may have.

    Yields:
        Its chunks, as they arrive.

    Raises:
        NextextUnavailable: When the read fails.
        ArtifactTooLarge: Before the chunk that would take the body past ``cap``.
    """
    seen = 0
    with _errors("artifact"):
        async for chunk in response.aiter_bytes():
            seen += len(chunk)
            if seen > cap:
                raise ArtifactTooLarge
            if chunk:
                yield chunk


class NextextClient:
    """Reach Nextext for the jobs of Ghostwriter's clips."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """Create the client.

        Args:
            settings: The base URL and the key.
            transport: Injected transport (tests pass an ``httpx.MockTransport``); ``None`` uses httpx's own.
        """
        self._settings = settings
        self._transport = transport

    def _http(self, read_s: float) -> httpx.AsyncClient:
        """Return a client for one call.

        Args:
            read_s: The longest wait for each read of the answer.

        Returns:
            A client on the base URL that takes no proxy from the environment; connect 3 s, write 10 s, pool 3 s.
        """
        return httpx.AsyncClient(
            base_url=self._settings.nextext_api_base,
            transport=self._transport,
            trust_env=False,
            timeout=httpx.Timeout(read_s, connect=_CONNECT_S, write=_WRITE_S, pool=_POOL_S),
        )

    def _headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Return the headers every call but the health probe carries.

        Args:
            headers: The operation's headers; only its purpose and request id are sent on.

        Returns:
            The purpose and the request id, the key, and ``Accept-Encoding: identity``.

        Raises:
            NextextUnavailable: ``not_configured`` without the key or the base.
        """
        key = self._settings.nextext_api_key
        if key is None or not self._settings.nextext_enabled:
            raise NextextUnavailable("not_configured")
        forwarded = {name: value for name, value in headers.items() if name.lower() in _FORWARDED}
        return {**forwarded, "Authorization": f"Bearer {key}", "Accept-Encoding": "identity"}

    async def create(self, chunks: AsyncIterator[bytes], *, ext: str, mime: str, headers: Mapping[str, str]) -> str:
        """Make the job of one clip, streaming the clip in.

        Args:
            chunks: The clip's bytes, as they arrive; closed when the create returns or raises.
            ext: The clip's suffix, lower-cased, with its dot, such as ``.mp4``; Nextext sees ``media`` and this.
            mime: The clip's media type, without parameters.
            headers: The operation's headers (:func:`ghostwriter.audit.forward_headers`).

        Returns:
            Nextext's id for the job.

        Raises:
            ValueError: If ``ext`` is not a lower-case suffix or ``mime`` not a bare media type; nothing is sent.
            UploadTooLarge: Past :data:`~ghostwriter.workbench.MAX_ATTACHMENT_BYTES`, before more is sent.
            NextextUnavailable: For any answer but a ``201`` with a valid ``job_id``.
        """
        try:
            return await self._create(chunks, ext, mime, headers)
        finally:
            aclose = getattr(chunks, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _create(self, chunks: AsyncIterator[bytes], ext: str, mime: str, headers: Mapping[str, str]) -> str:
        if not _EXT.fullmatch(ext):
            raise ValueError("ext must be a lower-case suffix")
        if not _MEDIA_TYPE.fullmatch(mime):
            raise ValueError("mime must be a bare media type")
        with _errors("create"):
            boundary = secrets.token_hex(16)
            sent = {
                **self._headers(headers),
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            }
            body = _job_body(boundary, ext, mime, chunks)
            async with (
                self._http(CREATE_READ_S) as client,
                client.stream("POST", "/jobs", content=body, headers=sent) as response,
            ):
                if response.status_code != 201:
                    raise NextextUnavailable(f"http:{response.status_code}")
                answer = await _json_answer(response, _ANSWER_MAX_BYTES)
            job_id = answer.get("job_id") if isinstance(answer, dict) else None
            if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
                raise NextextUnavailable("bad_answer")
        logger.debug("nextext created job '{}'", job_id)
        return job_id

    async def list_jobs(self, *, headers: Mapping[str, str]) -> list[NextextJob]:
        """List the jobs Nextext holds for Ghostwriter's token.

        Args:
            headers: The operation's headers; the list is refused without a purpose.

        Returns:
            The jobs, each item that reads; one that does not is left out and counted in one warning.

        Raises:
            NextextUnavailable: ``http:<status>``, ``bad_answer`` for a body that is longer than
                :data:`LIST_MAX_BYTES` or does not read, ``timeout`` or ``unreachable``.
        """
        with _errors("list"):
            sent = {**self._headers(headers), "Accept": "application/json"}
            async with (
                self._http(CALL_S) as client,
                client.stream("GET", "/jobs", headers=sent) as response,
            ):
                if response.status_code != 200:
                    raise NextextUnavailable(f"http:{response.status_code}")
                answer = await _json_answer(response, LIST_MAX_BYTES)
            listed = answer.get("jobs") if isinstance(answer, dict) else None
            if not isinstance(listed, list):
                raise NextextUnavailable("bad_answer")
        jobs = [job for entry in listed if (job := _job(entry)) is not None]
        if len(jobs) < len(listed):
            logger.warning("nextext list: {} of {} items did not read", len(listed) - len(jobs), len(listed))
        return jobs

    async def poll(self, job_id: str, *, headers: Mapping[str, str]) -> NextextJob:
        """Read one job from the list: never its result, which holds the transcript.

        Args:
            job_id: Nextext's id for the job.
            headers: The operation's headers.

        Returns:
            The job's item; with the status ``gone`` when the list does not name it (it was deleted, or Nextext
            restarted, its jobs living in memory only).

        Raises:
            NextextUnavailable: As :meth:`list_jobs`.
        """
        for job in await self.list_jobs(headers=headers):
            if job.job_id == job_id:
                return job
        return NextextJob(job_id, "gone", None, False, None, datetime.now(UTC))

    @contextlib.asynccontextmanager
    async def artifact(
        self, job_id: str, name: str, *, headers: Mapping[str, str]
    ) -> AsyncIterator[AsyncIterator[bytes] | None]:
        """Stream one artifact of a job, within its cap.

        Args:
            job_id: Nextext's id for the job, as :meth:`create` returned it.
            name: :data:`JSONL` or :data:`KEYFRAMES`.
            headers: The operation's headers.

        Yields:
            The artifact's chunks, as they arrive; ``None`` when Nextext made no such artifact (``404``: a clip
            without speech has no transcript, audio has no frames, a job that is gone has neither).

        Raises:
            ValueError: If ``name`` is neither artifact or ``job_id`` is not an id Nextext gives; nothing is sent.
            ArtifactTooLarge: When the declared ``Content-Length`` passes the cap, before the body is read, or
                from the chunks, before the one that passes it.
            NextextUnavailable: On any other answer, and on a read that fails.
        """
        cap = ARTIFACT_CAPS.get(name)
        if cap is None or not _JOB_ID.fullmatch(job_id):
            raise ValueError("not an artifact of a job")
        chunks: AsyncIterator[bytes] | None = None
        async with contextlib.AsyncExitStack() as stack:
            with _errors("artifact"):
                sent = self._headers(headers)
                client = await stack.enter_async_context(self._http(ARTIFACT_READ_S))
                response = await stack.enter_async_context(
                    client.stream("GET", f"/jobs/{job_id}/artifacts/{name}", headers=sent)
                )
                if response.status_code == 200:
                    declared = response.headers.get("content-length", "")
                    if _LENGTH.fullmatch(declared) and int(declared) > cap:
                        raise ArtifactTooLarge
                    chunks = await stack.enter_async_context(contextlib.aclosing(_capped(response, cap)))
                elif response.status_code != 404:
                    raise NextextUnavailable(f"http:{response.status_code}")
            yield chunks

    async def delete(self, job_id: str, *, headers: Mapping[str, str]) -> bool:
        """Delete a job, whatever state it is in. A job that is already gone is deleted.

        Args:
            job_id: Nextext's id for the job.
            headers: The operation's headers.

        Returns:
            ``True`` for ``204`` and ``404``; ``False`` for anything else, logged by its token. It never raises,
            a cancellation apart, so a cleanup that must go on can call it.
        """
        if not _JOB_ID.fullmatch(job_id):
            logger.warning("nextext delete failed: invalid_id")
            return False
        try:
            with _errors("delete"):
                sent = self._headers(headers)
                async with (
                    self._http(CALL_S) as client,
                    client.stream("DELETE", f"/jobs/{job_id}", headers=sent) as response,
                ):
                    if response.status_code not in (204, 404):
                        raise NextextUnavailable(f"http:{response.status_code}")
        except NextextUnavailable:
            return False
        return True

    async def healthy(self) -> bool:
        """Ask ``GET /health``, without the key. It never raises, a cancellation apart.

        Returns:
            ``True`` only for ``200``: Nextext answers it while its Whisper is down, so it says Nextext is up, not
            that a clip will transcribe.
        """
        try:
            async with self._http(HEALTH_S) as client:
                response = await client.get("/health", headers={"Accept-Encoding": "identity"})
        except Exception as exc:
            logger.debug("nextext health: {}", type(exc).__name__)
            return False
        if response.status_code != 200:
            logger.debug("nextext health: http:{}", response.status_code)
            return False
        return True


def serves_media(version: str) -> bool:
    """Say whether a workbench of this version takes clips: 0.9.0 and later.

    Args:
        version: The version workbench names itself, as its discovery carries it.

    Returns:
        ``True`` for ``X.Y`` or ``X.Y.Z...`` of ASCII digits with ``(X, Y)`` at :data:`MEDIA_WORKBENCH` or
        later; ``False`` for an older one, an empty one, and any other text.
    """
    found = _RELEASE.fullmatch(version)
    return found is not None and (int(found[1]), int(found[2])) >= MEDIA_WORKBENCH


class NextextHealth:
    """Whether Nextext is up, probed in the background, and whether clips are offered.

    :meth:`run` asks :meth:`NextextClient.healthy` once, then waits until a clip's job reports Nextext failing
    (:meth:`failed`) and asks again; while Nextext does not answer ``200`` it asks again after
    :data:`PROBE_FIRST_S`, doubling to :data:`PROBE_MAX_S`. Start-up never waits for it: until the first probe
    has answered, Nextext is not healthy and no clip is offered.

    Attributes:
        healthy: Whether the last probe said Nextext is up and no job has failed since; ``False`` at first.
        checked: Set after each probe, whatever it found.
    """

    def __init__(self, client: NextextClient) -> None:
        """Start unhealthy, with no probe made.

        Args:
            client: The client whose :meth:`~NextextClient.healthy` is the probe.
        """
        self._client = client
        self.healthy = False
        self.checked = asyncio.Event()
        self._up = asyncio.Event()
        self._failed = asyncio.Event()

    def _mark(self, healthy: bool) -> None:
        self.healthy = healthy
        if healthy:
            self._up.set()
        else:
            self._up.clear()

    async def run(self) -> None:
        """Probe for as long as the process serves; cancelled at shutdown.

        A probe that says healthy is followed by a wait for :meth:`failed`, then by a probe at once; one that does
        not is followed by a sleep, :data:`PROBE_FIRST_S` at first and doubling to :data:`PROBE_MAX_S`, which a
        healthy probe starts over.
        """
        delay = PROBE_FIRST_S
        known: bool | None = None
        while True:
            self._failed.clear()
            up = await self._client.healthy()
            self._mark(up)
            self.checked.set()
            if up != known:
                known = up
                if up:
                    logger.info("Nextext answers its health probe; video and audio are offered with workbench 0.9.")
                else:
                    logger.warning("Nextext does not answer its health probe; video and audio are not offered.")
            if up:
                delay = PROBE_FIRST_S
                await self._failed.wait()
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, PROBE_MAX_S)

    def failed(self) -> None:
        """Take Nextext as down at once, and have :meth:`run` probe it again; on the event loop."""
        self._mark(False)
        self._failed.set()

    async def wait_healthy(self, timeout_s: float) -> bool:
        """Wait until Nextext is healthy, at most ``timeout_s``.

        Args:
            timeout_s: The longest wait, in seconds.

        Returns:
            Whether Nextext is healthy: at once when it is, else once a probe says so, or at the limit.
        """
        if self.healthy:
            return True
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_s):
                await self._up.wait()
        return self.healthy

    def offered(self, workbench: WorkbenchClient | None) -> bool:
        """Say whether video and audio are offered: Nextext is healthy and the workbench takes clips.

        The version is read from workbench's cached discovery (:func:`~ghostwriter.vision.system_call`, the listing
        without images), which blocks at most once per cache window: call this from a thread, never on the event
        loop. An unhealthy Nextext is answered without asking workbench.

        Args:
            workbench: The workbench client, ``None`` while workbench is off.

        Returns:
            ``True`` when :attr:`healthy`, there is a workbench, and its version is one that :func:`serves_media`.
        """
        if not self.healthy or workbench is None:
            return False
        found = workbench.discover(call=system_call(), images=False)
        return found is not None and serves_media(found.server_version)
