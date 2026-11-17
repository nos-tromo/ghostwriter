"""Who a request acts as: the gateway's identity, trusted only with the edge secret, and the dev fallback."""

from pathlib import Path

from _pytest.logging import LogCaptureFixture
from fastapi.testclient import TestClient

from tests.conftest import CapturedAuditStream, as_user, events, make_settings, probe_app

NOBODY: dict[str, str | None] = {"username": None, "display_name": None}
FORGED = {"X-Auth-User": "user-a", "X-Edge-Secret": "x" * 32}


def test_identity_headers_count_only_with_the_edge_secret(client: TestClient, loguru_caplog: LogCaptureFixture) -> None:
    """The gateway's headers count with its secret; a wrong or missing secret leaves the caller anonymous."""
    assert client.get("/api/whoami", headers=as_user("user-a")).json() == {"username": "user-a", "display_name": None}
    named = {**as_user("user-a"), "X-Auth-Name": "User A"}
    assert client.get("/api/whoami", headers=named).json() == {"username": "user-a", "display_name": "User A"}
    assert client.get("/api/whoami", headers=FORGED).json() == NOBODY
    assert client.get("/api/whoami", headers={"X-Auth-User": "user-a"}).json() == NOBODY
    refused = client.post("/test", headers=FORGED)
    assert (refused.status_code, refused.json()) == (401, {"detail": "Missing authenticated principal."})
    assert "Ignored identity headers" in loguru_caplog.text
    assert "user-a" not in loguru_caplog.text and "x" * 32 not in loguru_caplog.text


def test_the_dev_identity_counts_only_without_a_secret(tmp_path: Path, audit_stream: CapturedAuditStream) -> None:
    """Without the secret the dev identity acts, as the system; beside a secret it never does."""
    dev = make_settings(tmp_path, edge_secret=None, default_identity="dev")
    with TestClient(probe_app(dev, audit_stream)) as client:
        assert client.post("/test").json() == {"user": "dev", "via": "system"}
        assert client.get("/api/whoami").json() == {"username": "dev", "display_name": None}
    both = make_settings(tmp_path, default_identity="dev")
    with TestClient(probe_app(both, audit_stream)) as client:
        assert client.post("/test").status_code == 401
        assert client.get("/api/whoami").json() == NOBODY


def test_without_a_secret_identity_headers_count_from_any_caller(
    tmp_path: Path, audit_stream: CapturedAuditStream
) -> None:
    """The dev fallback takes ``X-Auth-User`` alone, before the dev identity; with neither, 401."""
    dev = make_settings(tmp_path, edge_secret=None, default_identity="dev")
    with TestClient(probe_app(dev, audit_stream)) as client:
        assert client.post("/test", headers={"X-Auth-User": "user-a"}).json() == {"user": "user-a", "via": "edge"}
        assert client.get("/api/whoami", headers={"X-Auth-User": "user-a"}).json()["username"] == "user-a"
    with TestClient(probe_app(make_settings(tmp_path, edge_secret=None), audit_stream)) as client:
        assert client.post("/test").status_code == 401


def test_no_parameter_or_header_lets_a_user_act_as_another(
    client: TestClient, audit_stream: CapturedAuditStream
) -> None:
    """Neither an ``owner`` parameter nor a group header changes who acts."""
    headers = {**as_user("user-a"), "X-Auth-Groups": "admins"}
    assert client.post("/test?owner=user-b", headers=headers).json() == {"user": "user-a", "via": "edge"}
    assert events(audit_stream)[-1]["actor"] == {"user": "user-a", "via": "edge"}
