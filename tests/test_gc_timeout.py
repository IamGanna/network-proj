"""Tests for group chat timeout (Discord-style timeout in gc)."""
import time
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

from chat_core import ChatCore
import client
import server
from tests.support import Events, LoopbackServer


class GroupTimeoutTests(unittest.TestCase):
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

    def test_owner_can_timeout_member_blocks_sending_preserves_reading(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")

        # Alice creates group
        self.assertTrue(alice.create_group("study"))
        be.wait(lambda m: m.get("type") == "groups" and "study" in m.get("groups", {}))
        
        # Bob joins group
        self.assertTrue(bob.join_group("study"))
        ae.wait(lambda m: m.get("type") == "groups" and "Bob" in m.get("groups", {}).get("study", []))

        # Alice timeouts Bob for 60 seconds
        self.assertTrue(alice.timeout_member("study", "Bob", 60))

        # Both receive timeout_notice and announcement
        t_notice = {"type": "timeout_notice", "group": "study", "target": "Bob", "seconds": 60}
        self.assertEqual(ae.wait(lambda m: m.get("type") == "timeout_notice"), t_notice)
        self.assertEqual(be.wait(lambda m: m.get("type") == "timeout_notice"), t_notice)
        
        sys_msg = ae.wait(lambda m: m.get("type") == "group_msg" and m.get("from") == "[System]")
        self.assertIn("Bob was timed out by Alice", sys_msg["text"])

        # Bob tries to send a message -> rejected with error
        self.assertTrue(bob.group_msg("study", "can I speak?"))
        err = be.wait(lambda m: m.get("type") == "error")
        self.assertIn("You are timed out in 'study'", err["text"])

        # Alice can still send message -> Bob receives it (Bob can read!)
        self.assertTrue(alice.group_msg("study", "quiet please"))
        expected_msg = {"type": "group_msg", "group": "study", "from": "Alice", "text": "quiet please"}
        self.assertEqual(ae.wait(lambda m: m == expected_msg), expected_msg)
        self.assertEqual(be.wait(lambda m: m == expected_msg), expected_msg)

    def test_non_owner_cannot_timeout_member(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        carol, ce = self.connect("Carol")

        alice.create_group("study")
        be.wait(lambda m: m.get("type") == "groups" and "study" in m.get("groups", {}))
        bob.join_group("study")
        carol.join_group("study")
        ae.wait(lambda m: len(m.get("groups", {}).get("study", [])) == 3)

        # Bob (not owner) tries to timeout Carol
        self.assertTrue(bob.timeout_member("study", "Carol", 60))
        err = be.wait(lambda m: m.get("type") == "error")
        self.assertIn("Only the group admin can timeout members", err["text"])

    def test_cannot_timeout_self(self):
        alice, ae = self.connect("Alice")
        alice.create_group("study")
        ae.wait(lambda m: "study" in m.get("groups", {}))

        # Alice tries to timeout Alice
        self.assertTrue(alice.timeout_member("study", "Alice", 60))
        err = ae.wait(lambda m: m.get("type") == "error")
        self.assertIn("You cannot timeout yourself", err["text"])

    def test_untimeout_restores_sending(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")

        alice.create_group("study")
        be.wait(lambda m: "study" in m.get("groups", {}))
        bob.join_group("study")
        ae.wait(lambda m: "Bob" in m.get("groups", {}).get("study", []))

        # Timeout Bob
        alice.timeout_member("study", "Bob", 60)
        be.wait(lambda m: m.get("type") == "timeout_notice")

        # Untimeout Bob (duration 0)
        alice.timeout_member("study", "Bob", 0)
        un_notice = be.wait(lambda m: m.get("type") == "timeout_notice" and m.get("seconds") == 0)
        self.assertEqual(un_notice["target"], "Bob")

        # Bob can send message again!
        self.assertTrue(bob.group_msg("study", "I am back!"))
        expected_msg = {"type": "group_msg", "group": "study", "from": "Bob", "text": "I am back!"}
        self.assertEqual(ae.wait(lambda m: m == expected_msg), expected_msg)
        self.assertEqual(be.wait(lambda m: m == expected_msg), expected_msg)

    def test_timeout_expires_automatically(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")

        alice.create_group("study")
        be.wait(lambda m: "study" in m.get("groups", {}))
        bob.join_group("study")
        ae.wait(lambda m: "Bob" in m.get("groups", {}).get("study", []))

        # Timeout Bob for 1 second
        alice.timeout_member("study", "Bob", 1)
        be.wait(lambda m: m.get("type") == "timeout_notice")

        # Wait 1.1 seconds for expiration
        time.sleep(1.1)

        # Bob can send message now
        self.assertTrue(bob.group_msg("study", "expired timeout"))
        expected_msg = {"type": "group_msg", "group": "study", "from": "Bob", "text": "expired timeout"}
        self.assertEqual(ae.wait(lambda m: m == expected_msg), expected_msg)
        self.assertEqual(be.wait(lambda m: m == expected_msg), expected_msg)

    def test_ui_timeout_badge_auto_expires_in_members_panel(self):
        root = tk.Tk()
        try:
            with patch.object(client, "ChatCore", return_value=MagicMock()):
                app = client.ChatClient(root)
            app.name = "Alice"
            app.login_frame.destroy()
            app.build_ui()
            app.handle({"type": "groups", "groups": {"study": ["Alice", "Bob"]}})
            app.select(("group", "study"))

            # Initially no timeout badge
            def get_member_badges():
                labels = [w.cget("text") for r in app.member_list.content.winfo_children()
                          for w in r.winfo_children() if isinstance(w, tk.Label)]
                buttons = [w.cget("text") for r in app.member_list.content.winfo_children()
                           for w in r.winfo_children() if isinstance(w, tk.Button)]
                return labels, buttons

            labels, btns = get_member_badges()
            self.assertNotIn(" Timed out ", labels)
            self.assertIn("Timeout", btns)

            # Apply 1 second timeout to Bob
            app.handle({"type": "timeout_notice", "group": "study", "target": "Bob", "seconds": 1})
            labels, btns = get_member_badges()
            self.assertIn(" Timed out ", labels)
            self.assertIn("Manage", btns)

            # Before expiry, badge is still present
            app.check_timeout_countdown()
            labels, btns = get_member_badges()
            self.assertIn(" Timed out ", labels)

            # Wait for expiry
            time.sleep(1.05)
            app.check_timeout_countdown()

            # Now Timed out tag must disappear automatically and Manage returns to Timeout
            labels, btns = get_member_badges()
            self.assertNotIn(" Timed out ", labels)
            self.assertIn("Timeout", btns)
            self.assertNotIn("Manage", btns)
        finally:
            app.close()

    def test_ui_own_timeout_badge_and_input_auto_restores(self):
        root = tk.Tk()
        try:
            with patch.object(client, "ChatCore", return_value=MagicMock()):
                app = client.ChatClient(root)
            app.name = "Bob"
            app.login_frame.destroy()
            app.build_ui()
            app.handle({"type": "groups", "groups": {"study": ["Alice", "Bob"]}})
            app.select(("group", "study"))

            # Bob receives timeout_notice
            app.handle({"type": "timeout_notice", "group": "study", "target": "Bob", "seconds": 1})
            self.assertEqual(str(app.entry.cget("state")), "disabled")
            self.assertEqual(str(app.send_button.cget("state")), "disabled")
            self.assertEqual(str(app.file_button.cget("state")), "disabled")
            self.assertIn("⏳ You are timed out", app.subtitle.cget("text"))

            labels = [w.cget("text") for r in app.member_list.content.winfo_children()
                      for w in r.winfo_children() if isinstance(w, tk.Label)]
            self.assertIn(" Timed out ", labels)

            # Wait for expiry
            time.sleep(1.05)
            app.check_timeout_countdown()

            # Now Timed out badge disappears and input controls re-enable
            labels = [w.cget("text") for r in app.member_list.content.winfo_children()
                      for w in r.winfo_children() if isinstance(w, tk.Label)]
            self.assertNotIn(" Timed out ", labels)
            self.assertEqual(str(app.entry.cget("state")), "normal")
            self.assertEqual(str(app.send_button.cget("state")), "normal")
            self.assertEqual(str(app.file_button.cget("state")), "normal")
            self.assertEqual(app.subtitle.cget("text"), "Only members see these messages")
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
