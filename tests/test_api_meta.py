"""What ``/api/config`` tells the SPA is switched on, and what the backend does not serve.

Every name, id and value here is invented.
"""

import time
from pathlib import Path

import anyio
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghostwriter.main import create_app
from ghostwriter.upload import ATTACHMENT_TYPES, IMAGE_TYPES, MEDIA_TYPES
from tests.conftest import NEXTEXT_BASE, NEXTEXT_KEY, CapturedAuditStream, as_user, make_settings
from tests.fakes import FakeDoor, FakeNextext, tools_result


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
        "media_types": [],
        "media": False,
        "max_images": 8,
        "vision": False,
        "max_attachment_bytes": 52428800,
        "max_recording_s": 120,
        "max_recording_bytes": 10485760,
        "chat_retention_days": 90,
        "file_retention_days": 7,
        "version": "0.6.1",
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


MEDIA_SUFFIXES = sorted(MEDIA_TYPES)


def _clip_app(tmp_path: Path, door: FakeDoor, nextext: httpx.AsyncBaseTransport, **overrides: object) -> FastAPI:
    settings = make_settings(tmp_path, nextext_api_base=NEXTEXT_BASE, nextext_api_key=NEXTEXT_KEY, **overrides)
    return create_app(
        settings,
        audit_stream=CapturedAuditStream(),
        workbench_transport=httpx.MockTransport(door),
        nextext_transport=nextext,
    )


def _probed(app: FastAPI) -> None:
    """Wait, from the test's thread, until the probe has asked Nextext once."""
    deadline = time.monotonic() + 5
    while not app.state.media.checked.is_set():
        assert time.monotonic() < deadline, "the probe never ended"
        time.sleep(0.01)


def test_config_offers_clips_with_nextext_and_workbench_0_9(tmp_path: Path) -> None:
    """A healthy Nextext and a workbench of 0.9.0: the eleven clip suffixes, sorted, and attachable."""
    app = _clip_app(tmp_path, FakeDoor(server_version="0.9.0"), httpx.MockTransport(FakeNextext()))
    with TestClient(app) as client:
        _probed(app)
        config = client.get("/api/config", headers=as_user()).json()
    assert len(MEDIA_SUFFIXES) == 11
    assert (config["media"], config["media_types"]) == (True, MEDIA_SUFFIXES)
    assert config["attachment_types"] == sorted({*ATTACHMENT_TYPES, *MEDIA_TYPES})
    assert (config["vision"], config["image_types"]) == (False, [])


def test_config_offers_clips_beside_pictures(tmp_path: Path) -> None:
    """With vision on too, the attachable types hold documents, pictures and clips."""
    app = _clip_app(tmp_path, FakeDoor(server_version="0.9.0"), httpx.MockTransport(FakeNextext()), vision=True)
    with TestClient(app) as client:
        _probed(app)
        config = client.get("/api/config", headers=as_user()).json()
    assert config["attachment_types"] == sorted({*ATTACHMENT_TYPES, *IMAGE_TYPES, *MEDIA_TYPES})
    assert (config["vision"], config["media"]) == (True, True)


def test_no_clips_against_workbench_0_8(tmp_path: Path) -> None:
    """A workbench before 0.9.0 cannot take a clip, whatever Nextext says."""
    app = _clip_app(tmp_path, FakeDoor(server_version="0.8.0"), httpx.MockTransport(FakeNextext()))
    with TestClient(app) as client:
        _probed(app)
        assert app.state.media.healthy is True
        config = client.get("/api/config", headers=as_user()).json()
    assert (config["media"], config["media_types"]) == (False, [])
    assert config["attachment_types"] == sorted(ATTACHMENT_TYPES)


def test_no_clips_while_nextext_is_down(tmp_path: Path) -> None:
    """A Nextext whose health is not 200 offers none, though workbench takes them."""
    fake = FakeNextext()
    fake.health = 503
    app = _clip_app(tmp_path, FakeDoor(server_version="0.9.0"), httpx.MockTransport(fake))
    with TestClient(app) as client:
        _probed(app)
        config = client.get("/api/config", headers=as_user()).json()
    assert (config["media"], config["media_types"]) == (False, [])
    assert config["attachment_types"] == sorted(ATTACHMENT_TYPES)


def test_no_clips_without_a_key(tmp_path: Path) -> None:
    """A base without the token switches clips off: there is no client, no probe, and Nextext is never asked."""
    fake = FakeNextext()
    settings = make_settings(tmp_path, nextext_api_base=NEXTEXT_BASE)
    app = create_app(
        settings,
        audit_stream=CapturedAuditStream(),
        workbench_transport=httpx.MockTransport(FakeDoor(server_version="0.9.0")),
        nextext_transport=httpx.MockTransport(fake),
    )
    with TestClient(app) as client:
        config = client.get("/api/config", headers=as_user()).json()
    assert (app.state.nextext, app.state.media) == (None, None)
    assert (config["media"], config["media_types"]) == (False, [])
    assert fake.requests == []


def test_no_clips_without_workbench(tmp_path: Path) -> None:
    """Clips go through workbench: without it, a healthy Nextext offers none."""
    app = _clip_app(
        tmp_path, FakeDoor(server_version="0.9.0"), httpx.MockTransport(FakeNextext()), workbench_api_key=None
    )
    with TestClient(app) as client:
        _probed(app)
        config = client.get("/api/config", headers=as_user()).json()
    assert (config["attachments"], config["media"], config["media_types"]) == (False, False, [])


def test_a_hanging_nextext_never_holds_start_up(tmp_path: Path) -> None:
    """A Nextext that never answers: the app is up and ``/api/config`` answers at once, with no clips offered."""

    async def hang(request: httpx.Request) -> httpx.Response:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    app = _clip_app(tmp_path, FakeDoor(server_version="0.9.0"), httpx.MockTransport(hang))
    started = time.monotonic()
    with TestClient(app) as client:
        config = client.get("/api/config", headers=as_user()).json()
        assert time.monotonic() - started < 3
        assert not app.state.media.checked.is_set()
    assert (config["media"], config["media_types"]) == (False, [])


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
