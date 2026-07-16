"""
realtime_bridge.py
Puente entre el audio PCM16 de Recall.ai y la Realtime API de OpenAI.
Maneja STT + LLM + TTS en un solo WebSocket, con barge-in nativo y function calling.
"""
import asyncio
import base64
import difflib
import json
import time
import traceback

import numpy as np
import websockets

from config import (
    OPENAI_API_KEY,
    OPENAI_REALTIME_URL,
    TRAINING_SYSTEM_PROMPT,
    REALTIME_TOOLS,
    DEMO_MODULE_PATHS,
    CONFIG_MODULE_PATHS,
)

# Recall.ai manda audio_separate_raw a 16kHz mono PCM16, pero la Realtime API
# de OpenAI solo acepta audio/pcm a 24kHz como input. Sin resamplear, el audio
# le llega "acelerado" (interpretado a 24kHz siendo en realidad 16kHz), lo que
# degrada el VAD y la transcripción — entre otras cosas, falla en detectar el
# fin de turno de forma confiable.
_RECALL_SAMPLE_RATE = 16000
_OPENAI_SAMPLE_RATE = 24000

# Tools de puro "cleanup" (ej: cerrar un modal para revelar lo que hay debajo) que
# pueden correr en silencio SIN disparar la protección anti "cascada muda": no avanzan
# el guion ni saltan de pantalla, solo dejan visible lo que se va a narrar a continuación.
_SILENT_OK_TOOLS = frozenset({"config_usuarios_cerrar_modal"})


def _resample_16k_to_24k(pcm16: bytes) -> bytes:
    if not pcm16:
        return pcm16
    samples = np.frombuffer(pcm16, dtype="<i2")
    if samples.size == 0:
        return pcm16
    n_out = int(round(samples.size * _OPENAI_SAMPLE_RATE / _RECALL_SAMPLE_RATE))
    x_old = np.arange(samples.size)
    x_new = np.linspace(0, samples.size - 1, n_out)
    resampled = np.interp(x_new, x_old, samples.astype(np.float32))
    return resampled.astype("<i2").tobytes()


# Bias de vocabulario para gpt-4o-transcribe (ver _on_session_created). Cuando el audio
# de un turno no tiene voz real (ruido, cola de eco, turno abierto por error), el modelo
# de transcripción "autocompleta" devolviendo este mismo texto como si fuera lo que dijo
# el usuario — es una alucinación conocida de Whisper/gpt-4o-transcribe ante audio sin
# contenido. _is_prompt_echo() detecta esos casos para no loguearlos como habla real.
TRANSCRIPTION_PROMPT = (
    "Mi Gestión Web, MGW, caja, balanza, ARCA, AFIP, presupuesto, factura electrónica, "
    "FCE, F8, F4, stock, proveedores, producción, vuelto"
)


def _is_prompt_echo(transcript: str) -> bool:
    norm = transcript.strip().strip(".,").lower()
    # Una palabra suelta del rubro ("caja", "stock") es habla real perfectamente posible —
    # solo tratamos como alucinación una porción larga/multi-palabra que coincide con el
    # prompt, no cualquier substring corto (sino "caja" sola ya matchearía siempre).
    if len(norm) < 20:
        return False
    prompt_norm = TRANSCRIPTION_PROMPT.lower()
    return norm in prompt_norm or difflib.SequenceMatcher(None, norm, prompt_norm).ratio() > 0.6


class RealtimeBridge:
    """
    Gestiona la sesión con la Realtime API de OpenAI.

    El caller (bot.py) crea una instancia por sesión de Recall y llama a run(audio_queue).
    El bridge maneja toda la lógica de audio, tools y eventos de forma autónoma.
    """

    # Backstop: si no llega audio nuevo en este lapso con un turno de usuario abierto,
    # forzamos el commit manualmente. Cubre el caso en que Recall deja de mandar paquetes
    # durante silencio real (p.ej. DTX) y el VAD del servidor nunca llega a evaluarlo.
    # Tiene que ser bastante más laxo que una pausa normal al hablar (respirar, pensar
    # la frase) — con 0.9s cortaba turnos a mitad de oración y partía la transcripción.
    _TURN_GAP_TIMEOUT = 2.5

    # La API rechaza un commit si el buffer tiene menos de esto (error
    # input_audio_buffer_commit_empty). Si el watchdog dispara con menos que esto
    # acumulado, no hay nada real que comitear.
    _MIN_COMMIT_MS = 100.0

    def __init__(
        self,
        send_to_agent,       # async fn(dict) — envía mensajes a agent.html
        send_navigate,       # async fn(path: str)
        send_logged_in,      # async fn()
        send_stop_audio,     # async fn()
        run_acceso_demo,     # async fn() → bool
        silent_login,        # async fn() → bool  (login silencioso sin demo)
        run_caja_fase1,      # async fn() → bool  (legacy, por si acaso)
        run_caja_fase2,      # async fn() → bool  (legacy, por si acaso)
        caja_step_buscar,    # async fn(product_name: str) → str
        caja_step_agregar,   # async fn() → str
        caja_step_seleccionar, # async fn(method: str) → str
        caja_step_descuento, # async fn() → str
        caja_step_cerrar,    # async fn(method: str) → str
        pw_start,            # async fn()
        pw_stop,             # async fn()
        on_screenshot,       # async fn(b64: str)
        on_screenshot_end,   # async fn()
        acceso_login_done,   # asyncio.Event
        fase2_press_f8,      # asyncio.Event
        reset_caja_fases,        # fn()
        system_prompt               = None,   # str — prompt de sistema de ESTA sesión (con foco module/field/user_name)
        agent_audio_done_event      = None,   # asyncio.Event — set por agent.html cuando termina de reproducir
        demo_estadisticas           = None,   # async fn() → str
        demo_stock                  = None,   # async fn() → str
        demo_clientes               = None,   # async fn() → str
        clientes_nuevo_cliente      = None,   # async fn() → str
        clientes_importar           = None,   # async fn() → str
        clientes_ver_detalle        = None,   # async fn() → str
        balanza_navegar             = None,   # async fn() → str
        balanza_agregar_producto    = None,   # async fn(nombre, id) → str
        balanza_mostrar_tickets     = None,   # async fn() → str
        balanza_ir_a_caja           = None,   # async fn() → str
        balanza_abrir_cf            = None,   # async fn() → str
        balanza_cobrar_ticket       = None,   # async fn() → str
        proveedores_ver_lista            = None,   # async fn() → str
        proveedores_abrir_historial      = None,   # async fn() → str
        proveedores_abrir_modal_compra   = None,   # async fn() → str
        proveedores_registrar_compra     = None,   # async fn() → str
        proveedores_abrir_carrito        = None,   # async fn() → str
        proveedores_cargar_producto      = None,   # async fn() → str
        proveedores_finalizar_detalle    = None,   # async fn() → str
        proveedores_registrar_pago       = None,   # async fn() → str
        produccion_ver_plantillas         = None,   # async fn() → str
        produccion_ver_detalle_plantilla  = None,   # async fn() → str
        produccion_ir_a_produccion        = None,   # async fn() → str
        produccion_nueva_produccion       = None,   # async fn() → str
        produccion_seleccionar_plantilla  = None,   # async fn() → str
        produccion_completar_y_registrar  = None,   # async fn() → str
        config_navegar                        = None,   # async fn(seccion: str) → str
        config_usuarios_nuevo                 = None,   # async fn() → str
        config_usuarios_scroll_permisos_de    = None,   # async fn() → str
        config_usuarios_expandir_permisos_caja = None,  # async fn() → str
        config_usuarios_cerrar_modal          = None,   # async fn() → str
        config_listas_nueva                   = None,   # async fn() → str
        config_grupos_nuevo                   = None,   # async fn() → str
        config_productos_nuevo                = None,   # async fn() → str
        config_productos_importar             = None,   # async fn() → str
        config_precios_editar_grupo_almacen   = None,   # async fn() → str
        config_precios2_grupo_carne           = None,   # async fn() → str
        config_precios_historial_detalle_grupo    = None,   # async fn() → str
        config_precios_historial_detalle_producto = None,   # async fn() → str
        config_combos_nuevo                   = None,   # async fn() → str
        config_combos_editar                  = None,   # async fn() → str
        config_formas_pago_nueva              = None,   # async fn() → str
        config_descuentos_nuevo               = None,   # async fn() → str
        config_terminales_nueva               = None,   # async fn() → str
        config_impuestos_nuevo                = None,   # async fn() → str
        config_gastos_nuevo_concepto          = None,   # async fn() → str
        config_gastos_crear_concepto          = None,   # async fn() → str
        config_gastos_eliminar_concepto       = None,   # async fn() → str
        finalizar_capacitacion                = None,   # async fn() → str
        # Módulo 2: Caja y Caja Mayor
        caja_ir_a_apertura                    = None,   # async fn() → str
        caja_abrir_turno                      = None,   # async fn() → str
        caja_ver_lista_ventas                 = None,   # async fn() → str
        caja_ver_detalle_venta                = None,   # async fn() → str
        caja_retiros_navegar                  = None,   # async fn() → str
        caja_retiros_nuevo                    = None,   # async fn() → str
        caja_retiros_ingresar_ejemplo         = None,   # async fn() → str
        caja_retiros_abrir_aprobar            = None,   # async fn() → str
        caja_retiros_confirmar_aprobar        = None,   # async fn() → str
        caja_cierre_navegar                   = None,   # async fn() → str
        caja_cierre_nuevo                     = None,   # async fn() → str
        caja_cierre_confirmar                 = None,   # async fn() → str
        caja_cierre_ver_resultado             = None,   # async fn() → str
        caja_cierre_nuevo_movimiento          = None,   # async fn() → str
        caja_cierre_movimiento_pago_proveedor = None,   # async fn() → str
        caja_cierre_movimiento_finalizar_proveedor = None,   # async fn() → str
        caja_mayor_navegar                    = None,   # async fn() → str
        caja_mayor_nuevo_arqueo               = None,   # async fn() → str
        caja_mayor_detalle_arqueo             = None,   # async fn() → str
        caja_mayor_ver_movimientos            = None,   # async fn() → str
        caja_mayor_cheques_navegar            = None,   # async fn() → str
        caja_mayor_cheques_emitir             = None,   # async fn() → str
        caja_mayor_cheques_completar          = None,   # async fn() → str
        caja_mayor_cheques_filtrar_todos      = None,   # async fn() → str
        rrhh_navegar                          = None,   # async fn() → str
        rrhh_personal_nuevo                   = None,   # async fn() → str
        rrhh_personal_editar                  = None,   # async fn() → str
        rrhh_personal_ficha                   = None,   # async fn() → str
        rrhh_personal_cliente_asociado        = None,   # async fn() → str
        rrhh_fichaje_navegar                  = None,   # async fn() → str
        rrhh_fichaje_nuevo                    = None,   # async fn() → str
    ):
        self._send_to_agent      = send_to_agent
        self._send_navigate      = send_navigate
        self._send_logged_in     = send_logged_in
        self._send_stop_audio    = send_stop_audio
        self._run_acceso_demo    = run_acceso_demo
        self._silent_login       = silent_login
        self._run_caja_fase1     = run_caja_fase1
        self._run_caja_fase2     = run_caja_fase2
        self._caja_step_buscar   = caja_step_buscar
        self._caja_step_agregar  = caja_step_agregar
        self._caja_step_seleccionar = caja_step_seleccionar
        self._caja_step_descuento = caja_step_descuento
        self._caja_step_cerrar   = caja_step_cerrar
        self._pw_start           = pw_start
        self._pw_stop            = pw_stop
        self._on_screenshot      = on_screenshot
        self._on_screenshot_end  = on_screenshot_end
        self._acceso_login_done  = acceso_login_done
        self._fase2_press_f8     = fase2_press_f8
        self._reset_caja_fases   = reset_caja_fases
        self._agent_audio_done           = agent_audio_done_event
        self._audio_bytes_this_response  = 0          # bytes PCM acumulados para calcular duración
        self._audio_done_fallback_handle = None        # handle de loop.call_later para fallback server-side
        self._demo_estadisticas          = demo_estadisticas
        self._demo_stock                 = demo_stock
        self._demo_clientes              = demo_clientes
        self._clientes_nuevo_cliente     = clientes_nuevo_cliente
        self._clientes_importar          = clientes_importar
        self._clientes_ver_detalle       = clientes_ver_detalle
        self._balanza_navegar            = balanza_navegar
        self._balanza_agregar_producto   = balanza_agregar_producto
        self._balanza_mostrar_tickets    = balanza_mostrar_tickets
        self._balanza_ir_a_caja          = balanza_ir_a_caja
        self._balanza_abrir_cf           = balanza_abrir_cf
        self._balanza_cobrar_ticket      = balanza_cobrar_ticket
        self._proveedores_ver_lista          = proveedores_ver_lista
        self._proveedores_abrir_historial    = proveedores_abrir_historial
        self._proveedores_abrir_modal_compra = proveedores_abrir_modal_compra
        self._proveedores_registrar_compra   = proveedores_registrar_compra
        self._proveedores_abrir_carrito      = proveedores_abrir_carrito
        self._proveedores_cargar_producto    = proveedores_cargar_producto
        self._proveedores_finalizar_detalle  = proveedores_finalizar_detalle
        self._proveedores_registrar_pago     = proveedores_registrar_pago
        self._produccion_ver_plantillas        = produccion_ver_plantillas
        self._produccion_ver_detalle_plantilla = produccion_ver_detalle_plantilla
        self._produccion_ir_a_produccion       = produccion_ir_a_produccion
        self._produccion_nueva_produccion      = produccion_nueva_produccion
        self._produccion_seleccionar_plantilla = produccion_seleccionar_plantilla
        self._produccion_completar_y_registrar = produccion_completar_y_registrar
        self._config_navegar                        = config_navegar
        self._config_usuarios_nuevo                 = config_usuarios_nuevo
        self._config_usuarios_scroll_permisos_de    = config_usuarios_scroll_permisos_de
        self._config_usuarios_expandir_permisos_caja = config_usuarios_expandir_permisos_caja
        self._config_usuarios_cerrar_modal          = config_usuarios_cerrar_modal
        self._config_listas_nueva                   = config_listas_nueva
        self._config_grupos_nuevo                   = config_grupos_nuevo
        self._config_productos_nuevo                = config_productos_nuevo
        self._config_productos_importar             = config_productos_importar
        self._config_precios_editar_grupo_almacen   = config_precios_editar_grupo_almacen
        self._config_precios2_grupo_carne           = config_precios2_grupo_carne
        self._config_precios_historial_detalle_grupo    = config_precios_historial_detalle_grupo
        self._config_precios_historial_detalle_producto = config_precios_historial_detalle_producto
        self._config_combos_nuevo                   = config_combos_nuevo
        self._config_combos_editar                  = config_combos_editar
        self._config_formas_pago_nueva              = config_formas_pago_nueva
        self._config_descuentos_nuevo               = config_descuentos_nuevo
        self._config_terminales_nueva               = config_terminales_nueva
        self._config_impuestos_nuevo                = config_impuestos_nuevo
        self._config_gastos_nuevo_concepto          = config_gastos_nuevo_concepto
        self._config_gastos_crear_concepto          = config_gastos_crear_concepto
        self._config_gastos_eliminar_concepto       = config_gastos_eliminar_concepto
        self._finalizar_capacitacion                = finalizar_capacitacion
        # Módulo 2
        self._caja_ir_a_apertura                    = caja_ir_a_apertura
        self._caja_abrir_turno                      = caja_abrir_turno
        self._caja_ver_lista_ventas                 = caja_ver_lista_ventas
        self._caja_ver_detalle_venta                = caja_ver_detalle_venta
        self._caja_retiros_navegar                  = caja_retiros_navegar
        self._caja_retiros_nuevo                    = caja_retiros_nuevo
        self._caja_retiros_ingresar_ejemplo         = caja_retiros_ingresar_ejemplo
        self._caja_retiros_abrir_aprobar            = caja_retiros_abrir_aprobar
        self._caja_retiros_confirmar_aprobar        = caja_retiros_confirmar_aprobar
        self._caja_cierre_navegar                   = caja_cierre_navegar
        self._caja_cierre_nuevo                     = caja_cierre_nuevo
        self._caja_cierre_confirmar                 = caja_cierre_confirmar
        self._caja_cierre_ver_resultado             = caja_cierre_ver_resultado
        self._caja_cierre_nuevo_movimiento          = caja_cierre_nuevo_movimiento
        self._caja_cierre_movimiento_pago_proveedor = caja_cierre_movimiento_pago_proveedor
        self._caja_cierre_movimiento_finalizar_proveedor = caja_cierre_movimiento_finalizar_proveedor
        self._caja_mayor_navegar                    = caja_mayor_navegar
        self._caja_mayor_nuevo_arqueo               = caja_mayor_nuevo_arqueo
        self._caja_mayor_detalle_arqueo             = caja_mayor_detalle_arqueo
        self._caja_mayor_ver_movimientos            = caja_mayor_ver_movimientos
        self._caja_mayor_cheques_navegar            = caja_mayor_cheques_navegar
        self._caja_mayor_cheques_emitir             = caja_mayor_cheques_emitir
        self._caja_mayor_cheques_completar          = caja_mayor_cheques_completar
        self._caja_mayor_cheques_filtrar_todos      = caja_mayor_cheques_filtrar_todos
        self._rrhh_navegar                          = rrhh_navegar
        self._rrhh_personal_nuevo                   = rrhh_personal_nuevo
        self._rrhh_personal_editar                  = rrhh_personal_editar
        self._rrhh_personal_ficha                   = rrhh_personal_ficha
        self._rrhh_personal_cliente_asociado        = rrhh_personal_cliente_asociado
        self._rrhh_fichaje_navegar                  = rrhh_fichaje_navegar
        self._rrhh_fichaje_nuevo                    = rrhh_fichaje_nuevo

        # Prompt de sistema propio de la sesión (foco module/field/user_name del campus).
        # Si no se pasa, cae al prompt de capacitación por defecto.
        self._system_prompt         = system_prompt or TRAINING_SYSTEM_PROMPT

        self._ws                    = None
        self._pw_started            = False
        self._is_speaking           = False   # True mientras la API envía audio de respuesta
        self._barge_in_active       = False   # True desde que se detecta barge-in hasta response.done/cancelled
        self._mute_until            = 0.0     # monotonic timestamp; drops audio chunks until then
        self._last_transcript       = ""      # último transcript de Malena (para detectar preguntas)
        self._auto_continue_task: asyncio.Task | None = None
        self._demo_started          = False   # True después del primer navigate_to_module
        self._tool_is_running       = False   # True mientras _handle_tool está ejecutando
        # call_id → name (para asociar arguments.done con el nombre de la tool)
        self._pending_calls: dict[str, str] = {}

        self._response_active       = False   # True entre response.created y response.done/cancelled
        self._response_has_audio    = False   # True si la respuesta activa ya emitió algún audio.delta
        self._pending_response_reason: str | None = None  # motivo de un response.create diferido por respuesta activa
        self._last_response_create_reason = ""    # su motivo, para reintentarlo si rebota por conflicto
        self._user_turn_open        = False   # True entre speech_started y speech_stopped (o commit forzado)
        self._last_user_audio_ts    = 0.0      # monotonic ts del último chunk de audio real reenviado a la API
        self._pending_audio_ms      = 0.0      # ms de audio real acumulado en el buffer desde el último commit/clear
        self._nudge_pending         = False   # True si ya hay un nudge "Continuá con el protocolo" sin responder en la conversación
        self._auto_continue_empty_count = 0    # respuestas vacías consecutivas tras un nudge (para backoff)
        self._audio_end_time        = 0.0      # monotonic ts estimado de fin de reproducción del audio actual
        self._user_needs_reply      = False    # True cuando el usuario habló/interrumpió y todavía no se le contestó
        self._exchange_active       = False    # True mientras el usuario está consultando/interrumpiendo; frena la marcha del auto-continue
        self._silent_tool_streak    = 0        # tools ejecutadas/pospuestas sin narrar nada desde la última narración (evita cascada muda)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def run(self, audio_queue: asyncio.Queue):
        """Bucle principal: conecta, procesa, reconecta ante errores."""
        while True:
            try:
                async with websockets.connect(
                    OPENAI_REALTIME_URL,
                    additional_headers={
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
                        self._turn_watchdog(ws),
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
                self._last_user_audio_ts = time.monotonic()
                chunk = _resample_16k_to_24k(chunk)
                self._pending_audio_ms += len(chunk) / 2 / _OPENAI_SAMPLE_RATE * 1000
                b64 = base64.b64encode(chunk).decode()
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": b64,
                }))
            except Exception as e:
                print(f"[RT] Error enviando audio: {e}")
                break

    def _reset_pending_audio(self):
        self._pending_audio_ms = 0.0

    async def _turn_watchdog(self, ws):
        """Backstop de fin de turno: si Recall deja de mandar paquetes de audio durante
        un silencio real (p.ej. DTX de WebRTC), el VAD del servidor nunca recibe frames
        para evaluar el silencio y el turno queda abierto indefinidamente. Si pasa
        _TURN_GAP_TIMEOUT sin audio nuevo mientras hay un turno abierto, forzamos el
        commit manualmente."""
        while True:
            await asyncio.sleep(0.25)
            if not self._user_turn_open:
                continue
            if time.monotonic() < self._mute_until:
                continue
            gap = time.monotonic() - self._last_user_audio_ts
            if gap >= self._TURN_GAP_TIMEOUT:
                self._user_turn_open = False
                if self._pending_audio_ms < self._MIN_COMMIT_MS:
                    # No hay nada real para comitear (p.ej. un speech_started disparado por
                    # ruido que ya quedó vaciado por otro clear) — comitear esto solo nos
                    # devuelve "buffer too small" de la API y no hay nada que narrar.
                    print(f"[RT] Watchdog: turno abierto sin audio real ({self._pending_audio_ms:.0f}ms) — se descarta sin commit")
                    self._reset_pending_audio()
                    continue
                print(f"[RT] Watchdog: sin audio nuevo hace {gap:.2f}s con turno abierto — forzando fin de turno")
                try:
                    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    self._reset_pending_audio()
                    # El commit manual NO dispara create_response (eso solo pasa cuando
                    # el VAD del servidor detecta speech_stopped por su cuenta) — sin este
                    # request_response explícito, el turno queda comiteado pero Malena
                    # nunca contesta hasta que vuelve a fluir audio real.
                    await self._request_response(ws, "watchdog gap timeout")
                except Exception as e:
                    print(f"[RT] Error forzando commit: {e}")

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
                self._response_has_audio = True
                # Malena arrancó a hablar en una respuesta real (no cancelada por barge-in):
                # está atendiendo al usuario, así que el turno pendiente queda saldado.
                self._user_needs_reply = False
                # Narró algo: se resetea la racha de tools mudas anti-cascada.
                self._silent_tool_streak = 0
                # Acumular bytes para calcular duración (base64 → aprox bytes PCM)
                self._audio_bytes_this_response += len(delta) * 3 // 4
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
                # Cancelar timer anterior si existía
                if self._audio_done_fallback_handle:
                    self._audio_done_fallback_handle.cancel()
                    self._audio_done_fallback_handle = None
                # Calcular duración esperada del audio (PCM16 a 24kHz = 2 bytes/muestra)
                pcm_bytes = self._audio_bytes_this_response
                self._audio_bytes_this_response = 0
                if pcm_bytes > 0:
                    duration_secs = (pcm_bytes // 2) / 24000.0
                    self._audio_end_time = time.monotonic() + duration_secs + 1.0
                    # Fallback server-side: si agent.html no manda audio_done,
                    # el servidor lo señaliza después de que el audio debería haber terminado.
                    try:
                        loop = asyncio.get_running_loop()
                        self._audio_done_fallback_handle = loop.call_later(
                            duration_secs + 0.5,
                            self._agent_audio_done.set,
                        )
                    except RuntimeError:
                        pass
                else:
                    self._audio_end_time = time.monotonic()
                self._agent_audio_done.clear()
            # Abrir ventana anti-eco: descarta audio por 400ms para que el eco
            # de la voz de Malena no llegue a la API como si fuera el usuario.
            self._mute_until = time.monotonic() + 0.8
            # Si el usuario tiene un turno abierto (nos está hablando encima / barge-in),
            # NO limpiar el buffer — borraría su audio real todavía no comiteado.
            if not self._user_turn_open:
                try:
                    await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    self._reset_pending_audio()
                except Exception:
                    pass
            # NOTA: el auto-continue NO se dispara acá. Cuando una respuesta combina
            # narración + tool call (protocolo "decí Y LLAMÁ la tool en la misma
            # respuesta"), response.output_audio.done llega ANTES que
            # response.function_call_arguments.done — en ese momento _tool_is_running
            # todavía es False, así que programar el auto-continue acá dispara un
            # response.create fantasma que choca con el tool call real que está en
            # curso. Se programa en response.done, cuando ya se sabe con certeza si
            # esta respuesta disparó una tool o no.

        elif etype == "response.created":
            self._response_active = True
            self._response_has_audio = False

        elif etype == "response.cancelled":
            self._is_speaking = False
            self._barge_in_active = False
            self._on_response_ended(ws)

        elif etype == "response.output_audio_transcript.done":
            transcript = event.get("transcript", "")
            if transcript:
                self._last_transcript = transcript
                print(f"[RT] 🤖 Malena: '{transcript}'")

        elif etype == "input_audio_buffer.speech_started":
            self._cancel_auto_continue()
            # Cualquier nudge pendiente queda obsoleto frente a un turno real del usuario —
            # si la demo lo necesita de nuevo más adelante, se inserta uno fresco.
            self._nudge_pending = False
            self._auto_continue_empty_count = 0
            self._user_turn_open = True
            self._last_user_audio_ts = time.monotonic()
            # Malena sigue siendo AUDIBLE si todavía está generando audio (_is_speaking) o si
            # ya terminó de generar pero el cliente aún está reproduciendo el buffer
            # (_audio_end_time en el futuro). Esta segunda condición es clave: OpenAI termina
            # de GENERAR mucho antes de que el usuario termine de ESCUCHAR, así que sin ella
            # una interrupción durante la cola de reproducción no cortaba nada — Malena
            # terminaba la frase igual porque _is_speaking ya era False.
            malena_audible = self._is_speaking or (
                self._audio_end_time > 0 and time.monotonic() < self._audio_end_time
            )
            if malena_audible:
                print("[RT] Barge-in — deteniendo audio")
                # Solo marcamos barge-in "activo" (bloquea deltas y espera un
                # response.cancelled) si hay una respuesta viva para cancelar. Si Malena ya
                # terminó de generar y solo quedaba la cola de reproducción, no llega ningún
                # evento de cancelación y dejar el flag en True congelaría el audio futuro.
                if self._response_active:
                    self._barge_in_active = True
                # El usuario interrumpió: hay algo que dijo que todavía no se contestó. Hasta
                # que Malena hable de nuevo, no puede saltar a una tool del guion (lo bloquea
                # el guard en function_call_arguments.done).
                self._user_needs_reply = True
                # "El usuario tiene la palabra": frena la marcha de la demo hasta que la
                # consulta se resuelva y haya un silencio real (ver _auto_continue).
                self._exchange_active = True
                # Descartar cualquier auto-continue diferido: el usuario quiere hablar, no
                # que la demo le pase por encima cuando termine la respuesta activa.
                if self._pending_response_reason == "auto-continue":
                    self._pending_response_reason = None
                self._is_speaking = False
                self._audio_end_time = time.monotonic()  # ya no suena nada
                await self._send_stop_audio()
                # Cancelar fallback timer y señalizar done — audio fue cortado
                if self._audio_done_fallback_handle:
                    self._audio_done_fallback_handle.cancel()
                    self._audio_done_fallback_handle = None
                self._audio_bytes_this_response = 0
                if self._agent_audio_done:
                    self._agent_audio_done.set()
            else:
                print("[RT] 🎤 Usuario: [hablando...]")

        elif etype == "input_audio_buffer.speech_stopped":
            print("[RT] 🎤 Usuario: [fin de turno]")
            self._user_turn_open = False

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
                if not self._response_has_audio and self._user_needs_reply:
                    # PATRÓN DEL BUG "no me da bola": el usuario habló/interrumpió y el modelo,
                    # en vez de contestarle, saltó directo a una tool del guion sin decir nada.
                    # Posponemos la tool: le devolvemos al modelo un function_call_output que le
                    # ordena atender primero al usuario, y pedimos una respuesta nueva. La tool
                    # se retoma cuando el modelo la vuelva a llamar después de contestar.
                    print(f"[RT] ⛔ Tool '{name}' pospuesta — el usuario habló y todavía no le contestaste")
                    self._user_needs_reply = False
                    self._mute_until = time.monotonic()  # sin mute: queremos escuchar al usuario
                    try:
                        await ws.send(json.dumps({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": (
                                    "NO EJECUTADO. El usuario te interrumpió o te dijo algo que "
                                    "todavía no contestaste. Primero respondé o atendé lo que te dijo, "
                                    "hablando, SIN llamar ninguna tool. Cuando lo resuelvas, retomás este paso."
                                ),
                            },
                        }))
                        await self._request_response(ws, "atender usuario tras interrupción")
                    except Exception as e:
                        print(f"[RT] Error posponiendo tool: {e}")
                    return
                if not self._response_has_audio:
                    # La tool viene en una respuesta sin audio. Hay dos casos MUY distintos:
                    #
                    #  (a) El modelo narró el paso en la respuesta ANTERIOR y ahora, en un
                    #      turno aparte, dispara la tool sola. Es aceptable: el cliente ya
                    #      escuchó la explicación, la acción simplemente ocurre después.
                    #      Dejamos pasar UNA tool muda por narración.
                    #
                    #  (b) Cascada muda: varias tools seguidas, cada una en su respuesta,
                    #      SIN narrar nada entre medio — la demo "hace las cosas" en
                    #      silencio saltando pantallas (el bug del módulo 1). Eso lo
                    #      frenamos: posponemos la 2da tool muda consecutiva y le pedimos
                    #      al modelo que narre antes de seguir.
                    #
                    # _silent_tool_streak cuenta tools mudas desde la última narración; se
                    # resetea a 0 en cuanto llega audio (response.audio.delta).
                    _MAX_SILENT_STREAK = 3   # tras posponer 2 veces sin lograr narración, ejecutar igual para no trabar
                    if name in _SILENT_OK_TOOLS:
                        # Tool exenta: dejarla correr en silencio sin tocar la racha. Necesitamos
                        # que ocurra ANTES de la próxima narración (ej: cerrar el modal para que la
                        # frase siguiente describa la lista que quedó visible).
                        print(f"[RT] 🔈 Tool '{name}' exenta de cascada muda — ejecutando en silencio")
                    elif self._silent_tool_streak == 0:
                        # Primera muda desde la última narración → la narración previa la
                        # cubrió. Dejar pasar y contar.
                        self._silent_tool_streak = 1
                    elif self._silent_tool_streak < _MAX_SILENT_STREAK:
                        # 2da+ muda seguida sin narrar nada en el medio → cascada. Posponer.
                        self._silent_tool_streak += 1
                        print(f"[RT] 🔇 Cascada muda: tool '{name}' sin narración previa — pospuesta, "
                              f"pidiendo narración (racha {self._silent_tool_streak}/{_MAX_SILENT_STREAK})")
                        # No dejar auto-continue apilando otro response.create encima del que
                        # vamos a pedir — sería más ruido de conflicto sin sentido.
                        self._cancel_auto_continue()
                        try:
                            await ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": (
                                        "NO EJECUTADO. Venís ejecutando pasos en silencio, sin explicar "
                                        "nada en voz alta. ANTES de volver a llamar esta tool, decí en voz "
                                        "alta la explicación de este paso. Recién cuando la hayas dicho, "
                                        "volvé a llamar la tool. Un paso por vez, siempre narrado."
                                    ),
                                },
                            }))
                            await self._request_response(ws, "narrar antes de tool")
                        except Exception as e:
                            print(f"[RT] Error posponiendo tool muda: {e}")
                        return
                    else:
                        # El modelo insiste en no narrar tras varios postpones. Ejecutamos
                        # igual para no trabar la demo y reseteamos la racha.
                        print(f"[RT] ⚠️  Tool '{name}' sin narración tras {_MAX_SILENT_STREAK} intentos — ejecutando igual")
                        self._silent_tool_streak = 0
                # Mute audio while tool runs so VAD can't commit mid-execution speech
                self._mute_until = time.monotonic() + 60.0
                # No limpiar si hay un turno de usuario abierto — el audio real
                # todavía no comiteado se perdería sin que el usuario haya terminado.
                if not self._user_turn_open:
                    try:
                        await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                        self._reset_pending_audio()
                    except Exception:
                        pass
                self._tool_is_running = True
                # Descartar cualquier response.create diferido de ANTES de esta tool —
                # quedó pendiente de cuando esta misma respuesta todavía no había emitido
                # el function call. Si se reintenta ahora, choca con el tool call que
                # recién está arrancando (la API todavía no tiene su function_call_output).
                # _send_tool_result/_send_nav_result van a pedir la siguiente respuesta
                # cuando la tool termine — no se pierde nada.
                self._pending_response_reason = None
                asyncio.create_task(self._handle_tool(ws, call_id, name, args_str))

        elif etype == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript and not _is_prompt_echo(transcript):
                print(f"[RT] 🎤 Usuario: '{transcript}'")
                # El usuario dijo algo real (aunque no haya interrumpido audio): hasta que
                # Malena le conteste, no puede avanzar el guion con una tool muda, y la demo
                # entra en pausa (no auto-continuar por encima suyo).
                self._user_needs_reply = True
                self._exchange_active = True

        elif etype == "response.done":
            status = event.get("response", {}).get("status", "")
            if status == "cancelled":
                print(f"[RT] Respuesta cancelada (barge-in)")
                self._barge_in_active = False
            elif self._response_has_audio:
                # El modelo reaccionó de verdad (al nudge, a una tool o al usuario) — el
                # nudge pendiente, si lo había, ya cumplió su propósito.
                self._nudge_pending = False
                self._auto_continue_empty_count = 0
            if status != "cancelled" and self._demo_started and not self._tool_is_running:
                # Auto-continuar solo durante la demo y cuando esta respuesta no
                # disparó una tool (si la disparó, _send_tool_result/_send_nav_result
                # se encargan de pedir la siguiente respuesta cuando la tool termine).
                if not self._response_has_audio and self._nudge_pending:
                    # Respuesta vacía con nudge ya en la conversación — acumular para backoff
                    self._auto_continue_empty_count += 1
                self._cancel_auto_continue()
                self._auto_continue_task = asyncio.create_task(self._auto_continue())
            self._on_response_ended(ws)

        elif etype == "error":
            err = event.get("error", {})
            msg = err.get("message", "")
            code = err.get("code", "")
            if "input_audio_transcription" in msg:
                print(f"[RT] ℹ️  Transcripción de usuario no soportada por este modelo")
            elif "active response" in msg.lower():
                self._response_active = True
                # Nuestro pedido (tool_result, nav_result, auto-continue, watchdog) chocó
                # con una respuesta que ya estaba activa — server auto-creando la suya por
                # turn_detection.create_response, u otro pedido nuestro que todavía no
                # terminó. Sea cual sea el motivo, ese pedido representa un resultado real
                # (narración de un tool, continuación de la demo) que si no se reintenta se
                # pierde para siempre y la demo queda muda hasta que el usuario hable.
                # Reintentar de más en el caso ambiguo solo genera un response.create extra
                # inofensivo — mucho mejor que perder el turno en silencio.
                if not self._pending_response_reason:
                    print(f"[RT] ⚠️  response.create rechazado (conflicto) — reintentando ({self._last_response_create_reason})")
                    self._pending_response_reason = self._last_response_create_reason
            else:
                print(f"[RT] ⚠️  Error: {code} — {msg}")

        elif etype == "conversation.item.created":
            item = event.get("item", {})
            if item.get("role") == "user":
                for part in item.get("content", []):
                    if part.get("type") == "input_text" and part.get("text"):
                        print(f"[RT] 🎤 Usuario: '{part['text']}'")
                    elif part.get("type") == "input_audio" and part.get("transcript"):
                        if not _is_prompt_echo(part["transcript"]):
                            print(f"[RT] 🎤 Usuario: '{part['transcript']}'")

        else:
            # Log eventos no manejados para diagnóstico
            _skip = {"input_audio_buffer.committed", "input_audio_buffer.cleared",
                     "conversation.item.added",
                     "conversation.item.done",
                     "response.output_item.done", "response.content_part.added",
                     "response.content_part.done", "rate_limits.updated",
                     "response.output_audio_transcript.delta",
                     "response.function_call_arguments.delta",
                     "conversation.item.input_audio_transcription.delta",
                     "session.updated"}
            if etype not in _skip:
                print(f"[RT] Evento no manejado: {etype}")

    # ── Auto-continuación ──────────────────────────────────────────────────────

    def _cancel_auto_continue(self):
        if self._auto_continue_task and not self._auto_continue_task.done():
            self._auto_continue_task.cancel()
        self._auto_continue_task = None

    def _on_response_ended(self, ws):
        """Llamado en response.done / response.cancelled. Si había un response.create
        diferido (porque llegó mientras otra respuesta estaba activa), lo reintenta."""
        self._response_active = False
        if self._pending_response_reason:
            reason = self._pending_response_reason
            self._pending_response_reason = None
            asyncio.create_task(self._request_response(ws, reason))

    async def _request_response(self, ws=None, reason: str = ""):
        """Pide una respuesta nueva a la API, evitando pisar una que ya esté en curso.
        El servidor también dispara respuestas por su cuenta (turn_detection.create_response)
        cuando el usuario termina de hablar; sin este guard, un response.create nuestro
        puede chocar con esa respuesta automática y perderse en silencio."""
        target_ws = ws or self._ws
        if not target_ws:
            return
        if self._response_active:
            print(f"[RT] response.create diferido ({reason}) — ya hay una respuesta activa")
            self._pending_response_reason = reason
            return
        self._response_active = True
        self._last_response_create_reason = reason
        try:
            await target_ws.send(json.dumps({"type": "response.create"}))
        except Exception as e:
            print(f"[RT] Error pidiendo respuesta ({reason}): {e}")
            self._response_active = False

    async def _wait_for_audio_done(self, timeout: float = 30.0):
        """Espera a que agent.html termine de reproducir el audio actual.
        El timeout es dinámico: al menos `timeout`, pero suficiente para cubrir
        la duración real del audio calculada a partir de los bytes PCM recibidos."""
        if not self._agent_audio_done:
            return
        try:
            remaining = max(0.0, self._audio_end_time - time.monotonic())
            actual_timeout = max(timeout, remaining + 3.0)
            await asyncio.wait_for(self._agent_audio_done.wait(), timeout=actual_timeout)
        except asyncio.TimeoutError:
            print("[RT] Timeout esperando audio_done del agente — continuando igual")

    async def _auto_continue(self):
        """Si el usuario no habla después de que Malena termina, continuar la demo.

        Pedir un response.create "pelado" (sin ítem nuevo en la conversación) suele
        no alcanzar: el modelo no tiene nada nuevo a qué reaccionar y devuelve una
        respuesta vacía (sin audio ni tool call) — hay que pedir varias seguidas
        hasta que reaccione, lo que se percibe como pasos lentos con muchos
        auto-continue en el medio. Insertar un mensaje system corto antes le da un
        turno real al que reaccionar en vez de depender de que actúe sobre silencio.
        """
        # Si el usuario está en medio de una consulta / acaba de interrumpir, NO pisamos:
        # esperamos un silencio largo antes de retomar la demo. Mientras tanto, si el
        # usuario habla, speech_started cancela esta tarea y la conversación sigue por
        # turnos normales (pregunta → respuesta). Solo retomamos si aguanta el silencio.
        was_exchange = self._exchange_active
        if was_exchange:
            delay = 8.0
        elif self._nudge_pending:
            # Backoff exponencial: 3s → 5.5s → 8s (tope) por cada respuesta vacía
            # consecutiva tras el nudge, para no spamear la API con response.create.
            delay = min(3.0 + 2.5 * max(self._auto_continue_empty_count - 1, 0), 8.0)
        elif self._last_transcript.rstrip().endswith("?"):
            delay = 8.0
        else:
            delay = 1.5
        try:
            await asyncio.sleep(delay)
            await self._wait_for_audio_done(timeout=15.0)
            if self._ws:
                # Sobrevivimos el silencio: la consulta terminó, retomamos la demo.
                self._exchange_active = False
                print("[RT] Auto-continue: disparando siguiente respuesta...")
                # Si ya hay un nudge sin responder (la respuesta anterior volvió vacía),
                # no apilar otro idéntico — eso entierra cualquier turno real del usuario
                # que haya quedado en el medio bajo varias copias de "continuá ahora" y el
                # modelo termina ignorando lo que el usuario dijo. Reintentamos la misma
                # respuesta sobre el nudge que ya está en la conversación.
                if not self._nudge_pending:
                    self._nudge_pending = True
                    # Tras una consulta, el nudge le recuerda retomar donde dejó sin repetir;
                    # en marcha normal, simplemente avanza al paso siguiente.
                    nudge_text = (
                        "El cliente ya terminó su consulta. Retomá la explicación donde la "
                        "dejaste antes de la interrupción, sin repetir lo que ya dijiste, y "
                        "seguí con el protocolo."
                        if was_exchange else
                        "Continuá con el siguiente paso del protocolo, ahora."
                    )
                    try:
                        await self._ws.send(json.dumps({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "system",
                                "content": [{
                                    "type": "input_text",
                                    "text": nudge_text,
                                }],
                            },
                        }))
                    except Exception as e:
                        print(f"[RT] Error insertando nudge de auto-continue: {e}")
                await self._request_response(self._ws, "auto-continue")
        except asyncio.CancelledError:
            pass

    # ── Configuración de sesión ────────────────────────────────────────────────

    async def _on_session_created(self, ws):
        # Todo en un solo session.update: si la transcripción se manda en un update
        # separado, ese segundo update reemplaza session.audio.input entero y borra
        # el turn_detection/noise_reduction que configuramos acá.
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": self._system_prompt,
                "tools": REALTIME_TOOLS,
                "tool_choice": "auto",
                "audio": {
                    "input": {
                        # Recall manda 16kHz; _send_audio_loop resamplea a 24kHz antes
                        # de mandarlo — es el único rate que soporta audio/pcm acá.
                        "format": {"type": "audio/pcm", "rate": 24000},
                        # Threshold más alto que el default (0.5): con 0.6 el ruido/eco de
                        # una videollamada (sobre todo sin auriculares, la voz de Malena
                        # rebotando en el micrófono del usuario) disparaba speech_started
                        # falsos que cortaban a Malena a mitad de frase sin que el usuario
                        # interrumpiera de verdad. 0.75 exige voz más fuerte/sostenida.
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.75,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                            "create_response": True,
                            "interrupt_response": True,
                        },
                        "noise_reduction": {"type": "near_field"},
                        "transcription": {
                            "model": "gpt-4o-transcribe",
                            "language": "es",
                            # Lista de vocabulario, no una oración: un prompt en forma de frase
                            # natural es lo que el modelo "autocompleta" alucinando cuando el
                            # audio no tiene voz real (ruido, eco, turno abierto por error).
                            "prompt": TRANSCRIPTION_PROMPT,
                        },
                    },
                },
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
        await self._request_response(ws, "saludo inicial")
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
                await self._request_response(self._ws, "inject_context")
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
        is_final = (name == "finalizar_capacitacion")

        try:
            # Toda tool de acción necesita el navegador Playwright iniciado. Si el
            # usuario pide explícitamente una sección, el modelo suele saltar la tool
            # de entrada (*_navegar) y llamar directo a una acción; sin este guard
            # esa acción correría con _page=None → "browser no iniciado". Es
            # idempotente (early-return si ya arrancó), así que las llamadas
            # explícitas en cada branch quedan como no-op. Excepciones:
            # navigate_to_module (solo carga el iframe, gestiona ACCESO aparte) y
            # finalizar_capacitacion (no usa el navegador).
            if not is_nav and not is_final:
                await self._ensure_playwright()

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

            elif name == "caja_aplicar_descuento":
                print("[CAJA] Aplicando descuento 10% en efectivo...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_step_descuento()

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

            elif name == "clientes_nuevo_cliente":
                print("[DEMO] Clientes: abriendo modal nuevo cliente...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._clientes_nuevo_cliente() if self._clientes_nuevo_cliente else "Modal de nuevo cliente abierto."

            elif name == "clientes_importar":
                print("[DEMO] Clientes: abriendo importación por Excel...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._clientes_importar() if self._clientes_importar else "Modal de importación abierto."

            elif name == "clientes_ver_detalle":
                print("[DEMO] Clientes: abriendo detalle del cliente...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._clientes_ver_detalle() if self._clientes_ver_detalle else "Detalle del cliente abierto."
                await self._on_screenshot_end()

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
                await self._ensure_playwright()
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

            elif name == "proveedores_ver_lista":
                print("[DEMO] Proveedores: mostrando lista de proveedores...")
                await self._do_navigate("PROVEEDORES")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._proveedores_ver_lista() if self._proveedores_ver_lista else "Lista de proveedores visible."

            elif name == "proveedores_abrir_historial":
                print("[DEMO] Proveedores: abriendo historial del proveedor (click Editar)...")
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
                print("[DEMO] Proveedores: cargando producto Media res...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._proveedores_cargar_producto() if self._proveedores_cargar_producto else "Producto cargado."

            elif name == "proveedores_finalizar_detalle":
                print("[DEMO] Proveedores: finalizando detalle de compra...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._proveedores_finalizar_detalle() if self._proveedores_finalizar_detalle else "Detalle finalizado."

            elif name == "proveedores_registrar_pago":
                print("[DEMO] Proveedores: registrando pago al proveedor...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._proveedores_registrar_pago() if self._proveedores_registrar_pago else "Pago registrado."
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

            elif name == "config_navegar":
                seccion = args.get("seccion", "")
                print(f"[DEMO] Config: navegando a {seccion}...")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                self._demo_started = True
                config_path = CONFIG_MODULE_PATHS.get(seccion)
                if config_path:
                    await self._send_navigate(config_path)
                result = await self._config_navegar(seccion) if self._config_navegar else f"Navegado a {seccion}."

            elif name == "config_usuarios_nuevo":
                print("[DEMO] Config: abriendo modal nuevo usuario...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_usuarios_nuevo() if self._config_usuarios_nuevo else "Modal de nuevo usuario abierto."

            elif name == "config_usuarios_scroll_permisos_de":
                print("[DEMO] Config: scroll a selector Permisos del usuario...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_usuarios_scroll_permisos_de() if self._config_usuarios_scroll_permisos_de else "Visible selector de Permisos del usuario."

            elif name == "config_usuarios_expandir_permisos_caja":
                print("[DEMO] Config: expandiendo permisos de Caja...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_usuarios_expandir_permisos_caja() if self._config_usuarios_expandir_permisos_caja else "Acordeón de permisos de Caja expandido."

            elif name == "config_usuarios_cerrar_modal":
                print("[DEMO] Config: cerrando modal de nuevo usuario...")
                result = await self._config_usuarios_cerrar_modal() if self._config_usuarios_cerrar_modal else "Modal cerrado."

            elif name == "config_listas_nueva":
                print("[DEMO] Config: abriendo modal nueva lista de precios...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_listas_nueva() if self._config_listas_nueva else "Modal de nueva lista de precios abierto."

            elif name == "config_grupos_nuevo":
                print("[DEMO] Config: abriendo modal nuevo grupo...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_grupos_nuevo() if self._config_grupos_nuevo else "Modal de nuevo grupo abierto."

            elif name == "config_productos_nuevo":
                print("[DEMO] Config: abriendo modal nuevo producto...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_productos_nuevo() if self._config_productos_nuevo else "Modal de nuevo producto abierto."

            elif name == "config_productos_importar":
                print("[DEMO] Config: abriendo modal importar productos...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_productos_importar() if self._config_productos_importar else "Modal de importación abierto."

            elif name == "config_precios_editar_grupo_almacen":
                print("[DEMO] Config: editando grupo Almacén en precios...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_precios_editar_grupo_almacen() if self._config_precios_editar_grupo_almacen else "Detalle de precios del grupo Almacén abierto."

            elif name == "config_precios2_grupo_carne":
                print("[DEMO] Config: filtrando grupo Carne en precios2...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_precios2_grupo_carne() if self._config_precios2_grupo_carne else "Filtrado por grupo Carne."

            elif name == "config_precios_historial_detalle_grupo":
                print("[DEMO] Config: abriendo detalle de grupo en historial de precios...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_precios_historial_detalle_grupo() if self._config_precios_historial_detalle_grupo else "Detalle de productos del grupo abierto."

            elif name == "config_precios_historial_detalle_producto":
                print("[DEMO] Config: abriendo historial de cambios de precio del producto...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_precios_historial_detalle_producto() if self._config_precios_historial_detalle_producto else "Historial de cambios de precio del producto abierto."

            elif name == "config_combos_nuevo":
                print("[DEMO] Config: abriendo modal nuevo combo...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_combos_nuevo() if self._config_combos_nuevo else "Modal de nuevo combo abierto."

            elif name == "config_combos_editar":
                print("[DEMO] Config: abriendo editor del combo...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_combos_editar() if self._config_combos_editar else "Editor del combo abierto."

            elif name == "config_formas_pago_nueva":
                print("[DEMO] Config: abriendo modal nueva forma de pago...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_formas_pago_nueva() if self._config_formas_pago_nueva else "Modal de nueva forma de pago abierto."

            elif name == "config_descuentos_nuevo":
                print("[DEMO] Config: abriendo modal nuevo descuento...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_descuentos_nuevo() if self._config_descuentos_nuevo else "Modal de nuevo descuento abierto."

            elif name == "config_terminales_nueva":
                print("[DEMO] Config: abriendo modal nueva terminal...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_terminales_nueva() if self._config_terminales_nueva else "Modal de nueva terminal abierto."

            elif name == "config_impuestos_nuevo":
                print("[DEMO] Config: abriendo modal nuevo impuesto...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_impuestos_nuevo() if self._config_impuestos_nuevo else "Modal de nuevo impuesto abierto."

            elif name == "config_gastos_nuevo_concepto":
                print("[DEMO] Config: abriendo modal nuevo concepto de gasto...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_gastos_nuevo_concepto() if self._config_gastos_nuevo_concepto else "Modal de nuevo concepto abierto."

            elif name == "config_gastos_crear_concepto":
                print("[DEMO] Config: creando concepto de gasto de prueba...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._config_gastos_crear_concepto() if self._config_gastos_crear_concepto else "Concepto creado."

            elif name == "config_gastos_eliminar_concepto":
                print("[DEMO] Config: eliminando concepto de prueba (interno)...")
                result = await self._config_gastos_eliminar_concepto() if self._config_gastos_eliminar_concepto else "Concepto eliminado."

            elif name == "caja_ir_a_apertura":
                print("[CAJA2] Navegando a formulario de apertura...")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                self._demo_started = True
                # Igual que caja_mayor_navegar/config_navegar: fijamos la vista base del
                # iframe en caja.php. Sin esto el iframe quedaba en /home.php (de _send_logged_in)
                # y solo dependíamos del overlay de screenshot para mostrar la apertura.
                await self._send_navigate(DEMO_MODULE_PATHS.get("CAJA", "/caja.php"))
                result = await self._caja_ir_a_apertura() if self._caja_ir_a_apertura else "Formulario de apertura visible."

            elif name == "caja_abrir_turno":
                print("[CAJA2] Confirmando apertura de turno...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_abrir_turno() if self._caja_abrir_turno else "Turno de caja abierto."

            elif name == "caja_ver_lista_ventas":
                print("[CAJA2] Mostrando lista de ventas...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_ver_lista_ventas() if self._caja_ver_lista_ventas else "Lista de ventas visible."

            elif name == "caja_ver_detalle_venta":
                print("[CAJA2] Abriendo detalle de la venta más reciente...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_ver_detalle_venta() if self._caja_ver_detalle_venta else "Detalle de venta abierto."

            elif name == "caja_retiros_navegar":
                print("[CAJA2] Navegando a retiros...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_retiros_navegar() if self._caja_retiros_navegar else "Sección de retiros visible."

            elif name == "caja_retiros_nuevo":
                print("[CAJA2] Abriendo modal nuevo retiro...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_retiros_nuevo() if self._caja_retiros_nuevo else "Modal de nuevo retiro abierto."

            elif name == "caja_retiros_ingresar_ejemplo":
                print("[CAJA2] Cargando retiro de ejemplo de $10.000...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_retiros_ingresar_ejemplo() if self._caja_retiros_ingresar_ejemplo else "Retiro de ejemplo creado en estado pendiente."

            elif name == "caja_retiros_abrir_aprobar":
                print("[CAJA2] Abriendo confirmación de aprobación del retiro...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_retiros_abrir_aprobar() if self._caja_retiros_abrir_aprobar else "Modal de aprobación abierto."

            elif name == "caja_retiros_confirmar_aprobar":
                print("[CAJA2] Confirmando aprobación del retiro...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_retiros_confirmar_aprobar() if self._caja_retiros_confirmar_aprobar else "Retiro aprobado."

            elif name == "caja_cierre_navegar":
                print("[CAJA2] Navegando a cierre de caja...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_cierre_navegar() if self._caja_cierre_navegar else "Sección de cierre visible."

            elif name == "caja_cierre_nuevo":
                print("[CAJA2] Iniciando nuevo cierre de caja...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_cierre_nuevo() if self._caja_cierre_nuevo else "Formulario de cierre abierto."

            elif name == "caja_cierre_confirmar":
                print("[CAJA2] Confirmando cierre con $500.000...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_cierre_confirmar() if self._caja_cierre_confirmar else "Cierre confirmado."

            elif name == "caja_cierre_ver_resultado":
                print("[CAJA2] Mostrando resultado del cierre...")
                result = await self._caja_cierre_ver_resultado() if self._caja_cierre_ver_resultado else "Resultado del cierre visible."

            elif name == "caja_cierre_nuevo_movimiento":
                print("[CAJA2] Abriendo modal nuevo movimiento de cierre...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_cierre_nuevo_movimiento() if self._caja_cierre_nuevo_movimiento else "Modal de nuevo movimiento abierto."

            elif name == "caja_cierre_movimiento_pago_proveedor":
                print("[CAJA2] Seleccionando Pago a proveedor...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_cierre_movimiento_pago_proveedor() if self._caja_cierre_movimiento_pago_proveedor else "Formulario de pago a proveedor visible."

            elif name == "caja_cierre_movimiento_finalizar_proveedor":
                print("[CAJA2] Finalizando pago a proveedor de $100.000...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_cierre_movimiento_finalizar_proveedor() if self._caja_cierre_movimiento_finalizar_proveedor else "Pago a proveedor registrado."

            elif name == "caja_mayor_navegar":
                print("[CAJA-MAYOR] Navegando a Caja Mayor...")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                self._demo_started = True
                await self._send_navigate(DEMO_MODULE_PATHS.get("CAJA MAYOR", "/caja_administracion_caja.php"))
                result = await self._caja_mayor_navegar() if self._caja_mayor_navegar else "Pantalla de Caja Mayor visible."

            elif name == "caja_mayor_nuevo_arqueo":
                print("[CAJA-MAYOR] Abriendo modal nuevo arqueo...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_mayor_nuevo_arqueo() if self._caja_mayor_nuevo_arqueo else "Modal de nuevo arqueo abierto."

            elif name == "caja_mayor_detalle_arqueo":
                print("[CAJA-MAYOR] Abriendo detalle del arqueo...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_mayor_detalle_arqueo() if self._caja_mayor_detalle_arqueo else "Detalle del arqueo visible."

            elif name == "caja_mayor_ver_movimientos":
                print("[CAJA-MAYOR] Mostrando movimientos de caja mayor...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_mayor_ver_movimientos() if self._caja_mayor_ver_movimientos else "Movimientos de caja mayor visibles."

            elif name == "caja_mayor_cheques_navegar":
                print("[CAJA-MAYOR] Navegando a cheques...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_mayor_cheques_navegar() if self._caja_mayor_cheques_navegar else "Sección de cheques visible."

            elif name == "caja_mayor_cheques_emitir":
                print("[CAJA-MAYOR] Abriendo modal de nuevo cheque...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_mayor_cheques_emitir() if self._caja_mayor_cheques_emitir else "Modal de nuevo cheque abierto."

            elif name == "caja_mayor_cheques_completar":
                print("[CAJA-MAYOR] Completando y emitiendo cheque...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_mayor_cheques_completar() if self._caja_mayor_cheques_completar else "Cheque emitido."

            elif name == "caja_mayor_cheques_filtrar_todos":
                print("[CAJA-MAYOR] Aplicando filtro Todos en cheques...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._caja_mayor_cheques_filtrar_todos() if self._caja_mayor_cheques_filtrar_todos else "Filtro Todos aplicado."
                await self._on_screenshot_end()

            elif name == "rrhh_navegar":
                print("[RRHH] Navegando a rrhh_personal.php...")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._rrhh_navegar() if self._rrhh_navegar else "Sección de Recursos Humanos visible."

            elif name == "rrhh_personal_nuevo":
                print("[RRHH] Abriendo modal de nuevo personal...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._rrhh_personal_nuevo() if self._rrhh_personal_nuevo else "Modal de nuevo personal abierto."

            elif name == "rrhh_personal_editar":
                print("[RRHH] Entrando a editar personal id 1...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._rrhh_personal_editar() if self._rrhh_personal_editar else "Detalle del personal abierto."

            elif name == "rrhh_personal_ficha":
                print("[RRHH] Abriendo pestaña Ficha...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._rrhh_personal_ficha() if self._rrhh_personal_ficha else "Pestaña Ficha abierta."

            elif name == "rrhh_personal_cliente_asociado":
                print("[RRHH] Resaltando selector cliente asociado...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._rrhh_personal_cliente_asociado() if self._rrhh_personal_cliente_asociado else "Selector cliente asociado resaltado."

            elif name == "rrhh_fichaje_navegar":
                print("[RRHH] Navegando a fichaje...")
                await self._ensure_playwright()
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._rrhh_fichaje_navegar() if self._rrhh_fichaje_navegar else "Sección de fichaje visible."

            elif name == "rrhh_fichaje_nuevo":
                print("[RRHH] Abriendo modal de nuevo fichaje...")
                await self._wait_for_audio_done(timeout=20.0)
                result = await self._rrhh_fichaje_nuevo() if self._rrhh_fichaje_nuevo else "Modal de nuevo fichaje abierto."
                await self._on_screenshot_end()

            elif name == "finalizar_capacitacion":
                print("[DEMO] Finalizando capacitación — bot abandona la llamada...")
                await self._wait_for_audio_done(timeout=20.0)
                if self._finalizar_capacitacion:
                    await self._finalizar_capacitacion()
                result = "Capacitación finalizada."

            else:
                result = f"Tool desconocida: {name}"

        except Exception as e:
            print(f"[TOOL] Error en {name}: {e}")
            traceback.print_exc()
            result = f"Error ejecutando {name}: {e}"

        if is_final:
            self._tool_is_running = False
            return
        elif is_nav:
            await self._send_nav_result(ws, call_id, result)
        else:
            await self._send_tool_result(ws, call_id, result)

        self._tool_is_running = False

    async def _send_tool_result(self, ws, call_id: str, output: str):
        # Clear any audio accumulated during tool execution, resume with short mute
        self._mute_until = time.monotonic() + 1.5
        # No limpiar si el usuario ya empezó a hablar (turno abierto) — borraría su
        # audio real todavía no comiteado, dejando el watchdog sin nada para comitear.
        if not self._user_turn_open:
            try:
                await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                self._reset_pending_audio()
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
            await self._request_response(ws, "tool_result")
        except Exception as e:
            print(f"[RT] Error enviando resultado de tool: {e}")

    async def _send_nav_result(self, ws, call_id: str, output: str):
        """Envía tool result de navigate_to_module SIN response.create — auto-continue lo dispara."""
        self._mute_until = time.monotonic() + 1.5
        if not self._user_turn_open:
            try:
                await ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                self._reset_pending_audio()
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
            await self._ensure_playwright(silent_login=False)
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

    async def _ensure_playwright(self, silent_login: bool = True):
        if self._pw_started:
            return
        self._pw_started = True
        self._reset_caja_fases()
        self._acceso_login_done.clear()
        self._fase2_press_f8.clear()
        try:
            await self._pw_start()
            await self._send_logged_in()
            # Si el usuario pidió una sección directa sin pasar por ACCESO,
            # logueamos silenciosamente para que la navegación no quede trabada
            # en la pantalla de login. Con silent_login=False (flujo ACCESO) el
            # login lo hace la demo visible de acceso.
            if silent_login and self._silent_login and not self._acceso_login_done.is_set():
                await self._silent_login()
        except Exception as e:
            print(f"[RT] Error iniciando Playwright: {e}")
            self._pw_started = False
