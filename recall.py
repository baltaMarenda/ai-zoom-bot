"""
recall.py
Interacción con la API de Recall.ai:
- Crear bot y mandarlo a una reunión
- Enviar audio al bot (output_audio)
"""
import base64
import requests
from config import RECALL_API_KEY, RECALL_REGION, PUBLIC_WS_URL

RECALL_BASE = f"https://{RECALL_REGION}.recall.ai/api/v1"

# Bot ID activo (se setea al crear el bot)
current_bot_id: str | None = None


def create_bot(meeting_url: str, bot_name: str = "Malena - Mi Gestión Web") -> dict:
    """
    Crea un bot de Recall.ai y lo manda a la reunión.
    Configura el WebSocket para recibir audio en tiempo real.
    """
    global current_bot_id

    payload = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "recording_config": {
            # Audio mezclado de todos los participantes, en tiempo real
            "audio_mixed_raw": {},
            "realtime_endpoints": [
                {
                    "type": "websocket",
                    "url": PUBLIC_WS_URL,          # tu FastAPI /audio
                    "events": ["audio_mixed_raw.data"],
                }
            ],
        },
        # Necesario para poder llamar a output_audio después
        "automatic_audio_output": {
            "in_call_recording": {
                "data": {
                    "kind": "mp3",
                    "b64_data": _silence_mp3_b64(),
                }
            }
        },
    }

    response = requests.post(
        f"{RECALL_BASE}/bot/",
        headers={
            "Authorization": RECALL_API_KEY,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    current_bot_id = data["id"]
    print(f"[Recall] Bot creado: {current_bot_id}")
    return data


def bot_speak(mp3_bytes: bytes, bot_id: str | None = None) -> bool:
    """
    Envía audio MP3 al bot para que lo reproduzca en la reunión.
    Reemplaza el afplay de tu código local.
    """
    bid = bot_id or current_bot_id
    if not bid:
        print("[Recall] ERROR: no hay bot_id activo")
        return False

    b64_audio = base64.b64encode(mp3_bytes).decode()
    response = requests.post(
        f"{RECALL_BASE}/bot/{bid}/output_audio/",
        headers={
            "Authorization": RECALL_API_KEY,
            "Content-Type": "application/json",
        },
        json={"kind": "mp3", "b64_data": b64_audio},
        timeout=15,
    )

    if response.status_code == 200:
        print("[Recall] Audio enviado al bot ✓")
        return True
    else:
        print(f"[Recall] ERROR output_audio: {response.status_code} - {response.text}")
        return False


def get_bot_status(bot_id: str | None = None) -> dict:
    """Consulta el estado del bot (joining, in_call, done, etc.)."""
    bid = bot_id or current_bot_id
    response = requests.get(
        f"{RECALL_BASE}/bot/{bid}/",
        headers={"Authorization": RECALL_API_KEY},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def _silence_mp3_b64() -> str:
    """
    MP3 de silencio mínimo en base64.
    Requerido para activar automatic_audio_output al crear el bot.
    """
    # Header MP3 válido (frame de silencio)
    silence = bytes([0xFF, 0xFB, 0x90, 0x00] + [0x00] * 413)
    return base64.b64encode(silence).decode()