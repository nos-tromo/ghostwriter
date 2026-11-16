"""The audit chain: its operating mode, purpose, request ids, children, and failing closed."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ghostwriter.audit import (
    RequestContext,
    audited,
    forward_headers,
    open_emitter,
    required_endpoint_actions,
    system_operation,
)
from ghostwriter.utils.audit_event import Actor, Purpose
from ghostwriter.utils.audit_runtime import encode_purpose
from tests.conftest import KEY, SECRET, CapturedAuditStream, as_user, events, make_settings, probe_app

ACTOR = Actor("user-a", "edge")
HEX_ID = re.compile(r"[0-9a-f]{32}")


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
        "workbench": True,
    }
    assert SECRET not in audit_stream.getvalue() and KEY not in audit_stream.getvalue()


def test_a_failed_audit_write_refuses_the_request(tmp_path: Path, audit_stream: CapturedAuditStream) -> None:
    """Audited work is refused when its event cannot be written, or when no chain is open."""
    app = probe_app(make_settings(tmp_path), audit_stream)
    with TestClient(app) as client:
        audit_stream.failing = True
        refused = client.post("/test", headers=as_user())
        audit_stream.failing = False
    assert (refused.status_code, refused.json()) == (500, {"detail": "Request failed."})
    unopened = TestClient(probe_app(make_settings(tmp_path), audit_stream)).post("/test", headers=as_user())
    assert (unopened.status_code, unopened.json()) == (500, {"detail": "Request failed."})


def test_only_endpoint_actions_are_audited_or_reported(tmp_path: Path) -> None:
    """A route records only endpoint actions; the config reports only those that need a purpose."""
    with pytest.raises(ValueError):
        audited("admin", "test.op")
    with pytest.raises(ValueError):
        audited("collect", "test.op", also=("lifecycle",))
    settings = make_settings(tmp_path, require_purpose=frozenset({"ai_call", "combine", "query"}))
    assert required_endpoint_actions(settings) == ["ai_call", "query"]
