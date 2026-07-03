"""
bot.py
Pipeline de Malena — Realtime API bridge.
Audio: envía PCM16 via WebSocket a la webpage del agente.
Navegación: envía comandos navigate via WebSocket a la webpage del agente.
"""
import asyncio
import base64
import json
import traceback

from config import DEMO_MODULE_PATHS
from mgw_session import mgw_login
from mgw_playwright import (
    pw_start, pw_stop,
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
    proveedores_abrir_historial, proveedores_abrir_modal_compra,
    proveedores_registrar_compra, proveedores_abrir_carrito,
    proveedores_cargar_producto, proveedores_finalizar_detalle,
    proveedores_registrar_pago,
    produccion_ver_plantillas, produccion_ver_detalle_plantilla,
    produccion_ir_a_produccion, produccion_nueva_produccion,
    produccion_seleccionar_plantilla, produccion_completar_y_registrar,
    config_navegar,
    config_usuarios_nuevo, config_usuarios_scroll_permisos_de,
    config_usuarios_expandir_permisos_caja,
    config_listas_nueva, config_grupos_nuevo,
    config_productos_nuevo, config_productos_importar,
    config_precios_editar_grupo_almacen, config_precios2_grupo_carne,
    config_precios_historial_detalle_grupo, config_precios_historial_detalle_producto,
    config_combos_nuevo, config_combos_editar,
    config_formas_pago_nueva, config_descuentos_nuevo,
    config_terminales_nueva, config_impuestos_nuevo,
    config_gastos_nuevo_concepto, config_gastos_crear_concepto,
    config_gastos_eliminar_concepto,
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
)
from recall import leave_call

# ── Referencia al WebSocket de la webpage del agente ──────────────────────────
_agent_ws_list: list = []
_audio_done_event = asyncio.Event()


def set_agent_websocket(ws):
    global _agent_ws_list
    if ws is not None:
        _agent_ws_list.append(ws)
        print(f"[BOT] Agent WS conectado (total: {len(_agent_ws_list)})")


def remove_agent_websocket(ws):
    global _agent_ws_list
    try:
        _agent_ws_list.remove(ws)
    except ValueError:
        pass
    print(f"[BOT] Agent WS desconectado (restantes: {len(_agent_ws_list)})")


def on_agent_audio_done():
    """Llamado desde main.py cuando la webpage termina de reproducir audio."""
    _audio_done_event.set()


# ── Sesión activa (evita puentes superpuestos en reconexión de Recall) ─────────
_active_bridge = None

# ── Sincronización entre módulos ──────────────────────────────────────────────
_acceso_login_done    = asyncio.Event()
_fase1_complete_event = asyncio.Event()  # se setea cuando fase1 termina (ok o no)
_fase2_complete_event = asyncio.Event()
_fase2_press_f8       = asyncio.Event()
_fase2_task_created   = False

# ── Navegación MGW ────────────────────────────────────────────────────────────
_last_navigated_module: str = ""
_current_demo_module: str = ""
_module_locked: bool = False


# ── Helpers de comunicación con la webpage ────────────────────────────────────

async def _send_to_agent(msg: dict):
    if not _agent_ws_list:
        return
    dead = []
    for ws in list(_agent_ws_list):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            _agent_ws_list.remove(ws)
        except ValueError:
            pass


async def _send_audio(mp3_bytes: bytes):
    b64 = base64.b64encode(mp3_bytes).decode()
    await _send_to_agent({"type": "audio", "data": b64})


async def _send_navigate(path: str):
    await _send_to_agent({"type": "navigate", "path": path})


async def _send_reset():
    await _send_to_agent({"type": "reset"})


async def _send_stop_audio():
    await _send_to_agent({"type": "stop_audio"})


async def _send_logged_in():
    await _send_to_agent({"type": "logged_in"})


# ── Screenshots ───────────────────────────────────────────────────────────────

async def _on_screenshot(b64: str):
    await _send_to_agent({"type": "screenshot", "data": b64})


async def _on_screenshot_end():
    await _send_to_agent({"type": "screenshot_end"})


# ── Playwright helpers ────────────────────────────────────────────────────────

def unlock_demo_module():
    global _module_locked
    _module_locked = False


async def _run_acceso_demo() -> bool:
    print("[ACCESO] Iniciando demo de login...")
    ok = await demo_acceso_login(on_screenshot=_on_screenshot)
    await _send_to_agent({"type": "screenshot_end"})
    print(f"[ACCESO] Demo login {'✓' if ok else '✗'}")
    if ok:
        # El login de Playwright usa el mismo usuario/empresa que la sesión del proxy
        # (mgw_session.py) y la invalida del lado de MGW. Re-logueamos esa sesión para
        # que /mgw-proxy quede con cookies válidas y no nos termine devolviendo home.php
        # en navegaciones posteriores.
        loop = asyncio.get_event_loop()
        relogged = await loop.run_in_executor(None, mgw_login)
        print(f"[ACCESO] Re-login de sesión del proxy {'✓' if relogged else '✗'}")
    _acceso_login_done.set()
    return ok


async def _run_caja_fase1_inner() -> bool:
    if not _acceso_login_done.is_set():
        print("[CAJA] Esperando login de ACCESO...")
        try:
            await asyncio.wait_for(_acceso_login_done.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            print("[CAJA] Timeout esperando ACCESO — continuando igual")
    print("[CAJA] Iniciando Fase 1 (buscar + agregar)...")
    ok = await demo_caja_fase1_agregar(on_screenshot=_on_screenshot)
    await _send_to_agent({"type": "screenshot_end"})
    print(f"[CAJA] Fase 1 {'✓' if ok else '✗'}")
    _fase1_complete_event.set()
    return ok


async def _run_caja_fase2_inner(initial_delay: float = 0.0) -> bool:
    print("[CAJA] Iniciando Fase 2 (pago + cierre)...")
    # En el flujo Realtime, la señal de cierre la damos nosotros inmediatamente
    # para que Playwright no espere los 40s de timeout.
    _fase2_press_f8.set()
    ok = await demo_caja_fase2_pagar(
        on_screenshot=_on_screenshot,
        initial_delay=initial_delay,
        press_f8_signal=_fase2_press_f8,
    )
    await _send_to_agent({"type": "screenshot_end"})
    _fase2_complete_event.set()
    print(f"[CAJA] Fase 2 {'✓' if ok else '✗'}")
    return ok


async def _run_caja_fase2_con_prerequisito() -> bool:
    from mgw_playwright import _caja_fase1_done, _caja_fase1_launched
    if not _caja_fase1_done:
        if _caja_fase1_launched:
            # Fase 1 ya arrancó pero no terminó — esperamos su señal en lugar de relanzarla
            print("[CAJA] Esperando que Fase 1 termine antes de arrancar Fase 2...")
            try:
                await asyncio.wait_for(_fase1_complete_event.wait(), timeout=90.0)
            except asyncio.TimeoutError:
                print("[CAJA] Timeout esperando Fase 1 — continuando igual")
        else:
            ok1 = await _run_caja_fase1_inner()
            if not ok1:
                return False
        await asyncio.sleep(1.5)
    return await _run_caja_fase2_inner()


# ── Wrappers paso a paso para Realtime API ───────────────────────────────────

async def _caja_step_buscar(product_name: str) -> str:
    return await caja_step_buscar(product_name, on_screenshot=_on_screenshot)

async def _caja_step_agregar() -> str:
    return await caja_step_agregar(on_screenshot=_on_screenshot)

async def _caja_step_seleccionar_pago(method: str) -> str:
    return await caja_step_seleccionar_pago(method, on_screenshot=_on_screenshot)

async def _caja_step_descuento() -> str:
    return await caja_step_descuento(on_screenshot=_on_screenshot)

async def _caja_step_cerrar(method: str) -> str:
    return await caja_step_cerrar(method, on_screenshot=_on_screenshot)


async def _run_estadisticas_demo() -> str:
    return await run_demo_estadisticas(on_screenshot=_on_screenshot)

async def _run_stock_demo() -> str:
    return await run_demo_stock(on_screenshot=_on_screenshot)

async def _run_clientes_demo() -> str:
    return await run_demo_clientes(on_screenshot=_on_screenshot)

async def _clientes_nuevo_cliente() -> str:
    return await clientes_nuevo_cliente(on_screenshot=_on_screenshot)

async def _clientes_importar() -> str:
    return await clientes_importar(on_screenshot=_on_screenshot)

async def _clientes_ver_detalle() -> str:
    return await clientes_ver_detalle(on_screenshot=_on_screenshot)

async def _balanza_step_navegar() -> str:
    return await balanza_step_navegar(on_screenshot=_on_screenshot)

async def _balanza_step_agregar(operario_nombre: str, operario_id: str) -> str:
    return await balanza_step_agregar_producto(operario_nombre, operario_id, on_screenshot=_on_screenshot)

async def _balanza_step_mostrar_tickets() -> str:
    return await balanza_step_mostrar_tickets(on_screenshot=_on_screenshot)

async def _balanza_step_ir_a_caja() -> str:
    return await balanza_step_ir_a_caja(on_screenshot=_on_screenshot)

async def _balanza_step_abrir_cf() -> str:
    return await balanza_step_abrir_cf(on_screenshot=_on_screenshot)

async def _balanza_step_cobrar_ticket() -> str:
    return await balanza_step_cobrar_ticket(on_screenshot=_on_screenshot)

async def _proveedores_abrir_historial() -> str:
    return await proveedores_abrir_historial(on_screenshot=_on_screenshot)

async def _proveedores_abrir_modal_compra() -> str:
    return await proveedores_abrir_modal_compra(on_screenshot=_on_screenshot)

async def _proveedores_registrar_compra() -> str:
    return await proveedores_registrar_compra(on_screenshot=_on_screenshot)

async def _proveedores_abrir_carrito() -> str:
    return await proveedores_abrir_carrito(on_screenshot=_on_screenshot)

async def _proveedores_cargar_producto() -> str:
    return await proveedores_cargar_producto(on_screenshot=_on_screenshot)

async def _proveedores_finalizar_detalle() -> str:
    return await proveedores_finalizar_detalle(on_screenshot=_on_screenshot)

async def _proveedores_registrar_pago() -> str:
    return await proveedores_registrar_pago(on_screenshot=_on_screenshot)

async def _produccion_ver_plantillas() -> str:
    return await produccion_ver_plantillas(on_screenshot=_on_screenshot)

async def _produccion_ver_detalle_plantilla() -> str:
    return await produccion_ver_detalle_plantilla(on_screenshot=_on_screenshot)

async def _produccion_ir_a_produccion() -> str:
    return await produccion_ir_a_produccion(on_screenshot=_on_screenshot)

async def _produccion_nueva_produccion() -> str:
    return await produccion_nueva_produccion(on_screenshot=_on_screenshot)

async def _produccion_seleccionar_plantilla() -> str:
    return await produccion_seleccionar_plantilla(on_screenshot=_on_screenshot)

async def _produccion_completar_y_registrar() -> str:
    return await produccion_completar_y_registrar(on_screenshot=_on_screenshot)


async def _config_navegar(seccion: str) -> str:
    return await config_navegar(seccion, on_screenshot=_on_screenshot)

async def _config_usuarios_nuevo() -> str:
    return await config_usuarios_nuevo(on_screenshot=_on_screenshot)

async def _config_usuarios_scroll_permisos_de() -> str:
    return await config_usuarios_scroll_permisos_de(on_screenshot=_on_screenshot)

async def _config_usuarios_expandir_permisos_caja() -> str:
    return await config_usuarios_expandir_permisos_caja(on_screenshot=_on_screenshot)

async def _config_listas_nueva() -> str:
    return await config_listas_nueva(on_screenshot=_on_screenshot)

async def _config_grupos_nuevo() -> str:
    return await config_grupos_nuevo(on_screenshot=_on_screenshot)

async def _config_productos_nuevo() -> str:
    return await config_productos_nuevo(on_screenshot=_on_screenshot)

async def _config_productos_importar() -> str:
    return await config_productos_importar(on_screenshot=_on_screenshot)

async def _config_precios_editar_grupo_almacen() -> str:
    return await config_precios_editar_grupo_almacen(on_screenshot=_on_screenshot)

async def _config_precios2_grupo_carne() -> str:
    return await config_precios2_grupo_carne(on_screenshot=_on_screenshot)

async def _config_precios_historial_detalle_grupo() -> str:
    return await config_precios_historial_detalle_grupo(on_screenshot=_on_screenshot)

async def _config_precios_historial_detalle_producto() -> str:
    return await config_precios_historial_detalle_producto(on_screenshot=_on_screenshot)

async def _config_combos_nuevo() -> str:
    return await config_combos_nuevo(on_screenshot=_on_screenshot)

async def _config_combos_editar() -> str:
    return await config_combos_editar(on_screenshot=_on_screenshot)

async def _config_formas_pago_nueva() -> str:
    return await config_formas_pago_nueva(on_screenshot=_on_screenshot)

async def _config_descuentos_nuevo() -> str:
    return await config_descuentos_nuevo(on_screenshot=_on_screenshot)

async def _config_terminales_nueva() -> str:
    return await config_terminales_nueva(on_screenshot=_on_screenshot)

async def _config_impuestos_nuevo() -> str:
    return await config_impuestos_nuevo(on_screenshot=_on_screenshot)

async def _config_gastos_nuevo_concepto() -> str:
    return await config_gastos_nuevo_concepto(on_screenshot=_on_screenshot)

async def _config_gastos_crear_concepto() -> str:
    return await config_gastos_crear_concepto(on_screenshot=_on_screenshot)

async def _config_gastos_eliminar_concepto() -> str:
    return await config_gastos_eliminar_concepto(on_screenshot=_on_screenshot)

async def _finalizar_capacitacion() -> str:
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, leave_call)
        return f"Llamada finalizada: {result}"
    except Exception as e:
        print(f"[BOT] Error en leave_call: {e}")
        return f"Error al finalizar: {e}"


# ── Módulo 2: Caja y Caja Mayor ───────────────────────────────────────────────

async def _caja_ir_a_apertura() -> str:
    return await caja_ir_a_apertura(on_screenshot=_on_screenshot)

async def _caja_abrir_turno() -> str:
    return await caja_abrir_turno(on_screenshot=_on_screenshot)

async def _caja_ver_lista_ventas() -> str:
    return await caja_ver_lista_ventas(on_screenshot=_on_screenshot)

async def _caja_ver_detalle_venta() -> str:
    return await caja_ver_detalle_venta(on_screenshot=_on_screenshot)

async def _caja_retiros_navegar() -> str:
    return await caja_retiros_navegar(on_screenshot=_on_screenshot)

async def _caja_retiros_nuevo() -> str:
    return await caja_retiros_nuevo(on_screenshot=_on_screenshot)

async def _caja_retiros_ingresar_ejemplo() -> str:
    return await caja_retiros_ingresar_ejemplo(on_screenshot=_on_screenshot)

async def _caja_retiros_abrir_aprobar() -> str:
    return await caja_retiros_abrir_aprobar(on_screenshot=_on_screenshot)

async def _caja_retiros_confirmar_aprobar() -> str:
    return await caja_retiros_confirmar_aprobar(on_screenshot=_on_screenshot)

async def _caja_cierre_navegar() -> str:
    return await caja_cierre_navegar(on_screenshot=_on_screenshot)

async def _caja_cierre_nuevo() -> str:
    return await caja_cierre_nuevo(on_screenshot=_on_screenshot)

async def _caja_cierre_confirmar() -> str:
    return await caja_cierre_confirmar(on_screenshot=_on_screenshot)

async def _caja_cierre_ver_resultado() -> str:
    return await caja_cierre_ver_resultado(on_screenshot=_on_screenshot)

async def _caja_cierre_nuevo_movimiento() -> str:
    return await caja_cierre_nuevo_movimiento(on_screenshot=_on_screenshot)

async def _caja_cierre_movimiento_pago_proveedor() -> str:
    return await caja_cierre_movimiento_pago_proveedor(on_screenshot=_on_screenshot)

async def _caja_cierre_movimiento_finalizar_proveedor() -> str:
    return await caja_cierre_movimiento_finalizar_proveedor(on_screenshot=_on_screenshot)

async def _caja_mayor_navegar() -> str:
    return await caja_mayor_navegar(on_screenshot=_on_screenshot)

async def _caja_mayor_nuevo_arqueo() -> str:
    return await caja_mayor_nuevo_arqueo(on_screenshot=_on_screenshot)

async def _caja_mayor_detalle_arqueo() -> str:
    return await caja_mayor_detalle_arqueo(on_screenshot=_on_screenshot)

async def _caja_mayor_ver_movimientos() -> str:
    return await caja_mayor_ver_movimientos(on_screenshot=_on_screenshot)

async def _caja_mayor_cheques_navegar() -> str:
    return await caja_mayor_cheques_navegar(on_screenshot=_on_screenshot)

async def _caja_mayor_cheques_emitir() -> str:
    return await caja_mayor_cheques_emitir(on_screenshot=_on_screenshot)

async def _caja_mayor_cheques_completar() -> str:
    return await caja_mayor_cheques_completar(on_screenshot=_on_screenshot)

async def _caja_mayor_cheques_filtrar_todos() -> str:
    return await caja_mayor_cheques_filtrar_todos(on_screenshot=_on_screenshot)


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def handle_recall_audio(websocket):
    global _fase2_task_created, _active_bridge
    print("[WS] Recall.ai conectado ✓")

    # Cerrar sesión anterior si Recall reconectó sin que la vieja terminara
    if _active_bridge is not None:
        print("[BOT] Cerrando bridge anterior por reconexión de Recall...")
        try:
            await _active_bridge.close()
        except Exception as e:
            print(f"[BOT] Error cerrando bridge anterior: {e}")
        _active_bridge = None

    # Reset para nueva sesión
    _fase2_task_created = False
    reset_caja_fases()
    _acceso_login_done.clear()
    _fase1_complete_event.clear()
    _fase2_complete_event.clear()
    _fase2_press_f8.clear()

    audio_queue: asyncio.Queue = asyncio.Queue()

    # Limpiar el evento de audio para esta sesión
    _audio_done_event.clear()

    from realtime_bridge import RealtimeBridge
    bridge = RealtimeBridge(
        send_to_agent          = _send_to_agent,
        send_navigate          = _send_navigate,
        send_logged_in         = _send_logged_in,
        send_stop_audio        = _send_stop_audio,
        run_acceso_demo        = _run_acceso_demo,
        run_caja_fase1         = _run_caja_fase1_inner,
        run_caja_fase2         = _run_caja_fase2_con_prerequisito,
        caja_step_buscar       = _caja_step_buscar,
        caja_step_agregar      = _caja_step_agregar,
        caja_step_seleccionar  = _caja_step_seleccionar_pago,
        caja_step_descuento    = _caja_step_descuento,
        caja_step_cerrar       = _caja_step_cerrar,
        pw_start               = pw_start,
        pw_stop                = pw_stop,
        on_screenshot          = _on_screenshot,
        on_screenshot_end      = _on_screenshot_end,
        acceso_login_done      = _acceso_login_done,
        fase2_press_f8         = _fase2_press_f8,
        reset_caja_fases       = reset_caja_fases,
        agent_audio_done_event = _audio_done_event,
        demo_estadisticas           = _run_estadisticas_demo,
        demo_stock                  = _run_stock_demo,
        demo_clientes               = _run_clientes_demo,
        clientes_nuevo_cliente      = _clientes_nuevo_cliente,
        clientes_importar           = _clientes_importar,
        clientes_ver_detalle        = _clientes_ver_detalle,
        balanza_navegar             = _balanza_step_navegar,
        balanza_agregar_producto    = _balanza_step_agregar,
        balanza_mostrar_tickets     = _balanza_step_mostrar_tickets,
        balanza_ir_a_caja           = _balanza_step_ir_a_caja,
        balanza_abrir_cf            = _balanza_step_abrir_cf,
        balanza_cobrar_ticket       = _balanza_step_cobrar_ticket,
        proveedores_abrir_historial      = _proveedores_abrir_historial,
        proveedores_abrir_modal_compra   = _proveedores_abrir_modal_compra,
        proveedores_registrar_compra     = _proveedores_registrar_compra,
        proveedores_abrir_carrito        = _proveedores_abrir_carrito,
        proveedores_cargar_producto      = _proveedores_cargar_producto,
        proveedores_finalizar_detalle    = _proveedores_finalizar_detalle,
        proveedores_registrar_pago       = _proveedores_registrar_pago,
        produccion_ver_plantillas         = _produccion_ver_plantillas,
        produccion_ver_detalle_plantilla  = _produccion_ver_detalle_plantilla,
        produccion_ir_a_produccion        = _produccion_ir_a_produccion,
        produccion_nueva_produccion       = _produccion_nueva_produccion,
        produccion_seleccionar_plantilla  = _produccion_seleccionar_plantilla,
        produccion_completar_y_registrar  = _produccion_completar_y_registrar,
        config_navegar                        = _config_navegar,
        config_usuarios_nuevo                 = _config_usuarios_nuevo,
        config_usuarios_scroll_permisos_de    = _config_usuarios_scroll_permisos_de,
        config_usuarios_expandir_permisos_caja = _config_usuarios_expandir_permisos_caja,
        config_listas_nueva                   = _config_listas_nueva,
        config_grupos_nuevo                   = _config_grupos_nuevo,
        config_productos_nuevo                = _config_productos_nuevo,
        config_productos_importar             = _config_productos_importar,
        config_precios_editar_grupo_almacen   = _config_precios_editar_grupo_almacen,
        config_precios2_grupo_carne           = _config_precios2_grupo_carne,
        config_precios_historial_detalle_grupo    = _config_precios_historial_detalle_grupo,
        config_precios_historial_detalle_producto = _config_precios_historial_detalle_producto,
        config_combos_nuevo                   = _config_combos_nuevo,
        config_combos_editar                  = _config_combos_editar,
        config_formas_pago_nueva              = _config_formas_pago_nueva,
        config_descuentos_nuevo               = _config_descuentos_nuevo,
        config_terminales_nueva               = _config_terminales_nueva,
        config_impuestos_nuevo                = _config_impuestos_nuevo,
        config_gastos_nuevo_concepto          = _config_gastos_nuevo_concepto,
        config_gastos_crear_concepto          = _config_gastos_crear_concepto,
        config_gastos_eliminar_concepto       = _config_gastos_eliminar_concepto,
        finalizar_capacitacion                = _finalizar_capacitacion,
        # Módulo 2
        caja_ir_a_apertura                    = _caja_ir_a_apertura,
        caja_abrir_turno                      = _caja_abrir_turno,
        caja_ver_lista_ventas                 = _caja_ver_lista_ventas,
        caja_ver_detalle_venta                = _caja_ver_detalle_venta,
        caja_retiros_navegar                  = _caja_retiros_navegar,
        caja_retiros_nuevo                    = _caja_retiros_nuevo,
        caja_retiros_ingresar_ejemplo         = _caja_retiros_ingresar_ejemplo,
        caja_retiros_abrir_aprobar            = _caja_retiros_abrir_aprobar,
        caja_retiros_confirmar_aprobar        = _caja_retiros_confirmar_aprobar,
        caja_cierre_navegar                   = _caja_cierre_navegar,
        caja_cierre_nuevo                     = _caja_cierre_nuevo,
        caja_cierre_confirmar                 = _caja_cierre_confirmar,
        caja_cierre_ver_resultado             = _caja_cierre_ver_resultado,
        caja_cierre_nuevo_movimiento          = _caja_cierre_nuevo_movimiento,
        caja_cierre_movimiento_pago_proveedor = _caja_cierre_movimiento_pago_proveedor,
        caja_cierre_movimiento_finalizar_proveedor = _caja_cierre_movimiento_finalizar_proveedor,
        caja_mayor_navegar                    = _caja_mayor_navegar,
        caja_mayor_nuevo_arqueo               = _caja_mayor_nuevo_arqueo,
        caja_mayor_detalle_arqueo             = _caja_mayor_detalle_arqueo,
        caja_mayor_ver_movimientos            = _caja_mayor_ver_movimientos,
        caja_mayor_cheques_navegar            = _caja_mayor_cheques_navegar,
        caja_mayor_cheques_emitir             = _caja_mayor_cheques_emitir,
        caja_mayor_cheques_completar          = _caja_mayor_cheques_completar,
        caja_mayor_cheques_filtrar_todos      = _caja_mayor_cheques_filtrar_todos,
    )

    async def receive_from_recall():
        try:
            while True:
                message = await websocket.receive()
                if "text" in message:
                    data  = json.loads(message["text"])
                    event = data.get("event", "")
                    if event == "audio_separate_raw.data":
                        participant = data["data"]["data"].get("participant", {})
                        if participant.get("name") == "Malena - Mi Gestión Web":
                            continue
                        b64_audio = data["data"]["data"]["buffer"]
                        pcm_audio = base64.b64decode(b64_audio)
                        await audio_queue.put(pcm_audio)
                    else:
                        print(f"[WS] Evento: {event}")
                elif "bytes" in message:
                    chunk = message["bytes"]
                    if len(chunk) > 4:
                        await audio_queue.put(chunk[4:])
        except Exception as e:
            print(f"[WS] Recall.ai desconectado: {e}")
        finally:
            await audio_queue.put(None)

    _active_bridge = bridge
    try:
        await asyncio.gather(receive_from_recall(), bridge.run(audio_queue))
    except Exception as e:
        print(f"[BOT] Error en handle_recall_audio: {e}")
        traceback.print_exc()
    finally:
        if _active_bridge is bridge:
            _active_bridge = None
        await bridge.close()
        try:
            await pw_stop()
        except Exception:
            pass
        await _send_reset()
