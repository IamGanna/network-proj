"""Speech-to-text goes through the server: only the server holds the Typhoon key."""
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import wave
from unittest import mock

import server
import stt_service
from ai_service import read_env_value
from chat_core import ChatCore
from tests.support import Events, LoopbackServer


def wav_bytes(seconds):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * int(16000 * seconds))
    return buf.getvalue()


class ServerSttTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(server.clients, {})
        self.calls = []
        self.heard = threading.Event()

        def fake_transcribe(audio, key):
            self.calls.append((audio, key))
            self.heard.set()
            return "สวัสดี hello"
        patches = [mock.patch.object(server, "stt_key", "server-key"),
                   mock.patch.object(stt_service, "transcribe", fake_transcribe)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
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

    def settle(self, core, events):
        """A PM echo proves the server already handled everything sent before it."""
        core.pm(core.name, "sync")
        events.wait(lambda m: m.get("type") == "pm" and m.get("text") == "sync")
        return [m for m in events.drain() if m.get("type", "").startswith("stt_")]

    def test_recording_comes_back_as_text_to_the_speaker_only(self):
        alice, ae = self.connect("Alice")
        bob, be = self.connect("Bob")
        wav = wav_bytes(3)  # 3 chunks
        self.assertTrue(alice.send_speech("rec1", wav))
        reply = ae.wait(lambda m: m.get("type") in ("stt_text", "stt_error"))
        self.assertEqual(reply, {"type": "stt_text", "stt_id": "rec1", "text": "สวัสดี hello"})
        self.assertEqual(self.calls, [(wav, "server-key")])
        self.assertEqual(self.settle(bob, be), [])

    def test_without_a_key_the_recording_is_refused_once(self):
        alice, ae = self.connect("Alice")
        with mock.patch.object(server, "stt_key", ""):
            self.assertTrue(alice.send_speech("rec1", wav_bytes(3)))
            replies = self.settle(alice, ae)
        self.assertEqual(replies, [{"type": "stt_error", "stt_id": "rec1",
                                    "text": "Speech-to-text is not set up on the server"}])
        self.assertEqual(self.calls, [])

    def test_too_long_recording_is_refused_without_calling_typhoon(self):
        alice, ae = self.connect("Alice")
        with mock.patch.object(server, "MAX_SPEECH_BYTES", 50_000):
            self.assertTrue(alice.send_speech("rec1", wav_bytes(3)))
            replies = self.settle(alice, ae)
        self.assertEqual([m["text"] for m in replies], ["Recording is longer than 60 seconds"])
        self.assertEqual(self.calls, [])

    def test_non_wav_data_is_refused(self):
        alice, ae = self.connect("Alice")
        self.assertTrue(alice.send_speech("rec1", os.urandom(1000)))
        self.assertEqual([m["text"] for m in self.settle(alice, ae)], ["Audio must be a WAV recording"])
        self.assertEqual(self.calls, [])

    def test_a_second_recording_works_after_the_first(self):
        alice, ae = self.connect("Alice")
        for sid in ("rec1", "rec2"):
            self.assertTrue(alice.send_speech(sid, wav_bytes(1)))
            ae.wait(lambda m: m.get("type") == "stt_text" and m["stt_id"] == sid)
        self.assertEqual(len(self.calls), 2)


class ClientMicTests(unittest.TestCase):
    def setUp(self):
        import gc
        import tkinter as tk
        import client
        gc.collect()  # free earlier tests' Tk objects on the main thread
        self.client = client
        self.root = tk.Tk()
        with mock.patch.object(client, "ChatCore", return_value=mock.MagicMock()):
            self.app = client.ChatClient(self.root)
        self.app.name = "Alice"
        self.app.login_frame.destroy()
        self.app.build_ui()
        self.app.handle(dict(type="users", users=["Alice", "Bob"]))
        self.app.select(("pm", "Bob"))

    def tearDown(self):
        if not self.app.closed:
            self.app.close()

    def record(self, wav):
        with mock.patch.object(self.client.voice, "start_recording"), \
             mock.patch.object(self.client.voice, "stop_recording", return_value=wav):
            self.app._toggle_mic()
            self.app._toggle_mic()

    def test_recording_is_sent_and_the_reply_fills_the_message_box(self):
        sent = threading.Event()
        self.app.core.send_speech.side_effect = lambda sid, wav: sent.set() or True
        self.record(b"RIFF....WAVE")
        self.assertTrue(sent.wait(2))
        sid, wav = self.app.core.send_speech.call_args.args
        self.assertEqual(wav, b"RIFF....WAVE")
        self.assertEqual(self.app.mic_btn.cget("text"), "⏳")
        self.app.handle(dict(type="stt_text", stt_id="old-recording", text="ignored"))
        self.app.handle(dict(type="stt_text", stt_id=sid, text="สวัสดี"))
        self.assertEqual(self.app.entry.get(), "สวัสดี")
        self.assertEqual(self.app.mic_btn.cget("text"), "🎤")

    def test_speaker_reads_private_and_group_messages_from_others(self):
        self.app.handle(dict(type="groups", groups={"Team": ["Alice", "Bob"]}))
        self.app._toggle_speak()
        with mock.patch.object(self.client.voice, "speak") as speak:
            self.app.handle(dict(type="pm", **{"from": "Bob"}, to="Alice", text="hi from Bob"))
            self.app.handle(dict(type="group_msg", group="Team", **{"from": "Bob"}, text="team hi"))
            self.app.handle(dict(type="group_msg", group="Team", **{"from": "Alice"}, text="my own"))
        self.assertEqual([c.args[0] for c in speak.call_args_list], ["hi from Bob", "team hi"])

    def test_too_long_recording_is_not_sent(self):
        self.record(b"x" * (self.client.MAX_SPEECH_BYTES + 1))
        self.app.core.send_speech.assert_not_called()
        self.assertEqual(self.app.mic_btn.cget("text"), "🎤")


class TyphoonRequestTests(unittest.TestCase):
    def test_request_carries_key_model_and_audio(self):
        sent = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def fake_urlopen(request, timeout):
            sent["request"] = request
            return Response(json.dumps({"text": "  ok  "}).encode())
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(stt_service.transcribe(b"RIFF-audio", "k123"), "ok")
        request = sent["request"]
        self.assertEqual(request.get_header("Authorization"), "Bearer k123")
        self.assertIn(b"typhoon-asr-edge", request.data)
        self.assertIn(b"RIFF-audio", request.data)

    def test_http_error_becomes_a_safe_message(self):
        error = urllib.error.HTTPError(stt_service.TYPHOON_URL, 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(stt_service.SttError, "HTTP 401"):
                stt_service.transcribe(b"RIFF", "bad")

    def test_server_reads_the_typhoon_key_from_env_file(self):
        path = os.path.join(tempfile.mkdtemp(), ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("OPENROUTER_API_KEY=a\nTYPHOON_API_KEY='typhoon-key'\n")
        self.assertEqual(read_env_value("TYPHOON_API_KEY", path, environ={}), "typhoon-key")
        self.assertEqual(read_env_value("TYPHOON_API_KEY", path, environ={"TYPHOON_API_KEY": " env "}), "env")
