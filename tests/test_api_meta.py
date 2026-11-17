"""What ``/api/config`` tells the SPA is switched on, and what the backend does not serve.

Every name, id and value here is invented.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from ghostwriter.main import create_app
from tests.conftest import CapturedAuditStream, as_user, make_settings


def test_config_reports_what_the_spa_needs(client: TestClient) -> None:
    """The defaults: English, no required purpose, speech and attachments on, the limits and periods."""
    assert client.get("/api/config", headers=as_user()).json() == {
        "ui_language": "en",
        "require_purpose": [],
        "speech_input": True,
        "attachments": True,
        "attachment_types": [
            ".csv",
            ".docx",
            ".eml",
            ".htm",
            ".html",
            ".json",
            ".jsonl",
            ".md",
            ".ndjson",
            ".ods",
            ".odt",
            ".pdf",
            ".pptx",
            ".txt",
            ".xlsx",
        ],
        "max_attachment_bytes": 52428800,
        "max_recording_s": 120,
        "max_recording_bytes": 10485760,
        "chat_retention_days": 90,
        "file_retention_days": 7,
        "version": "0.1.0",
    }


def test_only_actions_the_routes_record_are_reported_as_required(tmp_path: Path) -> None:
    """An action no route records, as in a list shared with other services, is left out."""
    settings = make_settings(tmp_path, require_purpose=frozenset({"ai_call", "combine"}))
    with TestClient(create_app(settings, audit_stream=CapturedAuditStream())) as client:
        assert client.get("/api/config", headers=as_user()).json()["require_purpose"] == ["ai_call"]


def test_config_follows_the_settings_and_needs_no_identity(tmp_path: Path) -> None:
    """German, workbench off and chats kept: read before sign-in, as the SPA reads it first."""
    settings = make_settings(tmp_path, ui_language="de", workbench_api_key=None, chat_days=None)
    with TestClient(create_app(settings, audit_stream=CapturedAuditStream())) as client:
        config = client.get("/api/config").json()
    assert (config["ui_language"], config["attachments"], config["chat_retention_days"]) == ("de", False, None)


def test_no_api_documentation_is_served(client: TestClient) -> None:
    """Swagger UI, ReDoc and the schema are off: a peer on the backend's networks gets nothing of them."""
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path, headers=as_user()).status_code == 404
