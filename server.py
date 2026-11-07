"""Chat server: TCP sockets, a reader and FIFO writer per client, newline JSON.

Run: python server.py
"""
import json
import queue
import socket
import threading
import time

from protocol import (FrameReader, encode_frame, validate_client_message,
                      MAX_SESSIONS, MAX_GROUPS, OUTBOUND_QUEUE_FRAMES, LOGIN_TIMEOUT)

HOST, PORT = "0.0.0.0", 5000  # 0.0.0.0 = accept connections from other computers on the LAN

clients = {}  # name -> Session
groups = {}   # group name -> list of member names
active_transfers = {}  # file_id -> transfer metadata
group_creators = {}  # group name -> creator name
group_timeouts = {}  # group name -> {target_member_name: expiry_timestamp}
lock = threading.Lock()  # guards state, recipient selection and outbound enqueue order


def encode(msg):
    return encode_frame(msg)


class Session:
    """Only the writer touches sendall; enqueue never waits for a slow peer."""
    def __init__(self, conn):
        self.conn = conn
        self.name = None
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
                            group_creators.pop(group, None)
                            group_timeouts.pop(group, None)
                        elif group_creators.get(group) == name:
                            group_creators[group] = groups[group][0]
                if not clients:
                    active_transfers.clear()
                if not groups:
                    group_creators.clear()
                    group_timeouts.clear()
                broadcast_state(pending)
        try:
            session.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        session.conn.close()


def handle(conn, addr, *, session=None, accepted_at=None, login_timeout=LOGIN_TIMEOUT):
    session = Session(conn) if session is None else session
    deadline = (time.monotonic() if accepted_at is None else accepted_at) + login_timeout
    reader = FrameReader(conn)
    try:
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
                    elif new in clients:  # R3: unique names
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
                                if not target.enqueue(output):
                                    failed.add(target)

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
                            out = {"type": "group_msg", "group": g, "from": name, "text": msg["text"]}
                            try:
                                output = encode(out)
                            except ValueError:
                                send(session, {"type": "error", "text": "Frame limit reached"}, failed)
                            else:
                                for member in groups[g]:
                                    if not clients[member].enqueue(output):
                                        failed.add(clients[member])

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
    except (OSError, ValueError, RecursionError):
        pass  # connection dropped or bad data: treat as disconnect
    finally:
        session.close()


def serve(listener, stop=None, *, max_sessions=MAX_SESSIONS,
          login_timeout=LOGIN_TIMEOUT, on_session=None):
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
                   login_timeout=login_timeout)
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
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # IPv4 + TCP
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen()
    print(f"Server listening on port {PORT}. LAN IP (guess): {socket.gethostbyname(socket.gethostname())}")
    try:
        serve(srv)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
