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
                if not clients:
                    active_transfers.clear()
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
                        if filesize <= 0 or chunks <= 0 or filesize > 10 * 1024 * 1024:
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
                    if file_id not in active_transfers:
                        send(session, {"type": "error", "text": "Unknown or expired file transfer"}, failed)
                    else:
                        trans = active_transfers[file_id]
                        if trans["from"] != name:
                            send(session, {"type": "error", "text": "Unauthorized file chunk"}, failed)
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
