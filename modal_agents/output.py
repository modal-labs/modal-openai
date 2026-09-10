"""Collect complete text even when the stream delivers a final snapshot without deltas."""

from agent_api_sdk import (
    SessionEvent,
    SessionTurnOutputTextDeltaEvent,
    SessionTurnOutputTextDoneEvent,
)


class TextOutput:
    def __init__(self) -> None:
        self._parts: dict[tuple[int, int], str] = {}
        self._done: set[tuple[int, int]] = set()

    def add(self, event: SessionEvent) -> None:
        if isinstance(event, SessionTurnOutputTextDeltaEvent):
            key = (event.output_index, event.content_index)
            if key not in self._done:
                self._parts[key] = self._parts.get(key, "") + event.delta
        elif isinstance(event, SessionTurnOutputTextDoneEvent):
            # A final snapshot replaces earlier partial text, never appends to it.
            key = (event.output_index, event.content_index)
            self._parts[key] = event.text
            self._done.add(key)

    @property
    def text(self) -> str:
        return "".join(self._parts[key] for key in sorted(self._parts)).strip()
