"""The Ghostwriter backend, built by :func:`create_app`.

uvicorn runs it as ``uvicorn --factory ghostwriter.main:create_app`` with one
worker. The routes live under ``/api``; ``/metrics`` serves aggregate HTTP
metrics, never request content, for obs-plane's Prometheus. The lifespan holds
the process's one audit chain (:mod:`ghostwriter.audit`).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TextIO
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger
from prometheus_fastapi_instrumentator import Instrumentator

from ghostwriter import __version__
from ghostwriter.api import meta
from ghostwriter.audit import open_emitter, start_chain
from ghostwriter.config import Settings, load_settings
from ghostwriter.log_cfg import setup_logger
from ghostwriter.utils.audit_event import AuditWriteError

setup_logger()


def create_app(settings: Settings | None = None, *, audit_stream: TextIO | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: The settings to serve with; read from the environment when omitted.
        audit_stream: Where the audit chain goes; where ``AUDIT_OUTPUT`` selects when omitted.

    Returns:
        The app, with ``settings`` on ``app.state.settings``.

    Raises:
        ValueError: If the environment holds a value that cannot work.
    """
    if settings is None:
        settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Run the process's audit chain for as long as the app serves; if it cannot open, startup fails.

        Args:
            app: The application being served.

        Yields:
            None: Control to the server while it serves.
        """
        emitter = open_emitter(__version__, audit_stream)
        try:
            start_chain(app, emitter)
            yield
        finally:
            emitter.close()

    app = FastAPI(
        title="Ghostwriter",
        description="The federation's chat assistant.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings

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
        return JSONResponse(status_code=500, content={"detail": "Request failed."})

    @app.get("/api/health", summary="Liveness probe", tags=["Metadata"])
    def health() -> dict[str, str]:
        """Answer while the process serves.

        Returns:
            ``{"status": "ok"}``.
        """
        return {"status": "ok"}

    app.include_router(meta.router)
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    return app
