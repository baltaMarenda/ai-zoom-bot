"""
mgw_caja.py
Ejecuta una demo de venta completa en Mi Gestión Web via API.
Usado por el bot durante la etapa DEMO para mostrar el flujo de caja en vivo.
"""
import asyncio
import json
from mgw_session import mgw_get, is_logged_in
from config import MGW_URL

# Producto de demo: Huevos (id 1008) — ajustable
DEMO_PRODUCTO_ID  = 10
DEMO_PRODUCTO_NOMBRE = "Huevos"
DEMO_CANTIDAD     = 1

# Forma de pago 1 = Efectivo
DEMO_FORMA_PAGO   = 1

# factura=3 → Presupuesto (sin factura, en negro)
# factura=0 → Factura electrónica FCE (en blanco)
FACTURA_PRESUPUESTO = 3
FACTURA_FCE         = 0


def _session_post(path: str, data: dict) -> dict | None:
    """POST autenticado a MGW, retorna JSON o None."""
    from mgw_session import _session
    if _session is None:
        return None
    base = MGW_URL.rstrip("/")
    try:
        resp = _session.post(f"{base}{path}", data=data, timeout=15)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text[:200]}
    except Exception as e:
        print(f"[CAJA] Error POST {path}: {e}")
        return None


def _session_get_json(path: str) -> dict | list | None:
    """GET autenticado a MGW, retorna JSON o None."""
    from mgw_session import _session
    if _session is None:
        return None
    base = MGW_URL.rstrip("/")
    try:
        resp = _session.get(f"{base}{path}", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[CAJA] Error GET {path}: {e}")
        return None


# ── Pasos individuales ────────────────────────────────────────────────────────

def buscar_producto(nombre: str = DEMO_PRODUCTO_NOMBRE) -> dict | None:
    """
    Trae la lista de productos y busca uno por nombre.
    Retorna el producto encontrado o None.
    """
    productos = _session_get_json("/ajax_caja_productos_lista_new.php")
    if not productos:
        print(f"[CAJA] No se pudo traer lista de productos")
        return None

    # La lista puede ser un array de objetos con 'nombre' o 'descripcion'
    if isinstance(productos, list):
        nombre_lower = nombre.lower()
        for p in productos:
            p_nombre = (p.get("nombre") or p.get("descripcion") or "").lower()
            if nombre_lower in p_nombre:
                print(f"[CAJA] Producto encontrado: {p}")
                return p

    print(f"[CAJA] Producto '{nombre}' no encontrado en la lista")
    return None


def agregar_producto(producto_id: int, cantidad: int = 1, cliente_id: int = 0) -> dict | None:
    """Agrega un producto al ticket de caja actual via GET."""
    from mgw_session import _session
    if _session is None:
        return None

    base = MGW_URL.rstrip("/")
    try:
        resp = _session.get(
            f"{base}/ajax_caja_agregar_producto_consti.php",
            params={
                "cantidad": cantidad,
                "producto": producto_id,
                "cliente":  cliente_id,
            },
            timeout=15,
        )
        resp.raise_for_status()
        print(f"[CAJA] Agregar response ({resp.status_code}): '{resp.text[:200]}'")
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text[:200]}
    except Exception as e:
        print(f"[CAJA] Error agregando producto: {e}")
        return None


def verificar_venta() -> dict | None:
    """Verifica que la venta esté lista para cerrar."""
    result = _session_get_json("/ajax_caja_verificar_finalizar_venta.php")
    print(f"[CAJA] Verificar venta: {result}")
    return result


def finalizar_venta(total: float, factura: int = FACTURA_PRESUPUESTO,
                    forma_pago: int = DEMO_FORMA_PAGO, pago: float = 0) -> dict | None:
    """
    Cierra la venta via GET con query params.
    factura=3 → Presupuesto (en negro)
    factura=0 → Factura electrónica FCE (en blanco)
    """
    from mgw_session import _session
    if _session is None:
        return None

    params = {
        "forma_de_pago":    forma_pago,
        "comentario":       "",
        "pago":             "",
        "factura":          factura,
        "nombre":           "",
        "tipo":             "cuit",
        "dni":              "",
        "cuit":             "",
        "cliente":          1,
        "total":            total,
        "iibbp":            "undefined",
        "iibbc":            "undefined",
        "tipo_responsable": 5,
    }

    base = MGW_URL.rstrip("/")
    try:
        resp = _session.get(
            f"{base}/ajax_caja_productos_finalizar_factura_nuevo_consti.php",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        tipo = "Presupuesto" if factura == FACTURA_PRESUPUESTO else "FCE"
        print(f"[CAJA] Finalizar venta ({tipo}): {resp.text[:200]}")
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text[:200]}
    except Exception as e:
        print(f"[CAJA] Error finalizando venta: {e}")
        return None


# ── Demo completa ─────────────────────────────────────────────────────────────

async def demo_venta_completa(con_factura: bool = False,
                              on_step: callable = None) -> dict:
    """
    Ejecuta el flujo completo de demo de caja con delays para que el cliente
    pueda ver cada paso en pantalla.

    on_step(path) — callback opcional para navegar el iframe a una URL del proxy
    """
    loop = asyncio.get_event_loop()
    resultado = {
        "producto": DEMO_PRODUCTO_NOMBRE,
        "cantidad": DEMO_CANTIDAD,
        "total":    None,
        "tipo":     "FCE" if con_factura else "Presupuesto",
        "ok":       False,
    }

    if not is_logged_in():
        print("[CAJA] Sin sesión MGW")
        return resultado

    # 0. Navegar el iframe a caja.php y esperar que cargue
    if on_step:
        await on_step("/caja.php")
    await asyncio.sleep(3.0)  # dar tiempo a que el iframe cargue la caja

    # 1. Inicializar contexto server-side
    print("[CAJA] Inicializando contexto de caja...")
    await loop.run_in_executor(None, mgw_get, "/caja.php")
    await asyncio.sleep(1.0)

    # 2. Agregar producto — el cliente ve cómo aparece en el ticket
    print(f"[CAJA] Agregando producto {DEMO_PRODUCTO_ID} x{DEMO_CANTIDAD}...")
    agregar_result = await loop.run_in_executor(
        None, agregar_producto, DEMO_PRODUCTO_ID, DEMO_CANTIDAD, 0
    )
    print(f"[CAJA] Agregar resultado: {agregar_result}")

    # Refrescar el iframe para mostrar el producto agregado
    if on_step:
        await on_step("/caja.php")
    await asyncio.sleep(3.0)  # cliente ve el producto en el ticket

    # Extraer total
    total = 100.0
    if agregar_result and isinstance(agregar_result, dict):
        for key in ("total", "subtotal", "precio_total", "importe"):
            if agregar_result.get(key):
                total = float(agregar_result[key])
                break
    resultado["total"] = total

    # 3. Verificar
    await loop.run_in_executor(None, verificar_venta)
    await asyncio.sleep(1.0)

    # 4. Finalizar venta
    factura = FACTURA_FCE if con_factura else FACTURA_PRESUPUESTO
    fin_result = await loop.run_in_executor(
        None, finalizar_venta, total, factura, DEMO_FORMA_PAGO, total
    )

    # 5. Refrescar iframe para mostrar la venta cerrada en el historial
    if on_step:
        await on_step("/caja.php")
    await asyncio.sleep(2.0)

    if fin_result:
        resultado["ok"] = True
        print(f"[CAJA] ✓ Venta demo completada: {resultado}")
    else:
        print(f"[CAJA] ✗ Error cerrando venta")

    return resultado