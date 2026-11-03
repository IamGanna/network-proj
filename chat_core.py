"""Chat core: the client side of the server.py protocol, with no UI.

Any front end (Tkinter, CLI, web bridge) can use it:
    core = ChatCore("127.0.0.1", 5000)
    ok, name_or_error = core.login("maya")
    core.start(on_message)   # on_message(dict) runs on a background thread
    core.group_msg("study-room", "hi")
"""
import json
import socket
import threading
import time

from protocol import FrameReader, encode_frame, validate_client_message, validate_server_event


class ChatCore:
    def __init__(self, host, port, *, connect_timeout=5, login_timeout=5):
        self.host, self.port = host, port
        self.connect_timeout, self.login_timeout = connect_timeout, login_timeout
        self.sock = self.reader = None
        self.name = None
        self.receive_thread = None
        self._callback = None
        self._closed = False
        self._disconnected = False
        self._state_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._login_lock = threading.Lock()

    def _drop_transport(self, sock):
        with self._state_lock:
            if self.sock is not sock:
                return
            reader, self.reader = self.reader, None
            self.sock = None
            self.name = None
        # Shutdown wakes a blocked readline before closing its buffered wrapper.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        if reader is not None:
            reader.close()

    def _emit(self, message):
        with self._state_lock:
            callback = None if self._closed else self._callback
        if callback is not None:
            callback(message)

    def _notify_disconnected(self):
        with self._state_lock:
            if self._closed or self._disconnected:
                return
            self._disconnected = True
            callback = self._callback
        if callback is not None:
            callback({"type": "disconnected"})

    def send(self, msg):
        """True means local transport acceptance, not recipient display."""
        try:
            if any(not isinstance(value, str) or not value.strip() for value in msg.values()):
                raise ValueError("Names and message text must be nonempty strings.")
            validate_client_message(msg)
            data = encode_frame(msg)
        except (ValueError, TypeError) as exc:
            self._emit({"type": "error", "text": str(exc)})
            return False
        with self._send_lock:
            with self._state_lock:
                sock = self.sock if self.name and not self._closed else None
            if sock is None:
                self._emit({"type": "error", "text": "Not connected to the server."})
                return False
            try:
                sock.sendall(data)
                return True
            except OSError:
                self._emit({"type": "error", "text": "Could not send: connection lost."})
                self._notify_disconnected()
                self._drop_transport(sock)
                return False

    def login(self, name):
        """Blocking. Returns (True, name) or (False, error text). Call before start()."""
        if not isinstance(name, str) or not name.strip():
            return False, "Enter a name first."
        try:
            data = encode_frame(validate_client_message({'type': 'login', 'name': name}))
        except (ValueError, TypeError) as exc:
            return False, str(exc)
        if not self._login_lock.acquire(blocking=False):
            return False, "A login attempt is already in progress."
        sock = None
        try:
            with self._state_lock:
                if self._closed:
                    return False, "The client is closed."
                if self.name:
                    return False, "Already logged in."
                sock = self.sock
                if sock is None:
                    # Publish before connect so close() can cancel this attempt.
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    self.sock = sock
            if self.reader is None:
                sock.settimeout(self.connect_timeout)
                sock.connect((self.host, self.port))
                with self._state_lock:
                    if self._closed or self.sock is not sock:
                        return False, "Login cancelled."
                    self.reader = FrameReader(sock)
            deadline = time.monotonic() + self.login_timeout
            sock.settimeout(self.login_timeout)
            with self._send_lock:
                sock.sendall(data)
            line = self.reader.readline(deadline)
            if not line:
                raise ConnectionError("The server closed the connection.")
            reply = validate_server_event(json.loads(line))
            if reply["type"] != "login_ok":  # R3: name taken; same connection can retry.
                return False, reply["text"]
            if not isinstance(reply["name"], str) or not reply["name"]:
                raise ValueError("Invalid login response")
            with self._state_lock:
                if self._closed or self.sock is not sock:
                    return False, "Login cancelled."
                self.name = reply["name"]
            sock.settimeout(None)  # Healthy logged-in clients may remain idle.
            return True, self.name
        except (OSError, ValueError, RecursionError, KeyError, TypeError, AttributeError) as exc:
            if sock is not None:
                self._drop_transport(sock)
            if self._closed:
                return False, "Login cancelled."
            if isinstance(exc, TimeoutError):
                return False, "Connection or login timed out. Try again."
            return False, f"Could not log in: {exc}"
        finally:
            self._login_lock.release()

    def start(self, on_message):
        """Read server messages on a thread; ends with {"type": "disconnected"}."""
        with self._state_lock:
            if self._closed or not self.name or self.receive_thread is not None:
                return
            self._callback = on_message
            reader, sock = self.reader, self.sock

        def receive():
            try:
                for line in reader:
                    self._emit(json.loads(line))
            except (OSError, ValueError, RecursionError):
                pass
            finally:
                self._notify_disconnected()
                self._drop_transport(sock)

        with self._state_lock:
            if self._closed or self.receive_thread is not None:
                return
            self.receive_thread = threading.Thread(target=receive, daemon=True)
            self.receive_thread.start()

    def close(self):
        """Cancel pending reads/login and release resources; safe to call repeatedly."""
        with self._state_lock:
            self._closed = True
            sock, worker = self.sock, self.receive_thread
        if sock is not None:
            self._drop_transport(sock)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1)

    def pm(self, to, text):
        return self.send({"type": "pm", "to": to, "text": text})

    def group_msg(self, group, text):
        return self.send({"type": "group_msg", "group": group, "text": text})

    def create_group(self, group):
        return self.send({"type": "create_group", "group": group})

    def join_group(self, group):
        return self.send({"type": "join_group", "group": group})
