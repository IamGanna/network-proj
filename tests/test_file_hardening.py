"""File transfer edge cases: stated size, reused ids, open uploads per person."""
import base64
import json
import os
import tempfile
import time
import unittest

import server
from chat_core import ChatCore
from tests.support import Events, LoopbackServer


def raw(core, message):
    core.sock.sendall((json.dumps(message) + "\n").encode())


def start(file_id, target, filesize="10", chunks="1", target_type="pm"):
    return {"type": "file_start", "file_id": file_id, "target_type": target_type, "target": target,
            "filename": "a.txt", "filesize": filesize, "chunks": chunks}


def chunk(file_id, index, data):
    return {"type": "file_chunk", "file_id": file_id, "chunk": str(index), "data": base64.b64encode(data).decode()}


class FileHardeningTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(server.clients, {})
        self.peer = LoopbackServer(server.handle)
        self.cores = []

    def tearDown(self):
        for core in self.cores:
            core.close()
        self.peer.close()
        self.assertEqual(server.clients, {})
        self.assertEqual(server.active_transfers, {})

    def connect(self, name):
        core = ChatCore("127.0.0.1", self.peer.port)
        self.cores.append(core)
        self.assertEqual(core.login(name), (True, name))
        events = Events()
        core.start(events.put)
        return core, events

    def barrier(self, core, events):
        core.pm(core.name, "barrier")
        events.wait(lambda m: m.get("text") == "barrier")

    def test_data_beyond_the_stated_size_is_stopped(self):
        mallory, me = self.connect("Mallory")
        bob, be = self.connect("Bob")
        raw(mallory, start("f", "Bob", filesize="1000", chunks="1000"))
        be.wait(lambda m: m.get("type") == "file_start")
        for i in range(5):
            raw(mallory, chunk("f", i, os.urandom(600)))
        self.assertIn("bigger than", me.wait(lambda m: m.get("type") == "error")["text"])
        self.barrier(mallory, me)
        self.barrier(bob, be)
        self.assertEqual(sum(m.get("type") == "file_chunk" for m in be.seen), 1)
        self.assertEqual(server.active_transfers, {})
        raw(mallory, start("g", "Bob", filesize="1", chunks="5"))  # more chunks than bytes
        me.wait(lambda m: m.get("type") == "error" and "Invalid file size" in m["text"])

    def test_a_live_file_id_cannot_be_taken_over(self):
        alice, ae = self.connect("Alice")
        mallory, me = self.connect("Mallory")
        raw(alice, start("shared", "Mallory", chunks="2"))
        me.wait(lambda m: m.get("type") == "file_start")
        raw(mallory, start("shared", "Alice"))
        self.assertIn("already in use", me.wait(lambda m: m.get("type") == "error")["text"])
        raw(alice, chunk("shared", 0, b"hello"))
        raw(alice, chunk("shared", 1, b"world"))
        received = me.wait(lambda m: m.get("type") == "file_chunk" and m["chunk"] == "1")
        self.assertEqual(received["file_id"], "shared")
        self.barrier(alice, ae)
        self.assertFalse(any(m.get("type") == "error" for m in ae.seen))

    def test_at_most_three_uploads_open_per_person(self):
        mallory, me = self.connect("Mallory")
        bob, be = self.connect("Bob")
        for i in range(3):
            raw(mallory, start(f"f{i}", "Bob"))
        raw(mallory, start("f3", "Bob"))
        self.assertIn("at most 3", me.wait(lambda m: m.get("type") == "error")["text"])
        raw(mallory, chunk("f3", 0, b"x"))  # chunks of the refused upload are dropped quietly
        self.barrier(mallory, me)
        self.assertEqual(sum(m.get("type") == "error" for m in me.seen), 1)
        for i in range(3):
            raw(mallory, chunk(f"f{i}", 0, b"0123456789"))
        self.barrier(bob, be)
        deadline = time.monotonic() + 2
        while server.active_transfers and time.monotonic() < deadline:
            time.sleep(.01)

    def test_real_files_still_arrive_exactly(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        for size in (1, 2, 32766, 32767, 1_000_001):
            with self.subTest(size=size):
                data = os.urandom(size)
                path = os.path.join(tempfile.mkdtemp(), "f.bin")
                with open(path, "wb") as f:
                    f.write(data)
                self.assertTrue(alice.send_file("pm", "Bob", path))
                start_msg = be.wait(lambda m: m.get("type") == "file_start" and m["filesize"] == str(size))
                total = int(start_msg["chunks"])
                parts = {}
                deadline = time.monotonic() + 10
                while len(parts) < total and time.monotonic() < deadline:
                    for m in be.drain():
                        if m.get("type") == "file_chunk" and m["file_id"] == start_msg["file_id"]:
                            parts[int(m["chunk"])] = m["data"]
                    time.sleep(.01)
                self.assertEqual(b"".join(base64.b64decode(parts[i]) for i in range(total)), data)
                self.assertFalse(any(m.get("type") == "error" for m in ae.drain()))
