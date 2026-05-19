"""
bot.py
Pipeline principal del bot conversacional.
Adaptado de tu stt_test.py:
  - Reemplaza sd.InputStream → WebSocket de Recall.ai
  - Reemplaza afplay → recall.bot_speak()
  - Mantiene tu lógica de Deepgram, OpenAI y ElevenLabs intacta
"""
import asyncio
import base64
import json
import numpy as np
import websockets

from config import DEEPGRAM_API_KEY, DEEPGRAM_WS_URL
from ai import ask_ai, text_to_speech
from recall import bot_speak

# Flag global: evita que el bot procese su propio audio mientras habla
is_speaking = False


async def process_transcript(transcript: str):
    """
    Tu pipeline original: transcript → OpenAI → ElevenLabs → output_audio.
    Igual que en stt_test.py pero sin afplay.
    """
    global is_speaking

    if not transcript.strip() or is_speaking:
        return

    print(f"\n🧠 Usuario: {transcript}")
    is_speaking = True

    try:
        # 1. OpenAI (tu función ask_ai original, ahora en ai.py)
        reply = ask_ai(transcript)
        print(f"🤖 Malena: {reply}")

        # 2. ElevenLabs → MP3 bytes (tu función speak, ahora devuelve bytes)
        loop = asyncio.get_event_loop()
        mp3_bytes = await loop.run_in_executor(None, text_to_speech, reply)

        # 3. Enviar audio a la reunión via Recall (reemplaza afplay)
        await loop.run_in_executor(None, bot_speak, mp3_bytes)

    except Exception as e:
        print(f"[ERROR] process_transcript: {e}")
    finally:
        is_speaking = False


async def deepgram_pipeline(audio_source: asyncio.Queue):
    """
    Conecta a Deepgram y procesa transcripts.
    Igual que tu run() en stt_test.py, pero el audio viene de una Queue
    en vez del micrófono.
    """
    async with websockets.connect(
        DEEPGRAM_WS_URL,
        extra_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
        ping_interval=20,
    ) as ws:
        print("🎤 Deepgram conectado, escuchando audio de la reunión...")

        async def send_audio():
            while True:
                try:
                    chunk = await asyncio.wait_for(audio_source.get(), timeout=0.1)
                    if chunk is None:
                        break  # señal de cierre
                    await ws.send(chunk)
                except asyncio.TimeoutError:
                    # Igual que tu código: enviar silencio para mantener conexión
                    silence = np.zeros((1024, 1), dtype="int16")
                    await ws.send(silence.tobytes())

        async def receive_transcript():
            async for message in ws:
                result = json.loads(message)
                if "channel" in result:
                    transcript = result["channel"]["alternatives"][0]["transcript"]
                    if transcript and result.get("is_final"):
                        # No procesar si el bot está hablando (evita feedback)
                        if not is_speaking:
                            await process_transcript(transcript)

        await asyncio.gather(send_audio(), receive_transcript())


async def handle_recall_audio(websocket):
    """
    Handler del WebSocket que Recall.ai conecta para mandarte audio.
    Recall manda JSON con el audio en base64 dentro de data.data.buffer
    """
    print("[WS] Recall.ai conectado ✓")
    audio_queue = asyncio.Queue()

    # Arranca el pipeline de Deepgram en background
    deepgram_task = asyncio.create_task(deepgram_pipeline(audio_queue))

    # Keep-alive: ping cada 25s para evitar timeout
    async def keep_alive():
        while True:
            await asyncio.sleep(25)
            try:
                await websocket.ping()
            except Exception:
                break

    asyncio.create_task(keep_alive())

    try:
        while True:
            message = await websocket.receive()

            if "text" in message:
                data = json.loads(message["text"])
                event = data.get("event", "")

                if event == "audio_mixed_raw.data":
                    # Audio en base64 dentro de data.data.buffer
                    b64_audio = data["data"]["data"]["buffer"]
                    pcm_audio = base64.b64decode(b64_audio)

                    if not is_speaking:
                        await audio_queue.put(pcm_audio)
                else:
                    print(f"[WS] Evento: {event}")

            elif "bytes" in message:
                # Por si acaso manda binario también
                chunk = message["bytes"]
                if len(chunk) > 4 and not is_speaking:
                    await audio_queue.put(chunk[4:])

    except Exception as e:
        print(f"[WS] Recall.ai desconectado: {e}")
    finally:
        await audio_queue.put(None)  # señal de cierre para deepgram_pipeline
        deepgram_task.cancel()
        print("[WS] Pipeline cerrado")