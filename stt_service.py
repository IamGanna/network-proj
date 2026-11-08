"""Server-only Typhoon speech-to-text. Clients send audio over the chat socket; only the server holds the key."""
import json
import urllib.error
import urllib.request
import uuid

TYPHOON_URL = "https://api.opentyphoon.ai/v1/audio/transcriptions"


class SttError(Exception):
    """Its message is safe to show the user."""


def transcribe(wav, key, *, timeout=15):
    """Return the text Typhoon hears in a WAV recording."""
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\ntyphoon-asr-edge\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
            'Content-Type: audio/wav\r\n\r\n').encode() + wav + f'\r\n--{boundary}--\r\n'.encode()
    request = urllib.request.Request(TYPHOON_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = json.loads(response.read(65_536)).get("text", "")
    except urllib.error.HTTPError as exc:
        raise SttError(f"The speech service refused the request (HTTP {exc.code})") from None
    except (OSError, ValueError, AttributeError):
        raise SttError("The speech service is unreachable or sent a bad answer") from None
    if not isinstance(text, str):
        raise SttError("The speech service sent a bad answer")
    return text.strip()
