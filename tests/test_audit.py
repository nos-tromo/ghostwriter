"""The audit chain: its operating mode, purpose, request ids, children, and failing closed."""

import re
from pathlib import Path
from typing import Annotated, Any

import pytest
from _pytest.logging import LogCaptureFixture
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from ghostwriter.audit import (
    Operation,
    RequestContext,
    audited,
    forward_headers,
    open_emitter,
    required_endpoint_actions,
    system_operation,
)
from ghostwriter.utils.audit_event import Actor, AuditWriteError, Purpose
from ghostwriter.utils.audit_runtime import encode_purpose
from tests.conftest import KEY, SECRET, CapturedAuditStream, as_user, events, make_settings, probe_app

ACTOR = Actor("user-a", "edge")
HEX_ID = re.compile(r"[0-9a-f]{32}")
FAILED = (500, {"detail": "Request failed."})


def failing_app(tmp_path: Path, stream: CapturedAuditStream) -> FastAPI:
    """Build the probe app with routes that fail after ``collect`` / ``test.op`` began.

    Args:
        tmp_path: The test's temporary directory.
        stream: Where the app's audit chain goes.

    Returns:
        The app.
    """
    app = probe_app(make_settings(tmp_path), stream)
    collect = audited("collect", "test.op")

    @app.post("/fail/raise")
    def raise_after_begin(op: Annotated[Operation, Depends(collect)]) -> None:
        """Fail before recording."""
        raise RuntimeError("synthetic failure")

    @app.post("/fail/recorded")
    def raise_after_record(op: Annotated[Operation, Depends(collect)]) -> None:
        """Record, then fail."""
        op.record()
        raise RuntimeError("synthetic failure")

    @app.post("/fail/http/{code}")
    def raise_http(code: int, op: Annotated[Operation, Depends(collect)]) -> None:
        """Fail with an HTTP status."""
        raise HTTPException(status_code=code, detail="synthetic")

    @app.post("/fail/audit")
    def raise_audit(op: Annotated[Operation, Depends(collect)]) -> None:
        """Fail as a write elsewhere in the request would."""
        raise AuditWriteError("synthetic")

    @app.post("/fail/settle")
    def break_the_stream(op: Annotated[Operation, Depends(collect)]) -> None:
        """Fail while the audit stream is broken too."""
        stream.failing = True
        raise RuntimeError("synthetic failure")

    return app


def _new_events(client: TestClient, stream: CapturedAuditStream, path: str) -> tuple[Any, list[tuple[str, str, str]]]:
    """Send one request and return its answer and the events it added.

    Args:
        client: The client, inside the app's lifespan.
        stream: The app's audit stream.
        path: The route to post to.

    Returns:
        The response and the added events as ``(action, operation, outcome)``.
    """
    before = len(events(stream))
    response = client.post(path, headers=as_user())
    stream.failing = False
    return response, [(e["action"], e["operation"], e["outcome"]) for e in events(stream)[before:]]


@pytest.mark.parametrize("path", ["/fail/raise", "/fail/audit"])
def test_a_request_that_fails_after_begin_is_recorded_as_error(
    tmp_path: Path, audit_stream: CapturedAuditStream, path: str
) -> None:
    """An operation begun and never recorded is settled as ``error``; the chain stays valid."""
    with TestClient(failing_app(tmp_path, audit_stream), raise_server_exceptions=False) as client:
        response, added = _new_events(client, audit_stream, path)
    assert (response.status_code, response.json()) == FAILED
    assert added == [("collect", "test.op", "error")]


def test_a_recorded_operation_is_not_recorded_again(tmp_path: Path, audit_stream: CapturedAuditStream) -> None:
    """A request that fails after its outcome was written gets no second event."""
    with TestClient(failing_app(tmp_path, audit_stream), raise_server_exceptions=False) as client:
        response, added = _new_events(client, audit_stream, "/fail/recorded")
    assert (response.status_code, response.json()) == FAILED
    assert added == [("collect", "test.op", "ok")]


@pytest.mark.parametrize(
    ("code", "settled"), [(404, []), (409, []), (502, [("collect", "test.op", "error")])], ids=["404", "409", "502"]
)
def test_only_a_server_error_settles(
    tmp_path: Path, audit_stream: CapturedAuditStream, code: int, settled: list[tuple[str, str, str]]
) -> None:
    """A 4xx returns no data and records nothing; a 5xx settles the operation as ``error``."""
    with TestClient(failing_app(tmp_path, audit_stream)) as client:
        response, added = _new_events(client, audit_stream, f"/fail/http/{code}")
    assert (response.status_code, response.json()) == (code, {"detail": "synthetic"})
    assert added == settled


def test_settling_never_raises_and_logs_the_type_only(
    tmp_path: Path, audit_stream: CapturedAuditStream, loguru_caplog: LogCaptureFixture
) -> None:
    """A write that fails while settling is logged by its exception type; the answer is still the JSON 500."""
    with TestClient(failing_app(tmp_path, audit_stream), raise_server_exceptions=False) as client:
        response, added = _new_events(client, audit_stream, "/fail/settle")
    assert (response.status_code, response.json()) == FAILED
    assert added == []
    assert "AuditWriteError" in loguru_caplog.text
    assert "synthetic" not in loguru_caplog.text


def test_a_required_purpose_is_refused_with_428_and_recorded(tmp_path: Path, audit_stream: CapturedAuditStream) -> None:
    """An action that may also call the model needs a purpose once ``ai_call`` requires one."""
    with TestClient(probe_app(make_settings(tmp_path, require_purpose=frozenset({"ai_call"})), audit_stream)) as client:
        refused = client.post("/test", headers=as_user())
        assert (refused.status_code, refused.json()) == (
            428,
            {"detail": "A stated purpose is required for this request."},
        )
        last = events(audit_stream)[-1]
        assert (last["action"], last["operation"], last["outcome"]) == ("collect", "test.op", "denied")
        stated = {**as_user(), "X-Audit-Purpose": encode_purpose("case-0001", "review")}
        assert client.post("/test", headers=stated).status_code == 200
        assert events(audit_stream)[-1]["purpose"] == {"case_ref": "case-0001", "reason": "review"}


def test_a_malformed_purpose_is_refused_and_not_recorded(client: TestClient, audit_stream: CapturedAuditStream) -> None:
    """A purpose header that cannot be read is a 400, before any event."""
    before = len(events(audit_stream))
    refused = client.post("/test", headers={**as_user(), "X-Audit-Purpose": "reason=a&reason=b"})
    assert (refused.status_code, refused.json()) == (400, {"detail": "Invalid request."})
    assert len(events(audit_stream)) == before


def test_an_unstated_purpose_is_forwarded_as_ghostwriter_chat() -> None:
    """Without a stated purpose, workbench gets the fixed reason."""
    assert forward_headers(RequestContext(ACTOR, None, "req-0001")) == {
        "X-Audit-Purpose": encode_purpose(None, "ghostwriter chat"),
        "X-Request-ID": "req-0001",
    }


def test_a_stated_purpose_is_forwarded_as_stated() -> None:
    """A stated purpose goes on as the user stated it."""
    assert forward_headers(RequestContext(ACTOR, Purpose("case-0001", "review"), "req-0001")) == {
        "X-Audit-Purpose": encode_purpose("case-0001", "review"),
        "X-Request-ID": "req-0001",
    }


@pytest.mark.parametrize("sent", [None, "bad id!"])
def test_a_request_without_a_valid_id_gets_one(
    client: TestClient, audit_stream: CapturedAuditStream, sent: str | None
) -> None:
    """A missing or invalid ``X-Request-ID`` is replaced by a fresh id."""
    headers = as_user() if sent is None else {**as_user(), "X-Request-ID": sent}
    assert client.post("/test", headers=headers).status_code == 200
    assert HEX_ID.fullmatch(events(audit_stream)[-1]["request_id"])


def test_a_valid_request_id_is_kept(client: TestClient, audit_stream: CapturedAuditStream) -> None:
    """The gateway's correlation id is recorded as sent."""
    client.post("/test", headers={**as_user(), "X-Request-ID": "req-0001"})
    assert events(audit_stream)[-1]["request_id"] == "req-0001"


def test_children_point_at_their_primary_event(audit_stream: CapturedAuditStream) -> None:
    """A child needs its primary event, points at it, and the primary is written once."""
    emitter = open_emitter("0+test", audit_stream)
    emitter.start()
    try:
        op = system_operation(emitter, "delete", "test.op")
        with pytest.raises(RuntimeError):
            op.child("ai_call", "x")
        primary = op.record()
        child = op.child("ai_call", "x")
        op.fail()
        with pytest.raises(RuntimeError):
            op.record()
    finally:
        emitter.close()
    written = {e["event_id"]: e for e in events(audit_stream)}
    assert written[child]["parent_event_id"] == op.event_id == primary
    assert written[primary]["actor"] == {"user": None, "via": "system"}
    assert HEX_ID.fullmatch(written[primary]["request_id"])
    assert [e["outcome"] for e in events(audit_stream)].count("error") == 0


def test_the_chain_opens_with_the_operating_mode(client: TestClient, audit_stream: CapturedAuditStream) -> None:
    """The chain starts, then states how far callers are trusted and the periods; no secret is written."""
    start, mode = events(audit_stream)[:2]
    assert [(e["action"], e["operation"]) for e in (start, mode)] == [("lifecycle", "start"), ("admin", "config.audit")]
    assert mode["service"] == "ghostwriter"
    assert mode["criteria"] == {
        "edge_secret_enforced": True,
        "default_identity_enabled": False,
        "require_purpose": [],
        "chat_retention_days": 90,
        "file_retention_days": 7,
        "notice_days": 30,
        "speech_input": True,
        "vision": False,
        "workbench": True,
        "media": False,
    }
    assert SECRET not in audit_stream.getvalue() and KEY not in audit_stream.getvalue()


def test_the_operating_mode_says_whether_media_is_on(tmp_path: Path, audit_stream: CapturedAuditStream) -> None:
    """With Nextext's base and key set the start-up record says media is on; the key itself is never written."""
    nextext_key = "n" * 32
    settings = make_settings(tmp_path, nextext_api_base="http://nextext.test/api/v1", nextext_api_key=nextext_key)
    with TestClient(probe_app(settings, audit_stream)):
        pass
    mode = events(audit_stream)[1]
    assert (mode["operation"], mode["criteria"]["media"]) == ("config.audit", True)
    assert nextext_key not in audit_stream.getvalue()


def test_a_failed_audit_write_refuses_the_request(tmp_path: Path, audit_stream: CapturedAuditStream) -> None:
    """Audited work is refused when its event cannot be written, when no chain is open, or after it closed."""
    app = probe_app(make_settings(tmp_path), audit_stream)
    with TestClient(app) as client:
        audit_stream.failing = True
        refused = client.post("/test", headers=as_user())
        audit_stream.failing = False
    assert (refused.status_code, refused.json()) == FAILED
    closed = TestClient(app, raise_server_exceptions=False).post("/test", headers=as_user())
    assert (closed.status_code, closed.json()) == FAILED
    unopened = TestClient(probe_app(make_settings(tmp_path), audit_stream)).post("/test", headers=as_user())
    assert (unopened.status_code, unopened.json()) == FAILED


def test_only_endpoint_actions_are_audited_or_reported(tmp_path: Path) -> None:
    """A route records only endpoint actions; the config reports only those that need a purpose."""
    with pytest.raises(ValueError):
        audited("admin", "test.op")
    with pytest.raises(ValueError):
        audited("collect", "test.op", also=("lifecycle",))
    settings = make_settings(tmp_path, require_purpose=frozenset({"ai_call", "combine", "query"}))
    assert required_endpoint_actions(settings) == ["ai_call", "query"]
