"""Real core/server-handler routing baseline. Not a physical-computer demo."""
import unittest

from chat_core import ChatCore
import server
from tests.support import Events, LoopbackServer


class RubricIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(server.clients, {})
        self.assertEqual(server.groups, {})
        self.peer = LoopbackServer(server.handle)
        self.cores = []

    def tearDown(self):
        for core in self.cores:
            core.close()
        self.peer.close()
        self.assertEqual(server.clients, {})
        self.assertEqual(server.groups, {})

    def connect(self, name):
        core = ChatCore("127.0.0.1", self.peer.port)
        self.cores.append(core)
        self.assertEqual(core.login(name), (True, name))
        events = Events()
        core.start(events.put)
        return core, events

    def barrier(self, core, events, marker):
        # Same stream: all earlier server decisions precede this self PM.
        self.assertTrue(core.pm(core.name, marker))
        events.wait(lambda m: m.get("text") == marker)

    def test_unique_names_allow_rejected_connection_retry(self):
        alice, ae = self.connect("Alice")
        other = ChatCore("127.0.0.1", self.peer.port)
        self.cores.append(other)
        self.assertFalse(other.login("Alice")[0])
        self.assertEqual(other.login("Bob"), (True, "Bob"))
        ae.wait(lambda m: m == {"type": "users", "users": ["Alice", "Bob"]})

    def test_pm_echoes_once_and_excludes_unrelated_clients(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        carol, ce = self.connect("Carol")
        self.assertTrue(alice.send({"type": "pm", "from": "Carol", "to": "Bob", "text": "private ไทย"}))
        expected = {"type": "pm", "from": "Alice", "to": "Bob", "text": "private ไทย"}
        self.assertEqual(ae.wait(lambda m: m["type"] == "pm"), expected)
        self.assertEqual(be.wait(lambda m: m["type"] == "pm"), expected)
        self.barrier(alice, ae, "alice barrier")
        self.barrier(bob, be, "bob barrier")
        self.barrier(carol, ce, "carol barrier")
        self.assertEqual(ae.seen.count(expected), 1)
        self.assertEqual(be.seen.count(expected), 1)
        self.assertNotIn(expected, ce.seen)

    def test_global_metadata_explicit_join_and_members_only_routing(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        carol, ce = self.connect("Carol")
        self.assertTrue(alice.create_group("Alice room"))
        initial = {"type": "groups", "groups": {"Alice room": ["Alice"]}}
        for events in (ae, be, ce):
            self.assertEqual(events.wait(lambda m: m == initial), initial)
        self.assertTrue(bob.create_group("Bob room"))
        all_groups = {"Alice room": ["Alice"], "Bob room": ["Bob"]}
        for events in (ae, be, ce):
            events.wait(lambda m: m.get("groups") == all_groups)
        self.assertTrue(carol.group_msg("Alice room", "forbidden"))
        ce.wait(lambda m: m["type"] == "error")
        self.assertTrue(bob.send({"type": "join_group", "group": "Alice room", "name": "Carol"}))
        joined = {"Alice room": ["Alice", "Bob"], "Bob room": ["Bob"]}
        for events in (ae, be, ce):
            events.wait(lambda m: m.get("groups") == joined)
        self.assertTrue(alice.group_msg("Alice room", "members only"))
        expected = {"type": "group_msg", "group": "Alice room", "from": "Alice", "text": "members only"}
        self.assertEqual(ae.wait(lambda m: m["type"] == "group_msg"), expected)
        self.assertEqual(be.wait(lambda m: m["type"] == "group_msg"), expected)
        self.barrier(alice, ae, "alice barrier")
        self.barrier(bob, be, "bob barrier")
        self.barrier(carol, ce, "carol barrier")
        self.assertEqual(ae.seen.count(expected), 1)
        self.assertEqual(be.seen.count(expected), 1)
        self.assertNotIn(expected, ce.seen)
        self.assertFalse(any(m.get("text") == "forbidden" for e in (ae, be, ce) for m in e.seen))


if __name__ == "__main__":
    unittest.main()
