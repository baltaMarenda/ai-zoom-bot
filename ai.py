"""
ai.py
"""
import requests
import json
from openai import OpenAI
from config import (
    OPENAI_API_KEY,
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    PROMPTS_BY_STAGE,   # ← cambio
)

client = OpenAI(api_key=OPENAI_API_KEY)
conversation = []


def ask_ai(text: str, stage: str = "intro") -> str:
    """
    Manda el transcript a OpenAI y devuelve la respuesta de Malena.
    El system prompt cambia según el estado actual.
    """
    conversation.append({"role": "user", "content": text})

    system_prompt = PROMPTS_BY_STAGE.get(stage, PROMPTS_BY_STAGE["intro"])

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=conversation,
        instructions=system_prompt,
    )
    reply = response.output_text
    conversation.append({"role": "assistant", "content": reply})

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

def extract_lead_info(conversation_text: str) -> dict:
    """
    Usa OpenAI para extraer nombre y negocio del texto de la conversación.
    Devuelve {"nombre": "...", "negocio": "..."} o None en cada campo.
    """
    response = client.responses.create(
        model="gpt-4.1-mini",
        max_output_tokens=100,
        instructions="""
Sos un extractor de datos. Del texto que te paso, extraé:
- nombre: el nombre propio de la persona (solo el nombre, sin apellido si no lo dice)
- negocio: el tipo o rubro del negocio que mencionó

Respondé SOLO con JSON válido, sin explicaciones ni markdown.
Ejemplo: {"nombre": "Baltazar", "negocio": "carnicería"}
Si no encontrás algún dato, usá null.
""",
        input=[{"role": "user", "content": conversation_text}],
    )
    try:
        return json.loads(response.output_text)
    except Exception:
        return {"nombre": None, "negocio": None}