"""
recall.py
"""
import base64
import requests
from config import (
    RECALL_API_KEY, RECALL_REGION, PUBLIC_WS_URL, PUBLIC_BASE_URL,
    RECALL_EVERYONE_LEFT_TIMEOUT_S, RECALL_NOONE_JOINED_TIMEOUT_S,
    RECALL_WAITING_ROOM_TIMEOUT_S,
)

RECALL_BASE = f"https://{RECALL_REGION}.recall.ai/api/v1"

current_bot_id: str | None = None
BOT_NAME = "Malena - Mi Gestión Web"


def _with_sid(url: str, sid: str | None) -> str:
    """Agrega ?sid=... (o &sid=...) a una URL para rutear el WS/webpage a su sesión."""
    if not sid:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}sid={sid}"


def create_bot(meeting_url: str, bot_name: str = BOT_NAME, sid: str | None = None) -> dict:
    global current_bot_id

    # URL pública de la webpage del agente — con sid para que agent.html se conecte
    # al /agent-ws de ESTA sesión.
    agent_url = _with_sid(f"{PUBLIC_BASE_URL}/agent", sid)
    ws_url    = _with_sid(PUBLIC_WS_URL, sid)

    # El bot abandona la llamada solo cuando la reunión queda vacía / nadie entra /
    # queda en sala de espera. Al abandonar se cierra el WS de audio y el teardown
    # libera la credencial. Solo incluimos los timeouts con valor > 0.
    automatic_leave = {
        k: v for k, v in {
            "everyone_left_timeout": RECALL_EVERYONE_LEFT_TIMEOUT_S,
            "noone_joined_timeout":  RECALL_NOONE_JOINED_TIMEOUT_S,
            "waiting_room_timeout":  RECALL_WAITING_ROOM_TIMEOUT_S,
        }.items() if v and v > 0
    }

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
                    "url": ws_url,
                    "events": ["audio_separate_raw.data"],
                }
            ],
        },
    }
    if automatic_leave:
        payload["automatic_leave"] = automatic_leave

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


def leave_call(bot_id: str | None = None) -> dict:
    bid = bot_id or current_bot_id
    response = requests.post(
        f"{RECALL_BASE}/bot/{bid}/leave_call/",
        headers={"Authorization": RECALL_API_KEY},
        timeout=10,
    )
    response.raise_for_status()
    if response.status_code == 204:
        return {"status": "left"}
    try:
        return response.json()
    except Exception:
        return {"status": "left"}