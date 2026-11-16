"""Server-Sent Events: one frame per event, and the bridge from a blocking generator to the event loop.

:func:`aiter_sync_gen` is copied from docint's ``_aiter_sync_gen`` (``docint/core/api.py``),
with the same poll interval; it also takes the capacity limiter whose threads the generator
runs on, so a long stream holds none of the shared pool's.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any, Final

from anyio import CapacityLimiter, to_thread
from loguru import logger
from starlette.requests import Request

#: Seconds between two checks for a client gone while the generator yields nothing.
STREAM_DISCONNECT_POLL_INTERVAL_S: Final = 1.0


def sse_frame(event: str, data: Mapping[str, Any]) -> str:
    """Write one SSE frame.

    Args:
        event: The event name.
        data: The frame's data.

    Returns:
        ``event: <event>``, then the data as compact JSON on one line, then a blank line.
    """
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def aiter_sync_gen(
    gen_factory: Callable[[], Iterator[Any]],
    request: Request | None = None,
    limiter: CapacityLimiter | None = None,
) -> AsyncIterator[Any]:
    """Drive a blocking sync generator on a worker thread, yielding its items.

    The generator is built and fully iterated inside ``to_thread.run_sync`` so
    neither construction nor any ``next()`` runs on the asyncio event loop —
    that keeps the loop free to serve concurrent requests. Items cross back to
    the loop via a thread-safe queue: ``None`` signals normal completion and an
    ``Exception`` instance is re-raised on the loop.

    Args:
        gen_factory (Callable[[], Iterator[Any]]): Zero-arg callable returning
            the blocking generator. A factory (not the generator itself) is
            used so construction also happens off the loop.
        request (Request | None): Optional request polled for client
            disconnect; when disconnected the worker-awaiter is cancelled and
            iteration stops. The worker thread cannot be force-killed, so it
            runs to completion and its remaining output is discarded.
        limiter (CapacityLimiter | None): The limiter whose threads the
            generator runs on; ``None`` for the shared default pool.

    Yields:
        Any: Each item produced by the generator, in order.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _safe_put(item: Any) -> None:
        """Enqueue an item from the worker thread, tolerating a closed loop."""
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except Exception as exc:
            # The loop may be gone after a client disconnect/teardown; log
            # rather than letting the worker-thread exception vanish.
            logger.warning("Could not enqueue stream item (loop unavailable): {}", exc)

    def _pump() -> None:
        """Iterate the blocking generator, forwarding items then a sentinel."""
        try:
            for item in gen_factory():
                _safe_put(item)
            _safe_put(None)
        except Exception as exc:  # surface to the loop, then stop
            _safe_put(exc)

    task = asyncio.create_task(to_thread.run_sync(_pump, limiter=limiter))
    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(),
                    timeout=STREAM_DISCONNECT_POLL_INTERVAL_S,
                )
            except TimeoutError:
                if request is not None and await request.is_disconnected():
                    return
                continue
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        if not task.done():
            task.cancel()
