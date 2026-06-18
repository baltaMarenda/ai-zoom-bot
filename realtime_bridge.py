"""
realtime_bridge.py
Puente entre el audio PCM16 de Recall.ai y la Realtime API de OpenAI.
Maneja STT + LLM + TTS en un solo WebSocket, con barge-in nativo y function calling.
"""
import asyncio
import base64
import json
import time
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
        run_caja_fase1,      # async fn() → bool  (legacy, por si acaso)
        run_caja_fase2,      # async fn() → bool  (legacy, por si acaso)
        caja_step_buscar,    # async fn(product_name: str) → str
        caja_step_agregar,   # async fn() → str
        caja_step_seleccionar, # async fn(method: str) → str
        caja_step_cerrar,    # async fn(method: str) → str
        pw_start,            # async fn()
        pw_stop,             # async fn()
        on_screenshot,       # async fn(b64: str)
        on_screenshot_end,   # async fn()
        acceso_login_done,   # asyncio.Event
        fase2_press_f8,      # asyncio.Event
        reset_caja_fases,        # fn()
        agent_audio_done_event      = None,   # asyncio.Event — set por agent.html cuando termina de reproducir
        demo_estadisticas           = None,   # async fn() → str
        demo_stock                  = None,   # async fn() → str
        demo_clientes               = None,   # async fn() → str
        balanza_navegar             = None,   # async fn() → str
        balanza_agregar_producto    = None,   # async fn(nombre, id) → str
        balanza_mostrar_tickets     = None,   # async fn() → str
        balanza_ir_a_caja           = None,   # async fn() → str
        balanza_abrir_cf            = None,   # async fn() → str
        balanza_cobrar_ticket       = None,   # async fn() → str
        proveedores_abrir_historial      = None,   # async fn() → str
        proveedores_abrir_modal_compra   = None,   # async fn() → str
        proveedores_registrar_compra     = None,   # async fn() → str
        proveedores_abrir_carrito        = None,   # async fn() → str
        proveedores_cargar_producto      = None,   # async fn() → str
        proveedores_finalizar_detalle    = None,   # async fn() → str
        produccion_ver_plantillas         = None,   # async fn() → str
        produccion_ver_detalle_plantilla  = None,   # async fn() → str
        produccion_ir_a_produccion        = None,   # async fn() → str
        produccion_nueva_produccion       = None,   # async fn() → str
        produccion_seleccionar_plantilla  = None,   # async fn() → str
        produccion_completar_y_registrar  = None,   # async fn() → str
    ):
        self._send_to_agent      = send_to_agent
        self._send_navigate      = send_navigate
        self._send_logged_in     = send_logged_in
        self._send_stop_audio    = send_stop_audio
        self._run_acceso_demo    = run_acceso_demo
        self._run_caja_fase1     = run_caja_fase1
        self._run_caja_fase2     = run_caja_fase2
        self._caja_step_buscar   = caja_step_buscar
        self._caja_step_agregar  = caja_step_agregar
        self._caja_step_seleccionar = caja_step_seleccionar
        self._caja_step_cerrar   = caja_step_cerrar
        self._pw_start           = pw_start
        self._pw_stop            = pw_stop
        self._on_screenshot      = on_screenshot
        self._on_screenshot_end  = on_screenshot_end
        self._acceso_login_done  = acceso_login_done
        self._fase2_press_f8     = fase2_press_f8
        self._reset_caja_fases   = reset_caja_fases
        self._agent_audio_done           = agent_audio_done_event
        self._demo_estadisticas          = demo_estadisticas
        self._demo_stock                 = demo_stock
        self._demo_clientes              = demo_clientes
        self._balanza_navegar            = balanza_navegar
        self._balanza_agregar_producto   = balanza_agregar_producto
        self._balanza_mostrar_tickets    = balanza_mostrar_tickets
        self._balanza_ir_a_caja          = balanza_ir_a_caja
        self._balanza_abrir_cf           = balanza_abrir_cf
        self._balanza_cobrar_ticket      = balanza_cobrar_ticket
        self._proveedores_abrir_historial    = proveedores_abrir_historial
        self._proveedores_abrir_modal_compra = proveedores_abrir_modal_compra
        self._proveedores_registrar_compra   = proveedores_registrar_compra
        self._proveedores_abrir_carrito      = proveedores_abrir_carrito
        self._proveedores_cargar_producto    = proveedores_cargar_producto
        self._proveedores_finalizar_detalle  = proveedores_finalizar_detalle
        self._produccion_ver_plantillas        = produccion_ver_plantillas
        self._produccion_ver_detalle_plantilla = produccion_ver_detalle_plantilla
        self._produccion_ir_a_produccion       = produccion_ir_a_produccion
        self._produccion_nueva_produccion      = produccion_nueva_produccion
        self._produccion_seleccionar_plantilla = produccion_seleccionar_plantilla
        self._produccion_completar_y_registrar = produccion_completar_y_registrar

        self._ws                    = None
        self._pw_started            = False
        self._is_speaking           = False   # True mientras la API envía audio de respuesta
        self._barge_in_active       = False   # True desde que se detecta barge-in hasta response.done/cancelled
        self._mute_until            = 0.0     # monotonic timestamp; drops audio chunks until then
        self._main_session_ready    = False   # True después del primer session.updated exitoso
        self._last_transcript       = ""      # último transcript de Malena (para detectar preguntas)
        self._auto_continue_task: asyncio.Task | None = None
        self._demo_started          = False   # True después del primer navigate_to_module
        self._tool_is_running       = False   # True mientras _handle_tool está ejecutando
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
                # Recall desconectó — cerrar el WS de Realtime para que _receive_loop termine
                try:
                    await ws.close()
                except Exception:
                    pass
                break
            try:
                if time.monotonic() < self._mute_until:
                    continue  # ventana anti-eco: descartar chunk
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
            if delta and not self._barge_in_active:
                self._is_speaking = True
                await self._send_to_agent({
                    "type": "audio_pcm",
                    "data": delta,
                    "sampleRate": 24000,
                })

        elif etype in ("response.audio.done", "response.output_audio.done"):
            self._is_speaking = False
            await self._send_to_agent({"type": "audio_stream_end"})
            # Marcar que el agente debe señalizar cuando termina de reproducir
            if self._agent_audio_done:
                self._agent_audio_done.clear()
            # Abrir ventana anti-eco: descarta audio por 400ms para que el eco
            # de la voz de Malena no llegue a la API como si fuera el usuario.
            self._mute_until = time.monotonic() + 0.8
            try:
                await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
            except Exception:
                pass
            # Auto-continuar solo durante la demo y cuando no hay tool corriendo
            if self._demo_started and not self._tool_is_running:
                self._cancel_auto_continue()
                self._auto_continue_task = asyncio.create_task(self._auto_continue())

        elif etype == "response.cancelled":
            self._is_speaking = False
            self._barge_in_active = False

        elif etype == "response.output_audio_transcript.done":
            transcript = event.get("transcript", "")
            if transcript:
                self._last_transcript = transcript
                print(f"[RT] 🤖 Malena: '{transcript}'")

        elif etype == "input_audio_buffer.speech_started":
            self._cancel_auto_continue()
            if self._is_speaking:
                print("[RT] Barge-in — deteniendo audio")
                self._barge_in_active = True
                await self._send_stop_audio()
            else:
                print("[RT] 🎤 Usuario: [hablando...]")

        elif etype == "input_audio_buffer.speech_stopped":
            print("[RT] 🎤 Usuario: [fin de turno]")

        elif etype == "response.output_item.added":
            item = event.get("item", {})
            if item.get("type") == "function_call":
                call_id = item.get("call_id", "")
                name    = item.get("name", "")
                if call_id:
                    self._pending_calls[call_id] = name

        elif etype == "response.function_call_arguments.done":
            self._cancel_auto_continue()
            call_id  = event.get("call_id", "")
            args_str = event.get("arguments", "{}")
            name     = self._pending_calls.pop(call_id, "")
            if name:
                # Mute audio while tool runs so VAD can't commit mid-execution speech
                self._mute_until = time.monotonic() + 60.0
                try:
                    await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                except Exception:
                    pass
                self._tool_is_running = True
                asyncio.create_task(self._handle_tool(ws, call_id, name, args_str))

        elif etype == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                print(f"[RT] 🎤 Usuario: '{transcript}'")

        elif etype == "response.done":
            status = event.get("response", {}).get("status", "")
            if status == "cancelled":
                print(f"[RT] Respuesta cancelada (barge-in)")
                self._barge_in_active = False

        elif etype == "error":
            err = event.get("error", {})
            msg = err.get("message", "")
            if "input_audio_transcription" in msg:
                print(f"[RT] ℹ️  Transcripción de usuario no soportada por este modelo")
            else:
                print(f"[RT] ⚠️  Error: {err.get('code')} — {msg}")

        elif etype == "session.updated":
            if not self._main_session_ready:
                self._main_session_ready = True
                # Intentar habilitar transcripción como update separado — si falla, no rompe nada
                asyncio.create_task(self._try_enable_transcription(ws))

        elif etype == "conversation.item.created":
            item = event.get("item", {})
            if item.get("role") == "user":
                for part in item.get("content", []):
                    if part.get("type") == "input_text" and part.get("text"):
                        print(f"[RT] 🎤 Usuario: '{part['text']}'")
                    elif part.get("type") == "input_audio" and part.get("transcript"):
                        print(f"[RT] 🎤 Usuario: '{part['transcript']}'")

        elif etype == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                print(f"[RT] 🎤 Usuario: '{transcript}'")

        else:
            # Log eventos no manejados para diagnóstico
            _skip = {"input_audio_buffer.committed", "input_audio_buffer.cleared",
                     "conversation.item.added",
                     "conversation.item.done",
                     "response.created",
                     "response.output_item.done", "response.content_part.added",
                     "response.content_part.done", "rate_limits.updated",
                     "response.output_audio_transcript.delta",
                     "response.function_call_arguments.delta"}
            if etype not in _skip:
                print(f"[RT] Evento no manejado: {etype}")

    # ── Auto-continuación ──────────────────────────────────────────────────────

    def _cancel_auto_continue(self):
        if self._auto_continue_task and not self._auto_continue_task.done():
            self._auto_continue_task.cancel()
        self._auto_continue_task = None

    async def _wait_for_audio_done(self, timeout: float = 30.0):
        """Espera a que agent.html termine de reproducir el audio actual."""
        if not self._agent_audio_done:
            return
        try:
            await asyncio.wait_for(self._agent_audio_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            print("[RT] Timeout esperando audio_done del agente — continuando igual")

    async def _auto_continue(self):
        """Si el usuario no habla después de que Malena termina, continuar la demo."""
        delay = 12.0 if self._last_transcript.rstrip().endswith("?") else 5.0
        try:
            await asyncio.sleep(delay)
            await self._wait_for_audio_done(timeout=15.0)
            if self._ws:
                print("[RT] Auto-continue: disparando siguiente respuesta...")
                await self._ws.send(json.dumps({"type": "response.create"}))
        except asyncio.CancelledError:
            pass

    # ── Configuración de sesión ────────────────────────────────────────────────

    async def _try_enable_transcription(self, ws):
        """Intenta habilitar transcripción de usuario como update separado. Si falla, no afecta la sesión."""
        try:
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "input_audio_transcription": {
                        "model": "gpt-4o-transcribe",
                        "language": "es",
                        "prompt": "Conversación comercial en español rioplatense sobre software de gestión.",
                    },
                },
            }))
        except Exception as e:
            print(f"[RT] No se pudo activar transcripción: {e}")

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

        is_nav = (name == "navigate_to_module")

        try:
            if name == "navigate_to_module":
                result = await self._do_navigate(args.get("module", ""))

            elif name == "caja_buscar_producto":
                await self._ensure_playwright()
                product = args.get("product_name", "Huevos")
                print(f"[CAJA] Buscando '{product}'...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_step_buscar(product)

            elif name == "caja_agregar_producto":
                print("[CAJA] Agregando producto al ticket...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_step_agregar()

            elif name == "caja_seleccionar_pago":
                method = args.get("method", "efectivo")
                print(f"[CAJA] Seleccionando pago: {method}...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_step_seleccionar(method)

            elif name == "caja_cerrar_venta":
                method = args.get("method", "presupuesto")
                print(f"[CAJA] Cerrando venta con {method}...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_step_cerrar(method)
                await self._on_screenshot_end()

            elif name == "demo_estadisticas":
                print("[DEMO] Estadísticas de ventas...")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._demo_estadisticas() if self._demo_estadisticas else "Demo de estadísticas no disponible."
                await self._on_screenshot_end()

            elif name == "demo_stock":
                print("[DEMO] Existencias de stock...")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._demo_stock() if self._demo_stock else "Demo de stock no disponible."
                await self._on_screenshot_end()

            elif name == "demo_clientes":
                print("[DEMO] Formulario de clientes...")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._demo_clientes() if self._demo_clientes else "Demo de clientes no disponible."

            elif name == "balanza_navegar":
                print("[DEMO] Balanza: navegando...")
                await self._do_navigate("BALANZA")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._balanza_navegar() if self._balanza_navegar else "Pantalla de balanza cargada."

            elif name == "balanza_agregar_producto":
                operario_nombre = args.get("operario_nombre", "Balta")
                operario_id     = args.get("operario_id", "1")
                print(f"[DEMO] Balanza: agregando producto a {operario_nombre}...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._balanza_agregar_producto(operario_nombre, operario_id) if self._balanza_agregar_producto else f"Producto asignado a {operario_nombre}."

            elif name == "balanza_mostrar_tickets":
                print("[DEMO] Balanza: mostrando tickets pendientes...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._balanza_mostrar_tickets() if self._balanza_mostrar_tickets else "Tickets pendientes mostrados."

            elif name == "balanza_ir_a_caja":
                print("[DEMO] Balanza: navegando a caja...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._balanza_ir_a_caja() if self._balanza_ir_a_caja else "En sección de caja."

            elif name == "balanza_abrir_cf":
                print("[DEMO] Balanza: abriendo CF...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._balanza_abrir_cf() if self._balanza_abrir_cf else "CF abierto."

            elif name == "balanza_cobrar_ticket":
                print("[DEMO] Balanza: cobrando ticket y cerrando con F8...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._balanza_cobrar_ticket() if self._balanza_cobrar_ticket else "Ticket cobrado."
                await self._on_screenshot_end()

            elif name == "proveedores_abrir_historial":
                print("[DEMO] Proveedores: abriendo historial del proveedor...")
                await self._do_navigate("PROVEEDORES")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._proveedores_abrir_historial() if self._proveedores_abrir_historial else "Historial abierto."

            elif name == "proveedores_abrir_modal_compra":
                print("[DEMO] Proveedores: abriendo modal de nueva compra...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._proveedores_abrir_modal_compra() if self._proveedores_abrir_modal_compra else "Modal de compra abierto."

            elif name == "proveedores_registrar_compra":
                print("[DEMO] Proveedores: registrando compra...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._proveedores_registrar_compra() if self._proveedores_registrar_compra else "Compra registrada."

            elif name == "proveedores_abrir_carrito":
                print("[DEMO] Proveedores: abriendo carrito de detalle...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._proveedores_abrir_carrito() if self._proveedores_abrir_carrito else "Carrito abierto."

            elif name == "proveedores_cargar_producto":
                print("[DEMO] Proveedores: cargando producto Asado...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._proveedores_cargar_producto() if self._proveedores_cargar_producto else "Producto cargado."

            elif name == "proveedores_finalizar_detalle":
                print("[DEMO] Proveedores: finalizando detalle de compra...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._proveedores_finalizar_detalle() if self._proveedores_finalizar_detalle else "Detalle finalizado."
                await self._on_screenshot_end()

            elif name == "produccion_ver_plantillas":
                print("[DEMO] Producción: viendo plantillas...")
                await self._do_navigate("PRODUCCIÓN")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._produccion_ver_plantillas() if self._produccion_ver_plantillas else "Plantillas de producción mostradas."

            elif name == "produccion_ver_detalle_plantilla":
                print("[DEMO] Producción: viendo detalle de la plantilla Milanesas...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._produccion_ver_detalle_plantilla() if self._produccion_ver_detalle_plantilla else "Detalle de plantilla mostrado."

            elif name == "produccion_ir_a_produccion":
                print("[DEMO] Producción: navegando a producción...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._produccion_ir_a_produccion() if self._produccion_ir_a_produccion else "Sección de producción mostrada."

            elif name == "produccion_nueva_produccion":
                print("[DEMO] Producción: abriendo formulario de nueva producción...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._produccion_nueva_produccion() if self._produccion_nueva_produccion else "Formulario de nueva producción abierto."

            elif name == "produccion_seleccionar_plantilla":
                print("[DEMO] Producción: seleccionando plantilla Milanesas...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._produccion_seleccionar_plantilla() if self._produccion_seleccionar_plantilla else "Plantilla seleccionada."

            elif name == "produccion_completar_y_registrar":
                print("[DEMO] Producción: completando y registrando producción...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._produccion_completar_y_registrar() if self._produccion_completar_y_registrar else "Producción registrada."
                await self._on_screenshot_end()

            else:
                result = f"Tool desconocida: {name}"

        except Exception as e:
            print(f"[TOOL] Error en {name}: {e}")
            traceback.print_exc()
            result = f"Error ejecutando {name}: {e}"

        if is_nav:
            await self._send_nav_result(ws, call_id, result)
        else:
            await self._send_tool_result(ws, call_id, result)

        self._tool_is_running = False

    async def _send_tool_result(self, ws, call_id: str, output: str):
        # Clear any audio accumulated during tool execution, resume with short mute
        self._mute_until = time.monotonic() + 1.5
        try:
            await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
        except Exception:
            pass
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

    async def _send_nav_result(self, ws, call_id: str, output: str):
        """Envía tool result de navigate_to_module SIN response.create — auto-continue lo dispara."""
        self._mute_until = time.monotonic() + 1.5
        try:
            await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
        except Exception:
            pass
        try:
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }))
            # Sin response.create — el auto-continue lo dispara después del audio
            if self._demo_started:
                self._cancel_auto_continue()
                self._auto_continue_task = asyncio.create_task(self._auto_continue())
        except Exception as e:
            print(f"[RT] Error enviando nav result: {e}")

    # ── Navegación ─────────────────────────────────────────────────────────────

    async def _do_navigate(self, module: str) -> str:
        path = DEMO_MODULE_PATHS.get(module)
        if not path:
            return f"Módulo '{module}' no encontrado."

        self._demo_started = True
        print(f"[RT] Navegando a: {module} → {path}")
        await self._send_navigate(path)

        if module == "ACCESO":
            await self._ensure_playwright()
            asyncio.create_task(self._run_acceso_with_signal())

        return (
            f"Módulo {module} cargado en pantalla. "
            f"Describí en 1-2 frases lo que los usuarios ven ahora. "
            f"STOP — no llamés ninguna tool ni navigate_to_module en esta respuesta. "
            f"Terminá de hablar y el sistema te dará el turno automáticamente para continuar."
        )

    async def _run_acceso_with_signal(self):
        ok = await self._run_acceso_demo()
        print(f"[RT] ACCESO demo {'✓' if ok else '✗'}")

    async def _delayed_task(self, coro_fn, delay: float):
        """Espera `delay` segundos y luego ejecuta coro_fn() — da tiempo a que Malena hable primero."""
        await asyncio.sleep(delay)
        await coro_fn()

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
