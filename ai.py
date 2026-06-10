"""
ai.py
"""
import asyncio
import re
import requests
import json
from openai import OpenAI
from config import (
    OPENAI_API_KEY,
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ID,
    PROMPTS_BY_STAGE,
)

client = OpenAI(api_key=OPENAI_API_KEY)
conversation = []

# Separadores de oraciones para el streaming
SENTENCE_ENDINGS = re.compile(r'(?<=[.!?\n])\s+')


def ask_ai(text: str, stage: str = "intro") -> str:
    """Versión síncrona — se mantiene para compatibilidad."""
    conversation.append({"role": "user", "content": text})
    system_prompt = PROMPTS_BY_STAGE.get(stage, PROMPTS_BY_STAGE["intro"])

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=conversation,
        instructions=system_prompt,
    )
    reply = response.output_text
    conversation.append({"role": "assistant", "content": reply})

    if len(conversation) > 20:
        conversation.pop(1)
        conversation.pop(1)

    return reply


async def ask_ai_streaming(text: str, stage: str = "intro") -> tuple[str, asyncio.Queue]:
    """
    Llama a OpenAI con streaming, acumula el texto completo y llena
    una Queue con oraciones listas para TTS a medida que llegan.

    Retorna (full_reply, sentence_queue).
    La queue termina con None como señal de fin.
    """
    conversation.append({"role": "user", "content": text})
    system_prompt = PROMPTS_BY_STAGE.get(stage, PROMPTS_BY_STAGE["intro"])

    sentence_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _stream_and_enqueue():
        """Corre en un thread executor: hace el streaming y pone oraciones en la queue."""
        full_text = ""
        buffer = ""

        try:
            with client.responses.stream(
                model="gpt-4.1-mini",
                input=conversation,
                instructions=system_prompt,
            ) as stream:
                for event in stream:
                    # El evento de texto delta en la API responses
                    if hasattr(event, 'type') and event.type == 'response.output_text.delta':
                        delta = event.delta
                        if delta:
                            buffer += delta
                            full_text += delta

                            # Detectar fin de oración y encolar
                            parts = SENTENCE_ENDINGS.split(buffer)
                            if len(parts) > 1:
                                # Todas menos la última son oraciones completas
                                for sentence in parts[:-1]:
                                    sentence = sentence.strip()
                                    if sentence:
                                        loop.call_soon_threadsafe(
                                            sentence_queue.put_nowait, sentence
                                        )
                                buffer = parts[-1]  # Lo que quedó sin terminar

        except Exception as e:
            print(f"[ERROR streaming] {e}")
            # Fallback: usar ask_ai síncrono
            full_text = ask_ai.__wrapped__(text, stage) if hasattr(ask_ai, '__wrapped__') else ""

        # Encolar lo que quedó en el buffer (última oración sin punto final)
        if buffer.strip():
            loop.call_soon_threadsafe(sentence_queue.put_nowait, buffer.strip())

        # Señal de fin
        loop.call_soon_threadsafe(sentence_queue.put_nowait, None)

        return full_text

    # Correr el streaming en un thread para no bloquear el event loop
    full_reply = await loop.run_in_executor(None, _stream_and_enqueue)

    # Actualizar conversación con la respuesta completa
    # (puede que ya esté en full_reply si el stream terminó bien)
    if full_reply:
        conversation.append({"role": "assistant", "content": full_reply})
    else:
        # Si el stream falló, hacer llamada normal como fallback
        full_reply = ask_ai(text, stage)
        # Encolar la respuesta completa como una sola "oración"
        await sentence_queue.put(full_reply)
        await sentence_queue.put(None)

    if len(conversation) > 20:
        conversation.pop(1)
        conversation.pop(1)

    return full_reply, sentence_queue


def text_to_speech(text: str) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    data = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }
    response = requests.post(url, json=data, headers=headers, timeout=30)
    response.raise_for_status()
    return response.content


def reset_conversation():
    conversation.clear()


def classify_with_ai(text: str) -> str:
    """
    Clasifica una interrupción del usuario en:
      - "noise"       → ruido o accidental, ignorar
      - "backchannel" → confirma, acompaña o pide continuar
      - "question"    → pregunta real que hay que responder

    Usa GPT con temperatura 0 para máxima precisión y baja latencia.
    """
    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            max_output_tokens=16,
            instructions="""Sos un clasificador de interrupciones en una demo de ventas por videollamada en Argentina.
El bot (Malena) está hablando (o acaba de hablar) y el usuario dice algo. Clasificá en UNA categoría:

- noise: ruido o sílabas sin contenido ("eh", "mm", "ah", "mmm", "a")
- backchannel: acompañamiento mínimo SIN información — solo 1 a 3 palabras de reacción pura ("bueno", "dale", "ok", "claro", "perfecto", "entiendo", "re bien", "sí sí", "está bien", "de acuerdo", "ya"). Si empieza con "sí"/"no" pero agrega cualquier otro contenido → NO es backchannel.
- question: todo lo demás — preguntas, respuestas con información ("sí, hacemos X", "no, usamos Y", "tenemos una carnicería"), comentarios, pedidos de aclaración, o cualquier texto con contenido real más allá del acompañamiento.

Ejemplos:
"Sí" → backchannel
"Dale." → backchannel
"Claro, claro." → backchannel
"Sí, hacemos milanesas." → question (tiene información)
"No, no tenemos sistema." → question (tiene información)
"¿Y eso cómo funciona?" → question
"Ah, buenísimo." → backchannel
"Mmm" → noise
"Eh" → noise

REGLA CLAVE: si el texto contiene información concreta más allá de una sola reacción de acompañamiento, siempre es question.
Respondé SOLO con una palabra: noise, backchannel, o question.""",
            input=[{"role": "user", "content": text}],
        )
        result = response.output_text.strip().lower()
        if result not in ("noise", "backchannel", "question"):
            return "backchannel"
        return result
    except Exception as e:
        print(f"[classify_with_ai] Error: {e}, fallback backchannel")
        return "backchannel"


def extract_lead_info(conversation_text: str) -> dict:
    response = client.responses.create(
        model="gpt-4.1-mini",
        max_output_tokens=100,
        instructions="""
Sos un extractor de datos. Del texto que te paso, extraé:
- nombre: el nombre propio de la persona (solo el nombre, sin apellido si no lo dice)
- negocio: el tipo o rubro del negocio que mencionó

Respondé SOLO con JSON válido, sin explicaciones ni markdown.
Ejemplo: {"nombre": "Baltazar", "negocio": "carnicería"}
Si no encontrás algún dato, usá null.
""",
        input=[{"role": "user", "content": conversation_text}],
    )
    try:
        return json.loads(response.output_text)
    except Exception:
        return {"nombre": None, "negocio": None}