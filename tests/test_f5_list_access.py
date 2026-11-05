import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch
import client

class ListAccessTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        with patch.object(client, 'ChatCore', return_value=MagicMock()):
            self.app = client.ChatClient(self.root)
        self.app.name = 'Alice'
        self.app.login_frame.destroy()
        self.app.build_ui()
        self.root.update()

    def tearDown(self):
        self.app.close()

    def test_twenty_groups_does_not_hide_online_list(self):
        self.app.handle({'type': 'groups', 'groups': {f'Group{i:02}': ['Alice'] for i in range(20)}})
        self.app.handle({'type': 'users', 'users': ['Alice', 'Bob', 'Carol']})
        self.root.update()
        self.assertTrue(self.app.online_label.winfo_ismapped(), 'group growth hid online section')
        self.assertTrue(self.app.user_box.winfo_ismapped(), 'group growth hid online list')

    def visible(self, area, widget):
        self.root.update()
        y = widget.winfo_rooty() - area.canvas.winfo_rooty()
        return y >= 0 and y + widget.winfo_height() <= area.canvas.winfo_height() + 1

    def button_for(self, box, text):
        return next(w for row in box.winfo_children() for w in row.winfo_children()
                    if isinstance(w, tk.Button) and w.cget('text') == text)

    def test_minimum_size_scaling_and_member_scroll(self):
        members = ['Alice'] + [f'Member{i:02}' for i in range(25)]
        groups = {f'Group{i:02}': ['Alice'] for i in range(19)}
        groups['Preview'] = members[1:]
        self.app.handle({'type': 'groups', 'groups': groups})
        self.app.handle({'type': 'users', 'users': ['Alice'] + [f'User{i:02}' for i in range(20)]})
        for width, height in ((1100, 680), (820, 520)):
            with self.subTest(size=(width, height)):
                self.root.geometry(f'{client.px(width)}x{client.px(height)}')
                self.root.update()
                self.assertEqual((self.root.winfo_width(), self.root.winfo_height()),
                                 (client.px(width), client.px(height)))
                self.assertTrue(self.app.online_label.winfo_ismapped())
                self.app.group_list.canvas.yview_moveto(1)
                last_group = self.button_for(self.app.group_box, '#  Preview')
                self.assertTrue(self.visible(self.app.group_list, last_group))
                last_group.invoke()
                self.assertEqual(self.app.active, ('group', 'Preview'))
                self.app.core.join_group.assert_not_called()
                self.assertEqual(str(self.app.send_button.cget('state')), 'disabled')
                self.root.update()
                self.app.member_list.canvas.yview_moveto(1)
                final_row = self.app.member_list.content.winfo_children()[-1]
                self.assertTrue(self.visible(self.app.member_list, final_row))
                self.assertEqual(next(w.cget('text') for w in final_row.winfo_children()
                                      if isinstance(w, tk.Label)), 'Member24')
                self.app.user_list.canvas.yview_moveto(1)
                last_user = self.button_for(self.app.user_box, 'User19')
                self.assertTrue(self.visible(self.app.user_list, last_user))
                last_user.invoke()
                self.assertEqual(self.app.active, ('pm', 'User19'))
        self.app.show(('pm', 'User00'), 'User00', 'hello')
        self.root.update()
        row = self.button_for(self.app.user_box, 'User00').master
        self.assertIn(' 1 ', [w.cget('text') for w in row.winfo_children() if isinstance(w, tk.Label)])
        self.app.core.join_group.assert_not_called()
        self.app.core.group_msg.assert_not_called()

    def test_vertical_scrollbar_command_and_feedback(self):
        self.app.handle({'type': 'users', 'users': ['Alice'] + [f'User{i:02}' for i in range(30)]})
        area = self.app.user_list
        self.root.update()
        self.assertEqual(area.canvas.yview()[0], 0.0)
        before = area.vertical.get()
        command = area.vertical.cget('command')
        self.assertTrue(command)
        self.root.tk.call(command, 'moveto', 1.0)
        self.root.update()
        after = area.vertical.get()
        self.assertGreater(area.canvas.yview()[0], 0.0)
        self.assertNotEqual(before, after)
        self.assertAlmostEqual(after[0], area.canvas.yview()[0])
        self.assertAlmostEqual(after[1], area.canvas.yview()[1])
        self.assertTrue(self.visible(area, self.button_for(self.app.user_box, 'User29')))

    def test_local_wheel_keyboard_and_focus(self):
        self.app.handle({'type': 'users', 'users': ['Alice'] + [f'User{i:02}' for i in range(30)]})
        area = self.app.user_list
        self.root.update()
        area.canvas.yview_moveto(0)
        area.canvas.event_generate('<MouseWheel>', delta=-120)
        self.root.update()
        self.assertGreater(area.canvas.yview()[0], 0)
        area.canvas.focus_force()
        before = area.canvas.yview()[0]
        area.canvas.event_generate('<Next>')
        self.root.update()
        self.assertGreater(area.canvas.yview()[0], before)
        last = self.button_for(self.app.user_box, 'User29')
        last.focus_force()
        self.assertTrue(self.visible(area, last))
        self.assertIs(self.root.focus_get(), last)
        self.assertEqual(self.root.bind_all('<MouseWheel>'), '')

if __name__ == '__main__':
    unittest.main()

