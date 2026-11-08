import gc
import tkinter as tk
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import client
from tests.support import pump


class AiClientTests(unittest.TestCase):
    def setUp(self):
        gc.collect()  # free earlier tests' Tk objects here, on the main thread, not later on a worker (Tcl_AsyncDelete)
        self.root = tk.Tk()
        with patch.object(client, 'ChatCore', return_value=MagicMock()):
            self.app = client.ChatClient(self.root)
        self.app.name = 'Alice'
        self.app.login_frame.destroy()
        self.app.build_ui()
        self.app.handle(dict(type='users',users=['Alice','Bob','Carol']))
        self.app.handle(dict(type='groups',groups={'G':['Alice'], 'Preview':['Bob']}))
        self.app.core.request_ai.return_value = True
        self.app.select(('pm','Bob'))
        self.root.update_idletasks()

    def tearDown(self):
        if not self.app.closed:
            self.root.update_idletasks()
            self.app.close()

    def event(self, *, kind='ai_result', **changes):
        pending = self.app.pending_ai
        event = dict(type=kind,request_id=pending['request_id'],action=pending['action'],
                     room_type=pending['room'][0],room=pending['room'][1])
        event.update(dict(code='budget',text='AI budget exhausted') if kind=='ai_error' else
                     dict(text='short reply', message_count=2,cutoff=3))
        return event | changes

    def test_draft_freezes_only_requested_room_and_never_sends(self):
        self.app.entry.insert(0,'  original  ')
        self.app.start_ai('draft','brief')
        self.assertEqual(str(self.app.entry['state']), 'disabled')
        self.app.send_text()
        self.app.core.pm.assert_not_called()
        event = self.event()
        self.app.select(('pm','Carol'))
        self.assertEqual(str(self.app.entry['state']),'normal')
        self.app.entry.insert(0,'Carol text')
        self.app.handle(dict(type='groups',groups={'G':['Alice','Bob']}))
        self.app.handle(event)
        self.assertEqual(self.app.entry.get(),'Carol text')
        self.assertEqual(self.app.active,('pm','Carol'))
        self.app.select(('pm','Bob'))
        self.assertEqual(self.app.entry.get(),'short reply')
        self.app.core.pm.assert_not_called()
        self.app.handle(event)  # duplicate must not replace subsequent typing
        self.app.entry.insert('end',' edit')
        self.assertEqual(self.app.entry.get(),'short reply edit')

    def test_error_timeout_and_local_failure_restore_exact_draft(self):
        for outcome in ('error','timeout','send_failure'):
            self.app.entry.delete(0,'end'); self.app.entry.insert(0,'  exact ไทย  ')
            self.app.core.request_ai.return_value = outcome != 'send_failure'
            self.app.start_ai('draft')
            if outcome=='error': self.app.handle(self.event(kind='ai_error'))
            if outcome=='timeout': self.app.finish_ai_error('AI timed out.')
            if outcome=='send_failure': pump(self.root, lambda:self.app.pending_ai is None)
            self.assertIsNone(self.app.pending_ai)
            self.assertEqual(self.app.entry.get(),'  exact ไทย  ')
            self.assertEqual(str(self.app.entry['state']),'normal')

    def test_summary_is_private_readonly_and_preserves_typing(self):
        self.app.entry.insert(0,'my draft')
        self.app.start_ai('summary')
        self.app.entry.insert('end',' more')
        self.app.handle(self.event(text='Topic: demo\nPeople: Bob\nState: unresolved'))
        self.assertEqual(self.app.entry.get(),'my draft more')
        dialog = next(iter(self.app.ai_dialogs))
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        box = next(w for w in descendants(dialog) if isinstance(w,tk.Text))
        self.assertEqual(str(box['state']),'disabled')
        self.assertIn('unresolved',box.get('1.0','end'))
        self.assertNotIn('unresolved',self.app.rooms[('pm','Bob')].get('1.0','end'))

    def test_mismatched_result_is_ignored_and_membership_cancels(self):
        self.app.select(('group','G')); self.app.entry.insert(0,'group draft')
        self.app.start_ai('draft')
        self.app.handle(self.event(room='Other'))
        self.assertIsNotNone(self.app.pending_ai)
        self.app.handle(dict(type='groups',groups={'G':['Bob']}))
        self.assertIsNone(self.app.pending_ai)
        self.assertEqual(self.app.entry.get(),'group draft')
        self.assertEqual(str(self.app.entry['state']),'disabled')
        self.app.select(('group','Preview'))
        self.assertFalse(self.app.start_ai('summary'))

    def test_group_timeout_expiry_preserves_pending_draft_lock(self):
        self.app.select(('group', 'G'))
        self.app.entry.insert(0, 'my draft')
        self.assertTrue(self.app.start_ai('draft'))
        with patch.object(client.time, 'time', return_value=100):
            self.app.handle(dict(type='timeout_notice', group='G', target='Alice', seconds=10))
            self.assertEqual(str(self.app.file_button['state']), 'disabled')
            self.assertIn('timed out', self.app.subtitle['text'])
        with patch.object(client.time, 'time', return_value=110):
            self.app.check_timeout_countdown()
        self.assertEqual(self.app.subtitle['text'], 'Only members see these messages')
        self.assertEqual(str(self.app.file_button['state']), 'normal')
        self.assertEqual(str(self.app.entry['state']), 'disabled')
        self.assertEqual(str(self.app.send_button['state']), 'disabled')
        self.assertEqual(self.app.entry.get(), 'my draft')

    def test_instruction_dialog_captures_room_and_accepts_empty_instruction(self):
        self.app.draft_reply()
        dialog = self.app.instruction_dialog
        self.app.select(('pm','Carol'))
        self.app.instruction_generate.invoke()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.app.pending_ai['room'],('pm','Bob'))
        self.app.core.request_ai.assert_called_once_with(self.app.pending_ai['request_id'], 'draft','pm','Bob','')
        self.assertEqual(str(self.app.entry['state']),'normal')

    def test_close_cancels_owned_timer_and_dialogs(self):
        self.app.start_ai('draft')
        timer = self.app.pending_ai['timer']
        self.app.close()
        self.assertIsNone(self.app.pending_ai)
        self.assertNotIn(timer,self.root.tk.call('after','info'))

    def test_slow_local_write_never_blocks_tk_and_has_one_pending_writer(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set(); release.wait(2)
            return False
        self.app.core.request_ai.side_effect=slow
        self.app.entry.insert(0,'  keep this  ')
        try:
            before=time.monotonic()
            self.app.start_ai('draft')
            self.assertLess(time.monotonic()-before,.1)
            self.assertTrue(entered.wait(1))
            ticks=[]; self.root.after(10,lambda:ticks.append(True))
            pump(self.root,lambda:bool(ticks),timeout=.2)
            self.assertIsNotNone(self.app.pending_ai['timer'])
            self.app.finish_ai_error('Timed out')
            self.assertFalse(self.app.start_ai('summary'))
        finally:
            release.set()
        pump(self.root,lambda:self.app.ai_send_worker is None)
        self.assertEqual(self.app.entry.get(),'  keep this  ')

    def test_instruction_is_not_lost_to_overlapping_summary(self):
        self.app.draft_reply()
        dialog=self.app.instruction_dialog
        instruction=next(w for w in dialog.winfo_children() if isinstance(w,tk.Entry))
        instruction.insert(0,'ask about tomorrow')
        self.assertFalse(self.app.start_ai('summary'))
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(instruction.get(),'ask about tomorrow')
        self.app.instruction_generate.invoke()
        self.assertEqual(self.app.pending_ai['action'],'draft')
