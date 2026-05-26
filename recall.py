"""
recall.py
"""
import base64
import requests
from config import RECALL_API_KEY, RECALL_REGION, PUBLIC_WS_URL

RECALL_BASE = f"https://{RECALL_REGION}.recall.ai/api/v1"

current_bot_id: str | None = None
BOT_NAME = "Malena - Mi Gestión Web"


def create_bot(meeting_url: str, bot_name: str = BOT_NAME) -> dict:
    global current_bot_id

    payload = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "variant": {
            "zoom": "web_4_core",
            "google_meet": "web_4_core",
            "microsoft_teams": "web_4_core",
        },
        "recording_config": {
            # Audio separado por participante — solo llega audio humano, no el de Malena
            "audio_separate_raw": {},
            "realtime_endpoints": [
                {
                    "type": "websocket",
                    "url": PUBLIC_WS_URL,
                    "events": ["audio_separate_raw.data"],
                }
            ],
        },
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


def bot_stop(bot_id: str | None = None) -> bool:
    """Interrumpe el audio actual mandando un MP3 de silencio muy corto."""
    bid = bot_id or current_bot_id
    if not bid:
        return False

    silence = bytes([0xFF, 0xFB, 0x90, 0x00] + [0x00] * 52)
    b64_silence = base64.b64encode(silence).decode()

    try:
        response = requests.post(
            f"{RECALL_BASE}/bot/{bid}/output_audio/",
            headers={
                "Authorization": RECALL_API_KEY,
                "Content-Type": "application/json",
            },
            json={"kind": "mp3", "b64_data": b64_silence},
            timeout=5,
        )
        if response.status_code == 200:
            print("[Recall] Audio cortado ✓")
            return True
        else:
            print(f"[Recall] ERROR bot_stop: {response.status_code}")
            return False
    except Exception as e:
        print(f"[Recall] ERROR bot_stop: {e}")
        return False


def get_bot_status(bot_id: str | None = None) -> dict:
    bid = bot_id or current_bot_id
    response = requests.get(
        f"{RECALL_BASE}/bot/{bid}/",
        headers={"Authorization": RECALL_API_KEY},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def _silence_mp3_b64() -> str:
    silence = bytes([0xFF, 0xFB, 0x90, 0x00] + [0x00] * 413)
    return base64.b64encode(silence).decode()