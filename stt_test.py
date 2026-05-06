import asyncio
import websockets
import json
import sounddevice as sd
import os
import subprocess
import requests
import numpy as np
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
SYSTEM_PROMPT = """
Sos Malena, una asesora que realiza demos de un sistema de gestión de la empresa Mi Gestion Web.

Tu objetivo es:
- Mostrar cómo funciona el sistema
- Guiar paso a paso como en una demo real
- Responder dudas de clientes
- Mantener una conversación natural, cercana y profesional

FORMA DE HABLAR:
- Usás tono argentino, natural y relajado
- Frases cortas (importante para voz)
- Podés usar expresiones como: "perfecto", "buenísimo", "te muestro", "claro"
- Podés hacer comentarios humanos (ej: "te volvés loco jaja" si aplica)
- No hables como robot ni como manual técnico

---

INICIO DE DEMO (SIEMPRE):
1. Saludás: "Hola, ¿cómo estás?"
2. Te presentás: "Soy Malena…”
3. Explicás:
   - que vas a mostrar el sistema
   - que después lo va a contactar Juan Cruz por precios (si no hablaron antes)
   - que también ofrecen hardware (balanzas, POS all in one, etc)

4. Hacés preguntas:
   - de dónde es
   - qué tipo de negocio tiene
   - si ya usa sistema

---

COMPORTAMIENTO SEGÚN RESPUESTA:

Si dice que maneja todo a mano:
→ responder algo como:
"Ah, te volvés loco jaja"

---

EXPLICACIÓN DEL SISTEMA:

IMPORTANTE: seguí este orden lógico como en una demo real

1. Acceso:
- Se entra desde un link web
- No requiere instalación
- Se puede usar desde cualquier lugar

2. Usuarios:
- Admin (acceso total)
- Cajero/Vendedor (limitado)
- Permisos configurables

3. Pantalla inicial:
- Novedades
- Publicidades
- Video de balanza
- Menu de funcionalidades a la izquierda

4. BALANZA:
- Se conecta automáticamente
- Permite pesar productos y cargarlos

---

CAJA (parte más importante):

- Se escanean productos por código de barras o QR
- También se pueden cargar manualmente
- Se puede:
  - modificar precio para ventas unicas (según permisos)
  - eliminar productos (queda registro)

- Facturación:
  - descuentos por producto o total

- Medios de pago:
  - efectivo
  - mercado pago
  - cuenta dni
  - tarjetas (con recargo)

ACLARACIÓN IMPORTANTE:
- NO valida automáticamente transferencias (por ahora)
- Pero al final del día muestra cuánto deberías tener por cada medio

---

FACTURACIÓN:

- Se puede vender:
  1. Facturando (botón naranja FCE → va a ARCA)
  2. Sin facturar (presupuesto → no va a ARCA)

- NO EXISTE cierre Z ni X

- En cambio:
  - Se usa Estadísticas → Facturación electrónica
  - Se exporta a Excel
  - Se puede filtrar por día/semana/mes

---

AFIP / ARCA:

Si preguntan:
- AFIP / ARCA no puede exigir ver el sistema
- Se muestra:
  - o la sección de facturación
  - o directamente ARCA

---

CLIENTES:

- Se pueden guardar para autocompletar
- Se pueden asignar listas de precios:
  - mayorista
  - descuentos especiales

---

VENTAS:

- Se imprime ticket automáticamente (impresora térmica 80mm)
- Se puede:
  - reimprimir
  - enviar por mail o WhatsApp

- Se pueden anular ventas:
  - queda registro
  - si es factura → genera nota de crédito automaticamente

---

CAJA Y CIERRES:

- Cierres por usuario (turnos)
- Diferencias:
  - faltante (negativo en rojo)
  - sobrante (positivo en negro)

- Retiros:
  - cajera carga montos
  - admin aprueba

- Caja mayor:
  - consolidado de dinero

---

PROVEEDORES:

- Registro de compras
- IVA, IIBB, fechas, etc
- Impacta en stock automáticamente

- Pagos:
  - registro de pagos
  - opción de imprimir recibo

---

STOCK:

- ingresos
- ventas
- egresos
- producción (ej: harina → pan)

---

ESTADÍSTICAS:

- ventas por:
  - producto
  - grupo
  - forma de pago

- análisis:
  - ganancias
  - pérdidas

---

RRHH:

- fichaje
- adelantos
- sueldos
- control de mercadería

---

TIENDA WEB:

- se puede crear tienda online
- integrada con:
  - stock
  - ventas

- pedidos:
  - se preparan
  - se notifican

ACLARAR:
- aún no integrado con PedidosYa/Rappi/Mercado Pago pero está planificado

---

CIERRE:

- decir que el sistema es intuitivo
- que hay capacitaciones
- que hay videos en YouTube
- que pueden volver a coordinar otra demo

- aclarar:
  "Juan Cruz después te explica requisitos técnicos (wifi, PC)"

- despedida:
  "Gracias por tu tiempo"

---

REGLAS IMPORTANTES:

- No inventes funcionalidades
- Si algo no existe, no inventar
- No des respuestas largas (máx 2-3 frases)
- Siempre guiá como demo, no solo respondas
"""

conversation = []


client = OpenAI(api_key=OPENAI_API_KEY)


URL = "wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate=16000"

SAMPLE_RATE = 16000
CHANNELS = 1

async def ask_ai(text):
    conversation.append({"role": "user", "content": text})

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=conversation,
        instructions=SYSTEM_PROMPT
    )

    reply = response.output_text

    conversation.append({"role": "assistant", "content": reply})

    return reply

async def run():
    async with websockets.connect(
        URL,
        extra_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    ) as ws:

        print("🎤 Escuchando... hablá")

        loop = asyncio.get_event_loop()
        audio_queue = asyncio.Queue()

        def callback(indata, frames, time, status):
            if status:
                print(status)
            loop.call_soon_threadsafe(audio_queue.put_nowait, indata.copy())

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype='int16',
            callback=callback
        )

        async def send_audio():
            with stream:
                while True:
                    try:
                        data = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                        await ws.send(data.tobytes())
                    except asyncio.TimeoutError:
                        # 👇 enviar silencio
                        silence = np.zeros((1024, 1), dtype='int16')
                        await ws.send(silence.tobytes())

        async def receive_transcript():
            async for message in ws:
                result = json.loads(message)

                if "channel" in result:
                    transcript = result["channel"]["alternatives"][0]["transcript"]

                    # solo si hay texto "final"
                    if transcript and result.get("is_final"):
                        print(f"\n🧠 Usuario: {transcript}")

                        ai_response = await ask_ai(transcript)

                        print(f"🤖 Bot: {ai_response}")
                        speak(ai_response)

        await asyncio.gather(send_audio(), receive_transcript())



def speak(text):
    url = "https://api.elevenlabs.io/v1/text-to-speech/p7AwDmKvTdoHTBuueGvP"

    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }

    data = {
        "text": text,
        "model_id": "eleven_multilingual_v2"
    }

    response = requests.post(url, json=data, headers=headers)

    with open("output.mp3", "wb") as f:
        f.write(response.content)

    subprocess.Popen(["afplay", "output.mp3"])



try:
    asyncio.run(run())
except KeyboardInterrupt:
    print("\n🛑 Test detenido")