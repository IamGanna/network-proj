"""Ephemeral, complete observed histories. Caller holds the server state lock."""
from dataclasses import dataclass
import json
from protocol import AI_MAX_MESSAGES, AI_MAX_HISTORY_BYTES

RoomKey = tuple[str, str]


@dataclass(frozen=True)
class HistoryMessage:
    seq: int
    sender: str
    text: str


@dataclass(frozen=True)
class HistorySnapshot:
    room: RoomKey
    messages: tuple[HistoryMessage, ...]
    cutoff: int


class HistoryError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class SessionHistory:
    def __init__(self, *, max_messages=AI_MAX_MESSAGES, max_bytes=AI_MAX_HISTORY_BYTES):
        self.max_messages, self.max_bytes = max_messages, max_bytes
        self.clear()

    def clear(self):
        self.rooms = {}
        self.byte_count = self.message_count = self.seq = 0
        self.overflowed = False

    def record(self, room, sender, text):
        if self.overflowed:
            return
        self.seq += 1
        size = len(json.dumps(dict(room=room, seq=self.seq, sender=sender, text=text),
                              ensure_ascii=True, separators=(',', ':')).encode('utf-8'))
        if self.message_count + 1 > self.max_messages or self.byte_count + size > self.max_bytes:
            self.rooms.clear()
            self.byte_count = self.message_count = 0
            self.overflowed = True
            return
        self.rooms.setdefault(room, []).append((HistoryMessage(self.seq, sender, text), size))
        self.byte_count += size
        self.message_count += 1

    def snapshot(self, room):
        if self.overflowed:
            raise HistoryError('history_limit')
        rows = self.rooms.get(room)
        if not rows:
            raise HistoryError('no_history')
        messages = tuple(row[0] for row in rows)
        return HistorySnapshot(room, messages, messages[-1].seq)

    def forget_room(self, room):
        rows = self.rooms.pop(room, ())
        self.message_count -= len(rows)
        self.byte_count -= sum(row[1] for row in rows)
