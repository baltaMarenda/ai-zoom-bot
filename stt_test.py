import asyncio
import websockets
import json
import sounddevice as sd
import os
from dotenv import load_dotenv

load_dotenv()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

URL = "wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate=16000"

SAMPLE_RATE = 16000
CHANNELS = 1

async def run():
    async with websockets.connect(
        URL,
        extra_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    ) as ws:

        print("🎤 Escuchando... hablá")

        loop = asyncio.get_event_loop()

        # Cola para pasar audio del mic al websocket
        audio_queue = asyncio.Queue()

        def callback(indata, frames, time, status):
            if status:
                print(status)
            loop.call_soon_threadsafe(audio_queue.put_nowait, indata.copy())

        # Stream de micrófono
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype='int16',
            callback=callback
        )

        async def send_audio():
            with stream:
                while True:
                    data = await audio_queue.get()
                    await ws.send(data.tobytes())

        async def receive_transcript():
            async for message in ws:
                result = json.loads(message)

                if "channel" in result:
                    transcript = result["channel"]["alternatives"][0]["transcript"]
                    if transcript:
                        print("🧠:", transcript)

        await asyncio.gather(send_audio(), receive_transcript())

try:
    asyncio.run(run())
except KeyboardInterrupt:
    print("\n🛑 Test detenido")