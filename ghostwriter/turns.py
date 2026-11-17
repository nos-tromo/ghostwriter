"""The answers being written, at most one per chat, for the process's one worker.

A turn is begun before an answer is written and ended when it is done; while it
runs, the chat takes no other answer and is not deleted. Stopping sets the turn's
event, which the answer's loop reads; the turn still ends only when the loop does.
A delete holds the chat's turn itself while it runs. Only the claimant ends a turn:
:meth:`TurnRegistry.end` names the event its :meth:`TurnRegistry.begin` returned,
so a late or repeated end never frees a claim that came after. A turn that cannot
read its event yet, as one waiting for a thread, asks to be told of a stop
(:meth:`TurnRegistry.on_stop`).
"""

import threading
from collections.abc import Callable


class TurnRegistry:
    """The chats with a turn running, each with the event that stops it. Thread-safe."""

    def __init__(self) -> None:
        """Start with no turn running."""
        self._lock = threading.Lock()
        self._running: dict[str, threading.Event] = {}
        self._on_stop: dict[str, Callable[[], None]] = {}

    def begin(self, chat_id: str) -> threading.Event | None:
        """Begin a turn in a chat, unless one runs.

        Args:
            chat_id: The chat.

        Returns:
            The event that stops the new turn, or ``None`` while one runs.
        """
        with self._lock:
            if chat_id in self._running:
                return None
            cancel = self._running[chat_id] = threading.Event()
            return cancel

    def end(self, chat_id: str, event: threading.Event) -> None:
        """End the chat's turn, if it is the one ``event`` belongs to; any other is left as it is.

        Args:
            chat_id: The chat.
            event: The event :meth:`begin` returned to the claimant.
        """
        with self._lock:
            if self._running.get(chat_id) is event:
                del self._running[chat_id]
                self._on_stop.pop(chat_id, None)

    def on_stop(self, chat_id: str, event: threading.Event, hook: Callable[[], None]) -> None:
        """Call ``hook`` once when the chat's turn is asked to stop; at once if it was already.

        Nothing is called for a turn that has ended, or one that is not the one ``event`` belongs to.

        Args:
            chat_id: The chat.
            event: The event :meth:`begin` returned to the claimant.
            hook: What to call, outside the registry's lock.
        """
        with self._lock:
            if self._running.get(chat_id) is not event:
                return
            if not event.is_set():
                self._on_stop[chat_id] = hook
                return
        hook()

    def stop(self, chat_id: str) -> bool:
        """Ask the chat's turn to stop, and call its stop hook.

        Args:
            chat_id: The chat.

        Returns:
            Whether a turn was running.
        """
        with self._lock:
            cancel = self._running.get(chat_id)
            if cancel is None:
                return False
            cancel.set()
            hook = self._on_stop.pop(chat_id, None)
        if hook is not None:
            hook()
        return True

    def running(self, chat_id: str) -> bool:
        """Tell whether a turn runs in the chat.

        Args:
            chat_id: The chat.

        Returns:
            ``True`` from :meth:`begin` until :meth:`end`.
        """
        with self._lock:
            return chat_id in self._running
