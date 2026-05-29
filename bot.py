"""
bot.py
Pipeline principal de Malena.
Audio: en vez de llamar bot_speak(), envía MP3 via WebSocket a la webpage del agente.
Navegación: envía comandos navigate via WebSocket a la webpage del agente.
"""
import asyncio
import base64
import json
import struct
import websockets
import traceback

from config import (
    DEEPGRAM_API_KEY, DEEPGRAM_WS_URL,
    DEMO_NAV_KEYWORDS, DEMO_CREATE_CLIENT_KEYWORDS,
)
from state import ConversationState, Stage
from ai import ask_ai, text_to_speech, extract_lead_info, classify_with_ai
from mgw_playwright import pw_start, pw_login, pw_stop, demo_venta_caja

# ── Referencia al WebSocket de la webpage del agente ──────────────────────────
_agent_ws = None
_audio_done_event = asyncio.Event()


def set_agent_websocket(ws):
    global _agent_ws
    _agent_ws = ws
    print(f"[BOT] Agent WS {'conectado' if ws else 'desconectado'}")


def on_agent_audio_done():
    """Llamado desde main.py cuando la webpage termina de reproducir audio."""
    _audio_done_event.set()


# ── Estado de conversación ────────────────────────────────────────────────────
conv_state = ConversationState()

SPEAKING_COOLDOWN_EXTRA = 0.3
TRANSCRIPT_DEBOUNCE = 2.5
DEMO_SILENCE_TIMEOUT = 10.0

# ── Audio mutex ───────────────────────────────────────────────────────────────
_audio_lock = asyncio.Lock()

# ── Estado global ─────────────────────────────────────────────────────────────
is_speaking = False
pending_speech: str = ""
_turns_completed = 0

# ── Barge-in ──────────────────────────────────────────────────────────────────
barge_in_event = asyncio.Event()
barge_in_text: str = ""
handling_barge_in: bool = False
interrupted_context: str = ""
waiting_for_question: bool = False

# ── Demo loop ─────────────────────────────────────────────────────────────────
demo_continue_event = asyncio.Event()
_pending_user_input: list[str] = []
_demo_loop_started = False

# ── Navegación MGW ────────────────────────────────────────────────────────────
_last_navigated_module: str = ""

DEMO_MODULE_PATHS = {
    "USUARIOS":         "/configuracion_usuarios.php",
    "PANTALLA INICIAL": "/home.php",
    "BALANZA":          "/balanza3.php?balanza=6",
    "CAJA":             "/caja.php",
    "FACTURACIÓN":      "/venta.php",
    "VENTAS":           "/venta.php",
    "CLIENTES":         "/clientes.php",
    "CIERRES":          "/caja_cierre.php",
    "PROVEEDORES":      "/compras.php",
    "STOCK":            "/stock_existencia_2.php",
    "ESTADÍSTICAS":     "/estadisticas_ventas.php",
    "RRHH":             "/rrhh_personal.php",
    "TIENDA WEB":       "/mitiendaweb.php",
}


# ── Helpers de comunicación con la webpage ────────────────────────────────────

async def _send_to_agent(msg: dict):
    if _agent_ws is None:
        print("[BOT] Agent WS no disponible, ignorando mensaje")
        return
    try:
        await _agent_ws.send_json(msg)
    except Exception as e:
        print(f"[BOT] Error enviando a agent WS: {e}")


async def _send_audio(mp3_bytes: bytes):
    b64 = base64.b64encode(mp3_bytes).decode()
    await _send_to_agent({"type": "audio", "data": b64})


async def _send_navigate(path: str):
    await _send_to_agent({"type": "navigate", "path": path})


async def _send_reset():
    await _send_to_agent({"type": "reset"})


async def _send_logged_in():
    await _send_to_agent({"type": "logged_in"})


# ── Audio: reproducir y esperar ───────────────────────────────────────────────

def estimate_mp3_duration(mp3_bytes: bytes) -> float:
    try:
        i = 0
        while i < len(mp3_bytes) - 4:
            if mp3_bytes[i] == 0xFF and (mp3_bytes[i + 1] & 0xE0) == 0xE0:
                header = struct.unpack(">I", mp3_bytes[i:i+4])[0]
                bitrate_idx = (header >> 12) & 0xF
                bitrates = [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]
                bitrate_kbps = bitrates[bitrate_idx]
                if bitrate_kbps > 0:
                    audio_bytes = max(0, len(mp3_bytes) - 3000)
                    return (audio_bytes * 8) / (bitrate_kbps * 1000)
            i += 1
        return (len(mp3_bytes) * 8) / 128000
    except Exception:
        return 3.0


async def _speak_and_wait(mp3_bytes: bytes) -> bool:
    duration = estimate_mp3_duration(mp3_bytes)

    async with _audio_lock:
        _audio_done_event.clear()
        await _send_audio(mp3_bytes)

        wait_task    = asyncio.ensure_future(_audio_done_event.wait())
        barge_task   = asyncio.ensure_future(barge_in_event.wait())
        timeout_task = asyncio.ensure_future(
            asyncio.sleep(duration + SPEAKING_COOLDOWN_EXTRA + 2.0)
        )

        done, pending = await asyncio.wait(
            [wait_task, barge_task, timeout_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    return barge_in_event.is_set()


# ── Navegación MGW ────────────────────────────────────────────────────────────

async def _run_caja_demo(con_factura: bool = False):
    """Ejecuta la demo de venta con Playwright, enviando screenshots al iframe."""
    print(f"[CAJA] Iniciando demo Playwright ({'FCE' if con_factura else 'Presupuesto'})...")

    async def on_screenshot(b64: str):
        await _send_to_agent({"type": "screenshot", "data": b64})

    ok = await demo_venta_caja(on_screenshot=on_screenshot, con_factura=con_factura)

    # Notificar fin de demo — vuelve al iframe normal
    await _send_to_agent({"type": "screenshot_end"})

    if ok:
        print("[CAJA] ✓ Demo Playwright completada")
    else:
        print("[CAJA] ✗ Demo Playwright falló")


async def _mgw_navigate_from_reply(reply: str):
    global _last_navigated_module
    reply_lower = reply.lower()
    for keyword, module in DEMO_NAV_KEYWORDS.items():
        if keyword in reply_lower and module != _last_navigated_module:
            path = DEMO_MODULE_PATHS.get(module)
            if path:
                print(f"[MGW] Navegando a: {module} → {path}")
                await _send_navigate(path)
                _last_navigated_module = module
            break


async def _mgw_hook(reply: str):
    if conv_state.stage != Stage.DEMO:
        return
    await _mgw_navigate_from_reply(reply)

    # Si Malena menciona la caja, ejecutar demo de venta en paralelo
    reply_lower = reply.lower()
    if any(k in reply_lower for k in ["caja", "venta", "ticket", "cobro", "pago"]):
        if _last_navigated_module == "CAJA":
            con_factura = any(k in reply_lower for k in ["factura", "fce", "electrónica"])
            asyncio.create_task(_run_caja_demo(con_factura))


# ── Máquina de estados ────────────────────────────────────────────────────────

def classify_interruption(text: str) -> str:
    return classify_with_ai(text)


def _extract_lead_info(user_text: str):
    global conv_state
    if conv_state.lead_name and conv_state.negocio:
        return
    extracted = extract_lead_info(user_text)
    if not conv_state.lead_name and extracted.get("nombre"):
        conv_state.lead_name = extracted["nombre"].title()
        print(f"👤 Nombre: {conv_state.lead_name}")
    if not conv_state.negocio and extracted.get("negocio"):
        conv_state.negocio = extracted["negocio"]
        print(f"🏪 Negocio: {conv_state.negocio}")


def _extract_contact_info(text: str):
    global conv_state
    if "@" in text or any(c.isdigit() for c in text):
        conv_state.contact_info = text.strip()
        print(f"📞 Contacto: {conv_state.contact_info}")


def _maybe_advance_stage(user_text: str, malena_reply: str):
    global conv_state, _turns_completed, _demo_loop_started

    _turns_completed += 1
    stage = conv_state.stage
    user_lower = user_text.lower()

    if stage == Stage.INTRO:
        if _turns_completed >= 1:
            conv_state.advance()
            print(f"→ Estado: {conv_state.stage}")

    elif stage == Stage.CALIFICACION:
        _extract_lead_info(user_text)
        if conv_state.ready_for_demo():
            usuario_confirmo = any(w in user_lower for w in [
                "dale", "sí", "si", "bueno", "vamos", "perfecto", "claro",
                "ok", "va", "arrancá", "arranca", "mostrá", "muestra",
            ])
            malena_invito = any(w in malena_reply.lower() for w in [
                "te muestro", "arranquemos", "empecemos", "arrancamos",
                "vamos a ver", "voy a mostrar", "vamos a arrancar",
            ])
            if usuario_confirmo or malena_invito:
                conv_state.advance()
                print(f"→ Estado: {conv_state.stage}")
                print(f"📋 Lead: {conv_state.summary()}")
                if not _demo_loop_started:
                    _demo_loop_started = True
                    asyncio.create_task(_start_demo())

    elif stage == Stage.DEMO:
        palabras_cierre = [
            "no tengo más", "eso es todo", "perfecto así", "muchas gracias",
            "listo", "re bien", "estoy conforme", "con eso alcanza",
        ]
        if any(w in user_lower for w in palabras_cierre):
            conv_state.advance()
            print(f"→ Estado: {conv_state.stage}")

    elif stage == Stage.CIERRE:
        _extract_contact_info(user_text)


async def _start_demo():
    """Inicia Playwright, hace login y lanza el demo loop."""
    print("[BOT] Iniciando Playwright para la demo...")
    try:
        await pw_start()
        ok = await pw_login()
        if ok:
            print("[BOT] Playwright logueado ✓")
        else:
            print("[BOT] Login Playwright falló — demo continúa sin screenshots")
    except Exception as e:
        print(f"[BOT] Error iniciando Playwright: {e}")

    await _send_logged_in()
    await run_demo_loop()

    try:
        await pw_stop()
    except Exception:
        pass


# ── Pipeline principal (INTRO / CALIFICACION / CIERRE) ────────────────────────

async def process_transcript(transcript: str):
    global is_speaking, pending_speech, waiting_for_question

    if not transcript.strip():
        return

    if waiting_for_question:
        print(f"\n❓ [PREGUNTA POST-BARGE-IN] Usuario: {transcript}")
        await _answer_question(transcript)
        return

    if conv_state.stage == Stage.DEMO:
        if not is_speaking:
            demo_continue_event.set()
            _pending_user_input.append(transcript)
            print(f"[DEMO] Input encolado: '{transcript}'")
        return

    if is_speaking:
        return

    print(f"\n🧠 [{conv_state.stage.upper()}] Usuario: {transcript}")
    is_speaking = True
    barge_in_event.clear()

    try:
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(None, ask_ai, transcript, conv_state.stage)
        print(f"🤖 Malena: {reply}")

        if conv_state.stage == Stage.CALIFICACION:
            _extract_lead_info(transcript)

        _maybe_advance_stage(transcript, reply)
        pending_speech = reply

        mp3_bytes = await loop.run_in_executor(None, text_to_speech, reply)

        if barge_in_event.is_set():
            await _handle_barge_in()
            return

        barge_in = await _speak_and_wait(mp3_bytes)

        if barge_in:
            await _handle_barge_in()

    except Exception as e:
        print(f"[ERROR] process_transcript: {e}")
        traceback.print_exc()
    finally:
        is_speaking = False
        pending_speech = ""
        barge_in_event.clear()


# ── Loop autónomo de la demo ───────────────────────────────────────────────────

async def run_demo_loop():
    global is_speaking, pending_speech, _pending_user_input

    print("🎬 [DEMO LOOP] Iniciando...")
    loop = asyncio.get_event_loop()

    negocio = conv_state.negocio or "su negocio"
    nombre  = conv_state.lead_name or ""
    user_input_for_prompt = (
        f"Arrancá la demo para {nombre}, que tiene una {negocio}. "
        f"Empezá SIEMPRE por el módulo de CAJA — es el más importante. "
        f"Describí que van a hacer una venta de prueba en vivo: buscar el producto Huevos, "
        f"agregar cantidad, seleccionar método de pago efectivo, y cerrar con Presupuesto (F8). "
        f"Mencioná también la opción de FCE (F4) para factura electrónica. "
        f"3-5 oraciones, hablá de corrido."
    )

    while conv_state.stage == Stage.DEMO:
        barge_in_event.clear()
        demo_continue_event.clear()
        _pending_user_input.clear()

        if _audio_lock.locked() or waiting_for_question or handling_barge_in:
            print("[DEMO LOOP] Esperando interrupción en curso...")
            while _audio_lock.locked() or waiting_for_question or handling_barge_in:
                await asyncio.sleep(0.2)
            user_input_for_prompt = (
                "Continuá la demo desde donde estabas. Siguiente módulo, 3-4 oraciones."
            )

        try:
            print(f"\n🎬 [DEMO LOOP] Generando bloque...")
            is_speaking = True

            reply = await loop.run_in_executor(None, ask_ai, user_input_for_prompt, "demo")
            print(f"🤖 Malena: {reply}")
            pending_speech = reply

            _maybe_advance_stage(user_input_for_prompt, reply)
            await _mgw_hook(reply)

            mp3_bytes = await loop.run_in_executor(None, text_to_speech, reply)

            if barge_in_event.is_set():
                await _handle_barge_in()
                while waiting_for_question or handling_barge_in or _audio_lock.locked():
                    await asyncio.sleep(0.2)
                user_input_for_prompt = "Continuá la demo desde donde estabas. Siguiente módulo, 3-4 oraciones."
                is_speaking = False
                pending_speech = ""
                continue

            barge_in = await _speak_and_wait(mp3_bytes)
            is_speaking = False
            pending_speech = ""
            barge_in_event.clear()

            if barge_in:
                await _handle_barge_in()
                while waiting_for_question or handling_barge_in or _audio_lock.locked():
                    await asyncio.sleep(0.2)
                user_input_for_prompt = "Continuá la demo desde donde estabas. Siguiente módulo, 3-4 oraciones."
                continue

            print(f"[DEMO LOOP] Esperando input ({DEMO_SILENCE_TIMEOUT}s)...")
            silence_task = asyncio.ensure_future(asyncio.sleep(DEMO_SILENCE_TIMEOUT))
            user_task    = asyncio.ensure_future(demo_continue_event.wait())

            done, pending = await asyncio.wait(
                [silence_task, user_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            if demo_continue_event.is_set() and _pending_user_input:
                user_said = " ".join(_pending_user_input).strip()
                print(f"[DEMO LOOP] Usuario: '{user_said}'")
                kind = classify_interruption(user_said)
                print(f"[DEMO LOOP] Clasificación: {kind}")

                cierre_words = [
                    "no tengo más", "eso es todo", "perfecto así",
                    "muchas gracias", "listo", "re bien", "estoy conforme",
                ]
                if any(w in user_said.lower() for w in cierre_words):
                    conv_state.advance()
                    print(f"→ Estado: {conv_state.stage} (usuario cerró demo)")
                    break

                if kind in ("noise", "backchannel"):
                    user_input_for_prompt = (
                        "Perfecto, continuá con el siguiente módulo. 3-4 oraciones, hablá de corrido."
                    )
                else:
                    user_input_for_prompt = (
                        f"El usuario preguntó: '{user_said}'. "
                        f"Respondé en 1-2 oraciones y continuá con el siguiente módulo."
                    )
            else:
                print("[DEMO LOOP] Silencio — avanzando sola...")
                user_input_for_prompt = "Continuá con el siguiente módulo. 3-4 oraciones, hablá de corrido."

        except Exception as e:
            print(f"[ERROR] run_demo_loop: {e}")
            traceback.print_exc()
            is_speaking = False
            pending_speech = ""
            await asyncio.sleep(1)
            user_input_for_prompt = "Continuá con el siguiente módulo de la demo."

    print("🎬 [DEMO LOOP] Demo terminada.")
    await _send_reset()


# ── Barge-in handlers ─────────────────────────────────────────────────────────

async def _handle_barge_in():
    global barge_in_text, pending_speech, handling_barge_in, interrupted_context, waiting_for_question

    if handling_barge_in:
        barge_in_event.clear()
        return

    handling_barge_in = True
    interruption = barge_in_text
    kind = classify_interruption(interruption)
    print(f"⚡ Barge-in [{kind}]: '{interruption}'")

    barge_in_event.clear()
    barge_in_text = ""
    loop = asyncio.get_event_loop()

    try:
        interrupted_context = pending_speech

        if _is_complete_question(interruption):
            print("❓ Pregunta completa, respondiendo directo...")
            await _answer_question(interruption)
        else:
            ack_prompt = (
                "El usuario te interrumpió en medio de la demo. "
                "Decí SOLO una frase muy corta para cederle la palabra "
                "(ej: 'Sí, decime.', 'Claro, decime.', 'Dale, contame.'). "
                "Una sola oración."
            )
            ack_reply = await loop.run_in_executor(None, ask_ai, ack_prompt, conv_state.stage)
            print(f"🤖 Malena (ack): {ack_reply}")
            ack_mp3   = await loop.run_in_executor(None, text_to_speech, ack_reply)

            async with _audio_lock:
                _audio_done_event.clear()
                await _send_audio(ack_mp3)
                ack_duration = estimate_mp3_duration(ack_mp3)
                await asyncio.sleep(ack_duration + 0.5)

            print("👂 Esperando pregunta real del usuario...")
            waiting_for_question = True

    except Exception as e:
        print(f"[ERROR] _handle_barge_in: {e}")
        traceback.print_exc()
        handling_barge_in = False
        waiting_for_question = False
        interrupted_context = ""


def _is_complete_question(text: str) -> bool:
    text_lower = text.strip().lower().rstrip(".,!")
    solo_senal = {
        "para", "pará", "para un momento", "pará un momento",
        "una pregunta", "tengo una pregunta", "una duda", "tengo una duda",
        "espera", "esperá", "momento", "un momento",
        "ya", "ya sí", "ya, una pregunta",
    }
    if text_lower in solo_senal:
        return False
    if len(text.split()) > 4:
        return True
    interrogativas = ["qué", "que", "cómo", "como", "cuánto", "cuanto",
                      "cuándo", "cuando", "dónde", "donde", "cuál", "cual",
                      "quién", "quien", "por qué", "para qué"]
    if "?" in text or any(text_lower.startswith(w) for w in interrogativas):
        return True
    return False


async def _answer_question(question: str):
    global handling_barge_in, waiting_for_question, interrupted_context, is_speaking

    loop = asyncio.get_event_loop()

    try:
        kind = classify_interruption(question)
        print(f"💬 Post-barge-in [{kind}]: '{question}'")

        context_hint = (
            f" Antes de ser interrumpida, estabas diciendo: '{interrupted_context}'."
            if interrupted_context else ""
        )

        if kind in ("noise", "backchannel"):
            resume_prompt = (
                f"El usuario te había interrumpido pero dice '{question}' — no tiene pregunta real.{context_hint} "
                f"Retomá naturalmente en 1-2 oraciones."
                if interrupted_context else
                f"El usuario dice '{question}'. Continuá con la demo."
            )
        else:
            resume_prompt = (
                f"El usuario te preguntó: '{question}'.{context_hint} "
                f"Respondé en 1-2 oraciones y retomá la demo donde estabas."
            )

        reply = await loop.run_in_executor(None, ask_ai, resume_prompt, conv_state.stage)
        print(f"🤖 Malena (respuesta + retoma): {reply}")

        await _mgw_hook(reply)

        mp3      = await loop.run_in_executor(None, text_to_speech, reply)
        duration = estimate_mp3_duration(mp3)

        async with _audio_lock:
            is_speaking = True
            _audio_done_event.clear()
            await _send_audio(mp3)
            await asyncio.sleep(duration + SPEAKING_COOLDOWN_EXTRA)

    except Exception as e:
        print(f"[ERROR] _answer_question: {e}")
        traceback.print_exc()
    finally:
        handling_barge_in = False
        waiting_for_question = False
        interrupted_context = ""
        is_speaking = False


# ── Debounce de transcripts ───────────────────────────────────────────────────

_debounce_task: asyncio.Task | None = None
_accumulated_transcript: str = ""


async def _debounced_process(final_text: str):
    global _debounce_task, _accumulated_transcript
    try:
        await asyncio.sleep(TRANSCRIPT_DEBOUNCE)
        text = _accumulated_transcript.strip()
        _accumulated_transcript = ""
        if text:
            await process_transcript(text)
    except asyncio.CancelledError:
        pass


# ── Deepgram pipeline ─────────────────────────────────────────────────────────

async def deepgram_pipeline(audio_source: asyncio.Queue):
    global _debounce_task, _accumulated_transcript

    async with websockets.connect(
        DEEPGRAM_WS_URL,
        extra_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        ping_interval=10,
        ping_timeout=30,
        close_timeout=10,
    ) as ws:
        print("🎤 Deepgram conectado...")

        async def send_audio():
            KEEPALIVE_SILENCE = b'\x00\x00' * 1600
            while True:
                try:
                    chunk = await asyncio.wait_for(audio_source.get(), timeout=0.5)
                    if chunk is None:
                        break
                    await ws.send(chunk)
                except asyncio.TimeoutError:
                    await ws.send(KEEPALIVE_SILENCE)

        async def receive_transcript():
            global barge_in_text, _debounce_task, _accumulated_transcript

            async for message in ws:
                result = json.loads(message)
                if result.get("type") != "Results":
                    continue
                if not result.get("speech_final"):
                    continue

                transcript = result["channel"]["alternatives"][0]["transcript"]
                if not transcript:
                    continue

                if waiting_for_question:
                    _accumulated_transcript = (
                        (_accumulated_transcript + " " + transcript).strip()
                    )
                    print(f"[Debounce/pregunta] '{_accumulated_transcript}'")
                    if _debounce_task and not _debounce_task.done():
                        _debounce_task.cancel()
                    _debounce_task = asyncio.create_task(
                        _debounced_process(_accumulated_transcript)
                    )
                    continue

                if is_speaking:
                    if handling_barge_in:
                        continue
                    kind = classify_interruption(transcript)
                    if kind in ("noise", "backchannel"):
                        print(f"[BARGE-IN] Ignorado ({kind}): '{transcript}'")
                        continue
                    print(f"[BARGE-IN] Pregunta: '{transcript}'")
                    barge_in_text = transcript
                    barge_in_event.set()
                else:
                    _accumulated_transcript = (
                        (_accumulated_transcript + " " + transcript).strip()
                    )
                    print(f"[Debounce] '{_accumulated_transcript}'")
                    if _debounce_task and not _debounce_task.done():
                        _debounce_task.cancel()
                    _debounce_task = asyncio.create_task(
                        _debounced_process(_accumulated_transcript)
                    )

        await asyncio.gather(send_audio(), receive_transcript())


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def handle_recall_audio(websocket):
    print("[WS] Recall.ai conectado ✓")
    audio_queue = asyncio.Queue()

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

    async def run_deepgram():
        print("[DEEPGRAM] Task iniciado")
        try:
            await deepgram_pipeline(audio_queue)
        except Exception as e:
            print(f"[ERROR DEEPGRAM] {type(e).__name__}: {e}")
            traceback.print_exc()
        print("[DEEPGRAM] Task terminado")

    await asyncio.gather(receive_from_recall(), run_deepgram())