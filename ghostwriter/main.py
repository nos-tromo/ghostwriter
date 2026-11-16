"""The Ghostwriter backend, built by :func:`create_app`.

uvicorn runs it as ``uvicorn --factory ghostwriter.main:create_app`` with one
worker. The routes live under ``/api``; ``/metrics`` serves aggregate HTTP
metrics, never request content, for obs-plane's Prometheus.
"""

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from ghostwriter import __version__
from ghostwriter.config import Settings, load_settings
from ghostwriter.log_cfg import setup_logger

setup_logger()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: The settings to serve with; read from the environment when omitted.

    Returns:
        The app, with ``settings`` on ``app.state.settings``.

    Raises:
        ValueError: If the environment holds a value that cannot work.
    """
    if settings is None:
        settings = load_settings()
    app = FastAPI(
        title="Ghostwriter",
        description="The federation's chat assistant.",
        version=__version__,
    )
    app.state.settings = settings

    @app.get("/api/health", summary="Liveness probe", tags=["Metadata"])
    def health() -> dict[str, str]:
        """Answer while the process serves.

        Returns:
            ``{"status": "ok"}``.
        """
        return {"status": "ok"}

    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    return app
