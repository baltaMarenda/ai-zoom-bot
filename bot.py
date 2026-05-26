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

is_speaking = False
conv_state = ConversationState()

SPEAKING_COOLDOWN_EXTRA = 0.8
TRANSCRIPT_DEBOUNCE = 1.5

# ── Barge-in ──────────────────────────────────────────────────────────────────

pending_speech: str = ""
barge_in_event = asyncio.Event()
barge_in_text: str = ""
handling_barge_in: bool = False

def classify_interruption(text: str) -> str:
    """
    Clasifica la interrupción usando OpenAI (temperatura 0, rápido).
    Retorna: "noise" | "backchannel" | "question"
    """
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
                    # ElevenLabs agrega ~2-4KB de headers; restar estimado
                    audio_bytes = max(0, len(mp3_bytes) - 3000)
                    return (audio_bytes * 8) / (bitrate_kbps * 1000)
            i += 1
        return (len(mp3_bytes) * 8) / 128000
    except Exception:
        return 3.0


# ── Funciones auxiliares de estado ────────────────────────────────────────────

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


def _update_state(user_text: str, malena_reply: str):
    global conv_state
    stage = conv_state.stage
    reply_lower = malena_reply.lower()
    user_lower = user_text.lower()

    if stage == Stage.INTRO:
        if any(w in reply_lower for w in [
            "cómo te llamás", "cuál es tu nombre", "y vos", "cómo es tu nombre",
            "cómo te llamás", "como te llamas",
        ]):
            conv_state.advance()
            print(f"→ Estado: {conv_state.stage}")

    elif stage == Stage.CALIFICACION:
        _extract_lead_info(user_text)
        if conv_state.ready_for_demo() and any(w in reply_lower for w in [
            "te muestro", "arranquemos", "empecemos", "te cuento", "vamos a ver"
        ]):
            conv_state.advance()
            print(f"→ Estado: {conv_state.stage}")
            print(f"📋 Lead: {conv_state.summary()}")

    elif stage == Stage.DEMO:
        if any(w in user_lower for w in [
            "no tengo más", "eso es todo", "perfecto así", "muchas gracias", "listo", "re bien"
        ]):
            conv_state.advance()
            print(f"→ Estado: {conv_state.stage}")

    elif stage == Stage.CIERRE:
        _extract_contact_info(user_text)


# ── Pipeline principal ─────────────────────────────────────────────────────────

async def process_transcript(transcript: str):
    global is_speaking, conv_state, pending_speech

    if not transcript.strip() or is_speaking:
        return

    print(f"\n🧠 [{conv_state.stage.upper()}] Usuario: {transcript}")
    is_speaking = True
    barge_in_event.clear()

    try:
        loop = asyncio.get_event_loop()

        reply = await loop.run_in_executor(None, ask_ai, transcript, conv_state.stage)
        print(f"🤖 Malena: {reply}")
        _update_state(transcript, reply)
        pending_speech = reply

        mp3_bytes = await loop.run_in_executor(None, text_to_speech, reply)
        duration = estimate_mp3_duration(mp3_bytes)
        print(f"[Audio] Duración estimada: {duration:.1f}s")

        # Chequear barge-in ANTES de mandar audio (puede haber llegado durante TTS)
        if barge_in_event.is_set():
            await _handle_barge_in()
            return

        # Enviar audio completo a Recall
        await loop.run_in_executor(None, bot_speak, mp3_bytes)

        # Esperar duración real O barge-in, lo que llegue primero
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

        if barge_in_event.is_set():
            # Cortar el audio mandando silencio
            await loop.run_in_executor(None, bot_stop)
            await _handle_barge_in()

    except Exception as e:
        print(f"[ERROR] process_transcript: {e}")
        traceback.print_exc()
    finally:
        is_speaking = False
        pending_speech = ""
        barge_in_event.clear()


async def _handle_barge_in():
    global barge_in_text, pending_speech, handling_barge_in

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
        # Solo llegan questions (noise y backchannel se filtran antes)
        print("❓ Respondiendo pregunta...")
        question_prompt = (
            f"El usuario te interrumpió con una pregunta: '{interruption}'. "
            f"Respondé muy brevemente (1-2 oraciones) y retomá la demo."
        )
        reply = await loop.run_in_executor(None, ask_ai, question_prompt, conv_state.stage)
        print(f"🤖 Malena (respuesta): {reply}")
        mp3 = await loop.run_in_executor(None, text_to_speech, reply)
        duration = estimate_mp3_duration(mp3)
        await loop.run_in_executor(None, bot_speak, mp3)
        await asyncio.sleep(duration + SPEAKING_COOLDOWN_EXTRA)

    finally:
        handling_barge_in = False


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
            while True:
                try:
                    chunk = await asyncio.wait_for(audio_source.get(), timeout=1.0)
                    if chunk is None:
                        break
                    await ws.send(chunk)
                except asyncio.TimeoutError:
                    pass

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

                words = transcript.split()

                if is_speaking:
                    if handling_barge_in:
                        continue
                    kind = classify_interruption(transcript)
                    if kind == "noise" or kind == "backchannel":
                        # Backchannels ("bueno", "ah", "dale") no interrumpen —
                        # son reacciones naturales mientras el usuario escucha
                        print(f"[BARGE-IN] Ignorado ({kind}): '{transcript}'")
                        continue
                    # Solo "question" frena el audio
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
                        # Ignorar audio del propio bot (según doc, bots muted no producen
                        # audio en separate_raw, pero filtramos por nombre por seguridad)
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