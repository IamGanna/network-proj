import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch
import client
import server
from chat_core import ChatCore
from tests.support import Events, LoopbackServer, pump

class GroupPreviewTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        with patch.object(client, 'ChatCore', return_value=MagicMock()) as core:
            self.app = client.ChatClient(self.root)
        self.app.name = 'Bob'
        self.app.login_frame.destroy()
        self.app.build_ui()
        self.app.handle({'type': 'groups', 'groups': {'Room': ['Alice']}})

    def tearDown(self):
        self.app.close()

    def click_room(self):
        row = self.app.group_box.winfo_children()[0]
        next(w for w in row.winfo_children() if isinstance(w, tk.Button)).invoke()

    def test_nonmember_preview_does_not_join(self):
        self.click_room()
        self.app.core.join_group.assert_not_called()
        self.assertEqual(self.app.active, ('group', 'Room'))
        self.assertIn('Alice', str([w.cget('text') for r in self.app.member_list.content.winfo_children() for w in r.winfo_children() if isinstance(w, tk.Label)]))

    def test_preview_has_explicit_join_and_no_send(self):
        self.app.select(('pm', 'Alice'))
        self.app.entry.insert(0, 'draft')
        self.click_room()
        self.assertEqual(self.app.entry.get(), '')
        self.assertEqual(str(self.app.entry.cget('state')), 'disabled')
        self.assertEqual(str(self.app.send_button.cget('state')), 'disabled')
        self.app.send_text()
        self.app.core.group_msg.assert_not_called()
        self.assertEqual(self.app.entry.get(), '')
        self.app.join_button.invoke()
        self.app.core.join_group.assert_called_once_with('Room')
        self.app.handle({'type': 'groups', 'groups': {'Room': ['Alice', 'Bob']}})
        self.assertTrue(self.app.can_send(('group', 'Room')))
        self.assertEqual(str(self.app.entry.cget('state')), 'normal')
        self.app.send_text()
        self.app.core.group_msg.assert_not_called()
        self.app.select(('pm', 'Alice'))
        self.assertEqual(self.app.entry.get(), 'draft')

    def test_preview_updates_and_disappearing_group(self):
        self.click_room()
        self.app.handle({'type': 'groups', 'groups': {'Room': ['Alice', 'Carol']}})
        labels = [w.cget('text') for r in self.app.member_list.content.winfo_children()
                  for w in r.winfo_children() if isinstance(w, tk.Label)]
        self.assertIn('Carol', labels)
        self.app.handle({'type': 'groups', 'groups': {}})
        self.assertIsNone(self.app.active)
        self.app.join_group('Room')
        self.app.core.join_group.assert_not_called()
        self.app.select(('pm', 'Alice'))
        self.app.handle({'type': 'groups', 'groups': {}})
        self.assertEqual(self.app.active, ('pm', 'Alice'))

class GroupSocketTests(unittest.TestCase):
    def test_real_preview_join_and_privacy(self):
        self.assertEqual(server.clients, {})
        self.assertEqual(server.groups, {})
        peer = LoopbackServer(server.handle)
        cores = []
        root = tk.Tk()
        app = None
        try:
            def connect(name):
                core = ChatCore('127.0.0.1', peer.port)
                cores.append(core)
                self.assertEqual(core.login(name), (True, name))
                events = Events()
                core.start(events.put)
                return core, events
            alice, ae = connect('Alice')
            carol, ce = connect('Carol')
            with patch.object(client, 'HOST', '127.0.0.1'), patch.object(client, 'PORT', peer.port):
                app = client.ChatClient(root)
            self.assertEqual(app.core.login('Bob'), (True, 'Bob'))
            app.name = 'Bob'
            app.login_frame.destroy()
            app.build_ui()
            app.core.start(app.inbox.put)
            app.poll()
            self.assertTrue(alice.create_group('Room'))
            pump(root, lambda: app.groups.get('Room') == ['Alice'])
            ce.wait(lambda m: m.get('groups') == {'Room': ['Alice']})
            row = app.group_box.winfo_children()[0]
            next(w for w in row.winfo_children() if isinstance(w, tk.Button)).invoke()
            self.assertEqual(server.groups['Room'], ['Alice'])
            self.assertFalse(app.can_send(app.active))
            self.assertNotIn(('group', 'Room'), app.rooms)
            self.assertTrue(carol.group_msg('Room', 'forbidden'))
            ce.wait(lambda m: m['type'] == 'error')
            app.join_button.invoke()
            pump(root, lambda: app.can_send(('group', 'Room')))
            self.assertEqual(server.groups['Room'], ['Alice', 'Bob'])
            ce.wait(lambda m: m.get('groups') == {'Room': ['Alice', 'Bob']})
            self.assertTrue(alice.group_msg('Room', 'members only'))
            ae.wait(lambda m: m.get('text') == 'members only')
            pump(root, lambda: 'members only' in app.rooms[('group', 'Room')].get('1.0', 'end'))
            self.assertTrue(carol.pm('Carol', 'barrier'))
            ce.wait(lambda m: m.get('text') == 'barrier')
            self.assertFalse(any(m.get('text') in ('members only', 'forbidden') for m in ce.seen))
        finally:
            if app and not app.closed:
                app.close()
            else:
                root.destroy()
            for core in cores:
                core.close()
            peer.close()
        self.assertEqual(server.clients, {})
        self.assertEqual(server.groups, {})

if __name__ == '__main__':
    unittest.main()
