import io
import logging
from typing import Optional
from gtts import gTTS
from core.app_utils import LANGUAGE_CODE_TO_NAME

NAME_TO_CODE = {v: k for k, v in LANGUAGE_CODE_TO_NAME.items()}

# Mapping from app_utils language codes to gTTS language codes
# Some regional languages might not be supported directly by gTTS, we try our best.
GTTS_LANG_MAPPING = {
    "as": "bn", # Assamese might fallback to Bengali sounding voice if unsupported, or just raise error
}

def generate_audio(text: str, language_name: str) -> bytes:
    """
    Generate Text-to-Speech audio from text using gTTS.
    Returns the audio bytes (mp3 format) or None if unsupported/failed.
    """
    if not text:
        return None
        
    try:
        lang_code = NAME_TO_CODE.get(language_name, "en")
        gtts_lang = GTTS_LANG_MAPPING.get(lang_code, lang_code)
        
        tts = gTTS(text=text, lang=gtts_lang, slow=False)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except Exception as e:
        logging.error(f"Error generating TTS for {language_name}: {e}")
        return None

def transcribe_audio(audio_bytes: bytes, client=None, language: Optional[str] = None) -> str:
    """
    Transcribe Speech-to-Text audio bytes using OpenAI Whisper API.
    Returns the transcribed text.
    """
    if not audio_bytes:
        return ""
        
    if client is None:
        from core.app_utils import get_client
        client = get_client()
        
    if not client:
        logging.error("No API client available for transcription.")
        return ""
        
    try:
        # Create a file-like object with a name attribute required by OpenAI API
        file_obj = io.BytesIO(audio_bytes)
        file_obj.name = "audio.wav"
        
        kwargs = {
            "model": "whisper-1",
            "file": file_obj,
            "response_format": "text"
        }
        if language:
            kwargs["language"] = language
            
        response = client.audio.transcriptions.create(**kwargs)
        return response.strip()
    except Exception as e:
        logging.error(f"Error transcribing audio: {e}")
        return ""
