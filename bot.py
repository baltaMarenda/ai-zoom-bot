"""
bot.py
Pipeline de Malena — Realtime API bridge.
Audio: envía PCM16 via WebSocket a la webpage del agente.
Navegación: envía comandos navigate via WebSocket a la webpage del agente.
"""
import asyncio
import base64
import json
import traceback

from config import DEMO_MODULE_PATHS
from state import ConversationState, Stage
from mgw_playwright import (
    pw_start, pw_stop,
    demo_acceso_login,
    demo_caja_fase1_agregar, demo_caja_fase2_pagar,
    reset_caja_fases,
)

# ── Referencia al WebSocket de la webpage del agente ──────────────────────────
_agent_ws_list: list = []
_audio_done_event = asyncio.Event()


def set_agent_websocket(ws):
    global _agent_ws_list
    if ws is not None:
        _agent_ws_list.append(ws)
        print(f"[BOT] Agent WS conectado (total: {len(_agent_ws_list)})")


def remove_agent_websocket(ws):
    global _agent_ws_list
    try:
        _agent_ws_list.remove(ws)
    except ValueError:
        pass
    print(f"[BOT] Agent WS desconectado (restantes: {len(_agent_ws_list)})")


def on_agent_audio_done():
    """Llamado desde main.py cuando la webpage termina de reproducir audio."""
    _audio_done_event.set()


# ── Estado de conversación ────────────────────────────────────────────────────
conv_state = ConversationState()

# ── Sincronización entre módulos ──────────────────────────────────────────────
_acceso_login_done = asyncio.Event()
_fase2_complete_event = asyncio.Event()
_fase2_press_f8 = asyncio.Event()
_fase2_task_created = False

# ── Navegación MGW ────────────────────────────────────────────────────────────
_last_navigated_module: str = ""
_current_demo_module: str = ""
_module_locked: bool = False


# ── Helpers de comunicación con la webpage ────────────────────────────────────

async def _send_to_agent(msg: dict):
    if not _agent_ws_list:
        return
    dead = []
    for ws in list(_agent_ws_list):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            _agent_ws_list.remove(ws)
        except ValueError:
            pass


async def _send_audio(mp3_bytes: bytes):
    b64 = base64.b64encode(mp3_bytes).decode()
    await _send_to_agent({"type": "audio", "data": b64})


async def _send_navigate(path: str):
    await _send_to_agent({"type": "navigate", "path": path})


async def _send_reset():
    await _send_to_agent({"type": "reset"})


async def _send_stop_audio():
    await _send_to_agent({"type": "stop_audio"})


async def _send_logged_in():
    await _send_to_agent({"type": "logged_in"})


# ── Screenshots ───────────────────────────────────────────────────────────────

async def _on_screenshot(b64: str):
    await _send_to_agent({"type": "screenshot", "data": b64})


async def _on_screenshot_end():
    await _send_to_agent({"type": "screenshot_end"})


# ── Playwright helpers ────────────────────────────────────────────────────────

def unlock_demo_module():
    global _module_locked
    _module_locked = False


async def _run_acceso_demo() -> bool:
    print("[ACCESO] Iniciando demo de login...")
    ok = await demo_acceso_login(on_screenshot=_on_screenshot)
    await _send_to_agent({"type": "screenshot_end"})
    print(f"[ACCESO] Demo login {'✓' if ok else '✗'}")
    _acceso_login_done.set()
    return ok


async def _run_caja_fase1_inner() -> bool:
    if not _acceso_login_done.is_set():
        print("[CAJA] Esperando login de ACCESO...")
        try:
            await asyncio.wait_for(_acceso_login_done.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            print("[CAJA] Timeout esperando ACCESO — continuando igual")
    print("[CAJA] Iniciando Fase 1 (buscar + agregar)...")
    ok = await demo_caja_fase1_agregar(on_screenshot=_on_screenshot)
    await _send_to_agent({"type": "screenshot_end"})
    print(f"[CAJA] Fase 1 {'✓' if ok else '✗'}")
    return ok


async def _run_caja_fase2_inner(initial_delay: float = 0.0) -> bool:
    print("[CAJA] Iniciando Fase 2 (pago + cierre)...")
    ok = await demo_caja_fase2_pagar(
        on_screenshot=_on_screenshot,
        initial_delay=initial_delay,
        press_f8_signal=_fase2_press_f8,
    )
    await _send_to_agent({"type": "screenshot_end"})
    _fase2_complete_event.set()
    print(f"[CAJA] Fase 2 {'✓' if ok else '✗'}")
    return ok


async def _run_caja_fase2_con_prerequisito() -> bool:
    from mgw_playwright import _caja_fase1_done
    if not _caja_fase1_done:
        ok1 = await _run_caja_fase1_inner()
        if not ok1:
            return False
        await asyncio.sleep(1.5)
    return await _run_caja_fase2_inner()


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def handle_recall_audio(websocket):
    global conv_state, _fase2_task_created
    print("[WS] Recall.ai conectado ✓")

    # Reset para nueva sesión
    conv_state = ConversationState()
    _fase2_task_created = False
    reset_caja_fases()
    _acceso_login_done.clear()
    _fase2_complete_event.clear()
    _fase2_press_f8.clear()

    audio_queue: asyncio.Queue = asyncio.Queue()

    from realtime_bridge import RealtimeBridge
    bridge = RealtimeBridge(
        send_to_agent    = _send_to_agent,
        send_navigate    = _send_navigate,
        send_logged_in   = _send_logged_in,
        send_stop_audio  = _send_stop_audio,
        run_acceso_demo  = _run_acceso_demo,
        run_caja_fase1   = _run_caja_fase1_inner,
        run_caja_fase2   = _run_caja_fase2_con_prerequisito,
        pw_start         = pw_start,
        pw_stop          = pw_stop,
        on_screenshot    = _on_screenshot,
        on_screenshot_end = _on_screenshot_end,
        acceso_login_done = _acceso_login_done,
        fase2_press_f8   = _fase2_press_f8,
        reset_caja_fases = reset_caja_fases,
        conv_state       = conv_state,
    )

    async def receive_from_recall():
        try:
            while True:
                message = await websocket.receive()
                if "text" in message:
                    data  = json.loads(message["text"])
                    event = data.get("event", "")
                    if event == "audio_separate_raw.data":
                        participant = data["data"]["data"].get("participant", {})
                        if participant.get("name") == "Malena - Mi Gestión Web":
                            continue
                        b64_audio = data["data"]["data"]["buffer"]
                        pcm_audio = base64.b64decode(b64_audio)
                        await audio_queue.put(pcm_audio)
                    else:
                        print(f"[WS] Evento: {event}")
                elif "bytes" in message:
                    chunk = message["bytes"]
                    if len(chunk) > 4:
                        await audio_queue.put(chunk[4:])
        except Exception as e:
            print(f"[WS] Recall.ai desconectado: {e}")
        finally:
            await audio_queue.put(None)

    try:
        await asyncio.gather(receive_from_recall(), bridge.run(audio_queue))
    except Exception as e:
        print(f"[BOT] Error en handle_recall_audio: {e}")
        traceback.print_exc()
    finally:
        await bridge.close()
        try:
            await pw_stop()
        except Exception:
            pass
        await _send_reset()
