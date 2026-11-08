"""TTS (edge-tts + pygame) and microphone recording (sounddevice). The server does speech-to-text."""
import asyncio
import io
import os
import queue
import tempfile
import threading
import time
import wave

import edge_tts
import numpy as np
import pygame
import sounddevice as sd

_VOICE_TH = "th-TH-NiwatNeural"
_VOICE_EN = "en-US-AriaNeural"
_SR = 16000

_stream = None
_frames = []
_frames_lock = threading.Lock()

_tts_queue = queue.Queue()


def _tts_worker():
    pygame.mixer.init()
    while True:
        text = _tts_queue.get()
        if text is None:
            break
        voice = _VOICE_TH if any("฀" <= c <= "๿" for c in text) else _VOICE_EN

        async def _save(path, v=voice, t=text):
            await edge_tts.Communicate(t, voice=v).save(path)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        try:
            asyncio.run(_save(path))
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.05)
        except Exception as e:
            print(f"[voice] TTS error: {e}")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        _tts_queue.task_done()
    pygame.mixer.quit()


threading.Thread(target=_tts_worker, daemon=True).start()


def speak(text):
    _tts_queue.put(text)


def stop():
    """Drain queued TTS and stop any current playback immediately."""
    while not _tts_queue.empty():
        try:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
        except queue.Empty:
            break
    if pygame.mixer.get_init():
        pygame.mixer.music.stop()


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


def stop_recording():
    """Stop the mic and return the recording as WAV bytes, or b"" if nothing was captured."""
    global _stream
    if _stream is None:
        return b""
    _stream.stop()
    _stream.close()
    _stream = None
    with _frames_lock:
        frames = list(_frames)
    if not frames:
        return b""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(np.concatenate(frames, axis=0).tobytes())
    return buf.getvalue()
