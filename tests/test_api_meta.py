"""What ``/api/config`` tells the SPA is switched on, and what the backend does not serve.

Every name, id and value here is invented.
"""

from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghostwriter.main import create_app
from ghostwriter.upload import ATTACHMENT_TYPES, IMAGE_TYPES
from tests.conftest import CapturedAuditStream, as_user, make_settings
from tests.fakes import FakeDoor, tools_result


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
            ".dotx",
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
            ".potx",
            ".pptx",
            ".txt",
            ".xlsx",
        ],
        "image_types": [],
        "max_images": 8,
        "vision": False,
        "max_attachment_bytes": 52428800,
        "max_recording_s": 120,
        "max_recording_bytes": 10485760,
        "chat_retention_days": 90,
        "file_retention_days": 7,
        "version": "0.5.0",
    }


IMAGE_SUFFIXES = sorted(IMAGE_TYPES)


def _seeing_app(tmp_path: Path, door: FakeDoor, **overrides: object) -> FastAPI:
    settings = make_settings(tmp_path, vision=True, **overrides)
    return create_app(settings, audit_stream=CapturedAuditStream(), workbench_transport=httpx.MockTransport(door))


def test_config_offers_image_types_with_vision(tmp_path: Path) -> None:
    """Vision on, a workbench that lists ``view_file``: the ten picture suffixes, sorted, and attachable."""
    with TestClient(_seeing_app(tmp_path, FakeDoor(), max_images=5)) as client:
        config = client.get("/api/config", headers=as_user()).json()
    assert len(IMAGE_SUFFIXES) == 10
    assert config["image_types"] == IMAGE_SUFFIXES
    assert config["attachment_types"] == sorted({*ATTACHMENT_TYPES, *IMAGE_TYPES})
    assert (config["vision"], config["max_images"]) == (True, 5)


def test_config_offers_no_images_against_a_workbench_without_view_file(tmp_path: Path) -> None:
    """A workbench before 0.7 does not list ``view_file``: no picture is offered, documents are as before."""
    door = FakeDoor()
    listing = tools_result()
    listing["tools"] = [tool for tool in listing["tools"] if tool["name"] != "view_file"]
    door.answers["tools/list"] = listing
    with TestClient(_seeing_app(tmp_path, door)) as client:
        config = client.get("/api/config", headers=as_user()).json()
    assert (config["image_types"], config["vision"]) == ([], False)
    assert config["attachment_types"] == sorted(ATTACHMENT_TYPES)


def test_config_offers_no_images_without_workbench(tmp_path: Path) -> None:
    """With workbench off there is no tool to show a picture: none is offered."""
    settings = make_settings(tmp_path, vision=True, workbench_api_key=None)
    with TestClient(create_app(settings, audit_stream=CapturedAuditStream())) as client:
        config = client.get("/api/config").json()
    assert (config["attachments"], config["image_types"], config["vision"]) == (False, [], False)


def test_config_offers_no_images_once_ollama_says_the_model_cannot_see(tmp_path: Path) -> None:
    """Ollama lists no vision capability for the chat model: the probe turns the pictures off."""

    def show(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"capabilities": ["completion", "tools"]})

    settings = make_settings(
        tmp_path, vision=True, inference_provider="ollama", openai_api_base="http://ollama.test:11434/v1"
    )
    app = create_app(
        settings,
        audit_stream=CapturedAuditStream(),
        workbench_transport=httpx.MockTransport(FakeDoor()),
        inference_transport=httpx.MockTransport(show),
    )
    with TestClient(app) as client:
        assert app.state.vision.probed.wait(5)
        config = client.get("/api/config", headers=as_user()).json()
    assert (config["image_types"], config["vision"]) == ([], False)
    assert config["attachment_types"] == sorted(ATTACHMENT_TYPES)


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
