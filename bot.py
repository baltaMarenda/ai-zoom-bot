"""
bot.py
"""
import asyncio
import base64
import json
import numpy as np
import websockets

from config import DEEPGRAM_API_KEY, DEEPGRAM_WS_URL
from ai import ask_ai, text_to_speech
from recall import bot_speak

is_speaking = False


async def process_transcript(transcript: str):
    global is_speaking
    if not transcript.strip() or is_speaking:
        return

    print(f"\n🧠 Usuario: {transcript}")
    is_speaking = True

    try:
        reply = ask_ai(transcript)
        print(f"🤖 Malena: {reply}")

        loop = asyncio.get_event_loop()
        mp3_bytes = await loop.run_in_executor(None, text_to_speech, reply)
        await loop.run_in_executor(None, bot_speak, mp3_bytes)

    except Exception as e:
        print(f"[ERROR] process_transcript: {e}")
    finally:
        is_speaking = False


async def deepgram_pipeline(audio_source: asyncio.Queue):
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
                        break
                    await ws.send(chunk)
                except asyncio.TimeoutError:
                    silence = np.zeros((1024, 1), dtype="int16")
                    await ws.send(silence.tobytes())

        async def receive_transcript():
            async for message in ws:
                result = json.loads(message)
                print(f"[DEBUG DG] {result}")
                # Ignorar eventos que no son transcripción
                if result.get("type") != "Results":
                    continue
                    
                if "channel" in result:
                    transcript = result["channel"]["alternatives"][0]["transcript"]
                    if transcript and result.get("is_final"):
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
                        print(f"[DEBUG] PCM recibido: {len(pcm_audio)} bytes, primer chunk: {pcm_audio[:8].hex()}")
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
        print("[DEEPGRAM] Task terminado")

    await asyncio.gather(
        receive_from_recall(),
        run_deepgram(),
    )