"""When pictures are offered: the setting, a model that sees and a workbench that lists ``view_file``.

The probe is Ollama's ``/api/show``; the door is :class:`tests.fakes.FakeDoor`. Every name, id and value here
is invented.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from _pytest.logging import LogCaptureFixture
from fastapi.testclient import TestClient

from ghostwriter.config import Settings
from ghostwriter.main import create_app
from ghostwriter.vision import PROBE_TIMEOUT_S, VIEW_TOOL, Vision, system_call
from ghostwriter.workbench import WorkbenchClient
from tests.conftest import CapturedAuditStream, as_user, make_settings
from tests.fakes import FakeDoor, tools_result

BASE = "http://ollama.test:11434/v1"
SHOW = "http://ollama.test:11434/api/show"
NO_VISION = "The chat model lists no vision capability; images are not offered."
UNREAD = "The chat model's capabilities could not be read ({}); images stay as configured."


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "vision": True,
        "inference_provider": "ollama",
        "openai_api_base": BASE,
        "text_model": "chat-model",
        "openai_api_key": "router-key",
        **overrides,
    }
    return make_settings(tmp_path, **fields)


def _workbench(settings: Settings, door: FakeDoor) -> WorkbenchClient:
    return WorkbenchClient(settings, transport=httpx.MockTransport(door))


def _without_view_file() -> dict[str, Any]:
    listing = tools_result()
    listing["tools"] = [tool for tool in listing["tools"] if tool["name"] != VIEW_TOOL]
    return listing


def test_images_are_offered_with_vision_a_seeing_model_and_view_file(tmp_path: Path) -> None:
    """All three hold: offered. No ``view_file`` in the listing: not. Vision off or no workbench: not, unasked."""
    settings = make_settings(tmp_path, vision=True)
    door = FakeDoor()
    assert Vision(settings).images_offered(_workbench(settings, door)) is True

    old = FakeDoor()
    old.answers["tools/list"] = _without_view_file()
    assert Vision(settings).images_offered(_workbench(settings, old)) is False

    off = make_settings(tmp_path, vision=False)
    silent = FakeDoor()
    assert Vision(off).images_offered(_workbench(off, silent)) is False
    assert silent.requests == []

    assert Vision(settings).images_offered(None) is False


def test_a_model_that_cannot_see_offers_no_images_and_asks_nothing(tmp_path: Path) -> None:
    """``model_sees`` false, as after the probe: no discovery is made for the answer."""
    settings = make_settings(tmp_path, vision=True)
    door = FakeDoor()
    vision = Vision(settings)
    vision.model_sees = False
    assert vision.images_offered(_workbench(settings, door)) is False
    assert door.requests == []


def test_a_workbench_that_cannot_be_reached_offers_no_images(tmp_path: Path) -> None:
    """No discovery, no ``view_file``: no pictures."""
    settings = make_settings(tmp_path, vision=True)
    door = FakeDoor()
    door.answers["server/discover"] = httpx.Response(503)
    assert Vision(settings).images_offered(_workbench(settings, door)) is False


def test_the_offer_reads_a_cached_discovery(tmp_path: Path) -> None:
    """Two offers, one discovery: ``server/discover`` and ``tools/list`` once."""
    settings = make_settings(tmp_path, vision=True)
    door = FakeDoor()
    vision, workbench = Vision(settings), _workbench(settings, door)
    assert vision.images_offered(workbench) is True
    assert vision.images_offered(workbench) is True
    assert door.methods() == ["server/discover", "tools/list"]


def test_the_system_call_asks_as_the_system_for_no_chat() -> None:
    """The discovery carries the system's own request id and no chat's; the picture header is the client's."""
    first, second = system_call(), system_call()
    assert (first.owner, first.chat_id) == ("ghostwriter:system", "capability")
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
    assert not any(name.lower() == "x-accept-images" for name in first.headers)


def test_discovery_asks_for_the_tools_a_picture_taking_caller_gets(tmp_path: Path) -> None:
    """The listing's request carries ``X-Accept-Images: 1``, which is why the cached one lists ``view_file``."""
    settings = make_settings(tmp_path, vision=True)
    door = FakeDoor()
    Vision(settings).images_offered(_workbench(settings, door))
    sent = [request.headers.get("x-accept-images") for request, body in door.requests if body["method"] == "tools/list"]
    assert sent == ["1"]


async def test_ollama_without_vision_turns_images_off(tmp_path: Path, loguru_caplog: LogCaptureFixture) -> None:
    """The probe asks ``/api/show`` of the router's host for the chat model; no ``vision`` turns images off."""
    settings = _settings(tmp_path)
    seen: list[httpx.Request] = []

    def show(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"capabilities": ["completion", "tools"]})

    vision = Vision(settings)
    await vision.probe(transport=httpx.MockTransport(show))
    assert vision.model_sees is False and vision.probed.is_set()
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", SHOW)
    assert json.loads(request.content) == {"model": "chat-model"}
    assert request.headers["authorization"] == "Bearer router-key"
    assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), PROBE_TIMEOUT_S)
    assert NO_VISION in loguru_caplog.text


async def test_ollama_with_vision_keeps_images_on(tmp_path: Path, loguru_caplog: LogCaptureFixture) -> None:
    """A ``vision`` capability among the others changes nothing, and says nothing."""
    settings = _settings(tmp_path)

    def show(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"capabilities": ["completion", "vision", "tools"]})

    vision = Vision(settings)
    await vision.probe(transport=httpx.MockTransport(show))
    assert vision.model_sees is True and vision.probed.is_set()
    assert loguru_caplog.text == ""


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def _slow(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


@pytest.mark.parametrize(
    ("answer", "token"),
    [
        (httpx.Response(500, json={"error": "model 'chat-model' failed"}), "http:500"),
        (_refused, "ConnectError"),
        (_slow, "ReadTimeout"),
        (httpx.Response(200, content=b"<html>not json</html>"), "not_json"),
        (httpx.Response(200, json={"license": "invented"}), "no_list"),
        (httpx.Response(200, json={"capabilities": "vision"}), "no_list"),
        (httpx.Response(200, json={"capabilities": ["vision", 3]}), "no_list"),
        (httpx.Response(200, json=["vision"]), "no_list"),
    ],
)
async def test_a_failed_probe_keeps_the_setting(
    tmp_path: Path,
    loguru_caplog: LogCaptureFixture,
    answer: httpx.Response | Callable[[httpx.Request], httpx.Response],
    token: str,
) -> None:
    """Any answer but a list of capabilities leaves images as configured, warns with the kind, and ends the probe."""
    settings = _settings(tmp_path)

    def show(request: httpx.Request) -> httpx.Response:
        return answer if isinstance(answer, httpx.Response) else answer(request)

    vision = Vision(settings)
    await vision.probe(transport=httpx.MockTransport(show))
    assert vision.model_sees is True and vision.probed.is_set()
    assert UNREAD.format(token) in loguru_caplog.text
    for secret in ("chat-model", "router-key", "ollama.test"):
        assert secret not in loguru_caplog.text


class _SpyClient(httpx.AsyncClient):
    """An ``AsyncClient`` that remembers every instance built."""

    built: ClassVar[list[httpx.AsyncClient]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the client and remember it.

        Args:
            *args: Passed through.
            **kwargs: Passed through.
        """
        super().__init__(*args, **kwargs)
        _SpyClient.built.append(self)


async def test_the_probe_takes_no_proxy_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``trust_env`` is off: a proxy set in the environment never carries the probe somewhere else."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setattr(_SpyClient, "built", [])
    monkeypatch.setattr(httpx, "AsyncClient", _SpyClient)

    def show(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"capabilities": ["vision"]})

    vision = Vision(_settings(tmp_path))
    await vision.probe(transport=httpx.MockTransport(show))
    assert vision.model_sees is True
    assert [built.trust_env for built in _SpyClient.built] == [False]


@pytest.mark.parametrize("overrides", [{"inference_provider": "vllm"}, {"vision": False}], ids=["vllm", "vision-off"])
def test_no_probe_on_vllm(tmp_path: Path, overrides: dict[str, Any]) -> None:
    """The federation's router (vllm) is never asked, nor is Ollama while vision is off."""
    settings = _settings(tmp_path, **overrides)
    calls: list[httpx.Request] = []

    def show(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"capabilities": []})

    app = create_app(
        settings,
        audit_stream=CapturedAuditStream(),
        workbench_transport=httpx.MockTransport(FakeDoor()),
        inference_transport=httpx.MockTransport(show),
    )
    with TestClient(app) as client:
        assert client.get("/api/health", headers=as_user()).status_code == 200
        assert not app.state.vision.probed.wait(0.2)
    assert calls == []
    assert app.state.vision.model_sees is settings.vision


def test_the_app_probes_ollama_at_start(tmp_path: Path) -> None:
    """Ollama with vision on: the lifespan probes once, without holding the start."""
    seen: list[httpx.Request] = []

    def show(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"capabilities": ["completion", "vision"]})

    app = create_app(
        _settings(tmp_path),
        audit_stream=CapturedAuditStream(),
        workbench_transport=httpx.MockTransport(FakeDoor()),
        inference_transport=httpx.MockTransport(show),
    )
    with TestClient(app):
        assert app.state.vision.probed.wait(5)
    assert [str(request.url) for request in seen] == [SHOW]
    assert app.state.vision.model_sees is True
