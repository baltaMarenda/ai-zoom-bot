"""
realtime_bridge.py
Puente entre el audio PCM16 de Recall.ai y la Realtime API de OpenAI.
Maneja STT + LLM + TTS en un solo WebSocket, con barge-in nativo y function calling.
"""
import asyncio
import base64
import json
import traceback

import websockets

from config import (
    OPENAI_API_KEY,
    OPENAI_REALTIME_URL,
    REALTIME_SYSTEM_PROMPT,
    REALTIME_TOOLS,
    DEMO_MODULE_PATHS,
)


class RealtimeBridge:
    """
    Gestiona la sesión con la Realtime API de OpenAI.

    El caller (bot.py) crea una instancia por sesión de Recall y llama a run(audio_queue).
    El bridge maneja toda la lógica de audio, tools y eventos de forma autónoma.
    """

    def __init__(
        self,
        send_to_agent,       # async fn(dict) — envía mensajes a agent.html
        send_navigate,       # async fn(path: str)
        send_logged_in,      # async fn()
        send_stop_audio,     # async fn()
        run_acceso_demo,     # async fn() → bool
        run_caja_fase1,      # async fn() → bool
        run_caja_fase2,      # async fn() → bool
        pw_start,            # async fn()
        pw_stop,             # async fn()
        on_screenshot,       # async fn(b64: str)
        on_screenshot_end,   # async fn()
        acceso_login_done,   # asyncio.Event
        fase2_press_f8,      # asyncio.Event
        reset_caja_fases,    # fn()
        conv_state,          # ConversationState
    ):
        self._send_to_agent    = send_to_agent
        self._send_navigate    = send_navigate
        self._send_logged_in   = send_logged_in
        self._send_stop_audio  = send_stop_audio
        self._run_acceso_demo  = run_acceso_demo
        self._run_caja_fase1   = run_caja_fase1
        self._run_caja_fase2   = run_caja_fase2
        self._pw_start         = pw_start
        self._pw_stop          = pw_stop
        self._on_screenshot    = on_screenshot
        self._on_screenshot_end = on_screenshot_end
        self._acceso_login_done = acceso_login_done
        self._fase2_press_f8   = fase2_press_f8
        self._reset_caja_fases = reset_caja_fases
        self.conv_state        = conv_state

        self._ws               = None
        self._pw_started       = False
        self._is_speaking      = False   # True mientras la API envía audio de respuesta
        # call_id → name (para asociar arguments.done con el nombre de la tool)
        self._pending_calls: dict[str, str] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def run(self, audio_queue: asyncio.Queue):
        """Bucle principal: conecta, procesa, reconecta ante errores."""
        while True:
            try:
                async with websockets.connect(
                    OPENAI_REALTIME_URL,
                    extra_headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                    },
                    ping_interval=20,
                    ping_timeout=30,
                    close_timeout=10,
                ) as ws:
                    self._ws = ws
                    print("[RT] Conectado a OpenAI Realtime API ✓")

                    await asyncio.gather(
                        self._send_audio_loop(ws, audio_queue),
                        self._receive_loop(ws),
                    )
                # audio_queue recibió None (Recall desconectó) — salida limpia
                break

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[RT] Error: {type(e).__name__}: {e} — reconectando en 3s...")
                await asyncio.sleep(3)

        self._ws = None
        print("[RT] Bridge finalizado")

    async def close(self):
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── Audio hacia la Realtime API ────────────────────────────────────────────

    async def _send_audio_loop(self, ws, audio_queue: asyncio.Queue):
        """Lee PCM16 de la queue y lo envía a la Realtime API."""
        while True:
            chunk = await audio_queue.get()
            if chunk is None:
                break
            try:
                b64 = base64.b64encode(chunk).decode()
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": b64,
                }))
            except Exception as e:
                print(f"[RT] Error enviando audio: {e}")
                break

    # ── Recepción de eventos ───────────────────────────────────────────────────

    async def _receive_loop(self, ws):
        async for raw in ws:
            try:
                event = json.loads(raw)
                await self._dispatch(ws, event)
            except Exception as e:
                print(f"[RT] Error procesando evento: {e}")
                traceback.print_exc()

    async def _dispatch(self, ws, event: dict):
        etype = event.get("type", "")

        if etype == "session.created":
            await self._on_session_created(ws)

        elif etype in ("response.audio.delta", "response.output_audio.delta"):
            delta = event.get("delta", "")
            if delta:
                self._is_speaking = True
                await self._send_to_agent({
                    "type": "audio_pcm",
                    "data": delta,
                    "sampleRate": 24000,
                })

        elif etype in ("response.audio.done", "response.output_audio.done"):
            self._is_speaking = False
            await self._send_to_agent({"type": "audio_stream_end"})

        elif etype == "response.cancelled":
            self._is_speaking = False

        elif etype == "response.output_audio_transcript.done":
            transcript = event.get("transcript", "")
            if transcript:
                print(f"[RT] 🤖 Malena: '{transcript}'")

        elif etype == "input_audio_buffer.speech_started":
            if self._is_speaking:
                print("[RT] Barge-in — deteniendo audio")
                await self._send_stop_audio()

        elif etype == "response.output_item.added":
            item = event.get("item", {})
            if item.get("type") == "function_call":
                call_id = item.get("call_id", "")
                name    = item.get("name", "")
                if call_id:
                    self._pending_calls[call_id] = name

        elif etype == "response.function_call_arguments.done":
            call_id  = event.get("call_id", "")
            args_str = event.get("arguments", "{}")
            name     = self._pending_calls.pop(call_id, "")
            if name:
                asyncio.create_task(self._handle_tool(ws, call_id, name, args_str))

        elif etype == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                print(f"[RT] 🎤 Usuario: '{transcript}'")

        elif etype == "response.done":
            status = event.get("response", {}).get("status", "")
            if status == "cancelled":
                print(f"[RT] Respuesta cancelada (barge-in)")

        elif etype == "error":
            err = event.get("error", {})
            print(f"[RT] ⚠️  Error: {err.get('code')} — {err.get('message')}")

        else:
            # Log eventos no manejados para diagnóstico
            _skip = {"input_audio_buffer.committed", "input_audio_buffer.speech_stopped",
                     "input_audio_buffer.cleared",
                     "conversation.item.created", "conversation.item.added",
                     "conversation.item.done",
                     "session.updated", "response.created",
                     "response.output_item.done", "response.content_part.added",
                     "response.content_part.done", "rate_limits.updated",
                     "response.output_audio_transcript.delta",
                     "response.function_call_arguments.delta"}
            if etype not in _skip:
                print(f"[RT] Evento no manejado: {etype}")

    # ── Configuración de sesión ────────────────────────────────────────────────

    async def _on_session_created(self, ws):
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": REALTIME_SYSTEM_PROMPT,
                "tools": REALTIME_TOOLS,
                "tool_choice": "auto",
            },
        }))

        # Limpiar audio acumulado antes del intro para evitar que el VAD cancele la respuesta
        await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))

        # Disparar el saludo inicial de Malena (ella habla primero al unirse)
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Hola"}],
            },
        }))
        await ws.send(json.dumps({"type": "response.create"}))
        print("[RT] Sesión configurada, intro disparada ✓")

    async def inject_context(self, text: str):
        """Inyecta un mensaje de usuario para guiar al AI (transiciones de stage, etc.)."""
        if self._ws:
            try:
                await self._ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }))
                await self._ws.send(json.dumps({"type": "response.create"}))
            except Exception as e:
                print(f"[RT] inject_context error: {e}")

    # ── Tool dispatch ──────────────────────────────────────────────────────────

    async def _handle_tool(self, ws, call_id: str, name: str, args_str: str):
        try:
            args = json.loads(args_str)
        except Exception:
            args = {}

        print(f"[TOOL] {name}({args})")

        if name == "navigate_to_module":
            result = await self._do_navigate(args.get("module", ""))

        elif name == "demo_caja_fase1":
            await self._ensure_playwright()
            asyncio.create_task(self._run_caja_fase1())
            result = "Buscando y agregando el producto en caja."

        elif name == "demo_caja_fase2":
            await self._ensure_playwright()
            asyncio.create_task(self._run_caja_fase2())
            result = "Seleccionando método de pago y cerrando la venta."

        else:
            result = f"Tool desconocida: {name}"

        await self._send_tool_result(ws, call_id, result)

    async def _send_tool_result(self, ws, call_id: str, output: str):
        try:
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }))
            await ws.send(json.dumps({"type": "response.create"}))
        except Exception as e:
            print(f"[RT] Error enviando resultado de tool: {e}")

    # ── Navegación ─────────────────────────────────────────────────────────────

    async def _do_navigate(self, module: str) -> str:
        path = DEMO_MODULE_PATHS.get(module)
        if not path:
            return f"Módulo '{module}' no encontrado."

        print(f"[RT] Navegando a: {module} → {path}")
        await self._send_navigate(path)

        if module == "ACCESO":
            await self._ensure_playwright()
            asyncio.create_task(self._run_acceso_with_signal())

        return f"Navegando a {module}."

    async def _run_acceso_with_signal(self):
        ok = await self._run_acceso_demo()
        print(f"[RT] ACCESO demo {'✓' if ok else '✗'}")

    # ── Playwright lifecycle ───────────────────────────────────────────────────

    async def _ensure_playwright(self):
        if self._pw_started:
            return
        self._pw_started = True
        self._reset_caja_fases()
        self._acceso_login_done.clear()
        self._fase2_press_f8.clear()
        try:
            await self._pw_start()
            await self._send_logged_in()
        except Exception as e:
            print(f"[RT] Error iniciando Playwright: {e}")
            self._pw_started = False
