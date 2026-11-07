"""Chat server: TCP sockets, a reader and FIFO writer per client, newline JSON.

Run: python server.py
"""
import json
from collections import deque
import logging
import os
import queue
import socket
import sys
import threading
import time

from protocol import (FrameReader, enable_keepalive, encode_frame, validate_client_message,
                      validate_server_event, MAX_SESSIONS, MAX_GROUPS, OUTBOUND_QUEUE_FRAMES, LOGIN_TIMEOUT)
from ai_history import SessionHistory, HistoryError
from ai_service import AiFailure, AiService, AI_TIMEOUT, PUBLIC_ERRORS, load_server_config
from ai_runtime import AiRuntime

HOST, PORT = "0.0.0.0", 5000  # 0.0.0.0 = accept connections from other computers on the LAN

clients = {}  # name -> Session
groups = {}   # group name -> list of member names
lock = threading.Lock()  # guards state, recipient selection and outbound enqueue order


def encode(msg):
    return encode_frame(msg)


class Session:
    """Only the writer touches sendall; enqueue never waits for a slow peer."""
    def __init__(self, conn):
        self.conn = conn
        self.name = None
        self.history = SessionHistory()
        self.pending_ai = None
        self.recent_ai_ids = deque(maxlen=64)
        self.next_ai_at = 0
        self.closed = threading.Event()
        self._close_lock = threading.Lock()
        self.outbound = queue.Queue(maxsize=OUTBOUND_QUEUE_FRAMES)
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.writer.start()

    def enqueue(self, frame: bytes) -> bool:
        if self.closed.is_set():
            return False
        try:
            self.outbound.put_nowait(frame)
        except queue.Full:
            return False
        return True

    def _write(self):
        try:
            while not self.closed.is_set():
                try:
                    frame = self.outbound.get(timeout=.1)
                except queue.Empty:
                    continue
                if not self.closed.is_set():
                    self.conn.sendall(frame)
        except OSError:
            self.close()

    def _begin_close(self):
        with self._close_lock:
            if self.closed.is_set():
                return False
            self.closed.set()
            return True

    def close(self) -> None:
        disconnect_sessions({self})
        if threading.current_thread() is not self.writer:
            self.writer.join(1)


def send(session, msg, failed):
    if not session.enqueue(encode(msg)):
        failed.add(session)


def deliver_chat_locked(target, frame, room, sender, text, failed, *, record_history=True):
    if target.enqueue(frame):
        if record_history:
            target.history.record(room, sender, text)
    else:
        failed.add(target)


def submit_ai_locked(session, request, ai_runtime, failed):
    """Admission/snapshot only. The worker never performs HTTP under this lock."""
    identity = {key: request[key] for key in ('request_id', 'action', 'room_type', 'room')}
    rid = request['request_id']
    room = (request['room_type'], request['room'])

    def error(code):
        send(session, dict(type='ai_error', **identity, code=code, text=PUBLIC_ERRORS[code]), failed)

    if clients.get(session.name) is not session or session.closed.is_set():
        return
    if rid in session.recent_ai_ids:
        return  # A duplicate must not clear the original request's pending state.
    if session.pending_ai is not None:
        error('busy')
        return
    if ai_runtime is None or ai_runtime.stopped.is_set():
        error('unavailable')
        return
    generation = groups.get(room[1]) if room[0] == 'group' else None
    if room[0] == 'group' and session.name not in (generation or []):
        error('not_allowed')
        return
    try:
        snapshot = session.history.snapshot(room)
    except HistoryError as exc:
        error(exc.code)
        return
    now = time.monotonic()
    if now < session.next_ai_at:
        error('cooldown')
        return
    deadline = now + AI_TIMEOUT

    def job():
        try:
            if ai_runtime.stopped.is_set():
                raise AiFailure('unavailable')
            text = ai_runtime.service.generate(action=request['action'], requester=session.name,
                                               snapshot=snapshot, instruction=request['instruction'],
                                               deadline=deadline)
            if time.monotonic() >= deadline:
                raise AiFailure('timeout')
            event = dict(type='ai_result', **identity, text=text,
                         message_count=len(snapshot.messages), cutoff=snapshot.cutoff)
            try:
                validate_server_event(event)
            except ValueError:
                raise AiFailure('invalid_response') from None
        except Exception as exc:
            code = exc.code if isinstance(exc, AiFailure) else 'provider'
            event = dict(type='ai_error', **identity, code=code, text=PUBLIC_ERRORS[code])
        completion_failed = set()
        with lock:
            if session.pending_ai != rid:
                return
            session.pending_ai = None
            if session.closed.is_set() or clients.get(session.name) is not session:
                return
            if ai_runtime.stopped.is_set():
                event = dict(type='ai_error', **identity, code='unavailable', text=PUBLIC_ERRORS['unavailable'])
            if room[0] == 'group' and (groups.get(room[1]) is not generation or session.name not in generation):
                event = dict(type='ai_error', **identity, code='not_allowed', text=PUBLIC_ERRORS['not_allowed'])
            send(session, event, completion_failed)
        disconnect_sessions(completion_failed)

    def cancel():
        cancelled_failed = set()
        with lock:
            if session.pending_ai != rid:
                return
            session.pending_ai = None
            if not session.closed.is_set() and clients.get(session.name) is session:
                send(session, dict(type='ai_error', **identity, code='unavailable',
                                   text=PUBLIC_ERRORS['unavailable']), cancelled_failed)
        disconnect_sessions(cancelled_failed)

    # Publish before admission; the worker reacquires this lock before completion.
    session.pending_ai = rid
    if not ai_runtime.try_submit(job, on_cancel=cancel):
        session.pending_ai = None
        error('busy')
        return
    session.recent_ai_ids.append(rid)
    session.next_ai_at = now + 5


def state_fits(names, memberships):
    """Validate complete projected R4/R9 metadata before mutating any state."""
    try:
        encode({"type": "users", "users": sorted(names)})
        encode({"type": "groups", "groups": memberships})
    except ValueError:
        return False
    return True


def broadcast_state(failed):
    """Called under lock: freeze R4/R9 snapshots before any later mutation."""
    users = encode({"type": "users", "users": sorted(clients)})
    members = encode({"type": "groups", "groups": groups})
    for session in tuple(clients.values()):
        if not session.enqueue(users) or not session.enqueue(members):
            failed.add(session)


def disconnect_sessions(pending):
    """Remove each identity once; perform all socket operations outside lock."""
    pending = set(pending)
    while pending:
        session = pending.pop()
        if not session._begin_close():
            continue
        with lock:
            name = session.name
            if name is not None and clients.get(name) is session:
                del clients[name]
                for group in list(groups):
                    if name in groups[group]:
                        groups[group].remove(name)
                        if not groups[group]:
                            del groups[group]
                            for other in clients.values():
                                other.history.forget_room(('group', group))
                broadcast_state(pending)
            session.history.clear()
            session.pending_ai = None
        try:
            session.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        session.conn.close()
        if name is not None:
            logging.info("%s disconnected", name)


def handle(conn, addr, *, session=None, accepted_at=None, login_timeout=LOGIN_TIMEOUT, ai_runtime=None):
    session = Session(conn) if session is None else session
    deadline = (time.monotonic() if accepted_at is None else accepted_at) + login_timeout
    reader = FrameReader(conn)
    try:
        enable_keepalive(conn)
        while not session.closed.is_set():
            line = reader.readline(deadline if session.name is None else None)
            if not line:
                break
            failed = set()
            msg = json.loads(line)
            try:
                msg = validate_client_message(msg)
            except ValueError as exc:
                # A valid JSON frame with a bad schema does not poison the session.
                with lock:
                    send(session, {"type": "error", "text": f"Invalid command: {exc}"}, failed)
                disconnect_sessions(failed)
                continue
            kind = msg["type"]
            was_anonymous = session.name is None
            with lock:
                if session.closed.is_set():
                    break
                name = session.name
                if name is None and time.monotonic() >= deadline:
                    break
                if name is None:  # must log in first
                    new = msg["name"].strip() if kind == "login" else ""
                    if kind != "login" or not new:
                        send(session, {"type": "error", "text": "Please log in with a name"}, failed)
                    elif new.casefold() in map(str.casefold, clients):  # R3: unique names
                        send(session, {"type": "error", "text": f"Name '{new}' is already taken"}, failed)
                    elif not state_fits(list(clients) + [new], groups):
                        send(session, {"type": "error", "text": "State limit reached"}, failed)
                    else:
                        session.name = name = new
                        clients[name] = session
                        send(session, {"type": "login_ok", "name": name}, failed)
                        broadcast_state(failed)

                elif kind == "pm":  # R7: only sender and receiver get it
                    to = msg.get("to")
                    if to not in clients:
                        send(session, {"type": "error", "text": f"{to} is offline"}, failed)
                    else:
                        out = {"type": "pm", "from": name, "to": to, "text": msg["text"]}
                        try:
                            output = encode(out)
                        except ValueError:
                            send(session, {"type": "error", "text": "Frame limit reached"}, failed)
                        else:
                            for target in {clients[to], session}:
                                other = to if target is session else name
                                deliver_chat_locked(target, output, ('pm', other), name, msg['text'], failed,
                                                    record_history=ai_runtime is not None and not ai_runtime.stopped.is_set())

                elif kind == "create_group":  # R8: creator is the only initial member
                    g = msg["group"].strip()
                    if not g or g in groups:
                        send(session, {"type": "error", "text": f"Group '{g}' is empty or already exists"}, failed)
                    elif len(groups) >= MAX_GROUPS or not state_fits(clients, {**groups, g: [name]}):
                        send(session, {"type": "error", "text": "State limit reached"}, failed)
                    else:
                        groups[g] = [name]
                        broadcast_state(failed)

                elif kind == "join_group":  # R10: a client only ever adds itself
                    g = msg.get("group")
                    if g not in groups:
                        send(session, {"type": "error", "text": f"Group '{g}' does not exist"}, failed)
                    elif name not in groups[g]:
                        projected = {**groups, g: groups[g] + [name]}
                        if not state_fits(clients, projected):
                            send(session, {"type": "error", "text": "State limit reached"}, failed)
                        else:
                            groups[g].append(name)
                            broadcast_state(failed)

                elif kind == "group_msg":  # R11: only members get it, only members may send
                    g = msg.get("group")
                    if name not in groups.get(g, []):
                        send(session, {"type": "error", "text": f"You are not a member of '{g}'"}, failed)
                    else:
                        out = {"type": "group_msg", "group": g, "from": name, "text": msg["text"]}
                        try:
                            output = encode(out)
                        except ValueError:
                            send(session, {"type": "error", "text": "Frame limit reached"}, failed)
                        else:
                            for member in groups[g]:
                                deliver_chat_locked(clients[member], output, ('group', g), name, msg['text'], failed,
                                                    record_history=ai_runtime is not None and not ai_runtime.stopped.is_set())
                elif kind == 'ai_request':
                    submit_ai_locked(session, msg, ai_runtime, failed)
                else:
                    send(session, {"type": "error", "text": "Already logged in"}, failed)
            disconnect_sessions(failed)
            if was_anonymous and session.name:  # outside the lock: a paused console must not freeze the server
                logging.info("%s connected from %s:%s", session.name, addr[0], addr[1])
    except (OSError, ValueError, RecursionError):
        pass  # connection dropped or bad data: treat as disconnect
    finally:
        session.close()


def serve(listener, stop=None, *, max_sessions=MAX_SESSIONS,
          login_timeout=LOGIN_TIMEOUT, on_session=None, ai_runtime=None):
    """Actual bounded accept loop, also usable with an owned test listener.

    The slot is reserved before starting either session thread and released only
    after reader and writer cleanup. Stop closes all accepted sockets and joins
    every owned worker; listener ownership transfers to this function.
    """
    stop = threading.Event() if stop is None else stop
    active = {}  # reader thread -> accepted socket
    active_lock = threading.Lock()
    listener.settimeout(.1)

    def run(conn, addr, accepted_at):
        session = None
        try:
            session = Session(conn)
            if on_session is not None:
                on_session(session)
            handle(conn, addr, session=session, accepted_at=accepted_at,
                   login_timeout=login_timeout, ai_runtime=ai_runtime)
        finally:
            if session is not None:
                session.close()
            else:
                conn.close()
            with active_lock:
                active.pop(threading.current_thread(), None)

    try:
        while not stop.is_set():
            try:
                conn, addr = listener.accept()
            except TimeoutError:
                continue
            except OSError:  # a client vanished mid-connect: others must keep working
                time.sleep(.1)
                continue
            accepted_at = time.monotonic()
            conn.settimeout(None)
            with active_lock:
                admitted = len(active) < max_sessions
                if admitted:
                    worker = threading.Thread(target=run, args=(conn, addr, accepted_at), daemon=True)
                    active[worker] = conn
                    worker.start()
            if not admitted:
                conn.close()
    finally:
        listener.close()
        with active_lock:
            owned = list(active.items())
        for worker, conn in owned:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        for worker, _conn in owned:
            worker.join(2)
        if any(worker.is_alive() for worker, _conn in owned):
            raise RuntimeError("Server worker failed to stop")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # IPv4 + TCP
    if os.name != "nt":  # on Windows this option lets two servers share one port with no error
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, port))
    srv.listen()
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = "unknown, check ipconfig or ifconfig"
    print(f"Server listening on port {port}. LAN IP (guess): {ip}")
    runtime = None
    try:
        config = load_server_config()
        if config is not None:
            runtime = AiRuntime(AiService(config))
    except AiFailure:
        logging.warning('AI configuration could not be loaded; normal chat remains available.')
    print('AI enabled' if runtime is not None else 'AI disabled: configure OPENROUTER_API_KEY in server .env')
    try:
        serve(srv, ai_runtime=runtime)
    except KeyboardInterrupt:
        pass
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    main()
