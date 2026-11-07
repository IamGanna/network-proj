"""TTS (edge-tts + pygame) and STT (sounddevice + Typhoon API)."""
import asyncio
import io
import os
import tempfile
import threading
import wave

import edge_tts
import numpy as np
import pygame
import requests
import sounddevice as sd

_VOICE_TH = "th-TH-NiwatNeural"
_VOICE_EN = "en-US-AriaNeural"
_SR = 16000
_TYPHOON_URL = "https://api.opentyphoon.ai/v1/audio/transcriptions"

_stream = None
_frames = []
_frames_lock = threading.Lock()


def speak(text):
    """Fire-and-forget TTS; returns immediately."""
    def _run():
        voice = _VOICE_TH if any("฀" <= c <= "๿" for c in text) else _VOICE_EN

        async def _save(path):
            await edge_tts.Communicate(text, voice=voice).save(path)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        try:
            asyncio.run(_save(path))
            pygame.mixer.init()
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            import time
            while pygame.mixer.music.get_busy():
                time.sleep(0.05)
        except Exception as e:
            print(f"[voice] TTS error: {e}")
        finally:
            try:
                pygame.mixer.quit()
                os.unlink(path)
            except OSError:
                pass

    threading.Thread(target=_run, daemon=True).start()


def start_recording():
    """Open mic stream and start collecting frames."""
    global _stream, _frames
    with _frames_lock:
        _frames = []

    def _callback(indata, frames, time, status):
        with _frames_lock:
            _frames.append(indata.copy())

    _stream = sd.InputStream(samplerate=_SR, channels=1, dtype="int16", callback=_callback)
    _stream.start()


def stop_recording(on_done):
    """Stop recording, transcribe, call on_done(text) on a background thread."""
    global _stream
    if _stream is None:
        on_done("")
        return

    _stream.stop()
    _stream.close()
    _stream = None

    with _frames_lock:
        frames = list(_frames)

    def _transcribe():
        try:
            if not frames:
                on_done("")
                return
            audio = np.concatenate(frames, axis=0)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(_SR)
                w.writeframes(audio.tobytes())
            buf.seek(0)
            key = os.getenv("TYPHOON_API_KEY", "")
            r = requests.post(
                _TYPHOON_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("speech.wav", buf, "audio/wav")},
                data={"model": "typhoon-asr-edge"},
                timeout=15,
            )
            on_done(r.json().get("text", ""))
        except Exception as e:
            print(f"[voice] STT error: {e}")
            on_done("")

    threading.Thread(target=_transcribe, daemon=True).start()
