"""Unit tests for chunked file and picture transfer over sockets."""
import base64
import os
import tempfile
import unittest

from chat_core import ChatCore
import server
from tests.support import Events, LoopbackServer


class FileTransferTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(server.clients, {})
        self.assertEqual(server.groups, {})
        self.peer = LoopbackServer(server.handle)
        self.cores = []
        self.temp_files = []

    def tearDown(self):
        for core in self.cores:
            core.close()
        self.peer.close()
        for p in self.temp_files:
            try:
                os.remove(p)
            except OSError:
                pass
        self.assertEqual(server.clients, {})
        self.assertEqual(server.groups, {})

    def connect(self, name):
        core = ChatCore("127.0.0.1", self.peer.port)
        self.cores.append(core)
        self.assertEqual(core.login(name), (True, name))
        events = Events()
        core.start(events.put)
        return core, events

    def create_test_file(self, size_bytes, filename="test.dat"):
        tmp_dir = tempfile.gettempdir()
        path = os.path.join(tmp_dir, filename)
        data = os.urandom(size_bytes)
        with open(path, "wb") as f:
            f.write(data)
        self.temp_files.append(path)
        return path, data

    def test_pm_file_transfer_multi_chunk_exact_match(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        carol, ce = self.connect("Carol")

        # Create a 70 KB file (requires 3 chunks since chunk size is 32 KB)
        file_path, original_data = self.create_test_file(70 * 1024, "sample_doc.pdf")

        # Alice sends file to Bob
        ok = alice.send_file("pm", "Bob", file_path)
        self.assertTrue(ok)

        # Alice (sender) receives file_start echo
        ae.wait(lambda m: m.get("type") == "file_start" and m.get("filename") == "sample_doc.pdf")

        # Bob (recipient) receives file_start
        start_ev = be.wait(lambda m: m.get("type") == "file_start" and m.get("filename") == "sample_doc.pdf")
        self.assertEqual(start_ev["from"], "Alice")
        total_chunks = int(start_ev["chunks"])
        self.assertEqual(total_chunks, 3)

        # Bob receives all 3 chunks
        chunks = {}
        for _ in range(total_chunks):
            chunk_ev = be.wait(lambda m: m.get("type") == "file_chunk" and m.get("file_id") == start_ev["file_id"])
            chunks[int(chunk_ev["chunk"])] = chunk_ev["data"]

        self.assertEqual(len(chunks), total_chunks)
        reassembled = b"".join(base64.b64decode(chunks[i]) for i in range(total_chunks))
        self.assertEqual(reassembled, original_data)

        # Carol (unrelated peer) receives nothing
        self.assertFalse(any(m.get("type") in ("file_start", "file_chunk") for m in ce.seen))

    def test_group_file_transfer(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")

        alice.create_group("photos")
        be.wait(lambda m: m.get("type") == "groups" and "photos" in m.get("groups", {}))
        bob.join_group("photos")
        ae.wait(lambda m: "Bob" in m.get("groups", {}).get("photos", []))

        # Create a small 5 KB PNG-like file
        file_path, original_data = self.create_test_file(5 * 1024, "photo.png")

        ok = alice.send_file("group", "photos", file_path)
        self.assertTrue(ok)

        # Both receive file_start
        start_alice = ae.wait(lambda m: m.get("type") == "file_start")
        start_bob = be.wait(lambda m: m.get("type") == "file_start")
        self.assertEqual(start_alice["file_id"], start_bob["file_id"])

        # Bob receives the chunk
        chunk_ev = be.wait(lambda m: m.get("type") == "file_chunk")
        reassembled = base64.b64decode(chunk_ev["data"])
        self.assertEqual(reassembled, original_data)

    def test_offline_recipient_returns_error(self):
        alice, ae = self.connect("Alice")
        file_path, _ = self.create_test_file(1024, "offline.txt")

        alice.send_file("pm", "NonExistentUser", file_path)
        err = ae.wait(lambda m: m.get("type") == "error")
        self.assertIn("offline", err["text"])

    def test_nonmember_group_file_rejected(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        bob.create_group("secret")
        ae.wait(lambda m: "secret" in m.get("groups", {}))

        file_path, _ = self.create_test_file(1024, "intruder.txt")
        # Alice is not a member of 'secret'
        alice.send_file("group", "secret", file_path)
        err = ae.wait(lambda m: m.get("type") == "error")
        self.assertIn("not a member", err["text"])


    def test_client_ui_preview_for_sender_and_recipient_with_jpg_and_png(self):
        import tkinter as tk
        import client
        from PIL import Image
        import io

        root = tk.Tk()
        root.withdraw()
        app = client.ChatClient(root)
        app.name = "Alice"
        app.login_frame.destroy()
        app.build_ui()
        self.addCleanup(app.close)

        # 1. Create dummy JPG bytes
        im = Image.new("RGB", (200, 150), color="red")
        buf = io.BytesIO()
        im.save(buf, format="JPEG")
        jpg_bytes = buf.getvalue()

        # Sender view: show_file with raw_bytes
        app.show_file(("pm", "Bob"), "Alice", "photo.jpg", len(jpg_bytes), "fid1", raw_bytes=jpg_bytes)
        self.assertGreater(len(app.file_photos), 0)

        # Recipient view: show_file with raw_bytes
        app.show_file(("pm", "Bob"), "Bob", "received.jpg", len(jpg_bytes), "fid2", raw_bytes=jpg_bytes)
        self.assertGreater(len(app.file_photos), 1)

        # 2. Test PNG file preview
        buf_png = io.BytesIO()
        im.save(buf_png, format="PNG")
        png_bytes = buf_png.getvalue()
        app.show_file(("pm", "Bob"), "Bob", "diagram.png", len(png_bytes), "fid3", raw_bytes=png_bytes)
        self.assertGreater(len(app.file_photos), 2)


if __name__ == "__main__":
    unittest.main()
