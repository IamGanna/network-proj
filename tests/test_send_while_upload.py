"""Chat messages sent while a file uploads must not be refused, and the upload must finish."""
import os
import tempfile
import threading
import unittest

import server
from chat_core import ChatCore
from tests.support import Events, LoopbackServer


class SendWhileUploadTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(server.clients, {})
        self.peer = LoopbackServer(server.handle)
        self.cores = []

    def tearDown(self):
        for core in self.cores:
            core.close()
        self.peer.close()
        self.assertEqual(server.clients, {})

    def connect(self, name):
        core = ChatCore("127.0.0.1", self.peer.port)
        self.cores.append(core)
        self.assertEqual(core.login(name), (True, name))
        events = Events()
        core.start(events.put)
        return core, events

    def test_chatting_during_an_upload_refuses_nothing(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        path = os.path.join(tempfile.mkdtemp(), "big.bin")
        with open(path, "wb") as f:
            f.write(os.urandom(2_000_000))  # 62 chunks: the upload holds the socket for a while
        upload = {}
        worker = threading.Thread(target=lambda: upload.update(ok=alice.send_file("pm", "Bob", path)))
        worker.start()
        sent = [alice.pm("Bob", f"msg {i}") for i in range(40)]
        worker.join(30)
        self.assertTrue(upload.get("ok"), "the upload failed")
        self.assertEqual(sent.count(False), 0, "chat messages were refused during the upload")
        be.wait(lambda m: m.get("text") == "msg 39", timeout=10)
        self.assertFalse([m for m in ae.drain() if m.get("type") == "error"])
