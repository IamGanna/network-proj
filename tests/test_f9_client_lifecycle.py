import json
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from chat_core import ChatCore
from tests.support import Events, LoopbackServer, frame, pump


def accepting(conn, _addr):
    with conn.makefile("r", encoding="utf-8") as reader:
        for line in reader:
            msg = json.loads(line)
            if msg["type"] == "login":
                conn.sendall(frame({"type": "login_ok", "name": msg["name"]}))


class LifecycleTests(unittest.TestCase):
    def test_closed_tk_cycles_collected_before_fixture_workers(self):
        # A fatal Tcl finalizer must be isolated from the main test runner. The
        # child suppresses automatic GC only to force the suspected scheduling:
        # closed real UI cycles, next fixture, then an explicit worker collection.
        source = """
import gc
import threading
import tkinter as tk
import client
from tests.support import LoopbackServer

gc.disable()
def closed_ui():
    root = tk.Tk()
    root.withdraw()
    app = client.ChatClient(root)
    app.name = 'Alice'
    app.login_frame.destroy()
    app.build_ui()
    app.close()

closed_ui()
with LoopbackServer(lambda conn, addr: None):
    worker = threading.Thread(target=gc.collect)
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
gc.enable()
print('closed Tk worker-GC regression passed')
"""
        result = subprocess.run([sys.executable, "-B", "-X", "faulthandler", "-c", source],
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("closed Tk worker-GC regression passed", result.stdout)

    def ui(self, port):
        # Import behavior is covered separately; isolate startup from runner flags.
        with patch.object(sys, "argv", ["client.py"]):
            import client
        root = tk.Tk()
        def cleanup_root():
            try:
                root.destroy()
            except tk.TclError:
                pass
        self.addCleanup(cleanup_root)
        with patch.object(client, "HOST", "127.0.0.1"), patch.object(client, "PORT", port):
            app = client.ChatClient(root)
        self.addCleanup(app.close)
        root.update()
        return root, app

    def test_client_import_ignores_runner_arguments(self):
        result = subprocess.run([sys.executable, "-B", "-c",
            "import sys; sys.argv=['runner','-v','not-a-port']; import client; "
            "assert (client.HOST, client.PORT)==('127.0.0.1',5000)"],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unavailable_server_keeps_login_ui_and_can_retry(self):
        reserve = socket.socket()
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
        reserve.close()
        root, app = self.ui(port)
        app.name_entry.insert(0, "Alice")
        app.login()
        pump(root, lambda: bool(app.login_error.cget("text")), timeout=5)
        self.assertTrue(app.login_frame.winfo_exists())
        self.assertEqual(app.login_button.cget("state"), "normal")
        with LoopbackServer(accepting, port):
            app.login()
            pump(root, lambda: getattr(app, "name", None) == "Alice")
            self.assertTrue(app.center.winfo_exists())
            app.close()

    def test_silent_login_expires_without_blocking_tk(self):
        entered = threading.Event()
        def silent(conn, _addr):
            entered.set()
            while conn.recv(1024):
                pass
        with LoopbackServer(silent) as peer:
            root, app = self.ui(peer.port)
            app.core.login_timeout = .15
            app.name_entry.insert(0, "Alice")
            tick = []
            root.after(20, lambda: tick.append(True))
            before = time.monotonic()
            app.login()
            self.assertLess(time.monotonic() - before, .1)
            worker = app.login_worker
            app.login()  # Return binding cannot overlap the disabled button.
            self.assertIs(app.login_worker, worker)
            pump(root, lambda: bool(app.login_error.cget("text")))
            self.assertTrue(tick)
            self.assertTrue(entered.is_set())
            self.assertIn("timed out", app.login_error.cget("text").lower())
            self.assertFalse(worker.is_alive())
            app.close()

    def test_login_rejection_then_retry(self):
        def reject(conn, _addr):
            with conn.makefile("r", encoding="utf-8") as reader:
                self.assertEqual(json.loads(reader.readline())["name"], "Alice")
                conn.sendall(frame({"type": "error", "text": "Name already taken"}))
                self.assertEqual(json.loads(reader.readline())["name"], "Bob")
                conn.sendall(frame({"type": "login_ok", "name": "Bob"}))
                reader.read()
        with LoopbackServer(reject) as peer:
            core = ChatCore("127.0.0.1", peer.port)
            try:
                self.assertEqual(core.login("Alice"), (False, "Name already taken"))
                self.assertEqual(core.login("Bob"), (True, "Bob"))
            finally:
                core.close()

    def test_trickled_login_has_absolute_response_deadline(self):
        def trickle(conn, _addr):
            conn.recv(1024)
            for value in frame({"type": "login_ok", "name": "Alice"}):
                try:
                    conn.sendall(bytes([value]))
                except OSError:
                    return
                time.sleep(.02)
        with LoopbackServer(trickle) as peer:
            core = ChatCore("127.0.0.1", peer.port, login_timeout=.12)
            result = []
            worker = threading.Thread(target=lambda: result.append(core.login("Alice")), daemon=True)
            try:
                worker.start()
                worker.join(.4)
                self.assertFalse(worker.is_alive(), "Trickled bytes must not renew the response budget")
                self.assertFalse(result[0][0])
                self.assertIn("timed out", result[0][1].lower())
            finally:
                core.close()
                worker.join(1)

    def test_disconnect_poll_does_not_schedule_after_close(self):
        with LoopbackServer(accepting) as peer:
            root, app = self.ui(peer.port)
            app.name_entry.insert(0, "Alice")
            app.login()
            pump(root, lambda: getattr(app, "name", None) == "Alice")
            original_after = root.after
            def checked_after(*args):
                self.assertFalse(app.closed, "poll scheduled a callback after destroying root")
                return original_after(*args)
            app.inbox.put({"type": "disconnected"})
            with patch.object(root, "after", checked_after), patch("client.messagebox.showerror"):
                app.poll()
            self.assertTrue(app.closed)

    def test_login_preserves_buffered_state(self):
        def combined(conn, _addr):
            conn.recv(1024)
            conn.sendall(frame({"type": "login_ok", "name": "Alice"}) +
                         frame({"type": "users", "users": ["Alice"]}) +
                         frame({"type": "groups", "groups": {"room": ["Alice"]}}))
        with LoopbackServer(combined) as peer:
            core = ChatCore("127.0.0.1", peer.port)
            try:
                self.assertEqual(core.login("Alice"), (True, "Alice"))
                events = Events()
                core.start(events.put)
                core.start(events.put)
                events.wait(lambda m: m["type"] == "disconnected")
                self.assertEqual(events.seen, [
                    {"type": "users", "users": ["Alice"]},
                    {"type": "groups", "groups": {"room": ["Alice"]}},
                    {"type": "disconnected"}])
            finally:
                core.close()

    def test_split_and_coalesced_unicode_frames(self):
        text = "ไทย 🌟\nsecond line"
        def split(conn, _addr):
            conn.recv(1024)
            conn.sendall(frame({"type": "login_ok", "name": "Alice"}))
            data = frame({"type": "pm", "from": "Bob", "to": "Alice", "text": text})
            cut = data.index("🌟".encode()) + 2
            conn.sendall(data[:cut])
            conn.sendall(data[cut:] + frame({"type": "groups", "groups": {}}))
        with LoopbackServer(split) as peer:
            core = ChatCore("127.0.0.1", peer.port)
            try:
                self.assertTrue(core.login("Alice")[0])
                events = Events()
                core.start(events.put)
                events.wait(lambda m: m["type"] == "disconnected")
                self.assertEqual(events.seen, [
                    {"type": "pm", "from": "Bob", "to": "Alice", "text": text},
                    {"type": "groups", "groups": {}}, {"type": "disconnected"}])
            finally:
                core.close()

    def test_send_failure_returns_false(self):
        with LoopbackServer(accepting) as peer:
            core = ChatCore("127.0.0.1", peer.port)
            try:
                self.assertTrue(core.login("Alice")[0])
                events = Events()
                core.start(events.put)
                # A real local transport failure, without timing a remote FIN.
                core.sock.shutdown(socket.SHUT_WR)
                self.assertIs(core.pm("Bob", "unsent"), False)
                events.wait(lambda m: m["type"] == "disconnected")
                self.assertEqual(sum(m["type"] == "disconnected" for m in events.drain()), 1)
                self.assertTrue(any(m["type"] == "error" for m in events.seen))
            finally:
                core.close()

    def test_close_and_login_cancel_once(self):
        entered = threading.Event()
        def silent(conn, _addr):
            entered.set()
            while conn.recv(1024):
                pass
        with LoopbackServer(silent) as peer:
            root, app = self.ui(peer.port)
            app.name_entry.insert(0, "Alice")
            app.login()
            self.assertTrue(entered.wait(1))
            worker = app.login_worker
            app.close()
            app.close()
            self.assertFalse(worker.is_alive())
            self.assertIsNone(app.core.sock)
        with LoopbackServer(accepting) as peer:
            core = ChatCore("127.0.0.1", peer.port)
            self.assertTrue(core.login("Alice")[0])
            events = Events()
            core.start(events.put)
            core.close()
            core.close()
            self.assertFalse(core.receive_thread.is_alive())
            self.assertFalse(any(m["type"] == "disconnected" for m in events.drain()))


if __name__ == "__main__":
    unittest.main()
