"""Actual accept loop, cores and Tk UI; only external model generation is fake."""
import tkinter as tk
import unittest
from unittest.mock import patch
import client
import server
from tests.support import pump
from tests import test_ai_server


class AiEndToEndTests(unittest.TestCase):
    setUp = test_ai_server.AiServerTests.setUp
    tearDown = test_ai_server.AiServerTests.tearDown
    connect = test_ai_server.AiServerTests.connect

    def test_real_ui_draft_is_private_and_summary_keeps_chat_responsive(self):
        bob, be = self.connect('Bob')
        carol, ce = self.connect('Carol')
        root = tk.Tk()
        with patch.object(client,'PORT',self.peer.port), patch.object(client,'HOST','127.0.0.1'):
            app = client.ChatClient(root)
        try:
            app.name_entry.insert(0,'Alice')
            app.login()
            pump(root,lambda:getattr(app,'name',None)=='Alice' and 'Bob' in app.users)
            bob.pm('Alice','พรุ่งนี้คุยเรื่อง demo ได้ไหม?')
            pump(root,lambda:('pm','Bob') in app.rooms)
            app.select(('pm','Bob'))
            app.entry.insert(0,'old draft')
            self.assertTrue(app.start_ai('draft','ask for a time'))
            pump(root,lambda:app.pending_ai is None)
            self.assertEqual(app.entry.get(),'Private AI result')
            self.assertNotIn('Private AI result',app.rooms[('pm','Bob')].get('1.0','end'))
            for core, events in [(bob,be),(carol,ce)]:
                core.pm(core.name,'barrier'); events.wait(lambda m:m.get('text')=='barrier')
                self.assertFalse(any(m.get('text')=='Private AI result' for m in events.seen))
            # Summary must not freeze typing or TCP reception.
            with server.lock: server.clients['Alice'].next_ai_at=0
            self.service.entered.clear(); self.service.gate.clear()
            self.assertTrue(app.start_ai('summary'))
            self.assertTrue(self.service.entered.wait(1))
            ticks=[]; root.after(10,lambda:ticks.append(True))
            bob.pm('Alice','Another normal message')
            pump(root,lambda:ticks and 'Another normal message' in app.rooms[('pm','Bob')].get('1.0','end'))
            self.service.gate.set()
            pump(root,lambda:app.pending_ai is None)
            self.assertTrue(app.ai_dialogs)
            self.assertEqual(app.entry.get(),'Private AI result')
            # Only explicit Send adds AI draft text to the conversation.
            app.send_text()
            be.wait(lambda m:m.get('text')=='Private AI result' and m.get('from')=='Alice')
            pump(root,lambda:'Private AI result' in app.rooms[('pm','Bob')].get('1.0','end'))
        finally:
            self.service.gate.set()
            app.close()
