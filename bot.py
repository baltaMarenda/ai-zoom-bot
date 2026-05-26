"""
bot.py
"""
import asyncio
import base64
import json
import struct
import websockets
import traceback

from config import DEEPGRAM_API_KEY, DEEPGRAM_WS_URL
from recall import bot_speak, bot_stop
from state import ConversationState, Stage
from ai import ask_ai, text_to_speech, extract_lead_info, classify_with_ai

conv_state = ConversationState()

SPEAKING_COOLDOWN_EXTRA = 0.8
TRANSCRIPT_DEBOUNCE = 2.5
DEMO_SILENCE_TIMEOUT = 10.0

# ── Audio mutex ───────────────────────────────────────────────────────────────
_audio_lock = asyncio.Lock()

# ── Estado global ─────────────────────────────────────────────────────────────
is_speaking = False
pending_speech: str = ""

# Contador de intercambios completados (para avanzar INTRO → CALIFICACION)
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def classify_interruption(text: str) -> str:
    return classify_with_ai(text)


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
    """
    Reproduce audio bajo el lock y espera a que termine O a barge-in.
    Retorna True si hubo barge-in.
    """
    loop = asyncio.get_event_loop()
    duration = estimate_mp3_duration(mp3_bytes)

    async with _audio_lock:
        await loop.run_in_executor(None, bot_speak, mp3_bytes)

        wait_task = asyncio.ensure_future(asyncio.sleep(duration + SPEAKING_COOLDOWN_EXTRA))
        barge_task = asyncio.ensure_future(barge_in_event.wait())

        done, pending = await asyncio.wait(
            [wait_task, barge_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    return barge_in_event.is_set()


# ── Máquina de estados ────────────────────────────────────────────────────────

def _extract_lead_info(user_text: str):
    global conv_state
    if conv_state.lead_name and conv_state.negocio:
        return
    extracted = extract_lead_info(user_text)
    if not conv_state.lead_name and extracted.get("nombre"):
        conv_state.lead_name = extracted["nombre"].title()
        print(f"👤 Nombre capturado: {conv_state.lead_name}")
    if not conv_state.negocio and extracted.get("negocio"):
        conv_state.negocio = extracted["negocio"]
        print(f"🏪 Negocio capturado: {conv_state.negocio}")


def _extract_contact_info(text: str):
    global conv_state
    if "@" in text or any(c.isdigit() for c in text):
        conv_state.contact_info = text.strip()
        print(f"📞 Contacto capturado: {conv_state.contact_info}")


def _maybe_advance_stage(user_text: str, malena_reply: str):
    """
    Avanza el stage basándose en datos concretos, no en keywords frágiles.
    Llamado después de cada turno completado.
    """
    global conv_state, _turns_completed, _demo_loop_started

    _turns_completed += 1
    stage = conv_state.stage
    user_lower = user_text.lower()

    if stage == Stage.INTRO:
        # Avanzar a CALIFICACION después del primer turno completado
        # (Malena ya se presentó, ahora a calificar)
        if _turns_completed >= 1:
            conv_state.advance()
            print(f"→ Estado: {conv_state.stage}")

    elif stage == Stage.CALIFICACION:
        # Intentar extraer datos del usuario en cada turno
        _extract_lead_info(user_text)

        # Avanzar a DEMO cuando tengamos nombre + negocio
        # y el usuario haya dado el OK (confirma, o Malena ya lo invitó a la demo)
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
                    asyncio.create_task(run_demo_loop())

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


# ── Pipeline principal (INTRO / CALIFICACION / CIERRE) ────────────────────────

async def process_transcript(transcript: str):
    global is_speaking, pending_speech, waiting_for_question

    if not transcript.strip():
        return

    # Paso 2 del barge-in: capturar la pregunta real
    if waiting_for_question:
        print(f"\n❓ [PREGUNTA POST-BARGE-IN] Usuario: {transcript}")
        await _answer_question(transcript)
        return

    # Durante la demo el loop autónomo maneja todo
    if conv_state.stage == Stage.DEMO:
        if not is_speaking:
            demo_continue_event.set()
            _pending_user_input.append(transcript)
            print(f"[DEMO] Input del usuario encolado: '{transcript}'")
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

        # Intentar extraer datos del usuario antes de avanzar el estado
        if conv_state.stage == Stage.CALIFICACION:
            _extract_lead_info(transcript)

        _maybe_advance_stage(transcript, reply)
        pending_speech = reply

        mp3_bytes = await loop.run_in_executor(None, text_to_speech, reply)

        if barge_in_event.is_set():
            await loop.run_in_executor(None, bot_stop)
            await asyncio.sleep(0.4)
            await _handle_barge_in()
            return

        barge_in = await _speak_and_wait(mp3_bytes)

        if barge_in:
            await loop.run_in_executor(None, bot_stop)
            await asyncio.sleep(0.4)
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
    """
    Corre toda la etapa DEMO de forma autónoma.
    Malena habla de corrido módulo a módulo.
    El _audio_lock garantiza que nunca se superpone con _answer_question.
    """
    global is_speaking, pending_speech, _pending_user_input

    print("🎬 [DEMO LOOP] Iniciando demo autónoma...")
    loop = asyncio.get_event_loop()

    negocio = conv_state.negocio or "su negocio"
    nombre = conv_state.lead_name or ""
    user_input_for_prompt = (
        f"Arrancá la demo para {nombre}, que tiene una {negocio}. "
        f"Mostrá los primeros 2 módulos (ACCESO y USUARIOS) con 3-4 oraciones en total. "
        f"Hablá de corrido sin preguntar si querés continuar."
    )

    while conv_state.stage == Stage.DEMO:
        barge_in_event.clear()
        demo_continue_event.clear()
        _pending_user_input.clear()

        # Esperar si hay una interrupción en curso
        if _audio_lock.locked() or waiting_for_question or handling_barge_in:
            print("[DEMO LOOP] Esperando interrupción en curso...")
            while _audio_lock.locked() or waiting_for_question or handling_barge_in:
                await asyncio.sleep(0.2)
            user_input_for_prompt = (
                "Continuá la demo desde donde estabas. "
                "Siguiente módulo, 3-4 oraciones, hablá de corrido sin preguntar si seguimos."
            )

        try:
            print(f"\n🎬 [DEMO LOOP] Generando bloque...")
            is_speaking = True

            reply = await loop.run_in_executor(None, ask_ai, user_input_for_prompt, "demo")
            print(f"🤖 Malena: {reply}")
            pending_speech = reply

            # Registrar módulos vistos (opcional, para no repetir)
            _maybe_advance_stage(user_input_for_prompt, reply)

            mp3_bytes = await loop.run_in_executor(None, text_to_speech, reply)

            # Barge-in durante generación de TTS
            if barge_in_event.is_set():
                await loop.run_in_executor(None, bot_stop)
                await asyncio.sleep(0.4)
                await _handle_barge_in()
                while waiting_for_question or handling_barge_in or _audio_lock.locked():
                    await asyncio.sleep(0.2)
                user_input_for_prompt = (
                    "Continuá la demo desde donde estabas. "
                    "Siguiente módulo, 3-4 oraciones, sin preguntar si seguimos."
                )
                is_speaking = False
                pending_speech = ""
                continue

            # Reproducir
            barge_in = await _speak_and_wait(mp3_bytes)
            is_speaking = False
            pending_speech = ""
            barge_in_event.clear()

            if barge_in:
                await loop.run_in_executor(None, bot_stop)
                await asyncio.sleep(0.4)
                await _handle_barge_in()
                while waiting_for_question or handling_barge_in or _audio_lock.locked():
                    await asyncio.sleep(0.2)
                user_input_for_prompt = (
                    "Continuá la demo desde donde estabas. "
                    "Siguiente módulo, 3-4 oraciones, sin preguntar si seguimos."
                )
                continue

            # Audio terminó — ventana de silencio antes de avanzar sola
            print(f"[DEMO LOOP] Esperando input ({DEMO_SILENCE_TIMEOUT}s)...")

            silence_task = asyncio.ensure_future(asyncio.sleep(DEMO_SILENCE_TIMEOUT))
            user_task = asyncio.ensure_future(demo_continue_event.wait())

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
                print(f"[DEMO LOOP] Usuario dijo: '{user_said}'")
                kind = classify_interruption(user_said)
                print(f"[DEMO LOOP] Clasificación: {kind}")

                # Verificar si el usuario quiere cerrar la demo
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
                        "Perfecto, continuá con el siguiente módulo. "
                        "3-4 oraciones, hablá de corrido sin preguntar si seguimos."
                    )
                else:
                    user_input_for_prompt = (
                        f"El usuario preguntó: '{user_said}'. "
                        f"Respondé en 1-2 oraciones y continuá con el siguiente módulo "
                        f"sin preguntar si queremos seguir."
                    )
            else:
                # Silencio — avanzar sola
                print("[DEMO LOOP] Silencio — avanzando sola...")
                user_input_for_prompt = (
                    "Continuá con el siguiente módulo. "
                    "3-4 oraciones, hablá de corrido sin preguntar si seguimos."
                )

        except Exception as e:
            print(f"[ERROR] run_demo_loop: {e}")
            traceback.print_exc()
            is_speaking = False
            pending_speech = ""
            await asyncio.sleep(1)
            user_input_for_prompt = (
                "Continuá con el siguiente módulo de la demo."
            )

    print("🎬 [DEMO LOOP] Demo terminada.")


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
            # Ya tiene la pregunta — responder directo sin "Sí, decime"
            print("❓ Pregunta completa, respondiendo directo...")
            await _answer_question(interruption)
        else:
            # Solo señaló que quiere hablar — ceder la palabra y esperar
            ack_prompt = (
                "El usuario te interrumpió en medio de la demo. "
                "Decí SOLO una frase muy corta para cederle la palabra "
                "(ej: 'Sí, decime.', 'Claro, decime.', 'Dale, contame.'). "
                "Una sola oración, sin retomar el tema ni agregar nada más."
            )
            ack_reply = await loop.run_in_executor(None, ask_ai, ack_prompt, conv_state.stage)
            print(f"🤖 Malena (ack): {ack_reply}")
            ack_mp3 = await loop.run_in_executor(None, text_to_speech, ack_reply)
            ack_duration = estimate_mp3_duration(ack_mp3)

            async with _audio_lock:
                await loop.run_in_executor(None, bot_speak, ack_mp3)
                await asyncio.sleep(ack_duration + 0.3)

            print("👂 Esperando pregunta real del usuario...")
            waiting_for_question = True

    except Exception as e:
        print(f"[ERROR] _handle_barge_in: {e}")
        traceback.print_exc()
        handling_barge_in = False
        waiting_for_question = False
        interrupted_context = ""


def _is_complete_question(text: str) -> bool:
    """
    Determina si la interrupción ya es una pregunta/comentario completo
    que Malena puede responder directo, sin necesitar "Sí, decime" primero.

    Retorna False solo para frases que son pura señal de querer hablar:
    "para", "una pregunta", "espera", etc.
    """
    text_lower = text.strip().lower().rstrip(".,!")

    solo_senal = {
        "para", "pará", "para un momento", "pará un momento",
        "una pregunta", "tengo una pregunta", "una duda", "tengo una duda",
        "espera", "esperá", "momento", "un momento",
        "ya", "ya sí", "ya, una pregunta",
    }
    if text_lower in solo_senal:
        return False

    # Más de 4 palabras → casi seguro es contenido completo
    if len(text.split()) > 4:
        return True

    # Tiene signo de pregunta o palabra interrogativa → pregunta completa
    interrogativas = ["qué", "que", "cómo", "como", "cuánto", "cuanto",
                      "cuándo", "cuando", "dónde", "donde", "cuál", "cual",
                      "quién", "quien", "por qué", "para qué"]
    if "?" in text or any(text_lower.startswith(w) for w in interrogativas):
        return True

    # Corta y sin interrogativas → probablemente solo señal
    return False


async def _answer_question(question: str):
    global handling_barge_in, waiting_for_question, interrupted_context, is_speaking

    loop = asyncio.get_event_loop()

    try:
        kind = classify_interruption(question)
        print(f"💬 Clasificación post-barge-in [{kind}]: '{question}'")

        context_hint = (
            f" Antes de ser interrumpida, estabas diciendo: '{interrupted_context}'."
            if interrupted_context else ""
        )

        if kind in ("noise", "backchannel"):
            print("↩️ Usuario canceló la pregunta, retomando...")
            resume_prompt = (
                f"El usuario te había interrumpido pero dice '{question}' — no tiene pregunta real.{context_hint} "
                f"Retomá naturalmente desde donde estabas en 1-2 oraciones, sin mencionar la interrupción."
                if interrupted_context else
                f"El usuario dice '{question}'. Continuá con la demo normalmente."
            )
        else:
            print(f"❓ Respondiendo pregunta real: '{question}'")
            resume_prompt = (
                f"El usuario te preguntó: '{question}'.{context_hint} "
                f"Respondé en 1-2 oraciones y retomá el punto de la demo donde estabas, "
                f"sin sonar forzado ni repetir todo desde el principio."
            )

        reply = await loop.run_in_executor(None, ask_ai, resume_prompt, conv_state.stage)
        print(f"🤖 Malena (respuesta + retoma): {reply}")

        mp3 = await loop.run_in_executor(None, text_to_speech, reply)
        duration = estimate_mp3_duration(mp3)

        async with _audio_lock:
            is_speaking = True
            await loop.run_in_executor(None, bot_speak, mp3)
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
        print("🎤 Deepgram conectado, escuchando audio de la reunión...")

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

                # Modo "esperando pregunta" post-barge-in
                if waiting_for_question:
                    _accumulated_transcript = (
                        (_accumulated_transcript + " " + transcript).strip()
                    )
                    print(f"[Debounce/pregunta] Acumulado: '{_accumulated_transcript}'")
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
                    print(f"[BARGE-IN] Pregunta detectada: '{transcript}'")
                    barge_in_text = transcript
                    barge_in_event.set()
                else:
                    _accumulated_transcript = (
                        (_accumulated_transcript + " " + transcript).strip()
                    )
                    print(f"[Debounce] Acumulado: '{_accumulated_transcript}'")
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
                    data = json.loads(message["text"])
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

    await asyncio.gather(
        receive_from_recall(),
        run_deepgram(),
    )