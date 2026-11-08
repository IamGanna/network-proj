"""F3 online roster regressions using the real Tk widgets and socket path."""
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

import client
import server
from chat_core import ChatCore
from tests.support import Events, LoopbackServer, pump


class OnlineSelfTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(server.clients, {})
        self.assertEqual(server.groups, {})
        self.peer = LoopbackServer(server.handle)
        self.cores = []
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            if not app.closed:
                app.close()
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

    def ui(self, name="Alice", connect=False):
        root = tk.Tk()
        if connect:
            with patch.object(client, "HOST", "127.0.0.1"), patch.object(client, "PORT", self.peer.port):
                app = client.ChatClient(root)
        else:
            with patch.object(client, "ChatCore", return_value=MagicMock()):
                app = client.ChatClient(root)
        app.name = name
        app.login_frame.destroy()
        app.build_ui()
        self.apps.append(app)
        return root, app

    @staticmethod
    def roster_buttons(app):
        return [w for row in app.user_box.winfo_children() for w in row.winfo_children()
                if isinstance(w, tk.Button)]

    def test_online_list_includes_self_and_all_peers(self):
        root, app = self.ui()
        app.handle({"type": "users", "users": ["Alice", "Bob", "Carol"]})
        root.update()

        buttons = self.roster_buttons(app)
        self.assertTrue(root.winfo_ismapped())
        self.assertTrue(all(button.winfo_ismapped() for button in buttons))
        names = [button.cget("text").removesuffix(" (you)") for button in buttons]
        self.assertEqual(names.count("Alice"), 1)
        self.assertCountEqual(names, ["Alice", "Bob", "Carol"])
        self.assertEqual(len(names), 3)
        self.assertIn("DIRECT MESSAGES", app.dm_label.cget("text"))
        self.assertEqual(buttons[names.index("Alice")].cget("text"), "Alice (you)")

        buttons[names.index("Alice")].invoke()
        self.assertEqual(app.active, ("pm", "Alice"))

    def test_self_only_and_presence_changes(self):
        root, app = self.ui()
        app.handle({"type": "users", "users": ["Alice"]})
        root.update()
        self.assertEqual([button.cget("text") for button in self.roster_buttons(app)], ["Alice (you)"])
        self.assertIn("DIRECT MESSAGES", app.dm_label.cget("text"))

        app.handle({"type": "users", "users": ["Alice", "Bob"]})
        root.update()
        self.assertCountEqual([button.cget("text") for button in self.roster_buttons(app)],
                              ["Alice (you)", "Bob"])

        app.handle({"type": "users", "users": ["Alice", "Carol"]})
        root.update()
        current = [button.cget("text") for button in self.roster_buttons(app)]
        self.assertCountEqual(current, ["Alice (you)", "Carol"])
        self.assertNotIn("Bob", current)
        self.assertIn("DIRECT MESSAGES", app.dm_label.cget("text"))

    def test_self_pm_echo_once(self):
        root, app = self.ui(connect=True)
        self.assertEqual(app.core.login("Alice"), (True, "Alice"))
        bob, bob_events = self.connect("Bob")
        _carol, carol_events = self.connect("Carol")
        app.core.start(app.inbox.put)
        app.poll()

        pump(root, lambda: len(self.roster_buttons(app)) == 3)
        names = [button.cget("text") for button in self.roster_buttons(app)]
        self.assertCountEqual(names, ["Alice (you)", "Bob", "Carol"])

        self.assertTrue(app.core.pm("Alice", "one self echo"))
        pump(root, lambda: app.inbox.empty() and app.rooms.get(("pm", "Alice")) is not None
             and "one self echo" in app.rooms[("pm", "Alice")].get("1.0", "end"))
        content = app.rooms[("pm", "Alice")].get("1.0", "end")
        self.assertEqual(content.count("one self echo"), 1)
        self.assertTrue(app.core.pm("Bob", "Bob witness"))
        bob_events.wait(lambda m: m.get("text") == "Bob witness")
        self.assertTrue(app.core.pm("Carol", "Carol witness"))
        carol_events.wait(lambda m: m.get("text") == "Carol witness")
        self.assertFalse(any(m.get("text") == "one self echo" for m in bob_events.seen))
        self.assertFalse(any(m.get("text") == "one self echo" for m in carol_events.seen))


if __name__ == "__main__":
    unittest.main()
