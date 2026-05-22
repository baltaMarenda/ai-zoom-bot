"""
bot.py
"""
import asyncio
import base64
import json
import websockets
import traceback

from config import DEEPGRAM_API_KEY, DEEPGRAM_WS_URL
from recall import bot_speak
from state import ConversationState, Stage
from ai import ask_ai, text_to_speech, extract_lead_info

is_speaking = False
conv_state = ConversationState()


SPEAKING_COOLDOWN = 2.5  

async def process_transcript(transcript: str):
    global is_speaking, conv_state
    if not transcript.strip() or is_speaking:
        return

    print(f"\n🧠 [{conv_state.stage.upper()}] Usuario: {transcript}")
    is_speaking = True

    try:
        reply = ask_ai(transcript, stage=conv_state.stage)
        print(f"🤖 Malena: {reply}")

        _update_state(transcript, reply)

        loop = asyncio.get_event_loop()
        mp3_bytes = await loop.run_in_executor(None, text_to_speech, reply)
        await loop.run_in_executor(None, bot_speak, mp3_bytes)

        # Cooldown: espera que el eco de Malena se limpie del stream
        await asyncio.sleep(SPEAKING_COOLDOWN)

    except Exception as e:
        print(f"[ERROR] process_transcript: {e}")
    finally:
        is_speaking = False

def _update_state(user_text: str, malena_reply: str):
    global conv_state
    stage = conv_state.stage
    reply_lower = malena_reply.lower()
    user_lower = user_text.lower()

    if stage == Stage.INTRO:
        # Avanza cuando Malena pregunta el nombre al final del intro
        if any(w in reply_lower for w in [
            "cómo te llamás", "cuál es tu nombre", "y vos", "cómo es tu nombre"
        ]):
            conv_state.advance()
            print(f"→ Estado: {conv_state.stage}")

    elif stage == Stage.CALIFICACION:
        _extract_lead_info(user_text)
        # Avanza cuando Malena ya tiene los datos y arranca la demo
        if conv_state.ready_for_demo() and any(w in reply_lower for w in [
            "te muestro", "arranquemos", "empecemos", "te cuento", "vamos a ver"
        ]):
            conv_state.advance()
            print(f"→ Estado: {conv_state.stage}")
            print(f"📋 Lead: {conv_state.summary()}")

    elif stage == Stage.DEMO:
        # Avanza cuando el usuario cierra la demo
        if any(w in user_lower for w in [
            "no tengo más", "eso es todo", "perfecto así", "muchas gracias", "listo", "re bien"
        ]):
            conv_state.advance()
            print(f"→ Estado: {conv_state.stage}")

    elif stage == Stage.CIERRE:
        _extract_contact_info(user_text)


def _extract_lead_info(user_text: str):
    """Extrae nombre y negocio usando OpenAI en vez de heurística."""
    global conv_state

    # Solo llamamos si todavía falta algún dato
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
    """Captura teléfono o mail del cierre."""
    global conv_state
    if "@" in text or any(c.isdigit() for c in text):
        conv_state.contact_info = text.strip()
        print(f"📞 Contacto capturado: {conv_state.contact_info}")


async def deepgram_pipeline(audio_source: asyncio.Queue):
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
            async for message in ws:
                result = json.loads(message)

                if result.get("type") != "Results":
                    continue
                if not result.get("speech_final"):
                    continue

                transcript = result["channel"]["alternatives"][0]["transcript"]
                if not transcript or len(transcript.split()) < 2:
                    continue

                if not is_speaking:
                    await process_transcript(transcript)

        await asyncio.gather(send_audio(), receive_transcript())


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

                    if event == "audio_mixed_raw.data":
                        b64_audio = data["data"]["data"]["buffer"]
                        pcm_audio = base64.b64decode(b64_audio)
                        if not is_speaking:
                            await audio_queue.put(pcm_audio)
                    else:
                        print(f"[WS] Evento: {event}")

                elif "bytes" in message:
                    chunk = message["bytes"]
                    if len(chunk) > 4 and not is_speaking:
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