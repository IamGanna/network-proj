"""Without the voice packages (voice is None) the chat still works and the mic/speaker buttons are hidden."""
import gc
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

import client


class VoiceOptionalTests(unittest.TestCase):
    def test_no_voice_packages_hides_buttons_and_reads_messages(self):
        gc.collect()  # see test_ai_client: keep Tk cleanup on the main thread
        root = tk.Tk()
        with patch.object(client, "voice", None), patch.object(client, "ChatCore", return_value=MagicMock()):
            app = client.ChatClient(root)
            try:
                app.name = "Alice"
                app.login_frame.destroy()
                app.build_ui()
                app.handle({"type": "users", "users": ["Alice", "Bob"]})
                buttons = [w.cget("text") for w in app.send_button.master.winfo_children()
                           if isinstance(w, tk.Button)]
                self.assertNotIn("🎤", buttons)
                self.assertNotIn("🔊", buttons)
                app.handle({"type": "pm", "from": "Bob", "to": "Alice", "text": "hi"})
                self.assertIn("hi", app.rooms[("pm", "Bob")].get("1.0", "end"))
            finally:
                app.close()
