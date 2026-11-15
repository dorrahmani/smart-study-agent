"""generators/tts_gemini.py — Hebrew TTS via Gemini.

Uses gemini-2.5-flash-preview-tts (or 2.5-pro-tts) to synthesize Hebrew speech.
Returns path to an MP3/WAV file ready for Telegram voice send.
"""
from __future__ import annotations

import os
import tempfile
import wave
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def synthesize(text: str, voice: str = 'Sulafat', out_path: str | None = None,
               model: str = 'gemini-2.5-flash-preview-tts') -> str:
    """Synthesize Hebrew speech. Returns path to a .wav file.

    Args:
        text: Hebrew text to speak. Long texts auto-truncated to ~5000 chars.
        voice: Gemini voice name. Defaults to 'Sulafat' (Hebrew-capable).
        out_path: target file path; if None, writes to a temp file.
    """
    from google import genai
    from google.genai import types as genai_types

    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        raise RuntimeError('GEMINI_API_KEY missing')
    client = genai.Client(api_key=key)

    # Cap text to avoid hitting model limits
    if len(text) > 5000:
        text = text[:4800] + '... (קוצר)'

    if out_path is None:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        out_path = str(Path(tempfile.gettempdir()) / f'tts_{ts}.wav')

    resp = client.models.generate_content(
        model=model,
        contents=text,
        config=genai_types.GenerateContentConfig(
            response_modalities=['AUDIO'],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=voice,
                    ),
                ),
            ),
        ),
    )
    try:
        from utils.usage_tracker import log_usage
        # Gemini TTS pricing: $10/$70 per 1M tokens (preview). Rough estimate.
        n_chars = len(text)
        in_tokens = max(1, n_chars // 4)
        log_usage(service='gemini', model=model,
                  input_tokens=in_tokens, output_tokens=in_tokens * 2,
                  op='tts.synthesize')
    except Exception:
        pass

    # Extract PCM data from the response
    audio_bytes = None
    for cand in (resp.candidates or []):
        for part in (cand.content.parts or []):
            inline = getattr(part, 'inline_data', None)
            if inline and inline.data:
                audio_bytes = inline.data; break
        if audio_bytes: break

    if not audio_bytes:
        raise RuntimeError('TTS returned no audio data')

    # Gemini TTS returns 24kHz mono PCM raw bytes — wrap as WAV
    with wave.open(out_path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)        # 16-bit
        wf.setframerate(24000)
        wf.writeframes(audio_bytes)
    return out_path


if __name__ == '__main__':
    import sys
    text = ' '.join(sys.argv[1:]) or 'שלום, זה ניסיון של סוכן הלימוד.'
    print(synthesize(text))
