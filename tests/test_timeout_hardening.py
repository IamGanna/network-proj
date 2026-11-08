"""Group timeout edge cases: reconnecting, the reserved [System] name, huge durations."""
import json
import unittest

import server
from chat_core import ChatCore
from tests.support import Events, LoopbackServer


class TimeoutHardeningTests(unittest.TestCase):
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

    def group(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        alice.create_group("g")
        be.wait(lambda m: "g" in (m.get("groups") or {}))
        bob.join_group("g")
        ae.wait(lambda m: m.get("groups", {}).get("g") == ["Alice", "Bob"])
        return alice, ae, bob, be

    def test_reconnecting_does_not_end_a_timeout(self):
        alice, ae, bob, be = self.group()
        alice.timeout_member("g", "Bob", 3600)
        be.wait(lambda m: m.get("type") == "timeout_notice")
        bob.close()
        ae.wait(lambda m: m.get("groups", {}).get("g") == ["Alice"])
        bob, be = self.connect("Bob")
        bob.join_group("g")
        notice = be.wait(lambda m: m.get("type") == "timeout_notice")
        self.assertGreater(int(notice["seconds"]), 3500)
        bob.group_msg("g", "I escaped")
        self.assertIn("timed out", be.wait(lambda m: m.get("type") == "error")["text"])
        alice.pm("Alice", "barrier")
        ae.wait(lambda m: m.get("text") == "barrier")
        self.assertFalse(any(m.get("text") == "I escaped" for m in ae.seen))

    def test_system_name_is_reserved(self):
        for name in ("[System]", "[system]", "[SYSTEM]"):
            with self.subTest(name=name):
                core = ChatCore("127.0.0.1", self.peer.port)
                self.cores.append(core)
                ok, text = core.login(name)
                self.assertFalse(ok)
                self.assertIn("reserved", text)

    def test_huge_duration_is_refused_and_admin_stays_connected(self):
        alice, ae, bob, be = self.group()
        alice.sock.sendall((json.dumps({"type": "timeout_member", "group": "g", "target": "Bob",
                                        "duration": "9" * 400}) + "\n").encode())
        self.assertIn("Invalid timeout duration", ae.wait(lambda m: m.get("type") == "error")["text"])
        alice.timeout_member("g", "Bob", 31_536_001)
        ae.wait(lambda m: m.get("type") == "error")
        alice.timeout_member("g", "Bob", 604800)
        self.assertEqual(int(be.wait(lambda m: m.get("type") == "timeout_notice")["seconds"]), 604800)
