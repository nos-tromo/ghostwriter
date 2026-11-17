"""The Ghostwriter backend, built by :func:`create_app`.

uvicorn runs it as ``uvicorn --factory ghostwriter.main:create_app`` with one
worker. The routes live under ``/api``; ``/metrics`` serves aggregate HTTP
metrics, never request content, for obs-plane's Prometheus. The lifespan holds
the process's one audit chain (:mod:`ghostwriter.audit`) and its chat store,
``app.state.store`` (:mod:`ghostwriter.store`). ``app.state.workbench`` is the
workbench client, ``None`` while workbench is off, and ``app.state.turns`` the
process's one turn registry (:mod:`ghostwriter.turns`). The lifespan also holds
``app.state.agent``, the agent loop (:mod:`ghostwriter.agent.loop`), and
``app.state.turn_threads``, the limiter whose ``TURN_THREADS`` threads the turns
run on, apart from the pool that serves every other request. ``app.state.model``
is the model client, which dictation (:mod:`ghostwriter.api.speech`, registered
only while ``GHOSTWRITER_SPEECH_INPUT`` is on) shares with the turns. Every
request body is capped per route (:mod:`ghostwriter.body_limit`), as nginx caps it.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Final, TextIO
from urllib.parse import quote

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, Response
from loguru import logger
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException

from ghostwriter import __version__
from ghostwriter.agent.limits import TURN_THREADS
from ghostwriter.agent.loop import AgentLoop
from ghostwriter.api import chats, files, messages, meta, speech
from ghostwriter.audit import open_emitter, settle_failed_operations, start_chain
from ghostwriter.body_limit import BodyLimit
from ghostwriter.config import Settings, load_settings
from ghostwriter.db import init_db, make_engine, utcnow
from ghostwriter.inference import ModelClient
from ghostwriter.log_cfg import setup_logger
from ghostwriter.store import ChatStore
from ghostwriter.turns import TurnRegistry
from ghostwriter.utils.audit_event import AuditWriteError
from ghostwriter.workbench import WorkbenchClient

setup_logger()

#: The body of every 500 the backend answers itself.
FAILED: Final = {"detail": "Request failed."}


def create_app(
    settings: Settings | None = None,
    *,
    audit_stream: TextIO | None = None,
    workbench_transport: httpx.AsyncBaseTransport | None = None,
    model: ModelClient | None = None,
    now: Callable[[], datetime] = utcnow,
) -> FastAPI:
    """Build the application.

    Args:
        settings: The settings to serve with; read from the environment when omitted.
        audit_stream: Where the audit chain goes; where ``AUDIT_OUTPUT`` selects when omitted.
        workbench_transport: The transport to workbench (tests pass an ``httpx.MockTransport``);
            ``None`` uses httpx's own.
        model: The chat model (tests pass one over a fake router); the router of the
            settings when omitted.
        now: The store's clock (tests pass one set by hand).

    Returns:
        The app, with ``settings`` on ``app.state.settings``.

    Raises:
        ValueError: If the environment holds a value that cannot work.
    """
    if settings is None:
        settings = load_settings()
    chat_model = model if model is not None else ModelClient(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Run the audit chain and the chat store for as long as the app serves; if either cannot open, startup fails.

        The store's schema is brought to this release's version and the
        configured periods are put in force before the first request.

        Args:
            app: The application being served.

        Yields:
            None: Control to the server while it serves.
        """
        emitter = open_emitter(__version__, audit_stream)
        engine: Engine | None = None
        try:
            start_chain(app, emitter)
            engine = make_engine(settings.db_path)
            init_db(engine)
            store = ChatStore(engine, file_days=settings.file_days, notice_days=settings.notice_days, now=now)
            app.state.store = store
            store.apply_periods(settings.chat_days)
            app.state.agent = AgentLoop(model=chat_model, workbench=app.state.workbench, store=store, settings=settings)
            app.state.turn_threads = anyio.CapacityLimiter(TURN_THREADS)
            yield
        finally:
            if engine is not None:
                engine.dispose()
            emitter.close()

    app = FastAPI(
        title="Ghostwriter",
        description="The federation's chat assistant.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.model = chat_model
    app.state.workbench = (
        WorkbenchClient(settings, transport=workbench_transport) if settings.workbench_enabled else None
    )
    app.state.turns = TurnRegistry()

    @app.exception_handler(AuditWriteError)
    async def refuse_unrecorded(request: Request, exc: AuditWriteError) -> JSONResponse:
        """Refuse a request whose audit event could not be written, without its data.

        Args:
            request: The request.
            exc: The failed write.

        Returns:
            The generic 500.
        """
        logger.error("An audit event could not be written; refusing {} '{}'.", request.method, quote(request.url.path))
        settle_failed_operations(request)
        return JSONResponse(status_code=500, content=FAILED)

    @app.exception_handler(StarletteHTTPException)
    async def settle_server_errors(request: Request, exc: StarletteHTTPException) -> Response:
        """Settle the request's open operations on a 5xx; a 4xx returns no data and records nothing.

        Args:
            request: The request.
            exc: The HTTP error.

        Returns:
            FastAPI's usual answer for it.
        """
        if exc.status_code >= 500:
            settle_failed_operations(request)
        return await http_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def refuse_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Answer an unexpected error with the generic 500, after settling the request's open operations.

        Args:
            request: The request.
            exc: The error, logged by its type only.

        Returns:
            The generic 500.
        """
        logger.error("Unhandled {} on {} '{}'.", type(exc).__name__, request.method, quote(request.url.path))
        settle_failed_operations(request)
        return JSONResponse(status_code=500, content=FAILED)

    @app.get("/api/health", summary="Liveness probe", tags=["Metadata"])
    def health() -> dict[str, str]:
        """Answer while the process serves.

        Returns:
            ``{"status": "ok"}``.
        """
        return {"status": "ok"}

    app.include_router(meta.router)
    app.include_router(chats.router)
    app.include_router(messages.router)
    app.include_router(files.router)
    if settings.speech_input:
        app.include_router(speech.router)
    app.add_middleware(BodyLimit)
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    return app
