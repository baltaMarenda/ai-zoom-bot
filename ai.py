"""
ai.py
Lógica de OpenAI (ask_ai) y ElevenLabs TTS (speak).
Extraído de tu stt_test.py original, sin cambios de lógica.
"""
import requests
from openai import OpenAI
from config import (
    OPENAI_API_KEY,
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    SYSTEM_PROMPT,
)

client = OpenAI(api_key=OPENAI_API_KEY)

# Memoria conversacional (igual que en tu código original)
conversation = []


def ask_ai(text: str) -> str:
    """
    Manda el transcript a OpenAI y devuelve la respuesta de Malena.
    Usa el mismo patrón que tu stt_test.py: responses.create con instructions.
    """
    conversation.append({"role": "user", "content": text})

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=conversation,
        instructions=SYSTEM_PROMPT,
    )
    reply = response.output_text
    conversation.append({"role": "assistant", "content": reply})

    # Limitar memoria a últimas 20 interacciones (evita contexto infinito)
    if len(conversation) > 20:
        conversation.pop(1)
        conversation.pop(1)

    return reply


def text_to_speech(text: str) -> bytes:
    """
    ElevenLabs TTS → devuelve bytes MP3.
    HTTP directo, sin SDK (igual que tu código original).
    """
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    data = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }
    response = requests.post(url, json=data, headers=headers, timeout=30)
    response.raise_for_status()
    return response.content  # bytes MP3


def reset_conversation():
    """Limpia la memoria conversacional (útil entre reuniones)."""
    conversation.clear()