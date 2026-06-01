"""
mgw_playwright.py
Ejecuta la demo de caja con Playwright — browser real con JS.
Dividida en fases para sincronizar con lo que dice Malena.
"""
import asyncio
import base64
from playwright.async_api import async_playwright, Page

from config import MGW_URL, MGW_USER, MGW_EMPRESA, MGW_PASSWORD

# Producto demo — ID numérico del array JS de caja.php
DEMO_PRODUCTO_NOMBRE = "Maple de Huevos"
DEMO_PRODUCTO_ID     = 1008
DEMO_CANTIDAD        = 1

_pw_instance = None
_browser     = None
_page: Page | None = None

# Control de fases — para no repetir
_caja_fase1_done = False
_caja_fase2_done = False


async def _screenshot_b64() -> str:
    if _page is None:
        return ""
    try:
        img = await _page.screenshot(type="jpeg", quality=85)
        return base64.b64encode(img).decode()
    except Exception as e:
        print(f"[PW] Error screenshot: {e}")
        return ""


async def _snap(on_screenshot, delay: float = 0.0):
    if delay > 0:
        await asyncio.sleep(delay)
    if on_screenshot:
        b64 = await _screenshot_b64()
        if b64:
            await on_screenshot(b64)


def reset_caja_fases():
    """Resetea el estado de fases al iniciar una nueva demo."""
    global _caja_fase1_done, _caja_fase2_done
    _caja_fase1_done = False
    _caja_fase2_done = False


async def pw_start():
    global _pw_instance, _browser, _page
    _pw_instance = await async_playwright().start()
    _browser = await _pw_instance.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = await _browser.new_context(viewport={"width": 1280, "height": 720})
    _page = await context.new_page()
    print("[PW] Browser iniciado ✓")


async def pw_stop():
    global _pw_instance, _browser, _page
    if _browser:
        await _browser.close()
    if _pw_instance:
        await _pw_instance.stop()
    _browser = None
    _page = None
    _pw_instance = None
    print("[PW] Browser cerrado ✓")


async def pw_login() -> bool:
    if _page is None:
        return False
    try:
        await _page.goto(f"{MGW_URL.rstrip('/')}/index.php",
                         wait_until="domcontentloaded", timeout=20000)
        await _page.wait_for_selector('[name="empresa"]', timeout=10000)

        await _page.locator('[name="empresa"]').fill(MGW_EMPRESA)
        await asyncio.sleep(0.3)
        await _page.locator('[name="usuario"]').fill(MGW_USER)
        await asyncio.sleep(0.3)
        await _page.locator('[name="contrasena"]').fill(MGW_PASSWORD)
        await asyncio.sleep(0.3)

        await _page.locator('[name="btnlogin"], button[type="submit"], input[type="submit"]').first.click()
        await _page.wait_for_url("**/home.php", timeout=20000)
        print("[PW] Login OK ✓")
        return True
    except Exception as e:
        print(f"[PW] Error login: {e}")
        try:
            img = await _page.screenshot(type="jpeg", quality=70)
            b64 = base64.b64encode(img).decode()
            print(f"[PW] Screenshot post-error: data:image/jpeg;base64,{b64[:50]}...")
        except Exception:
            pass
        return False


# ── FASE 1: navegar a caja, tipear producto, agregar al ticket ────────────────

async def demo_caja_fase1_agregar(on_screenshot=None) -> bool:
    """
    Fase 1: muestra la caja vacía, tipea 'Huevos', agrega al ticket.
    Se llama cuando Malena habla de buscar el producto y agregar.
    """
    global _caja_fase1_done
    if _caja_fase1_done:
        print("[PW] Fase 1 ya ejecutada, saltando")
        return True
    if _page is None:
        print("[PW] Browser no iniciado")
        return False

    base = MGW_URL.rstrip("/")

    async def snap(delay: float = 1.5):
        await _snap(on_screenshot, delay)

    try:
        # 1. Navegar a caja
        print("[PW] [Fase 1] Navegando a caja...")
        await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=20000)
        await _page.wait_for_selector('input#producto, input[name="producto"]', timeout=15000)
        await snap(2.0)  # cliente ve la caja vacía

        # 2. Tipear "Huevos" letra por letra — visual para el cliente
        print(f"[PW] [Fase 1] Escribiendo 'Huevos'...")
        campo = _page.locator('input#producto, input[name="producto"]').first
        await campo.click()
        await campo.fill("")
        await campo.type("Huevos", delay=120)  # más lento para que se vea bien
        await snap(1.5)  # cliente ve el texto escrito y el dropdown

        # 3. Agregar producto via mgw_session (sesión autenticada server-side)
        print(f"[PW] [Fase 1] Agregando producto ID={DEMO_PRODUCTO_ID} via session...")
        from mgw_session import mgw_get as _mgw_get
        _agregar_url = f"/ajax_caja_agregar_producto_consti.php?cantidad={DEMO_CANTIDAD}&producto={DEMO_PRODUCTO_ID}&cliente=0"
        print(f"[PW] [Fase 1] URL: {_agregar_url}")
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, _mgw_get, _agregar_url)
        result_text = resp.text if resp else "sin respuesta"
        print(f"[PW] [Fase 1] Resultado agregar: {result_text!r}")

        # 4. Refrescar el iframe de caja para mostrar el producto en el ticket
        await _page.reload(wait_until="domcontentloaded")
        await _page.wait_for_selector('input#producto, input[name="producto"]', timeout=10000)
        await snap(2.5)  # cliente ve el producto en el ticket

        _caja_fase1_done = True
        print("[PW] [Fase 1] ✓ Producto agregado al ticket")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [Fase 1] Error: {e}")
        traceback.print_exc()
        await snap(0.5)
        return False


# ── FASE 2: seleccionar Efectivo, cerrar con Presupuesto F8 ───────────────────

async def demo_caja_fase2_pagar(on_screenshot=None, initial_delay: float = 0.0) -> bool:
    """
    Fase 2: selecciona Efectivo como forma de pago, cierra con Presupuesto (F8).
    initial_delay: segundos a esperar antes de arrancar (para sincronizar con el audio).
    """
    global _caja_fase2_done
    if _caja_fase2_done:
        print("[PW] Fase 2 ya ejecutada, saltando")
        return True
    if _page is None:
        print("[PW] Browser no iniciado")
        return False

    if initial_delay > 0:
        print(f"[PW] [Fase 2] Esperando {initial_delay}s antes de arrancar...")
        await asyncio.sleep(initial_delay)

    async def snap(delay: float = 1.5):
        await _snap(on_screenshot, delay)

    try:
        # 1. Seleccionar forma de pago Efectivo
        print("[PW] [Fase 2] Seleccionando Efectivo...")
        seleccionado = False

        # Intento A: botones/links/celdas visibles
        for selector in [
            'button:has-text("Efectivo")',
            'a:has-text("Efectivo")',
            '[onclick*="forma_pago"][onclick*="1"]',
            '[data-forma="1"]',
            'td:has-text("Efectivo")',
        ]:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    seleccionado = True
                    print(f"[PW] [Fase 2] Efectivo via '{selector}' ✓")
                    break
            except Exception:
                continue

        # Intento B: select_option
        if not seleccionado:
            for sel_selector in [
                'select#forma_de_pago',
                'select[name="forma_de_pago"]',
                'select:has(option:has-text("Efectivo"))',
            ]:
                try:
                    sel_el = _page.locator(sel_selector).first
                    if await sel_el.count() > 0:
                        await sel_el.select_option(label="Efectivo")
                        seleccionado = True
                        print(f"[PW] [Fase 2] Efectivo via select_option ✓")
                        break
                except Exception:
                    continue

        # Intento C: JS directo (select oculto)
        if not seleccionado:
            await _page.evaluate("""
                const selects = document.querySelectorAll('select');
                for (const s of selects) {
                    for (const opt of s.options) {
                        if (opt.text.toLowerCase().includes('efectivo') &&
                            !opt.text.toLowerCase().includes('%')) {
                            s.value = opt.value;
                            s.dispatchEvent(new Event('change', {bubbles: true}));
                            break;
                        }
                    }
                }
            """)
            print("[PW] [Fase 2] Efectivo forzado via JS ✓")

        await snap(3.0)  # cliente ve el panel de pago con vuelto — pausa más larga

        # 2. Cerrar con Presupuesto (F8) — opción más usada por los clientes
        await asyncio.sleep(8.0)  # pausa larga: Malena explica presupuesto F8 antes de cerrar
        print("[PW] [Fase 2] Cerrando con Presupuesto (F8)...")
        cerrado = False

        for selector in [
            'button:has-text("Presupuestar")',
            'a:has-text("Presupuestar")',
            '[onclick*="factura=3"]',
            '[onclick*="presupuesto"]',
            'button:has-text("F8")',
        ]:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    cerrado = True
                    print(f"[PW] [Fase 2] Presupuesto via '{selector}' ✓")
                    break
            except Exception:
                continue

        if not cerrado:
            # JS fallback — busca onclick con factura=3
            await _page.evaluate("""
                const todos = document.querySelectorAll('[onclick]');
                for (const el of todos) {
                    const oc = el.getAttribute('onclick') || '';
                    if (oc.includes('factura=3') || oc.includes('presupuest')) {
                        el.click();
                        break;
                    }
                }
            """)
            print("[PW] [Fase 2] Presupuesto via JS ✓")

        await snap(3.0)  # cliente ve la confirmación
        await snap(3.0)  # cliente ve el historial actualizado

        _caja_fase2_done = True
        print("[PW] [Fase 2] ✓ Venta cerrada con Presupuesto")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [Fase 2] Error: {e}")
        traceback.print_exc()
        await snap(0.5)
        return False


# ── Función legacy — mantiene compatibilidad si algo la llama ────────────────

async def demo_venta_caja(on_screenshot=None, con_factura: bool = False) -> bool:
    """Wrapper legacy: ejecuta fase 1 + fase 2 seguidas."""
    ok1 = await demo_caja_fase1_agregar(on_screenshot)
    await asyncio.sleep(1.0)
    ok2 = await demo_caja_fase2_pagar(on_screenshot)
    return ok1 and ok2