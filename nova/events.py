"""Event bus for Nova Agent — bridges agent core (thread) to TUI (main thread).

All internal print() calls are replaced with event emissions.
The TUI polls events via set_interval() to update the display.
"""

import logging
import threading
from queue import Queue, Empty

logger = logging.getLogger("nova")


class AgentEvent:
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    AGENT_RESPONSE = "agent_response"
    AGENT_THINKING = "agent_thinking"
    AGENT_DONE = "agent_done"
    ERROR = "error"
    ASK_USER = "ask_user"
    STATUS = "status"


class EventBus:
    """Thread-safe event bus — producer (agent thread) → consumer (TUI)."""

    def __init__(self):
        self._queue = Queue()
        self._listeners = []
        self._lock = threading.Lock()

    def emit(self, event_type, data=None):
        event = {"type": event_type, "data": data}
        self._queue.put(event)
        with self._lock:
            for listener in self._listeners:
                try:
                    listener(event_type, data)
                except Exception:
                    pass
        logger.debug(f"Event: {event_type} | {str(data)[:80] if data else ''}")

    def add_listener(self, callback):
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback):
        with self._lock:
            self._listeners = [l for l in self._listeners if l != callback]

    def poll(self):
        """Drain all queued events — called by TUI timer."""
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except Empty:
                break
        return events