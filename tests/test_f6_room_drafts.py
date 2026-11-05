import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch
import client


class RoomDraftTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        with patch.object(client, 'ChatCore', return_value=MagicMock()) as core:
            self.app = client.ChatClient(self.root)
        self.app.name = 'Alice'
        self.app.login_frame.destroy()
        self.app.build_ui()
        self.app.handle({'type': 'groups', 'groups': {'G': ['Alice'], 'Bob': ['Alice'], 'Preview': ['Bob']}})
        self.app.core.pm.return_value = True
        self.app.core.group_msg.return_value = True

    def tearDown(self):
        self.root.update_idletasks()
        self.app.close()

    def test_private_draft_is_not_group_draft(self):
        self.app.select(('pm', 'Bob'))
        self.app.entry.insert(0, ' secret  ')
        self.app.select(('group', 'G'))
        self.assertEqual(self.app.entry.get(), '')
        self.app.send_text()
        self.app.core.group_msg.assert_not_called()
        self.app.select(('pm', 'Bob'))
        self.assertEqual(self.app.entry.get(), ' secret  ')

    def test_round_trip_room_drafts_and_echo(self):
        self.app.select(('pm', 'Bob'))
        self.app.entry.insert(0, 'pm draft')
        self.app.select(('pm', 'Carol'))
        self.app.entry.insert(0, 'carol draft')
        self.app.select(('group', 'Bob'))
        self.app.entry.insert(0, 'group draft')
        self.app.select(('group', 'Bob'))
        self.assertEqual(self.app.entry.get(), 'group draft')
        self.app.send_button.invoke()
        self.assertEqual(self.app.entry.get(), '')
        self.assertEqual(self.app.rooms[('group', 'Bob')].get('1.0', 'end').strip(), '')
        self.app.handle({'type': 'group_msg', 'group': 'Bob', 'from': 'Alice', 'text': 'group draft'})
        self.assertEqual(self.app.rooms[('group', 'Bob')].get('1.0', 'end').count('group draft'), 1)
        self.app.handle({'type': 'groups', 'groups': {'G': ['Alice'], 'Bob': ['Alice'], 'Preview': ['Bob']}})
        self.assertEqual(self.app.entry.get(), '')
        self.app.select(None)
        self.app.select(('pm', 'Carol'))
        self.assertEqual(self.app.entry.get(), 'carol draft')
        self.app.select(('group', 'Preview'))
        self.assertEqual(self.app.entry.get(), '')
        self.app.select(('pm', 'Bob'))
        self.assertEqual(self.app.entry.get(), 'pm draft')
        self.app.select(('group', 'Bob'))
        self.assertEqual(self.app.entry.get(), '')

    def test_return_guard_and_membership_refresh_preserve_group_draft(self):
        self.app.select(('group', 'G'))
        self.app.entry.insert(0, 'group unsent')
        self.app.handle({'type': 'groups', 'groups': {'G': ['Bob']}})
        self.assertEqual(self.app.entry.get(), 'group unsent')
        self.assertEqual(str(self.app.entry.cget('state')), 'disabled')
        self.app.send_text(object())  # the Return binding calls this same handler
        self.app.core.group_msg.assert_not_called()
        self.app.select(('pm', 'Bob'))
        self.assertEqual(self.app.entry.get(), '')
        self.app.handle({'type': 'groups', 'groups': {'G': ['Alice', 'Bob']}})
        self.app.select(('group', 'G'))
        self.assertEqual(self.app.entry.get(), 'group unsent')
        self.app.core.group_msg.return_value = False
        self.app.send_text(object())
        self.assertEqual(self.app.entry.get(), 'group unsent')
        self.app.core.group_msg.return_value = True
        self.app.send_text(object())
        self.assertEqual(self.app.entry.get(), '')

    def test_return_binding_sends_and_clears_only_on_success(self):
        self.app.select(('pm', 'Bob'))
        self.app.entry.insert(0, 'via return')
        self.app.core.pm.return_value = False
        self.root.update()
        self.app.entry.focus_force()
        self.root.update()
        self.app.entry.event_generate('<Return>')
        self.assertEqual(self.app.entry.get(), 'via return')
        self.app.core.pm.return_value = True
        self.app.entry.event_generate('<Return>')
        self.app.core.pm.assert_called_with('Bob', 'via return')
        self.assertEqual(self.app.entry.get(), '')

    def test_local_send_failure_preserves_draft(self):
        self.app.select(('pm', 'Bob'))
        self.app.entry.insert(0, '  retain me  ')
        self.app.core.pm.return_value = False
        self.app.send_text()
        self.assertEqual(self.app.entry.get(), '  retain me  ')
        self.app.select(None)
        self.app.select(('pm', 'Bob'))
        self.assertEqual(self.app.entry.get(), '  retain me  ')
        self.app.core.pm.return_value = True
        self.app.send_text()
        self.assertEqual(self.app.entry.get(), '')
        self.app.select(('pm', 'Bob'))
        self.assertEqual(self.app.entry.get(), '')


if __name__ == '__main__':
    unittest.main()
