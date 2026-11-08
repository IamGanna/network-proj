"""Shared JSON schemas and bounded newline framing policy.

Validators reject malformed fields without coercion. Extra fields are tolerated
for compatibility; authority always comes from the server's connection state.
"""
import json
import re
import socket
import time

# Project policy, not assignment requirements or measured performance limits.
MAX_FRAME_BYTES = 65_536  # includes the terminating newline
MAX_TEXT_CHARS = 4_096
MAX_NAME_CHARS = 64
MAX_SESSIONS = 32  # active plus pending logins
MAX_GROUPS = 64
OUTBOUND_QUEUE_FRAMES = 1024  # shortcut: about 1000 unpaced messages at once still drop a client, pace bigger bursts
LOGIN_TIMEOUT = 10.0  # absolute server deadline from accept
AI_MAX_MESSAGES = 512
AI_MAX_HISTORY_BYTES = 262_144
AI_MAX_INSTRUCTION = 500
AI_TEXT_LIMITS = {'summary': 3000, 'draft': 600}
AI_ERROR_CODES = frozenset(('unavailable', 'no_history', 'not_allowed', 'history_limit',
                            'busy', 'cooldown', 'timeout', 'budget', 'rate_limit',
                            'provider', 'invalid_response'))


def enable_keepalive(sock):
    """Notice a dead link (Wi-Fi drop, laptop sleep) in about 20 s instead of never."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for option, value in (('TCP_KEEPIDLE', 10), ('TCP_KEEPINTVL', 3), ('TCP_KEEPCNT', 3)):
        if hasattr(socket, option):  # macOS names these differently
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)


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
    'file_start': ('file_id', 'target_type', 'target', 'filename', 'filesize', 'chunks'),
    'file_chunk': ('file_id', 'chunk', 'data'),
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
    'file_start': ('file_id', 'from', 'target_type', 'target', 'filename', 'filesize', 'chunks'),
    'file_chunk': ('file_id', 'chunk', 'data'),
    'timeout_notice': ('group', 'target', 'seconds'),
}


def _name(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_NAME_CHARS and value.isprintable()


def _validate_fields(msg, schemas):
    if not isinstance(msg, dict):
        raise ValueError('Expected a JSON object')
    kind = msg.get('type')
    if not isinstance(kind, str) or kind not in schemas:
        raise ValueError('Unknown or missing message type')
    for field in schemas[kind]:
        value = msg.get(field)
        if field == 'data':
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Field 'data' must be a nonempty string")
            continue
        if field in ('filesize', 'chunks', 'chunk', 'duration', 'seconds'):
            if isinstance(value, int) and value >= 0:
                continue
            if isinstance(value, str) and value.isdigit():
                continue
            raise ValueError(f"Field '{field}' must be a non-negative integer or digit string")
        if isinstance(value, str) and field != 'text' and len(value) > MAX_NAME_CHARS:
            raise ValueError(f"Field '{field}' exceeds the {MAX_NAME_CHARS}-character name limit")
        if not isinstance(value, str) or (field != 'text' and not _name(value)):
            raise ValueError(f"Field '{field}' must be " + ('a string' if field == 'text' else 'a nonempty string without invisible characters'))
        if field == 'text' and len(value) > MAX_TEXT_CHARS:
            raise ValueError('Text character limit exceeded')
    return msg


def validate_client_message(msg: object) -> dict:
    """Validate a command before routing; raise ValueError for bad schemas."""
    if isinstance(msg, dict) and msg.get('type') == 'ai_request':
        return _validate_ai(msg)
    return _validate_fields(msg, CLIENT_FIELDS)


def validate_server_event(msg: object) -> dict:
    """Validate an event, including nested public membership metadata."""
    if isinstance(msg, dict) and msg.get('type') in ('ai_result', 'ai_error'):
        return _validate_ai(msg)
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


def _validate_ai(msg):
    common = {'type', 'request_id', 'action', 'room_type', 'room'}
    kind = msg['type']
    extra = {'ai_request': {'instruction'}, 'ai_result': {'text', 'message_count', 'cutoff'},
             'ai_error': {'code', 'text'}}[kind]
    if set(msg) != common | extra:
        raise ValueError('Unexpected or missing AI fields')
    if not isinstance(msg['request_id'], str) or not re.fullmatch('[0-9a-f]{32}', msg['request_id']):
        raise ValueError('Invalid AI request ID')
    if msg['action'] not in ('summary', 'draft') or msg['room_type'] not in ('pm', 'group') or not _name(msg['room']):
        raise ValueError('Invalid AI action or room')
    if kind == 'ai_request':
        instruction = msg['instruction']
        if not isinstance(instruction, str) or len(instruction) > AI_MAX_INSTRUCTION or (msg['action'] == 'summary' and instruction):
            raise ValueError('Invalid AI instruction')
    else:
        limit = 256 if kind == 'ai_error' else AI_TEXT_LIMITS[msg['action']]
        value = msg['text']
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError('Invalid AI output text')
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ValueError('Invalid AI output encoding') from None
        if kind == 'ai_error':
            if not isinstance(msg['code'], str) or msg['code'] not in AI_ERROR_CODES:
                raise ValueError('Invalid AI error code')
        elif any(type(msg[k]) is not int or msg[k] < 0 for k in ('message_count', 'cutoff')) or msg['message_count'] > AI_MAX_MESSAGES:
            raise ValueError('Invalid AI history metadata')
    return msg
