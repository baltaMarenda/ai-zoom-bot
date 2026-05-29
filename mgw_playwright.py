"""
mgw_playwright.py
Ejecuta la demo de caja con Playwright — browser real con JS.
Toma screenshots en cada paso y los envía al iframe via WebSocket.
"""
import asyncio
import base64
from playwright.async_api import async_playwright, Page

from config import MGW_URL, MGW_USER, MGW_EMPRESA, MGW_PASSWORD

# Producto demo — ID numérico extraído del HTML de caja.php
# value: "1008" → Maple de Huevos
# value: "1009" → Docena de Huevos
DEMO_PRODUCTO_NOMBRE = "Maple de Huevos"
DEMO_PRODUCTO_ID     = 1008
DEMO_CANTIDAD        = 1

_pw_instance = None
_browser     = None
_page: Page | None = None


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


async def demo_venta_caja(on_screenshot=None, con_factura: bool = False) -> bool:
    """
    Ejecuta la demo de venta completa en Playwright.
    on_screenshot(b64) — callback que recibe cada screenshot para enviarlo al iframe.
    """
    if _page is None:
        print("[PW] Browser no iniciado")
        return False

    base = MGW_URL.rstrip("/")

    async def snap(delay: float = 1.5):
        await _snap(on_screenshot, delay)

    try:
        # ── 1. Navegar a caja — cliente ve la pantalla vacía ─────────────────
        print("[PW] Navegando a caja...")
        await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=20000)
        await _page.wait_for_selector('input#producto, input[name="producto"]', timeout=15000)
        await snap(2.0)  # cliente ve la caja vacía

        # ── 2. Simular búsqueda: escribir en el campo producto ───────────────
        # Esto es solo visual — el typeahead filtra localmente el array JS
        print(f"[PW] Escribiendo '{DEMO_PRODUCTO_NOMBRE}' en el campo...")
        campo = _page.locator('input#producto, input[name="producto"]').first
        await campo.click()
        await campo.fill("")
        await campo.type("Huevos", delay=80)  # tipear despacio para que se vea
        await snap(1.5)  # cliente ve el texto "Huevos" escrito

        # ── 3. Seleccionar del dropdown via JS (manipular typeahead) ─────────
        # El typeahead de Bootstrap filtra el array local y muestra un <ul>.
        # Lo activamos via JS seteando el valor y disparando el evento correcto.
        print(f"[PW] Seleccionando producto ID={DEMO_PRODUCTO_ID} via JS...")
        await _page.evaluate(f"""
            // Setear el valor visible del campo
            var campo = document.querySelector('input#producto, input[name="producto"]');
            if (campo) {{
                campo.value = '{DEMO_PRODUCTO_NOMBRE}';
                // Disparar el evento que usa el typeahead para confirmar selección
                $(campo).trigger('typeahead:selected');
                // También intentar via el data del typeahead directamente
                var ta = $(campo).data('typeahead');
                if (ta) {{
                    ta.query = '{DEMO_PRODUCTO_NOMBRE}';
                }}
            }}
            // Setear el ID oculto del producto (campo hidden que usa el form)
            var hiddenId = document.querySelector('input[name="id_producto"], input#id_producto');
            if (hiddenId) {{
                hiddenId.value = '{DEMO_PRODUCTO_ID}';
            }}
        """)
        await snap(1.0)  # cliente ve el producto seleccionado

        # ── 4. Agregar producto via fetch con ID numérico ────────────────────
        # El endpoint requiere el ID numérico, no el nombre
        print(f"[PW] Agregando producto ID={DEMO_PRODUCTO_ID} via fetch...")
        agregar_result = await _page.evaluate(f"""
            async () => {{
                const resp = await fetch(
                    '/ajax_caja_agregar_producto_consti.php?cantidad={DEMO_CANTIDAD}&producto={DEMO_PRODUCTO_ID}&cliente=0',
                    {{method: 'GET', credentials: 'same-origin'}}
                );
                return await resp.text();
            }}
        """)
        print(f"[PW] Resultado agregar: {agregar_result}")

        # Refrescar la lista de productos en el ticket (el iframe del ticket)
        await _page.evaluate("""
            if (typeof actualizar_lista_ventas === 'function') {
                actualizar_lista_ventas();
            } else if (typeof cargar_lista_ventas === 'function') {
                cargar_lista_ventas();
            }
        """)
        await snap(2.5)  # cliente ve el producto en el ticket

        # ── 5. Seleccionar forma de pago Efectivo ────────────────────────────
        # El select de forma de pago puede estar oculto; forzamos via JS
        print("[PW] Seleccionando Efectivo via JS...")

        # Primero intentar click en botones/links visibles
        seleccionado = False
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
                    print(f"[PW] Efectivo clickeado via '{selector}' ✓")
                    break
            except Exception:
                continue

        # Si no hubo botón visible: select_option o JS directo
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
                        print(f"[PW] Efectivo via select_option ✓")
                        break
                except Exception:
                    continue

        if not seleccionado:
            # Forzar con JS — encuentra el <select> aunque esté oculto
            await _page.evaluate("""
                const selects = document.querySelectorAll('select');
                for (const s of selects) {
                    for (const opt of s.options) {
                        if (opt.text.toLowerCase().includes('efectivo') &&
                            !opt.text.toLowerCase().includes('%')) {
                            s.value = opt.value;
                            s.dispatchEvent(new Event('change', {bubbles: true}));
                            console.log('Efectivo seteado via JS, value=' + opt.value);
                            break;
                        }
                    }
                }
            """)
            print("[PW] Efectivo forzado via JS ✓")

        await snap(2.0)  # cliente ve el panel de pago con vuelto

        # ── 6. Cerrar venta con Presupuesto ──────────────────────────────────
        print("[PW] Cerrando venta con Presupuesto...")
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
                    print(f"[PW] Venta cerrada via '{selector}' ✓")
                    break
            except Exception:
                continue

        if not cerrado:
            # Intentar via JS buscando el onclick con factura=3
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
            print("[PW] Presupuesto via JS ✓")

        await snap(2.5)  # confirmación de venta
        await snap(2.0)  # historial actualizado

        print("[PW] ✓ Demo de venta completada")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] Error en demo: {e}")
        traceback.print_exc()
        await snap(0.5)
        return False