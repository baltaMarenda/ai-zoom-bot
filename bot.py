"""
bot.py
Pipeline de Malena — Realtime API bridge, por sesión (multi-tenant).

Todo el estado que antes era global (lista de webpages, eventos de sincronización,
bridge activo) vive ahora en la BotSession. Este módulo construye el RealtimeBridge y
los wrappers de Playwright ligados a UNA sesión, y maneja su WebSocket de audio.

NOTA: en Fase 1 Playwright sigue siendo global (un solo browser). Los screenshots ya
se rutean por sesión (via session.on_screenshot); la aislación del browser por sesión
es la Fase 2.
"""
import asyncio
import base64
import json
import traceback

from mgw_playwright import (
    pw_start, pw_stop, pw_stop_state, init_pw_state, caja_fase_flags,
    pw_login,
    demo_acceso_login,
    demo_caja_fase1_agregar, demo_caja_fase2_pagar,
    caja_step_buscar, caja_step_agregar,
    caja_step_seleccionar_pago, caja_step_descuento, caja_step_cerrar,
    reset_caja_fases,
    run_demo_estadisticas, run_demo_stock, run_demo_clientes,
    clientes_nuevo_cliente, clientes_importar, clientes_ver_detalle,
    balanza_step_navegar, balanza_step_agregar_producto,
    balanza_step_mostrar_tickets, balanza_step_ir_a_caja,
    balanza_step_abrir_cf, balanza_step_cobrar_ticket,
    proveedores_ver_lista, proveedores_abrir_historial, proveedores_abrir_modal_compra,
    proveedores_registrar_compra, proveedores_abrir_carrito,
    proveedores_cargar_producto, proveedores_finalizar_detalle,
    proveedores_registrar_pago,
    produccion_ver_plantillas, produccion_ver_detalle_plantilla,
    produccion_ir_a_produccion, produccion_nueva_produccion,
    produccion_seleccionar_plantilla, produccion_completar_y_registrar,
    config_navegar,
    config_usuarios_nuevo, config_usuarios_scroll_permisos_de,
    config_usuarios_expandir_permisos_caja, config_usuarios_cerrar_modal,
    config_listas_nueva, config_grupos_nuevo,
    config_productos_nuevo, config_productos_importar,
    config_precios_editar_grupo_almacen, config_precios2_grupo_carne,
    config_precios_historial_detalle_grupo, config_precios_historial_detalle_producto,
    config_combos_nuevo, config_combos_editar,
    config_formas_pago_nueva, config_descuentos_nuevo,
    config_terminales_nueva, config_impuestos_nuevo,
    config_gastos_nuevo_concepto, config_gastos_crear_concepto,
    config_gastos_eliminar_concepto,
    gastos_navegar, gastos_pago_proveedor_abrir, gastos_pago_proveedor_seleccionar,
    gastos_nuevo_gasto_abrir, gastos_nuevo_gasto_completar, gastos_nuevo_gasto_agregar,
    # Módulo 2: Caja y Caja Mayor
    caja_ir_a_apertura, caja_abrir_turno,
    caja_ver_lista_ventas, caja_ver_detalle_venta,
    caja_retiros_navegar, caja_retiros_nuevo,
    caja_retiros_ingresar_ejemplo, caja_retiros_abrir_aprobar,
    caja_retiros_confirmar_aprobar,
    caja_cierre_navegar, caja_cierre_nuevo,
    caja_cierre_confirmar, caja_cierre_ver_resultado,
    caja_cierre_nuevo_movimiento,
    caja_cierre_movimiento_pago_proveedor,
    caja_cierre_movimiento_finalizar_proveedor,
    caja_mayor_navegar, caja_mayor_nuevo_arqueo,
    caja_mayor_detalle_arqueo, caja_mayor_ver_movimientos,
    caja_mayor_cheques_navegar, caja_mayor_cheques_emitir,
    caja_mayor_cheques_completar, caja_mayor_cheques_filtrar_todos,
    rrhh_navegar, rrhh_personal_nuevo, rrhh_personal_editar,
    rrhh_personal_ficha, rrhh_personal_cliente_asociado, rrhh_fichaje_navegar,
    rrhh_fichaje_nuevo,
)
import recall


# ── Gestión del WebSocket de la webpage del agente (por sesión) ───────────────

def set_agent_websocket(ws, session):
    if ws is not None and session is not None:
        session.agent_ws_list.append(ws)
        session.log.info(f"Agent WS conectado (total: {len(session.agent_ws_list)})")


def remove_agent_websocket(ws, session):
    if session is None:
        return
    try:
        session.agent_ws_list.remove(ws)
    except ValueError:
        pass
    session.log.info(f"Agent WS desconectado (restantes: {len(session.agent_ws_list)})")


def on_agent_audio_done(session):
    """Llamado desde main.py cuando la webpage termina de reproducir audio."""
    if session is not None:
        session.events["audio_done"].set()


# ── Binders: ligan una función de Playwright al on_screenshot de la sesión ────

def _bind0(fn, oscb):
    async def wrapper() -> str:
        return await fn(on_screenshot=oscb)
    return wrapper

def _bind1(fn, oscb):
    async def wrapper(a) -> str:
        return await fn(a, on_screenshot=oscb)
    return wrapper

def _bind2(fn, oscb):
    async def wrapper(a, b) -> str:
        return await fn(a, b, on_screenshot=oscb)
    return wrapper


# Tabla de wrappers sin argumentos (bridge_kwarg → función de Playwright)
_BIND0_TABLE = {
    "demo_estadisticas":              run_demo_estadisticas,
    "demo_stock":                     run_demo_stock,
    "demo_clientes":                  run_demo_clientes,
    "clientes_nuevo_cliente":         clientes_nuevo_cliente,
    "clientes_importar":              clientes_importar,
    "clientes_ver_detalle":           clientes_ver_detalle,
    "balanza_navegar":                balanza_step_navegar,
    "balanza_mostrar_tickets":        balanza_step_mostrar_tickets,
    "balanza_ir_a_caja":              balanza_step_ir_a_caja,
    "balanza_abrir_cf":               balanza_step_abrir_cf,
    "balanza_cobrar_ticket":          balanza_step_cobrar_ticket,
    "proveedores_ver_lista":          proveedores_ver_lista,
    "proveedores_abrir_historial":    proveedores_abrir_historial,
    "proveedores_abrir_modal_compra": proveedores_abrir_modal_compra,
    "proveedores_registrar_compra":   proveedores_registrar_compra,
    "proveedores_abrir_carrito":      proveedores_abrir_carrito,
    "proveedores_cargar_producto":    proveedores_cargar_producto,
    "proveedores_finalizar_detalle":  proveedores_finalizar_detalle,
    "proveedores_registrar_pago":     proveedores_registrar_pago,
    "produccion_ver_plantillas":        produccion_ver_plantillas,
    "produccion_ver_detalle_plantilla": produccion_ver_detalle_plantilla,
    "produccion_ir_a_produccion":       produccion_ir_a_produccion,
    "produccion_nueva_produccion":      produccion_nueva_produccion,
    "produccion_seleccionar_plantilla": produccion_seleccionar_plantilla,
    "produccion_completar_y_registrar": produccion_completar_y_registrar,
    "config_usuarios_nuevo":                  config_usuarios_nuevo,
    "config_usuarios_scroll_permisos_de":     config_usuarios_scroll_permisos_de,
    "config_usuarios_expandir_permisos_caja": config_usuarios_expandir_permisos_caja,
    "config_usuarios_cerrar_modal":           config_usuarios_cerrar_modal,
    "config_listas_nueva":                    config_listas_nueva,
    "config_grupos_nuevo":                    config_grupos_nuevo,
    "config_productos_nuevo":                 config_productos_nuevo,
    "config_productos_importar":              config_productos_importar,
    "config_precios_editar_grupo_almacen":    config_precios_editar_grupo_almacen,
    "config_precios2_grupo_carne":            config_precios2_grupo_carne,
    "config_precios_historial_detalle_grupo":    config_precios_historial_detalle_grupo,
    "config_precios_historial_detalle_producto": config_precios_historial_detalle_producto,
    "config_combos_nuevo":                    config_combos_nuevo,
    "config_combos_editar":                   config_combos_editar,
    "config_formas_pago_nueva":               config_formas_pago_nueva,
    "config_descuentos_nuevo":                config_descuentos_nuevo,
    "config_terminales_nueva":                config_terminales_nueva,
    "config_impuestos_nuevo":                 config_impuestos_nuevo,
    "config_gastos_nuevo_concepto":           config_gastos_nuevo_concepto,
    "config_gastos_crear_concepto":           config_gastos_crear_concepto,
    "config_gastos_eliminar_concepto":        config_gastos_eliminar_concepto,
    "gastos_navegar":                         gastos_navegar,
    "gastos_pago_proveedor_abrir":            gastos_pago_proveedor_abrir,
    "gastos_pago_proveedor_seleccionar":      gastos_pago_proveedor_seleccionar,
    "gastos_nuevo_gasto_abrir":               gastos_nuevo_gasto_abrir,
    "gastos_nuevo_gasto_completar":           gastos_nuevo_gasto_completar,
    "gastos_nuevo_gasto_agregar":             gastos_nuevo_gasto_agregar,
    # Módulo 2
    "caja_ir_a_apertura":                     caja_ir_a_apertura,
    "caja_abrir_turno":                       caja_abrir_turno,
    "caja_ver_lista_ventas":                  caja_ver_lista_ventas,
    "caja_ver_detalle_venta":                 caja_ver_detalle_venta,
    "caja_retiros_navegar":                   caja_retiros_navegar,
    "caja_retiros_nuevo":                     caja_retiros_nuevo,
    "caja_retiros_ingresar_ejemplo":          caja_retiros_ingresar_ejemplo,
    "caja_retiros_abrir_aprobar":             caja_retiros_abrir_aprobar,
    "caja_retiros_confirmar_aprobar":         caja_retiros_confirmar_aprobar,
    "caja_cierre_navegar":                    caja_cierre_navegar,
    "caja_cierre_nuevo":                      caja_cierre_nuevo,
    "caja_cierre_confirmar":                  caja_cierre_confirmar,
    "caja_cierre_ver_resultado":              caja_cierre_ver_resultado,
    "caja_cierre_nuevo_movimiento":           caja_cierre_nuevo_movimiento,
    "caja_cierre_movimiento_pago_proveedor":  caja_cierre_movimiento_pago_proveedor,
    "caja_cierre_movimiento_finalizar_proveedor": caja_cierre_movimiento_finalizar_proveedor,
    "caja_mayor_navegar":                     caja_mayor_navegar,
    "caja_mayor_nuevo_arqueo":                caja_mayor_nuevo_arqueo,
    "caja_mayor_detalle_arqueo":              caja_mayor_detalle_arqueo,
    "caja_mayor_ver_movimientos":             caja_mayor_ver_movimientos,
    "caja_mayor_cheques_navegar":             caja_mayor_cheques_navegar,
    "caja_mayor_cheques_emitir":              caja_mayor_cheques_emitir,
    "caja_mayor_cheques_completar":           caja_mayor_cheques_completar,
    "caja_mayor_cheques_filtrar_todos":       caja_mayor_cheques_filtrar_todos,
    "rrhh_navegar":                           rrhh_navegar,
    "rrhh_personal_nuevo":                    rrhh_personal_nuevo,
    "rrhh_personal_editar":                   rrhh_personal_editar,
    "rrhh_personal_ficha":                    rrhh_personal_ficha,
    "rrhh_personal_cliente_asociado":         rrhh_personal_cliente_asociado,
    "rrhh_fichaje_navegar":                   rrhh_fichaje_navegar,
    "rrhh_fichaje_nuevo":                     rrhh_fichaje_nuevo,
}


# ── Wrappers especiales (usan eventos/relogin/bot_id de la sesión) ────────────

async def _relogin_proxy(session):
    """
    El login de Playwright usa el mismo usuario/empresa que la sesión del proxy y la
    invalida del lado de MGW. Re-logueamos esa sesión para que /mgw-proxy quede con
    cookies válidas y no devuelva home.php en navegaciones posteriores.
    """
    if session.mgw is None:
        return
    loop = asyncio.get_event_loop()
    relogged = await loop.run_in_executor(None, session.mgw.login)
    session.log.info(f"Re-login de sesión del proxy {'✓' if relogged else '✗'}")


def _make_run_acceso_demo(session):
    oscb = session.on_screenshot
    async def run_acceso_demo() -> bool:
        session.log.info("[ACCESO] Iniciando demo de login...")
        ok = await demo_acceso_login(on_screenshot=oscb)
        await session.on_screenshot_end()
        session.log.info(f"[ACCESO] Demo login {'✓' if ok else '✗'}")
        if ok:
            await _relogin_proxy(session)
        session.events["acceso_login_done"].set()
        return ok
    return run_acceso_demo


def _make_silent_login(session):
    async def silent_login() -> bool:
        session.log.info("[LOGIN] Login silencioso de Playwright...")
        ok = await pw_login()
        session.log.info(f"[LOGIN] Login silencioso {'✓' if ok else '✗'}")
        if ok:
            await _relogin_proxy(session)
        session.events["acceso_login_done"].set()
        return ok
    return silent_login


def _make_caja_fase1(session):
    oscb = session.on_screenshot
    async def run_caja_fase1() -> bool:
        if not session.events["acceso_login_done"].is_set():
            session.log.info("[CAJA] Esperando login de ACCESO...")
            try:
                await asyncio.wait_for(session.events["acceso_login_done"].wait(), timeout=30.0)
            except asyncio.TimeoutError:
                session.log.info("[CAJA] Timeout esperando ACCESO — continuando igual")
        session.log.info("[CAJA] Iniciando Fase 1 (buscar + agregar)...")
        ok = await demo_caja_fase1_agregar(on_screenshot=oscb)
        await session.on_screenshot_end()
        session.log.info(f"[CAJA] Fase 1 {'✓' if ok else '✗'}")
        session.events["fase1_complete"].set()
        return ok
    return run_caja_fase1


def _make_caja_fase2(session, run_caja_fase1):
    oscb = session.on_screenshot

    async def _run_fase2_inner(initial_delay: float = 0.0) -> bool:
        session.log.info("[CAJA] Iniciando Fase 2 (pago + cierre)...")
        session.events["fase2_press_f8"].set()
        ok = await demo_caja_fase2_pagar(
            on_screenshot=oscb,
            initial_delay=initial_delay,
            press_f8_signal=session.events["fase2_press_f8"],
        )
        await session.on_screenshot_end()
        session.events["fase2_complete"].set()
        session.log.info(f"[CAJA] Fase 2 {'✓' if ok else '✗'}")
        return ok

    async def run_caja_fase2() -> bool:
        fase1_done, fase1_launched, _f2d, _f2l = caja_fase_flags()
        if not fase1_done:
            if fase1_launched:
                session.log.info("[CAJA] Esperando que Fase 1 termine antes de Fase 2...")
                try:
                    await asyncio.wait_for(session.events["fase1_complete"].wait(), timeout=90.0)
                except asyncio.TimeoutError:
                    session.log.info("[CAJA] Timeout esperando Fase 1 — continuando igual")
            else:
                ok1 = await run_caja_fase1()
                if not ok1:
                    return False
            await asyncio.sleep(1.5)
        return await _run_fase2_inner()

    return run_caja_fase2


def _make_finalizar(session):
    async def finalizar_capacitacion() -> str:
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, lambda: recall.leave_call(session.bot_id))
            return f"Llamada finalizada: {result}"
        except Exception as e:
            session.log.error(f"leave_call: {e}")
            return f"Error al finalizar: {e}"
    return finalizar_capacitacion


# ── Construcción de los kwargs del bridge ligados a la sesión ─────────────────

def _build_bridge_kwargs(session) -> dict:
    oscb = session.on_screenshot
    run_caja_fase1 = _make_caja_fase1(session)

    kwargs = {
        "send_to_agent":   session.send_to_agent,
        "send_navigate":   session.send_navigate,
        "send_logged_in":  session.send_logged_in,
        "send_stop_audio": session.send_stop_audio,
        "run_acceso_demo": _make_run_acceso_demo(session),
        "silent_login":    _make_silent_login(session),
        "run_caja_fase1":  run_caja_fase1,
        "run_caja_fase2":  _make_caja_fase2(session, run_caja_fase1),
        "caja_step_buscar":      _bind1(caja_step_buscar, oscb),
        "caja_step_agregar":     _bind0(caja_step_agregar, oscb),
        "caja_step_seleccionar": _bind1(caja_step_seleccionar_pago, oscb),
        "caja_step_descuento":   _bind0(caja_step_descuento, oscb),
        "caja_step_cerrar":      _bind1(caja_step_cerrar, oscb),
        "pw_start":          pw_start,
        "pw_stop":           pw_stop,
        "on_screenshot":     oscb,
        "on_screenshot_end": session.on_screenshot_end,
        "acceso_login_done": session.events["acceso_login_done"],
        "fase2_press_f8":    session.events["fase2_press_f8"],
        "reset_caja_fases":  reset_caja_fases,
        "system_prompt":     session.system_prompt,
        "agent_audio_done_event": session.events["audio_done"],
        # balanza_agregar_producto toma 2 args (nombre, id)
        "balanza_agregar_producto": _bind2(balanza_step_agregar_producto, oscb),
        # config_navegar toma 1 arg (sección)
        "config_navegar":    _bind1(config_navegar, oscb),
        "finalizar_capacitacion": _make_finalizar(session),
    }
    # Todos los wrappers sin argumentos
    for kwarg_name, fn in _BIND0_TABLE.items():
        kwargs[kwarg_name] = _bind0(fn, oscb)
    return kwargs


# ── WebSocket handler (por sesión) ────────────────────────────────────────────

async def handle_recall_audio(websocket, session):
    session.log.info("[WS] Recall.ai conectado ✓")

    # Cerrar bridge anterior si Recall reconectó sin que el viejo terminara
    if session.bridge is not None:
        session.log.info("Cerrando bridge anterior por reconexión de Recall...")
        try:
            await session.bridge.close()
        except Exception as e:
            session.log.error(f"cerrando bridge anterior: {e}")
        session.bridge = None

    # Instalar el holder de estado de Playwright de ESTA sesión en el task raíz, ANTES
    # de crear tasks hijos (gather/bridge), para que todos hereden el mismo holder y
    # compartan browser/page/flags dentro de la sesión, aislados de otras sesiones.
    creds = session.creds
    pw_state = init_pw_state(
        empresa =creds.empresa  if creds else None,
        usuario =creds.usuario  if creds else None,
        password=creds.password if creds else None,
    )

    # Reset de estado para esta conexión
    session.fase2_task_created = False
    reset_caja_fases()
    for ev in session.events.values():
        ev.clear()

    audio_queue: asyncio.Queue = asyncio.Queue()

    from realtime_bridge import RealtimeBridge
    bridge = RealtimeBridge(**_build_bridge_kwargs(session))
    session.bridge = bridge
    # Hook para que el teardown (que corre en OTRO task, sin el ContextVar seteado)
    # pueda cerrar el browser exacto de esta sesión vía su holder capturado.
    session.pw_close = lambda: pw_stop_state(pw_state)

    async def receive_from_recall():
        try:
            while True:
                message = await websocket.receive()
                if "text" in message:
                    data  = json.loads(message["text"])
                    event = data.get("event", "")
                    if event == "audio_separate_raw.data":
                        participant = data["data"]["data"].get("participant", {})
                        if participant.get("name") == session.bot_name:
                            continue
                        b64_audio = data["data"]["data"]["buffer"]
                        pcm_audio = base64.b64decode(b64_audio)
                        session.touch()   # actividad humana → reinicia reloj de inactividad
                        await audio_queue.put(pcm_audio)
                    else:
                        session.log.info(f"[WS] Evento: {event}")
                elif "bytes" in message:
                    chunk = message["bytes"]
                    if len(chunk) > 4:
                        session.touch()
                        await audio_queue.put(chunk[4:])
        except Exception as e:
            session.log.info(f"[WS] Recall.ai desconectado: {e}")
        finally:
            await audio_queue.put(None)

    try:
        await asyncio.gather(receive_from_recall(), bridge.run(audio_queue))
    except Exception as e:
        session.log.error(f"handle_recall_audio: {e}")
        traceback.print_exc()
    finally:
        try:
            await bridge.close()
        except Exception:
            pass
        if session.bridge is bridge:
            session.bridge = None
        await session.send_reset()
