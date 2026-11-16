"""The answers being written, at most one per chat, for the process's one worker.

A turn is begun before an answer is written and ended when it is done; while it
runs, the chat takes no other answer and is not deleted. Stopping sets the turn's
event, which the answer's loop reads; the turn still ends only when the loop does.
A delete holds the chat's turn itself while it runs.
"""

import threading


class TurnRegistry:
    """The chats with a turn running, each with the event that stops it. Thread-safe."""

    def __init__(self) -> None:
        """Start with no turn running."""
        self._lock = threading.Lock()
        self._running: dict[str, threading.Event] = {}

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

    def end(self, chat_id: str) -> None:
        """End the chat's turn; a chat without one is left as it is.

        Args:
            chat_id: The chat.
        """
        with self._lock:
            self._running.pop(chat_id, None)

    def stop(self, chat_id: str) -> bool:
        """Ask the chat's turn to stop.

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
