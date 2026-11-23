"""The Nextext client: one job per clip, polled through the list, artifacts streamed within their caps.

Nextext is mocked from the contract measured against its 1.21 API alone (:class:`tests.fakes.FakeNextext`).
Every name, id and value here is invented.
"""

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from loguru import logger

from ghostwriter import nextext
from ghostwriter.audit import RequestContext, forward_headers
from ghostwriter.nextext import (
    ARTIFACT_CAPS,
    JOB_OPTIONS,
    JSONL,
    KEYFRAMES,
    LIST_MAX_BYTES,
    MEDIA_NAME,
    PROBE_FIRST_S,
    PROBE_MAX_S,
    TERMINAL,
    ArtifactTooLarge,
    NextextClient,
    NextextHealth,
    NextextJob,
    NextextUnavailable,
    serves_media,
)
from ghostwriter.utils.audit_event import Actor
from ghostwriter.workbench import MAX_ATTACHMENT_BYTES, UploadTooLarge
from tests.conftest import NEXTEXT_BASE, NEXTEXT_KEY, make_settings
from tests.fakes import FAILURE_TEXT, JOB_CREATED_AT, JOB_ID, FakeNextext, FakeWorkbench, parse_job_upload

CFG = make_settings(Path("unused"), nextext_api_base=NEXTEXT_BASE, nextext_api_key=NEXTEXT_KEY)
HEADERS = forward_headers(RequestContext(Actor("user-a", "edge"), None, "req-0001"))
NAME = "harbour-tour.mp4"
CLIP = b"\x00\x00\x00\x18ftypmp42" + bytes(200)
MIB = 1024 * 1024
CREATED = datetime(2026, 11, 22, 22, 54, 0, 912174, tzinfo=UTC)


def _client(fake: FakeNextext, settings: Any = CFG) -> NextextClient:
    return NextextClient(settings, transport=httpx.MockTransport(fake))


def _client_for(handler: Any) -> NextextClient:
    return NextextClient(CFG, transport=httpx.MockTransport(handler))


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def _create(client: NextextClient, *parts: bytes) -> str:
    return await client.create(_chunks(*(parts or (CLIP,))), ext=".mp4", mime="video/mp4", headers=HEADERS)


def _length(value: str) -> list[tuple[bytes, bytes]]:
    """Return a Content-Length header as the wire carries it, so a non-ASCII value is not refused by httpx."""
    return [(b"Content-Length", value.encode("utf-8"))]


async def _read(client: NextextClient, job_id: str = JOB_ID, name: str = JSONL) -> list[bytes] | None:
    async with client.artifact(job_id, name, headers=HEADERS) as chunks:
        return None if chunks is None else [chunk async for chunk in chunks]


@pytest.fixture
def log_lines() -> Iterator[list[str]]:
    """Capture every loguru line, DEBUG included.

    Yields:
        The lines written while the test runs.
    """
    lines: list[str] = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level="DEBUG", format="{message}")
    try:
        yield lines
    finally:
        logger.remove(handler_id)


# --- the contract ------------------------------------------------------------


def test_the_job_options_are_the_ones_the_plan_fixes() -> None:
    """The options a job is created with are exactly these: frames at 20 a minute, at most 19, no captions."""
    assert JOB_OPTIONS == {
        "keyframes": True,
        "visual_context": False,
        "summarization": False,
        "keyframes_max": 19,
        "keyframes_per_minute": 20,
    }
    assert MEDIA_NAME == "media"
    assert (JSONL, KEYFRAMES) == ("docint.jsonl", "keyframes.zip")
    assert ARTIFACT_CAPS == {JSONL: 8 * MIB, KEYFRAMES: 32 * MIB}


def test_interrupted_is_terminal() -> None:
    """A job Nextext lost to a restart is over, like one that failed."""
    assert TERMINAL == frozenset({"completed", "failed", "cancelled", "interrupted"})
    assert not {"queued", "running", "gone"} & TERMINAL


# --- create ------------------------------------------------------------------


async def test_create_streams_the_clip_under_a_neutral_name() -> None:
    """The clip goes as the field file named media.<ext>, with the options beside it; Nextext never sees its name."""
    fake = FakeNextext()
    got = await _create(_client(fake), CLIP[:10], CLIP[10:])
    assert got == JOB_ID
    request, upload = fake.requests[-1], fake.uploads[-1]
    assert (request.method, request.url.path) == ("POST", "/api/v1/jobs")
    parsed = await parse_job_upload(upload)
    assert (
        parsed.options
        == JOB_OPTIONS
        == {
            "keyframes": True,
            "visual_context": False,
            "summarization": False,
            "keyframes_max": 19,
            "keyframes_per_minute": 20,
        }
    )
    assert (parsed.filename, parsed.content_type, parsed.data) == ("media.mp4", "video/mp4", CLIP)
    assert upload.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert "content-length" not in upload.headers and upload.headers["transfer-encoding"] == "chunked"
    assert upload.headers["authorization"] == f"Bearer {NEXTEXT_KEY}"
    assert upload.headers["x-audit-purpose"] == HEADERS["X-Audit-Purpose"]
    assert upload.headers["x-request-id"] == "req-0001"
    stem = NAME.removesuffix(".mp4").encode()
    sent = upload.body + str(request.url).encode() + str(dict(upload.headers)).encode()
    assert stem not in sent and NAME.encode() not in sent


async def test_create_sends_only_the_purpose_and_the_request_id_of_the_callers_headers() -> None:
    """Of the headers it is given, the client forwards the purpose and the request id, and adds its own key."""
    fake = FakeNextext()
    headers = {**HEADERS, "Authorization": "Bearer someone-elses", "X-Auth-User": "user-a"}
    await _client(fake).create(_chunks(CLIP), ext=".mp4", mime="video/mp4", headers=headers)
    sent = fake.uploads[-1].headers
    assert sent["authorization"] == f"Bearer {NEXTEXT_KEY}" and "x-auth-user" not in sent


@pytest.mark.parametrize(
    ("setup", "token"),
    [
        (lambda fake: setattr(fake, "create_status", 401), "http:401"),
        (lambda fake: setattr(fake, "create_status", 503), "http:503"),
        (lambda fake: setattr(fake, "raises", httpx.ConnectError("connection refused")), "unreachable"),
        (lambda fake: setattr(fake, "raises", httpx.ReadTimeout("slow")), "timeout"),
        (lambda fake: setattr(fake, "create_body", {"status": "queued"}), "bad_answer"),
        (lambda fake: setattr(fake, "create_body", {"job_id": "../x", "status": "queued"}), "bad_answer"),
        (lambda fake: setattr(fake, "create_body", {"job_id": 7, "status": "queued"}), "bad_answer"),
        (lambda fake: setattr(fake, "create_body", {"job_id": "a" * 65}), "bad_answer"),
    ],
    ids=["401", "503", "unreachable", "timeout", "no_job_id", "path_in_job_id", "job_id_not_text", "job_id_too_long"],
)
async def test_create_maps_refusals(setup: Any, token: str) -> None:
    """A refusal is its status, a transport failure unreachable or timeout, and an answer without a good id bad."""
    fake = FakeNextext()
    setup(fake)
    with pytest.raises(NextextUnavailable) as raised:
        await _create(_client(fake))
    assert raised.value.token == token


async def test_create_with_an_answer_that_is_no_json_is_a_bad_answer() -> None:
    """A 201 whose body is not a JSON object is a bad answer."""
    for answer in (httpx.Response(201, text="created"), httpx.Response(201, json=["job"])):
        with pytest.raises(NextextUnavailable) as raised:
            await _create(_client_for(lambda request, answer=answer: answer))
        assert raised.value.token == "bad_answer"


async def test_create_past_the_cap_is_cut_off() -> None:
    """Past MAX_ATTACHMENT_BYTES the stream stops; Nextext never gets a whole request."""
    fake = FakeNextext()
    parts = [b"\x00" * MIB] * (MAX_ATTACHMENT_BYTES // MIB) + [b"\x00"]
    with pytest.raises(UploadTooLarge):
        await _create(_client(fake), *parts)
    assert fake.requests == [] and fake.events == []


async def test_create_accepts_exactly_the_cap() -> None:
    """A clip of exactly MAX_ATTACHMENT_BYTES goes through whole; the cap cuts off only what passes it."""
    fake = FakeNextext()
    assert await _create(_client(fake), *([b"\x00" * MIB] * (MAX_ATTACHMENT_BYTES // MIB))) == JOB_ID
    assert len((await parse_job_upload(fake.uploads[-1])).data) == MAX_ATTACHMENT_BYTES


async def test_the_clips_stream_is_closed_on_a_cut_off() -> None:
    """On the cut-off the caller's iterator is closed at once, so its own cleanup runs."""
    closed: list[bool] = []

    async def chunks() -> AsyncIterator[bytes]:
        try:
            while True:
                yield b"\x00" * MIB
        finally:
            closed.append(True)

    with pytest.raises(UploadTooLarge):
        await _client(FakeNextext()).create(chunks(), ext=".mp4", mime="video/mp4", headers=HEADERS)
    assert closed == [True]


@pytest.mark.parametrize(
    ("ext", "mime"),
    [
        ("mp4", "video/mp4"),
        (".MP4", "video/mp4"),
        ('."\r\nX: y', "video/mp4"),
        ("", "video/mp4"),
        (".mp4", "video/mp4\r\nX: y"),
        (".mp4", "video"),
    ],
)
async def test_an_extension_or_type_that_cannot_be_written_is_refused_unsent(ext: str, mime: str) -> None:
    """A suffix or media type that would break the part's headers is refused before anything is sent."""
    fake = FakeNextext()
    with pytest.raises(ValueError):
        await _client(fake).create(_chunks(CLIP), ext=ext, mime=mime, headers=HEADERS)
    assert fake.requests == []


async def test_nothing_is_sent_without_a_key_and_a_base() -> None:
    """Without Nextext's key or base, no request goes out, and the token says it is not configured."""
    fake = FakeNextext()
    variants: list[dict[str, Any]] = [{"nextext_api_key": None}, {"nextext_api_base": ""}]
    for changes in variants:
        settings = {"nextext_api_base": NEXTEXT_BASE, "nextext_api_key": NEXTEXT_KEY, **changes}
        client = _client(fake, make_settings(Path("unused"), **settings))
        with pytest.raises(NextextUnavailable) as raised:
            await _create(client)
        assert raised.value.token == "not_configured"
    assert fake.requests == []


# --- poll and list -----------------------------------------------------------


async def test_poll_reads_the_list_and_never_a_result() -> None:
    """Each poll is one GET /jobs, whose item carries the fields; a job's result is never asked for."""
    fake = FakeNextext()
    fake.statuses = ["queued", "running", "completed"]
    fake.skipped, fake.skip_reason_code = True, "vad_no_speech"
    client = _client(fake)
    job_id = await _create(client)
    sent = len(fake.requests)
    polled = [await client.poll(job_id, headers=HEADERS) for _ in range(4)]
    assert [job.status for job in polled] == ["queued", "running", "completed", "completed"]
    assert [(r.method, r.url.path) for r in fake.requests[sent:]] == [("GET", "/api/v1/jobs")] * 4
    assert polled[-1] == NextextJob(JOB_ID, "completed", None, True, "vad_no_speech", CREATED)
    assert JOB_CREATED_AT.endswith("Z") and polled[-1].created_at.utcoffset() == timedelta(0)


async def test_poll_reads_a_failure_by_its_code() -> None:
    """A failed job carries its typed cause, never the error text."""
    fake = FakeNextext()
    fake.statuses, fake.error_code = ["failed"], "undecodable_media"
    client = _client(fake)
    job = await client.poll(await _create(client), headers=HEADERS)
    assert (job.status, job.error_code, job.skipped, job.skip_reason_code) == (
        "failed",
        "undecodable_media",
        False,
        None,
    )
    assert FAILURE_TEXT not in repr(job)


async def test_poll_picks_its_job_among_others() -> None:
    """The list holds other jobs of the same caller; the poll answers for its own."""
    fake = FakeNextext()
    fake.listed = [fake.item("b" * 32, "failed", error_code="internal")]
    client = _client(fake)
    job = await client.poll(await _create(client), headers=HEADERS)
    assert (job.job_id, job.status) == (JOB_ID, "completed")


async def test_a_job_that_is_not_listed_is_gone() -> None:
    """A job missing from the list, as after a restart of Nextext, is gone: terminal, neither skipped nor coded."""
    fake = FakeNextext()
    fake.statuses, fake.vanish_after = ["running"], 1
    client = _client(fake)
    job_id = await _create(client)
    assert (await client.poll(job_id, headers=HEADERS)).status == "running"
    before = datetime.now(UTC)
    gone = await client.poll(job_id, headers=HEADERS)
    assert (gone.job_id, gone.status, gone.error_code, gone.skipped, gone.skip_reason_code) == (
        JOB_ID,
        "gone",
        None,
        False,
        None,
    )
    assert before <= gone.created_at <= datetime.now(UTC)
    assert "gone" not in TERMINAL


async def test_the_list_is_read_item_by_item() -> None:
    """An item that does not read is left out; the others are served, with their times in UTC."""
    fake = FakeNextext()
    good = fake.item("c" * 32, "running", skipped=False)
    fake.listed = [
        good,
        fake.item("../x", "running"),
        fake.item("d" * 32, "weird"),
        fake.item("e" * 32, "running", created_at="yesterday"),
        fake.item("f" * 32, "running", skipped="yes"),
        fake.item("g" * 32, "running", created_at=None),
        {"status": "running"},
        "job",
    ]
    fake.listed.append(fake.item("h" * 32, "completed", created_at="2026-11-22T23:54:00+01:00"))
    fake.listed.append(fake.item("i" * 32, "completed", created_at="2026-11-22T22:54:00", error_code="Not A Token"))
    jobs = await _client(fake).list_jobs(headers=HEADERS)
    assert [job.job_id for job in jobs] == ["c" * 32, "h" * 32, "i" * 32]
    assert jobs[1].created_at == jobs[2].created_at == CREATED.replace(microsecond=0)
    assert jobs[2].error_code is None
    assert [(r.method, r.url.path) for r in fake.requests] == [("GET", "/api/v1/jobs")]
    assert fake.requests[0].headers["authorization"] == f"Bearer {NEXTEXT_KEY}"
    assert fake.requests[0].headers["x-audit-purpose"] == HEADERS["X-Audit-Purpose"]


@pytest.mark.parametrize(
    ("answer", "token"),
    [
        (httpx.Response(428, json={"detail": "purpose required"}), "http:428"),
        (httpx.Response(401, json={}), "http:401"),
        (httpx.Response(200, text="not json"), "bad_answer"),
        (httpx.Response(200, json=["jobs"]), "bad_answer"),
        (httpx.Response(200, json={"jobs": "none"}), "bad_answer"),
        (httpx.Response(200, json={}), "bad_answer"),
        (httpx.Response(200, content=b" " * (LIST_MAX_BYTES + 1)), "bad_answer"),
        (httpx.ConnectError("refused"), "unreachable"),
        (httpx.ReadTimeout("slow"), "timeout"),
    ],
    ids=[
        "428",
        "401",
        "not_json",
        "not_an_object",
        "jobs_not_a_list",
        "no_jobs",
        "too_large",
        "unreachable",
        "timeout",
    ],
)
async def test_the_list_maps_what_cannot_be_read(answer: httpx.Response | Exception, token: str) -> None:
    """A refusal is its status, a body that does not read is bad, and a transport failure keeps its kind."""

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    with pytest.raises(NextextUnavailable) as raised:
        await _client_for(handler).list_jobs(headers=HEADERS)
    assert raised.value.token == token


async def test_a_list_without_a_purpose_is_refused_by_nextext() -> None:
    """The fake refuses, as Nextext's default does, a list that carries no purpose: the client must send one."""
    fake = FakeNextext()
    with pytest.raises(NextextUnavailable) as raised:
        await _client(fake).list_jobs(headers={})
    assert raised.value.token == "http:428"


# --- artifacts ---------------------------------------------------------------


async def test_an_artifact_streams_within_its_cap() -> None:
    """The chunks are Nextext's own; an artifact it did not produce is None; one over its cap is cut off."""
    fake = FakeNextext()
    client = _client(fake)
    job_id = await _create(client)
    body = bytes(range(256)) * (3 * MIB // 256) + b"tail"
    fake.artifacts[JSONL] = body
    got = await _read(client, job_id)
    assert got is not None and len(got) > 1 and b"".join(got) == body
    assert fake.requests[-1].headers["authorization"] == f"Bearer {NEXTEXT_KEY}"
    assert fake.requests[-1].url.path == f"/api/v1/jobs/{JOB_ID}/artifacts/docint.jsonl"
    fake.artifacts[KEYFRAMES] = 404
    assert await _read(client, job_id, KEYFRAMES) is None
    fake.artifacts[JSONL] = bytes(ARTIFACT_CAPS[JSONL])
    exact = await _read(client, job_id)
    assert exact is not None and sum(map(len, exact)) == ARTIFACT_CAPS[JSONL]


@pytest.mark.parametrize("name", [JSONL, KEYFRAMES])
async def test_a_declared_length_over_the_cap_is_refused_before_the_body(name: str) -> None:
    """Content-Length past the cap raises at once: the body is never started."""
    fake = FakeNextext()
    client = _client(fake)
    job_id = await _create(client)
    fake.declared[name] = ARTIFACT_CAPS[name] + 1
    with pytest.raises(ArtifactTooLarge):
        await _read(client, job_id, name)
    assert fake.served == []


async def test_an_undeclared_body_over_its_cap_raises_before_its_last_chunk() -> None:
    """With no Content-Length the stream counts: it raises before the chunk that passes the cap is handed on."""
    fake = FakeNextext()
    client = _client(fake)
    job_id = await _create(client)
    fake.artifacts[JSONL] = bytes(ARTIFACT_CAPS[JSONL] + MIB // 2)
    fake.declared[JSONL] = None
    got: list[bytes] = []
    with pytest.raises(ArtifactTooLarge):
        async with client.artifact(job_id, JSONL, headers=HEADERS) as chunks:
            assert chunks is not None
            async for chunk in chunks:
                got.append(chunk)
    assert "content-length" not in str(fake.requests[-1].headers)
    assert sum(map(len, got)) == ARTIFACT_CAPS[JSONL] and fake.served == [JSONL]


async def test_a_declared_length_under_the_cap_does_not_excuse_a_longer_body() -> None:
    """A body longer than its declared length, and past the cap, is still cut off by the count."""
    fake = FakeNextext()
    client = _client(fake)
    job_id = await _create(client)
    fake.artifacts[JSONL] = bytes(ARTIFACT_CAPS[JSONL] + 1)
    fake.declared[JSONL] = 10
    with pytest.raises(ArtifactTooLarge):
        await _read(client, job_id)


@pytest.mark.parametrize("declared", ["\u00b2", "\u0663", "1" * 5000, "9" * 16, "-1", "1e9", " ", "0x10", "12 "])
async def test_a_declared_length_that_is_not_ascii_digits_counts_as_undeclared(declared: str) -> None:
    """A Content-Length that is no run of at most 15 ASCII digits is no declaration: no ValueError, the count holds."""
    body = bytes(ARTIFACT_CAPS[JSONL] + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"first"
            yield body

        return httpx.Response(200, headers=_length(declared), content=chunks())

    with pytest.raises(ArtifactTooLarge):
        await _read(_client_for(handler))

    def small(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_length(declared), content=_chunks(b"abc"))

    assert await _read(_client_for(small)) == [b"abc"]


async def test_a_declared_length_of_fifteen_digits_over_the_cap_is_refused_before_the_body() -> None:
    """The widest declaration read is 15 ASCII digits; one over the cap is refused with the body unstarted."""
    started: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        async def chunks() -> AsyncIterator[bytes]:
            started.append(True)
            yield b"never"

        return httpx.Response(200, headers={"Content-Length": "9" * 15}, content=chunks())

    with pytest.raises(ArtifactTooLarge):
        await _read(_client_for(handler))
    assert started == []


@pytest.mark.parametrize("status", [401, 410, 500, 503])
async def test_an_artifact_answer_that_is_neither_200_nor_404_is_unavailable(status: int) -> None:
    """Any other answer is its status."""
    fake = FakeNextext()
    client = _client(fake)
    job_id = await _create(client)
    fake.artifacts[JSONL] = status
    with pytest.raises(NextextUnavailable) as raised:
        await _read(client, job_id)
    assert raised.value.token == f"http:{status}"


async def test_an_artifact_of_a_job_that_is_gone_is_none() -> None:
    """Nextext answers 404 for a job it does not hold, as for an artifact it did not make."""
    fake = FakeNextext()
    client = _client(fake)
    job_id = await _create(client)
    assert await client.delete(job_id, headers=HEADERS) is True
    assert await _read(client, job_id) is None


@pytest.mark.parametrize(
    ("error", "token"), [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ReadError("reset"), "unreachable")]
)
async def test_a_stream_that_fails_midway_is_unavailable(error: Exception, token: str) -> None:
    """A read that fails after the first chunk is a failure of Nextext, by its kind."""

    async def body() -> AsyncIterator[bytes]:
        yield b"first"
        raise error

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    got: list[bytes] = []
    with pytest.raises(NextextUnavailable) as raised:
        async with _client_for(handler).artifact(JOB_ID, JSONL, headers=HEADERS) as chunks:
            assert chunks is not None
            async for chunk in chunks:
                got.append(chunk)
    assert (raised.value.token, got) == (token, [b"first"])


async def test_what_the_consumer_raises_inside_the_stream_is_not_mapped() -> None:
    """An error of the one who reads the artifact passes through as it is, whatever httpx class it has."""
    fake = FakeNextext()
    client = _client(fake)
    job_id = await _create(client)
    with pytest.raises(httpx.ReadTimeout):
        async with client.artifact(job_id, JSONL, headers=HEADERS) as chunks:
            assert chunks is not None
            raise httpx.ReadTimeout("not nextext's")


async def test_an_artifact_that_is_not_one_of_the_two_is_refused_unsent() -> None:
    """Only the two names are asked for, and only for an id Nextext could have given."""
    fake = FakeNextext()
    client = _client(fake)
    with pytest.raises(ValueError):
        await _read(client, JOB_ID, "../jobs")
    with pytest.raises(ValueError):
        await _read(client, "../x", JSONL)
    assert fake.requests == []


# --- delete ------------------------------------------------------------------


async def test_delete_is_fine_when_the_job_is_gone(loguru_caplog: pytest.LogCaptureFixture) -> None:
    """204 and 404 are both done; any other answer is False, logged by its token alone."""
    fake = FakeNextext()
    client = _client(fake)
    job_id = await _create(client)
    assert await client.delete(job_id, headers=HEADERS) is True
    assert (fake.deleted, fake.events) == ([JOB_ID], ["create", f"delete:{JOB_ID}"])
    assert await client.delete(job_id, headers=HEADERS) is True
    assert await client.delete("b" * 32, headers=HEADERS) is True
    assert loguru_caplog.records == []
    request = fake.requests[-1]
    assert (request.method, request.url.path) == ("DELETE", f"/api/v1/jobs/{'b' * 32}")
    assert request.headers["authorization"] == f"Bearer {NEXTEXT_KEY}"
    assert request.headers["x-request-id"] == "req-0001"
    fake.delete_status = 500
    assert await client.delete(job_id, headers=HEADERS) is False
    assert [record.getMessage() for record in loguru_caplog.records] == ["nextext delete failed: http:500"]


@pytest.mark.parametrize(
    ("error", "token"),
    [(httpx.ConnectError("refused"), "unreachable"), (httpx.ReadTimeout("slow"), "timeout")],
)
async def test_delete_never_raises_on_a_transport_failure(
    error: Exception, token: str, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """A failure to reach Nextext is False, never an exception, so a shielded cleanup goes on."""
    fake = FakeNextext()
    fake.raises = error
    assert await _client(fake).delete(JOB_ID, headers=HEADERS) is False
    assert [record.getMessage() for record in loguru_caplog.records] == [f"nextext delete failed: {token}"]


async def test_delete_of_an_id_nextext_could_not_have_given_sends_nothing(
    loguru_caplog: pytest.LogCaptureFixture,
) -> None:
    """An id that would reach another route is False, unsent."""
    fake = FakeNextext()
    assert await _client(fake).delete("../jobs", headers=HEADERS) is False
    assert fake.requests == [] and "invalid_id" in loguru_caplog.text


async def test_delete_without_a_key_is_false_and_sends_nothing() -> None:
    """Without the key the delete is False, not an exception."""
    fake = FakeNextext()
    client = _client(fake, make_settings(Path("unused"), nextext_api_base=NEXTEXT_BASE))
    assert await client.delete(JOB_ID, headers=HEADERS) is False
    assert fake.requests == []


# --- health ------------------------------------------------------------------


async def test_health_is_200_only() -> None:
    """Only a 200 is healthy; a refusal and a transport failure are not, and the key is never sent."""
    fake = FakeNextext()
    client = _client(fake)
    assert await client.healthy() is True
    fake.health = 503
    assert await client.healthy() is False
    fake.health = 204
    assert await client.healthy() is False
    fake.health = 200
    fake.raises = httpx.ConnectError("refused")
    assert await client.healthy() is False
    fake.raises = httpx.ReadTimeout("slow")
    assert await client.healthy() is False
    assert [(r.method, r.url.path) for r in fake.requests] == [("GET", "/api/v1/health")] * 5
    assert not [r for r in fake.requests if "authorization" in r.headers or "x-audit-purpose" in r.headers]


async def test_health_answers_without_a_key_too() -> None:
    """The probe asks nothing the key is for: it runs against a base alone."""
    fake = FakeNextext()
    client = _client(fake, make_settings(Path("unused"), nextext_api_base=NEXTEXT_BASE))
    assert await client.healthy() is True


# --- when clips are offered ----------------------------------------------------


@pytest.mark.parametrize("version", ["0.9.0", "0.9", "0.10.1", "1.0", "1.0.0", "2.3.4.5", "10.0"])
def test_serves_media(version: str) -> None:
    """Workbench 0.9 and later takes clips, however its version is written as dotted numbers."""
    assert serves_media(version) is True


@pytest.mark.parametrize(
    "version",
    ["0.8.3", "0.8.99", "0.9.x", "", "dev", "0", "v0.9.0", "0.9.0-rc1", "0.9.0+local", "\u0660.\u0669", "9" * 40],
)
def test_serves_no_media_before_0_9_or_without_a_version_that_reads(version: str) -> None:
    """An older workbench, no version, and anything that is not ``X.Y[.Z...]`` of ASCII digits does not."""
    assert serves_media(version) is False


@pytest.fixture
async def running() -> AsyncIterator[list[asyncio.Task[None]]]:
    """Hold the probe tasks a test starts, and cancel them when it ends.

    Yields:
        The list to append each task to.
    """
    tasks: list[asyncio.Task[None]] = []
    yield tasks
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _start(health: NextextHealth, running: list[asyncio.Task[None]]) -> asyncio.Task[None]:
    task = asyncio.create_task(health.run())
    running.append(task)
    return task


async def _checked(health: NextextHealth) -> None:
    await asyncio.wait_for(health.checked.wait(), 5)


async def test_the_probe_marks_nextext_healthy_on_200(running: list[asyncio.Task[None]]) -> None:
    """Healthy is false until a probe says otherwise; a 200 sets it, and the probe then waits for a failure."""
    fake = FakeNextext()
    health = NextextHealth(_client(fake))
    assert health.healthy is False and not health.checked.is_set()
    _start(health, running)
    await _checked(health)
    assert health.healthy is True
    await asyncio.sleep(0.05)
    assert [(r.method, r.url.path) for r in fake.requests] == [("GET", "/api/v1/health")]


async def test_a_probe_that_is_refused_is_not_healthy(running: list[asyncio.Task[None]]) -> None:
    """Any answer but a 200 leaves Nextext unhealthy, and ``checked`` says a probe ended."""
    fake = FakeNextext()
    fake.health = 503
    health = NextextHealth(_client(fake))
    _start(health, running)
    await _checked(health)
    assert health.healthy is False


async def test_a_failed_probe_tries_again_with_backoff(
    monkeypatch: pytest.MonkeyPatch, running: list[asyncio.Task[None]]
) -> None:
    """The waits between probes are 5 s, doubled each time to 300 s; a healthy probe starts them over."""
    fake = FakeNextext()
    fake.health = 503
    health = NextextHealth(_client(fake))
    waits: list[float] = []
    enough = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(delay: float) -> None:
        waits.append(delay)
        if len(waits) == 9:
            enough.set()
            await asyncio.Event().wait()
        await real_sleep(0)

    monkeypatch.setattr(nextext.asyncio, "sleep", sleep)
    _start(health, running)
    await asyncio.wait_for(enough.wait(), 5)
    assert waits == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert (PROBE_FIRST_S, PROBE_MAX_S) == (5.0, 300.0)
    assert health.healthy is False
    assert len([r for r in fake.requests if r.url.path.endswith("/health")]) == 9


async def test_a_healthy_probe_starts_the_backoff_over(
    monkeypatch: pytest.MonkeyPatch, running: list[asyncio.Task[None]]
) -> None:
    """After Nextext was healthy and fails again, the waits begin at 5 s, not where they stopped."""
    fake = FakeNextext()
    fake.health = 503
    health = NextextHealth(_client(fake))
    waits: list[float] = []
    real_sleep = asyncio.sleep
    gate = asyncio.Event()

    async def sleep(delay: float) -> None:
        waits.append(delay)
        if len(waits) == 3:
            fake.health = 200
        if len(waits) == 5:
            await asyncio.Event().wait()
        await real_sleep(0)
        if len(waits) == 3:
            gate.set()

    monkeypatch.setattr(nextext.asyncio, "sleep", sleep)
    _start(health, running)
    await asyncio.wait_for(gate.wait(), 5)
    for _ in range(100):
        if health.healthy:
            break
        await real_sleep(0.01)
    assert health.healthy is True and waits == [5, 10, 20]
    fake.health = 503
    health.failed()
    for _ in range(100):
        if len(waits) == 5:
            break
        await real_sleep(0.01)
    assert waits == [5, 10, 20, 5, 10]


async def test_a_failure_wakes_the_probe(running: list[asyncio.Task[None]]) -> None:
    """``failed`` drops the offer at once and has the probe ask again; a 200 then offers clips again."""
    fake = FakeNextext()
    health = NextextHealth(_client(fake))
    _start(health, running)
    await _checked(health)
    assert health.healthy is True
    health.checked.clear()
    fake.health = 503
    health.failed()
    assert health.healthy is False
    await _checked(health)
    assert health.healthy is False
    assert len([r for r in fake.requests if r.url.path.endswith("/health")]) == 2


async def test_a_failure_while_nextext_still_answers_is_probed_and_offered_again(
    running: list[asyncio.Task[None]],
) -> None:
    """The probe sees only that Nextext is up: a failed job it can recover from does not keep clips off."""
    fake = FakeNextext()
    health = NextextHealth(_client(fake))
    _start(health, running)
    await _checked(health)
    health.checked.clear()
    health.failed()
    assert health.healthy is False
    await _checked(health)
    assert health.healthy is True


async def test_wait_healthy_returns_at_once_when_healthy_and_waits_for_the_probe_otherwise(
    running: list[asyncio.Task[None]],
) -> None:
    """True without a wait while healthy; for a probe that has not answered yet, true as it does, false at the limit."""
    fake = FakeNextext()
    fake.health = 503
    health = NextextHealth(_client(fake))
    assert await health.wait_healthy(0.05) is False
    health.healthy = True
    assert await health.wait_healthy(0.0) is True
    health.healthy = False
    waiting = asyncio.create_task(health.wait_healthy(5))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    fake.health = 200
    _start(health, running)
    assert await asyncio.wait_for(waiting, 5) is True
    assert health.healthy is True


def _workbench(version: str | None) -> FakeWorkbench:
    fake = FakeWorkbench()
    if version is None:
        fake.discovery = None
    else:
        assert fake.discovery is not None
        fake.discovery = dataclasses.replace(fake.discovery, server_version=version)
    return fake


def test_clips_are_offered_while_nextext_is_healthy_and_workbench_is_0_9() -> None:
    """Healthy, and a cached discovery that names 0.9 or later; every other state offers none."""
    health = NextextHealth(_client(FakeNextext()))
    health.healthy = True
    assert health.offered(_workbench("0.9.0").as_client()) is True
    assert health.offered(_workbench("0.10.2").as_client()) is True
    assert health.offered(_workbench("0.8.3").as_client()) is False
    assert health.offered(_workbench("").as_client()) is False
    assert health.offered(_workbench(None).as_client()) is False
    assert health.offered(None) is False


def test_no_clips_are_offered_while_nextext_is_not_healthy_and_workbench_is_not_asked() -> None:
    """An unhealthy Nextext offers none, without a discovery."""
    health = NextextHealth(_client(FakeNextext()))
    workbench = _workbench("0.9.0")
    assert health.offered(workbench.as_client()) is False
    assert workbench.discoveries == []


def test_the_offer_asks_workbench_for_the_listing_without_images() -> None:
    """The version is read from the cached discovery of the system's call, which takes no images."""
    health = NextextHealth(_client(FakeNextext()))
    health.healthy = True
    workbench = _workbench("0.9.0")
    assert health.offered(workbench.as_client()) is True
    [(call, images)] = workbench.discoveries
    assert (call.owner, call.chat_id, images) == ("ghostwriter:system", "capability", False)


# --- the log -----------------------------------------------------------------


async def test_the_log_holds_no_names_texts_or_keys(log_lines: list[str]) -> None:
    """No file name, Nextext error text or key reaches a log line, on success or failure."""
    secret = "Hafenfest"
    fake = FakeNextext()
    fake.statuses, fake.error_code = ["failed"], "internal"
    client = _client(fake)
    job_id = await _create(client)
    await client.poll(job_id, headers=HEADERS)
    await _read(client, job_id)
    fake.declared[KEYFRAMES] = ARTIFACT_CAPS[KEYFRAMES] + 1
    with pytest.raises(ArtifactTooLarge):
        await _read(client, job_id, KEYFRAMES)
    for answer in (
        httpx.Response(503, text=f"{secret} {FAILURE_TEXT} {NAME}"),
        httpx.Response(201, text=secret),
        httpx.Response(401, json={"detail": f"{NEXTEXT_KEY} {secret}"}),
        httpx.ConnectError(f"{secret} {NEXTEXT_KEY}"),
    ):

        def handler(request: httpx.Request, answer: httpx.Response | Exception = answer) -> httpx.Response:
            if isinstance(answer, Exception):
                raise answer
            return answer

        broken = _client_for(handler)
        with pytest.raises(NextextUnavailable):
            await _create(broken)
        with pytest.raises(NextextUnavailable):
            await broken.list_jobs(headers=HEADERS)
        with pytest.raises(NextextUnavailable):
            await _read(broken, job_id)
        assert await broken.delete(job_id, headers=HEADERS) is False
        assert await broken.healthy() is False
    with pytest.raises(UploadTooLarge):
        await _create(client, *([b"\x00" * MIB] * (MAX_ATTACHMENT_BYTES // MIB + 1)))
    assert log_lines
    forbidden = (secret, FAILURE_TEXT, NAME, NAME.removesuffix(".mp4"), NEXTEXT_KEY, "user-a", json.dumps(JOB_OPTIONS))
    assert not [line for line in log_lines if any(value in line for value in forbidden)]
