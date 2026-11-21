"""When pictures are offered, and what Ollama says of the chat model.

Pictures are offered while three things hold (:meth:`Vision.images_offered`): ``GHOSTWRITER_VISION``
is on, the chat model can see, and workbench lists :data:`VIEW_TOOL`. The model is taken to see as
configured, except on Ollama, where :meth:`Vision.probe` reads the model's capabilities once at start
and turns the pictures off for a model that lists no ``vision``. Any other answer, or none, leaves the
setting as it is: the probe never blocks start-up and never turns pictures on.

workbench is asked with :func:`system_call`: the system's own call, for no chat, for the listing a call
that takes pictures gets (``images=True``: the client sends ``X-Accept-Images: 1``), ``view_file`` included
when workbench shows images. Its discovery reads no principal. Neither the probe's answer, the model's name
nor the router's key is ever logged: a failed probe logs its kind only.
"""

import threading
from typing import Final
from uuid import uuid4

import httpx
from loguru import logger

from ghostwriter.audit import SYSTEM_ACTOR, RequestContext, forward_headers
from ghostwriter.config import Settings
from ghostwriter.workbench import CallFor, WorkbenchClient

#: The tool workbench lists only to a caller that takes pictures, and that shows a file's pages or picture.
VIEW_TOOL: Final = "view_file"
#: The longest the probe waits for Ollama, in seconds, for connecting, writing and reading alike.
PROBE_TIMEOUT_S: Final = 5.0


def system_call() -> CallFor:
    """Return the call workbench's capabilities are asked for with: the system's, for no chat.

    The discovery adds ``X-Accept-Images: 1`` itself, when asked for the listing that takes images.

    Returns:
        The call, under a fresh request id.
    """
    return CallFor("ghostwriter:system", "capability", forward_headers(RequestContext(SYSTEM_ACTOR, None, uuid4().hex)))


class Vision:
    """Whether pictures are offered, for the one process.

    Attributes:
        model_sees: Whether the chat model can see: ``GHOSTWRITER_VISION`` at first, and ``False`` once
            Ollama lists no ``vision`` capability for it.
        probed: Set when a probe ended, whatever it found (tests wait on it).
    """

    def __init__(self, settings: Settings) -> None:
        """Start from the setting.

        Args:
            settings: ``GHOSTWRITER_VISION``, and the router's base, key and chat model for the probe.
        """
        self._settings = settings
        self.model_sees = settings.vision
        self.probed = threading.Event()

    def images_offered(self, workbench: WorkbenchClient | None) -> bool:
        """Say whether pictures are offered: the setting, a model that sees and a workbench that shows them.

        workbench is asked only while the first two hold, for the listing of a call that takes images. Its
        discovery is the cached, single-flight one, so this blocks at most once per cache window; call it
        from a thread, never on the event loop.

        Args:
            workbench: The workbench client, ``None`` while workbench is off.

        Returns:
            ``True`` when :attr:`model_sees`, there is a workbench, and it lists :data:`VIEW_TOOL`.
        """
        if not self.model_sees or workbench is None:
            return False
        found = workbench.discover(call=system_call(), images=True)
        return found is not None and any(tool.name == VIEW_TOOL for tool in found.tools)

    async def probe(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """Ask Ollama whether the chat model lists a ``vision`` capability, and turn pictures off if it does not.

        One ``POST /api/show`` to the router's own host (``OPENAI_API_BASE`` without its ``/v1``), taking no
        proxy from the environment. A ``vision`` capability changes nothing. Any other answer (a status
        other than 200, a transport error, a body that is not JSON or holds no list of strings under
        ``capabilities``) changes nothing either and logs a warning that names the kind of failure.
        :attr:`probed` is set last, whatever happens, a cancellation included.

        Args:
            transport: The transport to Ollama (tests pass an ``httpx.MockTransport``); ``None`` uses
                httpx's own.
        """
        try:
            try:
                capabilities = await self._show(transport)
            except _Unreadable as exc:
                self._unread(exc.token)
            except Exception as exc:
                self._unread(type(exc).__name__)
            else:
                if "vision" not in capabilities:
                    self.model_sees = False
                    logger.warning("The chat model lists no vision capability; images are not offered.")
        finally:
            self.probed.set()

    async def _show(self, transport: httpx.AsyncBaseTransport | None) -> list[str]:
        """Read the chat model's capabilities from Ollama's ``/api/show``.

        Args:
            transport: As :meth:`probe`.

        Returns:
            The capabilities, each a string.

        Raises:
            _Unreadable: For a status other than 200, a body that is not JSON, or one without a list of
                strings under ``capabilities``.
            httpx.HTTPError: When Ollama cannot be reached in :data:`PROBE_TIMEOUT_S`.
        """
        settings = self._settings
        base = settings.openai_api_base.removesuffix("/v1")
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=PROBE_TIMEOUT_S) as client:
            response = await client.post(
                f"{base}/api/show",
                json={"model": settings.text_model},
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )
        if response.status_code != 200:
            raise _Unreadable(f"http:{response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise _Unreadable("not_json") from None
        listed = body.get("capabilities") if isinstance(body, dict) else None
        if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
            raise _Unreadable("no_list")
        return listed

    @staticmethod
    def _unread(token: str) -> None:
        """Warn that the capabilities could not be read.

        Args:
            token: The kind of failure, never a body, a URL or a key.
        """
        logger.warning("The chat model's capabilities could not be read ({}); images stay as configured.", token)


class _Unreadable(Exception):
    """Ollama's answer holds no capabilities.

    Attributes:
        token: The kind of failure: ``http:<status>``, ``not_json`` or ``no_list``.
    """

    def __init__(self, token: str) -> None:
        """Keep the kind.

        Args:
            token: The kind of failure.
        """
        super().__init__(token)
        self.token = token
