"""Chat server: TCP sockets, a reader and FIFO writer per client, newline JSON.

Run: python server.py
"""
import base64
import json
from collections import deque
import logging
import os
import queue
import socket
import sys
import threading
import time

import activity
import stt_service
from protocol import (FrameReader, enable_keepalive, encode_frame, validate_client_message,
                      validate_server_event, MAX_SESSIONS, MAX_GROUPS, OUTBOUND_QUEUE_FRAMES, LOGIN_TIMEOUT,
                      MAX_SPEECH_BYTES, MAX_TEXT_CHARS)
from ai_history import SessionHistory, HistoryError
from ai_service import AiFailure, AiService, AI_TIMEOUT, PUBLIC_ERRORS, load_server_config, read_env_value
from ai_runtime import AiRuntime

HOST, PORT = "0.0.0.0", 5000  # 0.0.0.0 = accept connections from other computers on the LAN

clients = {}  # name -> Session
groups = {}   # group name -> list of member names
active_transfers = {}  # file_id -> transfer metadata
group_creators = {}  # group name -> creator name
group_timeouts = {}  # group name -> {target_member_name: expiry_timestamp}
stt_key = ""  # TYPHOON_API_KEY from the server .env, set in main(); empty turns speech-to-text off
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
        self.stt = None  # the recording being received: {"id", "total", "next", "audio"}
        self.stt_busy = False  # one transcription at a time per client
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


def receive_stt_locked(session, msg, failed):
    """Collect one recording's chunks in order; after the last one, transcribe off the lock."""
    sid, index, total = msg["stt_id"], int(msg["chunk"]), int(msg["chunks"])

    def refuse(text):
        session.stt = None
        send(session, {"type": "stt_error", "stt_id": sid, "text": text}, failed)

    if index == 0:
        if not stt_key:
            return refuse("Speech-to-text is not set up on the server")
        if session.stt_busy:
            return refuse("Still transcribing your last recording")
        if total < 1:
            return refuse("Bad audio data")
        session.stt = {"id": sid, "total": total, "next": 0, "audio": bytearray()}
    pending = session.stt
    if pending is None or pending["id"] != sid:
        return  # the rest of a refused recording is dropped quietly
    try:
        data = base64.b64decode(msg["data"], validate=True)
    except ValueError:
        return refuse("Bad audio data")
    if index != pending["next"] or total != pending["total"]:
        return refuse("Bad audio data")
    if len(pending["audio"]) + len(data) > MAX_SPEECH_BYTES:
        return refuse("Recording is longer than 60 seconds")
    pending["audio"] += data
    pending["next"] += 1
    if pending["next"] < total:
        return
    audio = bytes(pending["audio"])
    session.stt = None
    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return refuse("Audio must be a WAV recording")
    session.stt_busy = True
    threading.Thread(target=transcribe_job, args=(session, sid, audio, stt_key), daemon=True).start()


def transcribe_job(session, sid, audio, key):
    """Worker thread: the HTTP call to Typhoon never runs under the lock."""
    try:
        event = {"type": "stt_text", "stt_id": sid, "text": stt_service.transcribe(audio, key)[:MAX_TEXT_CHARS]}
    except Exception as exc:
        text = str(exc) if isinstance(exc, stt_service.SttError) else "Speech-to-text failed"
        event = {"type": "stt_error", "stt_id": sid, "text": text}
    failed = set()
    with lock:
        session.stt_busy = False
        send(session, event, failed)
    disconnect_sessions(failed)


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
                for fid in list(active_transfers):
                    if active_transfers[fid]["from"] == name:
                        del active_transfers[fid]
                for group in list(groups):
                    if name in groups[group]:
                        groups[group].remove(name)
                        if not groups[group]:
                            del groups[group]
                            for other in clients.values():
                                other.history.forget_room(('group', group))
                            group_creators.pop(group, None)
                            group_timeouts.pop(group, None)
                            activity.forget(("group", group))
                        elif group_creators.get(group) == name:
                            group_creators[group] = groups[group][0]
                if not clients:
                    active_transfers.clear()
                if not groups:
                    group_creators.clear()
                    group_timeouts.clear()
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
                    elif new.casefold() == "[system]":  # the server signs its notices with this name
                        send(session, {"type": "error", "text": "The name '[System]' is reserved"}, failed)
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
                        msg["text"] = activity.apply(name, ("pm", frozenset((name, to))), msg["text"])
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
                        group_creators[g] = name
                        group_timeouts[g] = {}
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
                            if g in group_timeouts and name in group_timeouts[g]:
                                exp = group_timeouts[g][name]
                                if time.time() < exp:
                                    send(session, {
                                        "type": "timeout_notice",
                                        "group": g,
                                        "target": name,
                                        "seconds": int(exp - time.time()) + 1,
                                    }, failed)

                elif kind == "group_msg":  # R11: only members get it, only members may send
                    g = msg.get("group")
                    if name not in groups.get(g, []):
                        send(session, {"type": "error", "text": f"You are not a member of '{g}'"}, failed)
                    else:
                        timeouts = group_timeouts.get(g, {})
                        now = time.time()
                        if name in timeouts and now < timeouts[name]:
                            remaining = int(timeouts[name] - now) + 1
                            if remaining >= 3600:
                                rem_str = f"{remaining // 3600}h {(remaining % 3600) // 60}m"
                            elif remaining >= 60:
                                rem_str = f"{remaining // 60}m {remaining % 60}s"
                            else:
                                rem_str = f"{remaining}s"
                            send(session, {"type": "error", "text": f"You are timed out in '{g}' ({rem_str} remaining)"}, failed)
                        else:
                            if name in timeouts and now >= timeouts[name]:
                                del timeouts[name]
                            msg["text"] = activity.apply(name, ("group", g), msg["text"])
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

                elif kind == "stt_chunk":  # speech-to-text: the server calls Typhoon with its own key
                    receive_stt_locked(session, msg, failed)

                elif kind == "file_start":
                    target_type = msg.get("target_type")
                    target = msg.get("target")
                    file_id = msg.get("file_id")
                    filename = msg.get("filename")
                    filesize_str = msg.get("filesize")
                    chunks_str = msg.get("chunks")

                    try:
                        filesize = int(filesize_str)
                        chunks = int(chunks_str)
                        if filesize <= 0 or chunks <= 0 or chunks > filesize or filesize > 10 * 1024 * 1024:
                            raise ValueError()
                    except (ValueError, TypeError):
                        send(session, {"type": "error", "text": "Invalid file size or chunk count (max 10MB)"}, failed)
                    else:
                        if target_type not in ("pm", "group"):
                            send(session, {"type": "error", "text": "Invalid target type"}, failed)
                        elif target_type == "pm" and target not in clients:
                            send(session, {"type": "error", "text": f"{target} is offline"}, failed)
                        elif target_type == "group" and (target not in groups or name not in groups[target]):
                            send(session, {"type": "error", "text": f"You are not a member of '{target}'"}, failed)
                        elif target_type == "group" and name in group_timeouts.get(target, {}) and time.time() < group_timeouts[target][name]:
                            send(session, {"type": "error", "text": f"You are timed out in '{target}'"}, failed)
                        elif file_id in active_transfers:  # reusing a live id would take over someone's upload
                            send(session, {"type": "error", "text": "That file id is already in use"}, failed)
                        elif sum(t["from"] == name for t in active_transfers.values()) >= 3:
                            send(session, {"type": "error", "text": "You can send at most 3 files at once"}, failed)
                        else:
                            out = {
                                "type": "file_start",
                                "file_id": file_id,
                                "from": name,
                                "target_type": target_type,
                                "target": target,
                                "filename": filename,
                                "filesize": filesize_str,
                                "chunks": chunks_str,
                            }
                            try:
                                output = encode(out)
                            except ValueError:
                                send(session, {"type": "error", "text": "Frame limit reached"}, failed)
                            else:
                                active_transfers[file_id] = {
                                    "from": name,
                                    "target_type": target_type,
                                    "target": target,
                                    "total_chunks": chunks,
                                    "received_chunks": 0,
                                    "filesize": filesize,
                                    "bytes": 0,
                                }
                                if target_type == "pm":
                                    for rec in {clients[target], session}:
                                        if not rec.enqueue(output):
                                            failed.add(rec)
                                else:
                                    for member in groups[target]:
                                        if not clients[member].enqueue(output):
                                            failed.add(clients[member])

                elif kind == "file_chunk":
                    file_id = msg.get("file_id")
                    chunk = msg.get("chunk")
                    data_chunk = msg.get("data")
                    if file_id in active_transfers:  # chunks of a refused or cancelled upload are dropped quietly
                        trans = active_transfers[file_id]
                        size = len(data_chunk) * 3 // 4  # decoded bytes, at most 2 over because of base64 padding
                        if trans["from"] != name:
                            send(session, {"type": "error", "text": "Unauthorized file chunk"}, failed)
                        elif trans["bytes"] + size > trans["filesize"] + 2:
                            del active_transfers[file_id]
                            send(session, {"type": "error", "text": "File is bigger than its stated size"}, failed)
                        else:
                            out = {
                                "type": "file_chunk",
                                "file_id": file_id,
                                "chunk": chunk,
                                "data": data_chunk,
                            }
                            try:
                                output = encode(out)
                            except ValueError:
                                send(session, {"type": "error", "text": "Frame limit reached"}, failed)
                            else:
                                trans["received_chunks"] += 1
                                trans["bytes"] += size
                                if trans["target_type"] == "pm":
                                    target = trans["target"]
                                    if target in clients:
                                        if not clients[target].enqueue(output):
                                            failed.add(clients[target])
                                else:
                                    g = trans["target"]
                                    for member in groups.get(g, []):
                                        if member != name:
                                            if not clients[member].enqueue(output):
                                                failed.add(clients[member])
                                if trans["received_chunks"] >= trans["total_chunks"]:
                                    del active_transfers[file_id]

                elif kind == "timeout_member":
                    g = msg.get("group")
                    target = msg.get("target")
                    duration_val = msg.get("duration", "60")
                    if g not in groups:
                        send(session, {"type": "error", "text": f"Group '{g}' does not exist"}, failed)
                    elif name not in groups.get(g, []):
                        send(session, {"type": "error", "text": f"You are not a member of '{g}'"}, failed)
                    elif group_creators.get(g) != name:
                        send(session, {"type": "error", "text": f"Only the group admin can timeout members"}, failed)
                    elif target not in groups.get(g, []):
                        send(session, {"type": "error", "text": f"'{target}' is not a member of '{g}'"}, failed)
                    elif target == name:
                        send(session, {"type": "error", "text": "You cannot timeout yourself"}, failed)
                    else:
                        try:
                            duration = int(duration_val)
                            if not 0 <= duration <= 31_536_000:  # at most a year
                                raise ValueError()
                        except ValueError:
                            send(session, {"type": "error", "text": "Invalid timeout duration"}, failed)
                        else:
                            timeouts = group_timeouts.setdefault(g, {})
                            now = time.time()
                            if duration == 0:
                                timeouts.pop(target, None)
                                notice_text = f"[System] {target}'s timeout was removed by {name}."
                            else:
                                timeouts[target] = now + duration
                                if duration >= 604800:
                                    dur_desc = f"{duration // 604800} week(s)"
                                elif duration >= 86400:
                                    dur_desc = f"{duration // 86400} day(s)"
                                elif duration >= 3600:
                                    dur_desc = f"{duration // 3600} hour(s)"
                                elif duration >= 60:
                                    dur_desc = f"{duration // 60} minute(s)"
                                else:
                                    dur_desc = f"{duration} second(s)"
                                notice_text = f"[System] {target} was timed out by {name} for {dur_desc}."

                            notice_event = {
                                "type": "timeout_notice",
                                "group": g,
                                "target": target,
                                "seconds": duration,
                            }
                            sys_msg = {
                                "type": "group_msg",
                                "group": g,
                                "from": "[System]",
                                "text": notice_text,
                            }
                            try:
                                ev_encoded = encode(notice_event)
                                msg_encoded = encode(sys_msg)
                            except ValueError:
                                send(session, {"type": "error", "text": "Frame limit reached"}, failed)
                            else:
                                for member in groups[g]:
                                    target_sess = clients.get(member)
                                    if target_sess:
                                        if not target_sess.enqueue(ev_encoded) or not target_sess.enqueue(msg_encoded):
                                            failed.add(target_sess)
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
    global stt_key
    try:
        stt_key = read_env_value('TYPHOON_API_KEY')
    except AiFailure:
        logging.warning('.env could not be read; speech-to-text is off.')
    print('Speech-to-text enabled' if stt_key else 'Speech-to-text disabled: configure TYPHOON_API_KEY in server .env')
    try:
        serve(srv, ai_runtime=runtime)
    except KeyboardInterrupt:
        pass
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    main()
