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
only while ``GHOSTWRITER_SPEECH_INPUT`` is on) shares with the turns; dictation
runs on ``app.state.speech_threads``, a limiter of ``SPEECH_THREADS`` threads of
its own. ``app.state.sweeper`` is the retention sweep (:mod:`ghostwriter.retention`),
which the lifespan runs a minute after start and then daily, and cancels at
shutdown. ``app.state.processor`` is the attachment processor (:mod:`ghostwriter.processing`), ``None``
while workbench is off: at start it prepares again what the last process left ``processing``, or, without
workbench, settles it; at shutdown its jobs are cancelled and awaited before the database goes. It takes a
video or audio clip to Nextext (``app.state.nextext``) and back before it prepares it. Right after the requeue the
lifespan starts its start reconcile (:meth:`~ghostwriter.processing.AttachmentProcessor.start_media`), which
start-up never waits for: once Nextext's probe passes, it deletes every job Nextext lists for Ghostwriter, then opens
the gate every clip, a requeued one too, waits for before it makes its job.
``app.state.vision`` decides whether pictures are offered (:mod:`ghostwriter.vision`); on
Ollama, with pictures on, the lifespan also starts its one probe of the chat model, which start-up never
waits for, and cancels it at shutdown. ``app.state.nextext`` is the Nextext client and ``app.state.media`` the
probe of its health, which decides with workbench's version whether video and audio are offered
(:mod:`ghostwriter.nextext`); both are ``None`` while Nextext is off (no base, or no key). The lifespan runs
the probe, which start-up never waits for, and cancels it at shutdown. Every request body is capped per route
(:mod:`ghostwriter.body_limit`), as nginx caps it.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
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
from ghostwriter.inference import SPEECH_THREADS, ModelClient
from ghostwriter.log_cfg import setup_logger
from ghostwriter.nextext import NextextClient, NextextHealth
from ghostwriter.processing import AttachmentProcessor
from ghostwriter.retention import FIRST_SWEEP_DELAY_S, SWEEP_INTERVAL_S, Sweeper
from ghostwriter.retention_rule import run_retention_loop
from ghostwriter.store import ChatStore
from ghostwriter.turns import TurnRegistry
from ghostwriter.utils.audit_event import AuditWriteError
from ghostwriter.vision import Vision
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
    inference_transport: httpx.AsyncBaseTransport | None = None,
    nextext_transport: httpx.AsyncBaseTransport | None = None,
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
        inference_transport: The transport of the Ollama probe (tests pass an ``httpx.MockTransport``);
            ``None`` uses httpx's own.
        nextext_transport: The transport to Nextext (tests pass an ``httpx.MockTransport``);
            ``None`` uses httpx's own.
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
        configured periods are put in force before the first request; attachments
        left ``processing`` are prepared again, or settled without workbench; then
        the retention sweep starts, the Ollama probe where it applies and Nextext's health probe while Nextext is
        on (start-up waits for neither); all are cancelled first at shutdown, then the
        attachment jobs, before the database goes.

        Args:
            app: The application being served.

        Yields:
            None: Control to the server while it serves.
        """
        emitter = open_emitter(__version__, audit_stream)
        engine: Engine | None = None
        sweeps: asyncio.Task[None] | None = None
        probe: asyncio.Task[None] | None = None
        media_probe: asyncio.Task[None] | None = None
        processor: AttachmentProcessor | None = None
        try:
            start_chain(app, emitter)
            engine = make_engine(settings.db_path)
            init_db(engine)
            store = ChatStore(engine, file_days=settings.file_days, notice_days=settings.notice_days, now=now)
            app.state.store = store
            store.apply_periods(settings.chat_days)
            workbench: WorkbenchClient | None = app.state.workbench
            if workbench is not None:
                processor = AttachmentProcessor(
                    store,
                    workbench,
                    emitter,
                    image_tokens=settings.image_tokens,
                    nextext=app.state.nextext,
                    health=app.state.media,
                    nextext_timeout_s=settings.nextext_timeout_s,
                )
                processor.requeue()
                processor.start_media()
            elif settled := store.settle_processing("unavailable"):
                logger.info("{} attachments left processing were settled without workbench.", settled)
            app.state.processor = processor
            app.state.agent = AgentLoop(
                model=chat_model,
                workbench=app.state.workbench,
                store=store,
                settings=settings,
                vision=app.state.vision,
                media=app.state.media,
            )
            app.state.turn_threads = anyio.CapacityLimiter(TURN_THREADS)
            app.state.speech_threads = anyio.CapacityLimiter(SPEECH_THREADS)
            app.state.sweeper = Sweeper(
                store=store,
                workbench=app.state.workbench,
                emitter=emitter,
                settings=settings,
                turns=app.state.turns,
                now=now,
                processor=processor,
            )
            sweeps = asyncio.create_task(
                run_retention_loop(app.state.sweeper.sweep, first_delay=FIRST_SWEEP_DELAY_S, interval=SWEEP_INTERVAL_S)
            )
            if settings.vision and settings.inference_provider == "ollama":
                probe = asyncio.create_task(app.state.vision.probe(transport=inference_transport))
            media: NextextHealth | None = app.state.media
            if media is not None:
                media_probe = asyncio.create_task(media.run())
            yield
        finally:
            for task in (media_probe, probe, sweeps):
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            if processor is not None:
                await processor.aclose()
            if engine is not None:
                engine.dispose()
            emitter.close()

    # No Swagger UI, ReDoc or schema: every peer on inference-net and workbench-net reaches
    # the backend, and Swagger UI would load its assets from a CDN.
    app = FastAPI(
        title="Ghostwriter",
        description="The federation's chat assistant.",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.model = chat_model
    app.state.workbench = (
        WorkbenchClient(settings, transport=workbench_transport) if settings.workbench_enabled else None
    )
    app.state.turns = TurnRegistry()
    app.state.vision = Vision(settings)
    nextext = NextextClient(settings, transport=nextext_transport) if settings.nextext_enabled else None
    app.state.nextext = nextext
    app.state.media = NextextHealth(nextext) if nextext is not None else None

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
