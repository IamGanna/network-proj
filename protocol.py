"""Shared JSON schemas and bounded newline framing policy.

Validators reject malformed fields without coercion. Extra fields are tolerated
for compatibility; authority always comes from the server's connection state.
"""
import json
import time

# Project policy, not assignment requirements or measured performance limits.
MAX_FRAME_BYTES = 65_536  # includes the terminating newline
MAX_TEXT_CHARS = 4_096
MAX_NAME_CHARS = 64
MAX_SESSIONS = 32  # active plus pending logins
MAX_GROUPS = 64
OUTBOUND_QUEUE_FRAMES = 64
LOGIN_TIMEOUT = 10.0  # absolute server deadline from accept


def encode_frame(message, *, max_bytes=None):
    """Check actual serialized UTF-8 bytes, including JSON escaping/newline."""
    data = (json.dumps(message) + '\n').encode('utf-8')
    if len(data) > (MAX_FRAME_BYTES if max_bytes is None else max_bytes):
        raise ValueError('Frame byte limit exceeded')
    return data


class FrameReader:
    """Byte-bounded input; decoding occurs only after a complete frame exists.

    Buffered coalesced frames survive login/start. No timed-out text wrapper is
    reused, and neither an incomplete UTF-8 character nor missing newline can
    grow the buffer beyond the frame budget.
    """
    def __init__(self, sock, *, max_bytes=None):
        self.sock = sock
        self.max_bytes = MAX_FRAME_BYTES if max_bytes is None else max_bytes
        self.buffer = bytearray()

    def readline(self, deadline=None):
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError('Login deadline expired')
            end = self.buffer.find(b'\n')
            if end >= 0:
                line = bytes(self.buffer[:end + 1])
                del self.buffer[:end + 1]
                return line.decode('utf-8')
            available = self.max_bytes - len(self.buffer)
            if available <= 0:
                raise ValueError('Frame byte limit exceeded')
            self.sock.settimeout(remaining)
            data = self.sock.recv(min(4096, available))
            if not data:
                if self.buffer:
                    raise ValueError('Incomplete frame')
                return ''
            self.buffer.extend(data)

    def close(self):
        self.buffer.clear()

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line

CLIENT_FIELDS = {
    'login': ('name',),
    'pm': ('to', 'text'),
    'create_group': ('group',),
    'join_group': ('group',),
    'group_msg': ('group', 'text'),
    'timeout_member': ('group', 'target', 'duration'),
}
SERVER_FIELDS = {
    'login_ok': ('name',),
    'users': (),
    'groups': (),
    'pm': ('from', 'to', 'text'),
    'group_msg': ('group', 'from', 'text'),
    'error': ('text',),
    'disconnected': (),
    'timeout_notice': ('group', 'target', 'seconds'),
}


def _name(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_NAME_CHARS


def _validate_fields(msg, schemas):
    if not isinstance(msg, dict):
        raise ValueError('Expected a JSON object')
    kind = msg.get('type')
    if not isinstance(kind, str) or kind not in schemas:
        raise ValueError('Unknown or missing message type')
    for field in schemas[kind]:
        value = msg.get(field)
        if field in ('duration', 'seconds'):
            if isinstance(value, int) and value >= 0:
                continue
            if isinstance(value, str) and value.isdigit():
                continue
            raise ValueError(f"Field '{field}' must be a non-negative integer or digit string")
        if isinstance(value, str) and field != 'text' and len(value) > MAX_NAME_CHARS:
            raise ValueError(f"Field '{field}' exceeds the {MAX_NAME_CHARS}-character name limit")
        if not isinstance(value, str) or (field != 'text' and not _name(value)):
            raise ValueError(f"Field '{field}' must be " + ('a string' if field == 'text' else 'a nonempty string'))
        if field == 'text' and len(value) > MAX_TEXT_CHARS:
            raise ValueError('Text character limit exceeded')
    return msg


def validate_client_message(msg: object) -> dict:
    """Validate a command before routing; raise ValueError for bad schemas."""
    return _validate_fields(msg, CLIENT_FIELDS)


def validate_server_event(msg: object) -> dict:
    """Validate an event, including nested public membership metadata."""
    msg = _validate_fields(msg, SERVER_FIELDS)
    if msg['type'] == 'users':
        users = msg.get('users')
        if not isinstance(users, list) or not all(_name(name) for name in users):
            raise ValueError("Field 'users' must be a list of names")
    elif msg['type'] == 'groups':
        groups = msg.get('groups')
        if not isinstance(groups, dict) or not all(
            _name(group) and isinstance(members, list) and all(_name(name) for name in members)
            for group, members in groups.items()
        ):
            raise ValueError("Field 'groups' must map group names to lists of member names")
    return msg
