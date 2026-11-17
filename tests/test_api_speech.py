"""Dictation: a recording goes to the router's speech-to-text, held in memory only, while speech input is on.

Every name, id and value here is invented.
"""

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from openai.types.audio import Transcription

from ghostwriter.inference import ModelClient, Transcript, cache_scope_for, inference_headers
from ghostwriter.main import create_app
from ghostwriter.utils.audit_event import sha256_text
from tests.conftest import CapturedAuditStream, as_user, events, make_settings
from tests.fakes import FakeOpenAI

MIB = 1024 * 1024
TEXT = "Summe der Spalte B"
WHISPER = "openai/whisper-large-v3"
UNSUPPORTED = (415, {"detail": "This recording format is not supported."})
TOO_LONG = (413, {"detail": "The recording is longer than allowed."})
NOT_TRANSCRIBED = (502, {"detail": "Speech could not be transcribed."})


@pytest.fixture
def fake_openai() -> FakeOpenAI:
    """Return the router, answering every transcription with ``TEXT``."""
    return FakeOpenAI()


def _app(tmp_path: Path, audit_stream: CapturedAuditStream, fake_openai: FakeOpenAI, **overrides: Any) -> FastAPI:
    settings = make_settings(tmp_path, **overrides)
    return create_app(settings, audit_stream=audit_stream, model=ModelClient(settings, client=fake_openai.as_client()))


@pytest.fixture
def client(tmp_path: Path, audit_stream: CapturedAuditStream, fake_openai: FakeOpenAI) -> Iterator[TestClient]:
    """Serve the app with the router behind ``fake_openai``."""
    with TestClient(_app(tmp_path, audit_stream, fake_openai)) as test_client:
        yield test_client


def _record(
    client: TestClient,
    data: bytes = b"OPUS-SENTINEL",
    mime: str = "audio/webm;codecs=opus",
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    files = {"file": ("rec.webm", data, mime)}
    return client.post("/api/transcribe", files=files, headers=as_user() if headers is None else headers)


def _transcriptions(stream: CapturedAuditStream) -> list[dict[str, Any]]:
    return [e for e in events(stream) if e["operation"] == "speech.transcribe"]


def test_a_recording_is_transcribed_through_the_router(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """The bytes go to the router's ASR as they came, under a name of their type, with the user's cache scope."""
    r = client.post(
        "/api/transcribe",
        files={"file": ("rec.webm", b"OPUS-SENTINEL", "audio/webm;codecs=opus")},
        headers=as_user(),
    )
    assert r.json() == {"text": "Summe der Spalte B"}
    assert (
        fake_openai.transcriptions[0]["model"] == "openai/whisper-large-v3"
        and fake_openai.transcriptions[0]["file"][1] == b"OPUS-SENTINEL"
    )
    [call] = fake_openai.transcriptions
    assert call["file"] == ("recording.webm", b"OPUS-SENTINEL", "audio/webm")
    assert (call["response_format"], call["timeout"]) == ("json", 60.0)
    assert set(call["extra_headers"]) == {"X-Cache-Scope", "X-Request-ID"}
    assert call["extra_headers"]["X-Cache-Scope"] == cache_scope_for("user-a")


@pytest.mark.parametrize("mime", ["audio/webm;codecs=opus", "audio/ogg; codecs=opus", "audio/mp4", "AUDIO/WEBM"])
def test_recordings_are_accepted_by_their_base_type(client: TestClient, fake_openai: FakeOpenAI, mime: str) -> None:
    """Review Focus 5: Firefox's, Chrome's and Safari's recordings, in any case, by their base type."""
    r = _record(client, mime=mime)
    assert (r.status_code, r.json()) == (200, {"text": TEXT})
    base = mime.split(";")[0].strip().lower()
    assert fake_openai.transcriptions[-1]["file"] == (f"recording.{base.split('/')[1]}", b"OPUS-SENTINEL", base)


def test_other_types_and_sizes_are_refused(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """Another type, a recording past 10 MiB, an empty one or no form: refused before the router, unrecorded."""
    before = len(events(audit_stream))
    r = _record(client, mime="video/webm")
    assert (r.status_code, r.json()) == UNSUPPORTED
    r = _record(client, b"\x00" * (10 * MIB + 1))
    assert (r.status_code, r.json()) == TOO_LONG
    r = _record(client, headers={**as_user(), "Content-Length": str(12 * MIB)})
    assert (r.status_code, r.json()) == TOO_LONG
    assert _record(client, b"").status_code == 400
    r = client.post("/api/transcribe", json={"file": "x"}, headers=as_user())
    assert r.status_code == 400
    assert _record(client, headers={}).status_code == 401
    assert fake_openai.transcriptions == [] and events(audit_stream)[before:] == []

    r = _record(client, b"\x00" * (10 * MIB))
    assert (r.status_code, len(fake_openai.transcriptions[0]["file"][1])) == (200, 10 * MIB)


def test_the_recording_and_the_transcript_are_kept_nowhere(
    client: TestClient, audit_stream: CapturedAuditStream, tmp_path: Path
) -> None:
    """Neither reaches the database, the audit trail or the log; the event names the output by its hash only."""
    logged: list[str] = []
    sink = logger.add(logged.append, level="DEBUG")
    try:
        assert _record(client).status_code == 200
    finally:
        logger.remove(sink)
    for kept in tmp_path.rglob("*"):
        assert not kept.is_file() or b"OPUS-SENTINEL" not in kept.read_bytes()
    assert "Summe der Spalte B" not in audit_stream.getvalue()
    assert not any("OPUS-SENTINEL" in line or TEXT in line for line in logged)
    [event] = _transcriptions(audit_stream)
    assert event["ai"]["output_sha256"] == sha256_text("Summe der Spalte B") and event["recipient"] == "inference:asr"
    assert (event["action"], event["outcome"], event["actor"]["user"]) == ("ai_call", "ok", "user-a")
    assert event["ai"] == {"model_alias": WHISPER, "output_sha256": sha256_text(TEXT)}
    assert event["criteria"] == {"bytes": 13, "mime": "audio/webm"}


def test_the_served_model_is_recorded_when_the_router_names_it(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """The SDK's result names no model; a router that adds one has it recorded as the event's ``model_id``."""
    fake_openai.transcript_model = "whisper-served"
    assert _record(client).status_code == 200
    [event] = _transcriptions(audit_stream)
    assert event["ai"] == {"model_alias": WHISPER, "model_id": "whisper-served", "output_sha256": sha256_text(TEXT)}


@pytest.mark.parametrize(
    ("answer", "served"), [({"text": TEXT}, None), ({"text": TEXT, "model": "whisper-1"}, "whisper-1")]
)
def test_the_sdk_sends_the_recording_as_a_form_and_reads_a_model_the_router_adds(
    tmp_path: Path, answer: dict[str, str], served: str | None
) -> None:
    """Through the real SDK: one multipart POST to audio/transcriptions; a ``model`` the router adds is read."""
    seen: list[httpx2.Request] = []

    def router(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=answer)

    sdk = openai.OpenAI(
        base_url="http://router.test/v1",
        api_key="router-key",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(router)),
    )
    model = ModelClient(make_settings(tmp_path), client=sdk)
    headers = inference_headers("user-a", "req-0001")
    got = model.transcribe(audio=b"OPUS-SENTINEL", filename="recording.webm", mime="audio/webm", headers=headers)
    assert got == Transcript(text=TEXT, model_id=served)
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", "http://router.test/v1/audio/transcriptions")
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert (
        request.headers["x-cache-scope"] == cache_scope_for("user-a") and request.headers["x-request-id"] == "req-0001"
    )
    assert request.extensions["timeout"]["read"] == 60.0
    body = request.read()
    assert b'filename="recording.webm"' in body and b"Content-Type: audio/webm" in body and b"OPUS-SENTINEL" in body
    assert WHISPER.encode() in body and b"user-a" not in body


def test_a_router_failure_is_502_and_recorded_as_an_error(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """An unreachable router, or an answer without a transcript: 502, the call recorded with ``error``."""
    fake_openai.transcript = openai.APIConnectionError(
        request=httpx2.Request("POST", "http://router.test/v1/audio/transcriptions")
    )
    r = _record(client)
    assert (r.status_code, r.json()) == NOT_TRANSCRIBED
    fake_openai.audio.transcriptions.create = lambda **_: Transcription.model_construct()
    r = _record(client)
    assert (r.status_code, r.json()) == NOT_TRANSCRIBED
    failed = _transcriptions(audit_stream)
    assert [(e["outcome"], e["ai"], e["criteria"]) for e in failed] == [
        ("error", {"model_alias": WHISPER}, {"bytes": 13, "mime": "audio/webm"})
    ] * 2


def test_a_required_purpose_refuses_a_transcription_unread(
    tmp_path: Path, audit_stream: CapturedAuditStream, fake_openai: FakeOpenAI
) -> None:
    """A transcription is an ``ai_call``: a required purpose refuses it with 428 before the router is asked."""
    with TestClient(_app(tmp_path, audit_stream, fake_openai, require_purpose=frozenset({"ai_call"}))) as client:
        assert _record(client).status_code == 428
    assert fake_openai.transcriptions == []
    [denied] = _transcriptions(audit_stream)
    assert denied["outcome"] == "denied"


def test_dictations_run_on_their_own_threads(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """Four stalled dictations hold the four speech threads, a fifth waits for one, and health still answers."""
    app = client.app
    assert isinstance(app, FastAPI)
    started: list[int] = []
    release = threading.Event()
    answer = fake_openai.audio.transcriptions.create

    def stalled(**kwargs: Any) -> Transcription:
        started.append(1)
        release.wait(10)
        return answer(**kwargs)

    fake_openai.audio.transcriptions.create = stalled
    with ThreadPoolExecutor(5) as pool:
        try:
            dictations = [pool.submit(_record, client) for _ in range(5)]
            deadline = time.monotonic() + 5
            while len(started) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.2)
            assert len(started) == 4
            assert app.state.speech_threads.borrowed_tokens == 4
            assert client.get("/api/health").json() == {"status": "ok"}
        finally:
            release.set()
        assert [dictation.result().status_code for dictation in dictations] == [200] * 5


def test_speech_off_removes_the_route_and_says_so(tmp_path: Path) -> None:
    """With ``GHOSTWRITER_SPEECH_INPUT=off`` the route is not there, and ``/api/config`` says so."""
    fake = FakeOpenAI()
    with TestClient(_app(tmp_path, CapturedAuditStream(), fake, speech_input=False)) as client:
        assert _record(client).status_code == 404
        assert client.get("/api/config", headers=as_user()).json()["speech_input"] is False
    assert fake.transcriptions == []
