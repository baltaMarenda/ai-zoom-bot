"""
recall.py
"""
import base64
import requests
from config import RECALL_API_KEY, RECALL_REGION, PUBLIC_WS_URL, PUBLIC_BASE_URL

RECALL_BASE = f"https://{RECALL_REGION}.recall.ai/api/v1"

current_bot_id: str | None = None
BOT_NAME = "Malena - Mi Gestión Web"


def create_bot(meeting_url: str, bot_name: str = BOT_NAME) -> dict:
    global current_bot_id

    # URL pública de la webpage del agente
    agent_url = f"{PUBLIC_BASE_URL}/agent"

    payload = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "variant": {
            "zoom": "web_4_core",
            "google_meet": "web_4_core",
            "microsoft_teams": "web_4_core",
        },
        # Output Media: el bot corre la webpage y la muestra como cámara
        # NOTA: output_media es incompatible con automatic_audio_output y output_audio endpoint.
        # Todo el audio ahora se maneja desde la webpage via WebSocket /agent-ws.
        "output_media": {
            "camera": {
                "kind": "webpage",
                "config": {
                    "url": agent_url,
                },
            }
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
    if not response.ok:
        print(f"[Recall] Error {response.status_code}: {response.text}")
    response.raise_for_status()
    data = response.json()
    current_bot_id = data["id"]
    print(f"[Recall] Bot creado con Output Media: {current_bot_id}")
    print(f"[Recall] Webpage del agente: {agent_url}")
    return data


def get_bot_status(bot_id: str | None = None) -> dict:
    bid = bot_id or current_bot_id
    response = requests.get(
        f"{RECALL_BASE}/bot/{bid}/",
        headers={"Authorization": RECALL_API_KEY},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()