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


class ChatCore:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port))
        self.reader = self.sock.makefile("r", encoding="utf-8")
        self.name = None

    def send(self, msg):
        self.sock.sendall((json.dumps(msg) + "\n").encode())

    def login(self, name):
        """Blocking. Returns (True, name) or (False, error text). Call before start()."""
        self.send({"type": "login", "name": name})
        line = self.reader.readline()
        if not line:
            return False, "The server closed the connection."
        reply = json.loads(line)
        if reply["type"] != "login_ok":  # R3: name taken
            return False, reply["text"]
        self.name = reply["name"]
        return True, self.name

    def start(self, on_message):
        """Read server messages on a thread; ends with {"type": "disconnected"}."""
        def receive():
            try:
                for line in self.reader:
                    on_message(json.loads(line))
            except (OSError, ValueError):
                pass
            on_message({"type": "disconnected"})
        threading.Thread(target=receive, daemon=True).start()

    def pm(self, to, text):
        self.send({"type": "pm", "to": to, "text": text})

    def group_msg(self, group, text):
        self.send({"type": "group_msg", "group": group, "text": text})

    def create_group(self, group):
        self.send({"type": "create_group", "group": group})

    def join_group(self, group):
        self.send({"type": "join_group", "group": group})
