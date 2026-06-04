"""
mgw_playwright.py
Ejecuta la demo de caja con Playwright — browser real con JS.
Dividida en fases para sincronizar con lo que dice Malena.
"""
import asyncio
import base64
from playwright.async_api import async_playwright, Page

from config import MGW_URL, MGW_USER, MGW_EMPRESA, MGW_PASSWORD, TEST_MODE

# Producto demo — ID numérico del array JS de caja.php
DEMO_PRODUCTO_NOMBRE = "Huevos"
DEMO_PRODUCTO_ID     = 10
DEMO_CANTIDAD        = 1

_pw_instance = None
_browser     = None
_page: Page | None = None

# Control de fases — para no repetir
_caja_fase1_done     = False
_caja_fase1_launched = False  # True en cuanto la task de Fase 1 entra a la función
_caja_fase2_done     = False
_caja_fase2_launched = False  # True en cuanto la task de Fase 2 entra a la función


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
    global _caja_fase1_done, _caja_fase1_launched, _caja_fase2_done, _caja_fase2_launched
    _caja_fase1_done     = False
    _caja_fase1_launched = False
    _caja_fase2_done     = False
    _caja_fase2_launched = False


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


# ── ACCESO: mostrar login en vivo con los datos del .env ─────────────────────

async def demo_acceso_login(on_screenshot=None) -> bool:
    """
    Muestra el proceso de ingreso al sistema en vivo:
    navega al login, tipea empresa/usuario/contraseña carácter a carácter y entra.
    """
    if _page is None:
        return False

    base = MGW_URL.rstrip("/")

    async def snap():
        """Captura inmediata — los delays son sleeps explícitos antes de llamar snap()."""
        await _snap(on_screenshot, 0.0)

    try:
        # Limpiar sesión previa para que el servidor muestre el formulario
        await _page.context.clear_cookies()

        print("[PW] [ACCESO] Navegando al login...")
        await _page.goto(f"{base}/index.php", wait_until="networkidle", timeout=20000)
        await _page.wait_for_selector('[name="empresa"]', timeout=10000)

        # Vaciar cualquier valor autocompletado por el navegador y bloquear autofill
        await _page.evaluate("""() => {
            document.querySelectorAll('input').forEach(inp => {
                inp.setAttribute('autocomplete', 'new-password');
                inp.value = '';
            });
        }""")

        # 2 s de pausa — el espectador ve el formulario completamente vacío
        await asyncio.sleep(2.0)
        await snap()  # ① formulario vacío

        # Empresa — visible letra por letra (150 ms/carácter)
        await _page.locator('[name="empresa"]').click()
        await _page.locator('[name="empresa"]').type(MGW_EMPRESA, delay=150)
        await asyncio.sleep(0.4)
        await snap()  # ② empresa completa

        # Usuario
        await _page.locator('[name="usuario"]').click()
        await _page.locator('[name="usuario"]').type(MGW_USER, delay=150)
        await asyncio.sleep(0.4)
        await snap()  # ③ usuario completo

        # Contraseña
        await _page.locator('[name="contrasena"]').click()
        await _page.locator('[name="contrasena"]').type(MGW_PASSWORD, delay=150)

        # 2 s antes de enviar — Malena termina de describir los campos mientras el usuario ve el form completo
        await asyncio.sleep(2.0)
        await snap()  # ④ formulario completo, a punto de ingresar

        print("[PW] [ACCESO] Enviando credenciales...")
        await _page.locator(
            '[name="btnlogin"], button[type="submit"], input[type="submit"]'
        ).first.click()
        await _page.wait_for_url("**/home.php", timeout=20000)

        await asyncio.sleep(1.5)
        await snap()  # ⑤ home del sistema

        print("[PW] [ACCESO] Demo login completada ✓")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [ACCESO] Error: {e}")
        traceback.print_exc()
        await snap()
        return False


# ── FASE 1: navegar a caja, tipear producto, agregar al ticket ────────────────

async def demo_caja_fase1_agregar(on_screenshot=None) -> bool:
    """
    Fase 1: muestra la caja vacía, tipea 'Huevos', agrega al ticket.
    Se llama cuando Malena habla de buscar el producto y agregar.
    """
    global _caja_fase1_done, _caja_fase1_launched
    if _caja_fase1_done or _caja_fase1_launched:
        print("[PW] Fase 1 ya en curso o completada, saltando")
        return True
    _caja_fase1_launched = True
    if _page is None:
        print("[PW] Browser no iniciado")
        return False

    base = MGW_URL.rstrip("/")

    async def snap(delay: float = 1.5):
        await _snap(on_screenshot, delay)

    try:
        print("[PW] [Fase 1] Navegando a caja...")
        await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=30000)
        await _page.wait_for_selector('input#producto, input[name="producto"]', timeout=15000)
        # Pausa fija para que el JS de caja termine de inicializarse (arqueo, autocomplete, etc.)
        await asyncio.sleep(3.0)
        print("[PW] [Fase 1] Caja lista ✓")

        await snap(2.0)  # cliente ve la caja vacía

        # 2. Tipear "Huevos" letra por letra — visual para el cliente
        print(f"[PW] [Fase 1] Escribiendo 'Huevos'...")
        campo = _page.locator('input#producto, input[name="producto"]').first
        await campo.click()
        await campo.fill("")
        await campo.type("Huevos", delay=120)

        # 3. Seleccionar sugerencia del dropdown de jQuery UI Autocomplete
        print("[PW] [Fase 1] Esperando sugerencia del autocomplete...")
        await _page.wait_for_selector(
            '.ui-autocomplete .ui-menu-item', state="visible", timeout=8000
        )
        await _page.locator('.ui-autocomplete .ui-menu-item').first.click()
        await asyncio.sleep(0.4)  # dar tiempo a que el campo quede poblado
        await snap(1.0)  # cliente ve el producto seleccionado

        # 4. Encontrar botón Agregar
        print("[PW] [Fase 1] Buscando botón Agregar...")
        agregar_btn = None
        for selector in [
            'button:has-text("Agregar")',
            '#btnAgregar',
            'button#Agregar',
            'input[value="Agregar"]',
            'button.btn-success',
        ]:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    agregar_btn = el
                    print(f"[PW] [Fase 1] Botón Agregar encontrado: '{selector}'")
                    break
            except Exception:
                continue

        print("[PW] [Fase 1] Haciendo clic en Agregar...")
        if agregar_btn is not None:
            await agregar_btn.click()
        else:
            await _page.evaluate("""
                const all = [...document.querySelectorAll('button, input[type="button"], a')];
                const btn = all.find(
                    e => (e.textContent || e.value || '').trim().toLowerCase() === 'agregar'
                );
                if (btn) btn.click();
            """)
            print("[PW] [Fase 1] Clic Agregar via JS fallback")
        # Pausa fija para que el ticket refleje el producto en pantalla
        await asyncio.sleep(2.5)
        await snap(0.0)  # cliente ve el producto en el ticket

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

async def demo_caja_fase2_pagar(on_screenshot=None, initial_delay: float = 0.0, press_f8_signal=None) -> bool:
    """
    Fase 2: selecciona Efectivo como forma de pago, cierra con Presupuesto (F8).
    initial_delay: segundos a esperar antes de arrancar (para sincronizar con el audio).
    """
    global _caja_fase2_done, _caja_fase2_launched
    if _caja_fase2_done or _caja_fase2_launched:
        print("[PW] Fase 2 ya en curso o completada, saltando")
        return True
    _caja_fase2_launched = True  # bloquear re-entrada antes de cualquier await
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

        # 2. Esperar a que Malena termine de hablar sobre pagos antes de cerrar con F8.
        #    Timeout extendido a 40s para cubrir bloques de audio largos sobre F8/FCE.
        if press_f8_signal is not None:
            try:
                await asyncio.wait_for(press_f8_signal.wait(), timeout=40.0)
                print("[PW] [Fase 2] Señal de audio recibida — presionando F8 ✓")
            except asyncio.TimeoutError:
                print("[PW] [Fase 2] Timeout 40s esperando señal — procediendo igual")
        else:
            await asyncio.sleep(8.0)  # fallback si se llama sin señal (demo_venta_caja legacy)

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


# ── Demo secuencial — flujo nuevo guiado por eventos ─────────────────────────

async def _reset_caja_items() -> None:
    """Elimina todos los ítems del ticket de caja si los hubiera de una sesión anterior."""
    if _page is None:
        return
    try:
        for _ in range(30):
            deleted = await _page.evaluate("""() => {
                const btn = document.querySelector(
                    '[onclick*="eliminar_producto"], [onclick*="quitar_producto"], '
                    + '[onclick*="caja_eliminar"], .btn-eliminar, '
                    + '[title*="liminar"], .fa-times-circle, .fa-trash'
                );
                if (btn) { btn.click(); return true; }
                return false;
            }""")
            if not deleted:
                break
            await asyncio.sleep(0.6)
        print("[PW] [RESET CAJA] Ticket limpio ✓")
    except Exception as e:
        print(f"[PW] [RESET CAJA] {e}")


async def _manejar_arqueo(on_screenshot=None) -> None:
    """
    Detecta la UI de apertura/arqueo de caja y la confirma.
    El caller ya esperó el tiempo suficiente para que el AJAX de arqueo haya disparado.
    Loggea todos los elementos visibles para facilitar el debug si falla.
    """
    if _page is None:
        return
    try:
        # Dump de todos los elementos interactivos visibles — clave para debug
        elementos = await _page.evaluate("""() => {
            return [...document.querySelectorAll('input, button, a, [onclick]')]
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                })
                .slice(0, 20)
                .map(el => ({
                    tag:     el.tagName,
                    id:      el.id || '',
                    name:    el.getAttribute('name') || '',
                    type:    el.getAttribute('type') || '',
                    value:   el.value || el.getAttribute('value') || '',
                    text:    el.textContent?.trim().slice(0, 40) || '',
                    onclick: (el.getAttribute('onclick') || '').slice(0, 60),
                    class:   el.className?.slice(0, 60) || ''
                }));
        }""")
        print(f"[PW] [ARQUEO] Elementos visibles en caja ({len(elementos)}):")
        for el in elementos:
            print(f"  {el}")

        # Buscar y hacer clic en el botón de confirmación del arqueo
        for selector in [
            'button:has-text("Iniciar")',
            'button:has-text("Abrir")',
            'button:has-text("Aceptar")',
            'button:has-text("Confirmar")',
            'button:has-text("Guardar")',
            'button:has-text("Continuar")',
            'button:has-text("Ok")',
            'button:has-text("OK")',
            'input[value="Iniciar"]',
            'input[value="Aceptar"]',
            'input[value="Confirmar"]',
            'input[value="Guardar"]',
            '[onclick*="arqueo"]',
            '[onclick*="guardar_arqueo"]',
            '[onclick*="iniciar_caja"]',
            '[onclick*="abrir_caja"]',
            '.modal-footer .btn-primary',
            '.modal-footer .btn-success',
            '.modal .btn-primary',
            '.modal .btn-success',
            'button[type="submit"]',
            'input[type="submit"]',
        ]:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    texto = await el.text_content() or selector
                    print(f"[PW] [ARQUEO] Confirmando via '{selector}' (texto: '{texto.strip()[:30]}')")
                    await el.click()
                    await asyncio.sleep(2.5)
                    print("[PW] [ARQUEO] Apertura de caja confirmada ✓")
                    if on_screenshot:
                        b64 = await _screenshot_b64()
                        if b64:
                            await on_screenshot(b64)
                    return
            except Exception:
                continue

        print("[PW] [ARQUEO] Ningún botón de arqueo encontrado — la interfaz principal podría estar directa")
    except Exception as e:
        print(f"[PW] [ARQUEO] Error: {e}")


# ── CLIENTES: abrir formulario de nuevo cliente con Playwright ────────────────

async def _demo_clientes_abrir_formulario(on_screenshot=None) -> bool:
    """
    Navega a clientes.php en el headless browser, hace click en 'Nuevo Cliente'
    y captura el formulario AJAX con estilos completos.
    No navega a ajax_clientes_nuevo_cliente.php directamente (devuelve HTML parcial).
    """
    if _page is None:
        return False

    base = MGW_URL.rstrip("/")

    async def snap():
        b64 = await _screenshot_b64()
        if b64 and on_screenshot:
            await on_screenshot(b64)

    try:
        print("[PW] [CLIENTES] Navegando a clientes.php...")
        await _page.goto(f"{base}/clientes.php", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2.5)
        await snap()  # lista de clientes

        print("[PW] [CLIENTES] Buscando botón Nuevo Cliente...")
        clicked = False
        for selector in [
            'a:has-text("Nuevo Cliente")',
            'button:has-text("Nuevo Cliente")',
            'a:has-text("Nuevo")',
            'button:has-text("Nuevo")',
            '[onclick*="nuevo_cliente"]',
            '[onclick*="NuevoCliente"]',
            'a.btn-success',
            'button.btn-success',
        ]:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    clicked = True
                    print(f"[PW] [CLIENTES] Click via '{selector}' ✓")
                    break
            except Exception:
                continue

        if not clicked:
            await _page.evaluate("""
                const all = [...document.querySelectorAll('a, button')];
                const btn = all.find(e => {
                    const t = (e.textContent || '').trim().toLowerCase();
                    return t.includes('nuevo') || t.includes('alta');
                });
                if (btn) btn.click();
            """)
            print("[PW] [CLIENTES] Click via JS fallback")

        await asyncio.sleep(3.0)
        await snap()  # formulario de nuevo cliente (cargado vía AJAX)
        print("[PW] [CLIENTES] ✓ Formulario visible")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [CLIENTES] Error: {e}")
        traceback.print_exc()
        await snap()
        return False


# ── ESTADÍSTICAS > VENTAS: filtrar por hoy y buscar ──────────────────────────

async def demo_estadisticas_ventas(on_screenshot=None) -> bool:
    """
    Navega a Estadísticas > Ventas, asegura la fecha de hoy y aprieta Buscar.
    Toma screenshots del formulario inicial y de los resultados.
    """
    if _page is None:
        return False

    import datetime
    base = MGW_URL.rstrip("/")
    hoy = datetime.date.today().strftime("%d/%m/%Y")

    async def snap(delay: float = 0.0):
        await _snap(on_screenshot, delay)

    try:
        print("[PW] [ESTAD] Navegando a estadisticas_ventas.php...")
        await _page.goto(
            f"{base}/estadisticas_ventas.php",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(2.0)
        await snap()  # vista inicial con los filtros visibles

        # Poner fecha de hoy en los campos de fecha que estén vacíos
        await _page.evaluate(f"""() => {{
            document.querySelectorAll('input[name*="fecha"], input[id*="fecha"]')
                .forEach(inp => {{
                    if (!inp.value) {{
                        inp.value = '{hoy}';
                        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                }});
        }}""")

        print("[PW] [ESTAD] Clic en Buscar...")
        buscar = _page.locator("#boton_buscar")
        if await buscar.count() > 0 and await buscar.is_visible():
            await buscar.click()
            print("[PW] [ESTAD] Clic en #boton_buscar ✓")
        else:
            await _page.evaluate("const b = document.getElementById('boton_buscar'); if(b) b.click();")
            print("[PW] [ESTAD] Clic via JS fallback ✓")

        await asyncio.sleep(3.0)
        await snap()  # resultados de ventas del día

        print("[PW] [ESTAD] Demo estadísticas ventas ✓")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [ESTAD] Error: {e}")
        traceback.print_exc()
        await snap()
        return False


# ── STOCK: mostrar existencias con botón Todos ────────────────────────────────

async def demo_stock_existencias(on_screenshot=None) -> bool:
    """
    Navega a Stock > Existencias en Playwright, aprieta el botón Todos
    y toma screenshots de la tabla completa para mostrar en la reunión.
    """
    if _page is None:
        return False

    base = MGW_URL.rstrip("/")

    async def snap(delay: float = 0.0):
        await _snap(on_screenshot, delay)

    try:
        print("[PW] [STOCK] Navegando a Existencias...")
        await _page.goto(
            f"{base}/stock_existencia_2.php",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(2.0)
        await snap()  # vista inicial antes de filtrar

        print("[PW] [STOCK] Haciendo clic en botón Todos...")
        todos_btn = _page.locator("#boton_grupo_todos")
        if await todos_btn.count() > 0 and await todos_btn.is_visible():
            await todos_btn.click()
            print("[PW] [STOCK] Clic en #boton_grupo_todos ✓")
        else:
            await _page.evaluate("""
                const btn = document.getElementById('boton_grupo_todos');
                if (btn) btn.click();
            """)
            print("[PW] [STOCK] Clic via JS fallback ✓")

        await asyncio.sleep(3.0)
        await snap()  # tabla completa con todas las existencias

        print("[PW] [STOCK] Demo existencias ✓")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [STOCK] Error: {e}")
        traceback.print_exc()
        await snap()
        return False


# ── PROVEEDORES: demo completa de compra e ingreso de stock ───────────────────

async def _demo_proveedores(
    decir_frase,
    on_screenshot=None,
    on_screenshot_end=None,
    navigate_fn=None,
) -> bool:
    """
    Demo de Proveedores:
    Editar → +Compra → form (solo importe) → Finalizar → carrito "Cargar productos"
    → agregar Vacío 10kg → Finalizar detalles → explicar Impaga (sin clickear).
    """
    if _page is None:
        return False

    base = MGW_URL.rstrip("/")

    async def snap():
        b64 = await _screenshot_b64()
        if b64 and on_screenshot:
            await on_screenshot(b64)

    async def snap_end():
        if on_screenshot_end:
            await on_screenshot_end()

    async def nav(path: str):
        if navigate_fn:
            await navigate_fn(path)

    async def click_first(selectors: list, label: str) -> bool:
        for selector in selectors:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    print(f"[PW] [PROV] {label} via '{selector}' ✓")
                    return True
            except Exception:
                continue
        return False

    try:
        # ── PASO 1: Navegar a proveedores + click "Editar" ────────────────────────
        await decir_frase(
            "Ahora pasamos a la sección de Proveedores. "
            "Acá tenemos cargados todos nuestros proveedores "
            "y es donde registramos las compras que les hacemos. "
            "Para ingresar una compra apretamos el botón azul 'Editar' "
            "que aparece a la derecha de cada proveedor."
        )

        await nav("/compras.php")
        print("[PW] [PROV] Navegando a compras.php...")
        await _page.goto(f"{base}/compras.php", wait_until="domcontentloaded", timeout=20000)

        try:
            await _page.wait_for_selector('tbody tr td', timeout=12000)
            print("[PW] [PROV] DataTable cargada ✓")
        except Exception:
            print("[PW] [PROV] Timeout esperando DataTable, continuando...")
        await asyncio.sleep(1.0)
        await snap()  # lista de proveedores

        print("[PW] [PROV] Buscando botón Editar del primer proveedor...")
        clicked = await click_first([
            'tbody tr:first-child [data-original-title="Editar"]',
            'tbody tr:first-child [title="Editar"]',
            '[data-original-title="Editar"]',
            '[title="Editar"]',
        ], "Editar proveedor")

        if not clicked:
            result = await _page.evaluate("""() => {
                const all = [...document.querySelectorAll('[data-original-title], [title]')];
                const btn = all.find(e => {
                    const t = (e.getAttribute('data-original-title') || e.getAttribute('title') || '').toLowerCase();
                    return t === 'editar';
                });
                if (btn) { btn.click(); return btn.outerHTML.slice(0, 100); }
                return null;
            }""")
            print(f"[PW] [PROV] Editar via JS: {result}")

        await asyncio.sleep(3.0)
        await snap()  # historial de compras del proveedor

        # ── PASO 2: Click "+ Compra" ──────────────────────────────────────────────
        await decir_frase(
            "Acá están todas las compras registradas para este proveedor. "
            "Para cargar una nueva apretamos el botón '+ Compra'."
        )

        clicked = await click_first([
            '[title="Nueva Compra"]',
            'a:has-text("+ Compra")',
            'button:has-text("+ Compra")',
            '[onclick*="movimientos_nuevo_compra"]',
            '[onclick*="nuevo_compra"]',
            '[onclick*="nueva_compra"]',
        ], "+ Compra")

        if not clicked:
            await _page.evaluate("""
                const all = [...document.querySelectorAll('a, button, [onclick], [title]')];
                const btn = all.find(e => {
                    const t = (e.textContent || '').trim().toLowerCase();
                    const title = (e.getAttribute('title') || '').toLowerCase();
                    return t.includes('+ compra') || title.includes('nueva compra');
                });
                if (btn) btn.click();
            """)
            print("[PW] [PROV] + Compra vía JS ✓")

        await asyncio.sleep(3.0)
        await snap()  # formulario nueva compra

        # ── PASO 3: Explicar el formulario, rellenar solo el Importe ─────────────
        await decir_frase(
            "En este formulario tenemos varios campos. "
            "La fecha de hoy es cuando hicimos la compra — también podemos poner fechas pasadas "
            "si nos olvidamos de cargarla en el momento. "
            "La fecha de vencimiento es cuando vence el pago, "
            "y podemos configurar una alerta para que nos avise el mismo día "
            "o con 3, 7, 10 o 20 días de anticipación, o que no alerte. "
            "También tenemos número de compra, tipo de factura, importe, comentarios e IVA. "
            "Para la demo cargamos solo el importe."
        )

        print("[PW] [PROV] Llenando importe de la compra...")
        for sel in [
            'input[name="importe"]', 'input[name="total"]', 'input[name="monto"]',
            'input[placeholder*="mporte"]',
        ]:
            try:
                el = _page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await el.fill("")
                    await el.type("150000", delay=120)
                    print(f"[PW] [PROV] Importe via '{sel}' ✓")
                    break
            except Exception:
                continue

        await asyncio.sleep(0.5)
        await snap()  # formulario con importe completado

        # ── PASO 4: Click Finalizar ───────────────────────────────────────────────
        print("[PW] [PROV] Clicando Finalizar compra...")
        clicked = await click_first([
            '#ingresar_compra_boton',
            'button[name="ingresar_compra_boton"]',
            'button:has-text("Finalizar")',
            'a:has-text("Finalizar")',
            '[onclick*="ingresar_compra"]',
        ], "Finalizar compra")

        if not clicked:
            await _page.evaluate("""
                const btn = document.getElementById('ingresar_compra_boton')
                    || [...document.querySelectorAll('button')].find(
                        e => e.textContent.trim().toLowerCase().includes('finalizar')
                    );
                if (btn) btn.click();
            """)
            print("[PW] [PROV] Finalizar via JS ✓")

        await asyncio.sleep(3.0)
        await snap()  # lista de compras con la recién creada

        # ── PASO 5: Click carrito verde "Cargar productos" ────────────────────────
        await decir_frase(
            "Perfecto, la compra ya quedó registrada. "
            "Pero todavía falta indicar los productos que compramos para que el stock se actualice. "
            "Para eso apretamos el botón del carrito verde a la derecha, que dice 'Cargar productos'."
        )

        clicked = await click_first([
            'tbody tr:first-child [data-original-title="Cargar productos"]',
            'tbody tr:first-child [title="Cargar productos"]',
            '[data-original-title="Cargar productos"]',
            '[title="Cargar productos"]',
            'tbody tr:first-child a.btn-success',
            '[onclick*="compra_detalles"]',
            '[onclick*="detalle_compra"]',
        ], "Cargar productos (carrito)")

        if not clicked:
            result = await _page.evaluate("""() => {
                const all = [...document.querySelectorAll('[data-original-title], [title], [onclick]')];
                const btn = all.find(e => {
                    const dt = (e.getAttribute('data-original-title') || e.getAttribute('title') || '').toLowerCase();
                    const oc = (e.getAttribute('onclick') || '').toLowerCase();
                    return dt.includes('cargar') || oc.includes('detalle') || oc.includes('carrito');
                });
                if (btn) { btn.click(); return btn.outerHTML.slice(0, 100); }
                return null;
            }""")
            print(f"[PW] [PROV] Cargar productos via JS: {result}")

        await asyncio.sleep(3.0)
        await snap()  # formulario de carga de productos (vacío)

        # ── PASO 6: Ingresar "Vacío" 10 kg y Agregar ─────────────────────────────
        await decir_frase(
            "Acá cargamos los productos que compramos. "
            "Buscamos el producto — en este caso 'Vacío' — "
            "indicamos los kilos, 10 en este ejemplo, y apretamos Agregar."
        )

        print("[PW] [PROV] Ingresando producto 'Vacío'...")
        for sel in [
            'input[name="producto"]', '#producto', 'input.ui-autocomplete-input',
            'input[placeholder*="roducto"]', 'input[placeholder*="uscar"]',
        ]:
            try:
                el = _page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await el.fill("")
                    await el.type("Vacío", delay=150)
                    print(f"[PW] [PROV] 'Vacío' via '{sel}' ✓")
                    break
            except Exception:
                continue

        await asyncio.sleep(1.5)
        try:
            await _page.wait_for_selector('.ui-autocomplete .ui-menu-item', state="visible", timeout=4000)
            await _page.locator('.ui-autocomplete .ui-menu-item').first.click()
            print("[PW] [PROV] Autocomplete seleccionado ✓")
        except Exception:
            print("[PW] [PROV] Sin autocomplete, continuando")

        await asyncio.sleep(0.5)

        # Peso — 10 kg
        for sel in [
            'input[name="peso"]', '#peso', 'input[name="kilos"]',
            'input[placeholder*="eso"]', 'input[placeholder*="kg"]',
        ]:
            try:
                el = _page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await el.fill("10")
                    print(f"[PW] [PROV] Peso 10kg via '{sel}' ✓")
                    break
            except Exception:
                continue

        await asyncio.sleep(0.3)
        await snap()  # formulario con datos antes de Agregar

        clicked = await click_first([
            '[onclick*="agregar_producto_compra"]',
            'button:has-text("Agregar")',
            'a:has-text("Agregar")',
            '#btnAgregar',
        ], "Agregar producto")

        if not clicked:
            await _page.evaluate("""
                const btn = [...document.querySelectorAll('button, a, [onclick]')].find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('agregar_producto_compra') || t === 'agregar';
                });
                if (btn) btn.click();
            """)
            print("[PW] [PROV] Agregar via JS ✓")

        await asyncio.sleep(2.0)
        await snap()  # producto en la lista de detalles

        # ── PASO 7: Finalizar detalles ────────────────────────────────────────────
        await decir_frase(
            "Podemos seguir sumando más productos a esta misma compra. "
            "Para la demo lo cerramos con uno solo — apretamos 'Finalizar detalles de compra'."
        )

        clicked = await click_first([
            '[onclick*="finalizar_compra_detalles"]',
            'a:has-text("Finalizar detalles")',
            'button:has-text("Finalizar detalles")',
        ], "Finalizar detalles")

        if not clicked:
            await _page.evaluate("""
                const btn = [...document.querySelectorAll('[onclick], a, button')].find(e => {
                    const oc = (e.getAttribute('onclick') || '').toLowerCase();
                    const t = (e.textContent || '').toLowerCase();
                    return oc.includes('finalizar_compra_detalles') || t.includes('finalizar detalle');
                });
                if (btn) btn.click();
            """)
            print("[PW] [PROV] Finalizar detalles via JS ✓")

        await asyncio.sleep(3.0)
        await snap()  # lista de compras — figura como Impaga

        # ── PASO 8: Solo explicar el estado Impaga (sin clickear) ─────────────────
        await decir_frase(
            "Listo, la compra quedó registrada y el stock de Vacío ya se actualizó. "
            "Ven que figura como 'Impaga' — porque todavía no la pagamos. "
            "Cuando hagamos el pago, simplemente apretamos sobre la etiqueta 'Impaga' "
            "y el sistema la pasa a pagada automáticamente."
        )

        await asyncio.sleep(1.5)
        await snap()

        await snap_end()
        print("[PW] [PROV] ✓ Demo de Proveedores completa")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [PROV] Error: {e}")
        traceback.print_exc()
        b64 = await _screenshot_b64()
        if b64 and on_screenshot:
            await on_screenshot(b64)
        return False



async def _demo_produccion(
    decir_frase,
    on_screenshot=None,
    on_screenshot_end=None,
    navigate_fn=None,
) -> bool:
    """
    Demo de Producción:
    Plantillas → nueva plantilla "Milanesas" → detalle (ingredientes entrada + salida)
    → Produccion → nueva produccion con esa plantilla.
    """
    if _page is None:
        return False

    base = MGW_URL.rstrip("/")

    async def snap():
        b64 = await _screenshot_b64()
        if b64 and on_screenshot:
            await on_screenshot(b64)

    async def snap_end():
        if on_screenshot_end:
            await on_screenshot_end()

    async def nav(path: str):
        if navigate_fn:
            await navigate_fn(path)

    async def click_first(selectors: list, label: str) -> bool:
        for selector in selectors:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    print(f"[PW] [PROD] {label} via '{selector}' ✓")
                    return True
            except Exception:
                continue
        return False

    async def ingresar_producto_detalle(nombre: str, cantidad: str):
        for sel in [
            'input[name="producto"]', '#producto', '.ui-autocomplete-input',
            'input[placeholder*="roducto"]', 'input[placeholder*="uscar"]',
        ]:
            try:
                el = _page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await el.fill("")
                    await el.type(nombre, delay=120)
                    print(f"[PW] [PROD] '{nombre}' via '{sel}' ✓")
                    break
            except Exception:
                continue

        await asyncio.sleep(1.5)
        try:
            await _page.wait_for_selector('.ui-autocomplete .ui-menu-item', state="visible", timeout=4000)
            await _page.locator('.ui-autocomplete .ui-menu-item').first.click()
            print(f"[PW] [PROD] Autocomplete '{nombre}' ✓")
        except Exception:
            print(f"[PW] [PROD] Sin autocomplete para '{nombre}'")

        try:
            cant_el = _page.locator('#cantidad').first
            if await cant_el.count() > 0:
                await cant_el.click()
                await cant_el.fill(cantidad)
                print(f"[PW] [PROD] Cantidad={cantidad} ✓")
        except Exception as e:
            print(f"[PW] [PROD] Error cantidad: {e}")

    try:
        # ── PASO 1: Plantillas ────────────────────────────────────────────────────
        await decir_frase(
            "Genial, entonces te muestro la sección de Producción. "
            "Acá tenemos dos partes: Producción y Plantillas, como se ve en el menú. "
            "Primero, para producir necesitamos tener la plantilla de qué usamos para elaborar. "
            "Vamos a Producción, Plantillas."
        )

        await nav("/produccion_plantillas.php")
        print("[PW] [PROD] Navegando a produccion_plantillas.php...")
        await _page.goto(f"{base}/produccion_plantillas.php", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2.0)
        await snap()  # lista de plantillas

        # ── PASO 2: Nueva plantilla ───────────────────────────────────────────────
        await decir_frase(
            "Agregamos una nueva plantilla apretando el botón Nueva plantilla. "
            "Ingresamos el nombre, en este caso Milanesas, y apretamos Agregar."
        )

        clicked = await click_first([
            'a[href="#modal_nuevo_plantilla_id"]',
            'a[onclick*="plantilla_nueva"]',
            'button[onclick*="plantilla_nueva"]',
            'a:has-text("Nueva plantilla")',
            'button:has-text("Nueva plantilla")',
        ], "Nueva plantilla")

        if not clicked:
            await _page.evaluate("""
                const btn = [...document.querySelectorAll('a, button')].find(e => {
                    const oc = (e.getAttribute('onclick') || '').toLowerCase();
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('plantilla_nueva') || t.includes('nueva plantilla');
                });
                if (btn) btn.click();
            """)
            print("[PW] [PROD] Nueva plantilla via JS ✓")

        await asyncio.sleep(2.0)
        await snap()  # modal nueva plantilla

        # Nombre de la plantilla
        for sel in ['input[name="nombre"]', 'input[id="nombre"]', 'input[placeholder*="ombre"]', 'input[type="text"]']:
            try:
                el = _page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await el.fill("")
                    await el.type("Milanesas", delay=120)
                    print(f"[PW] [PROD] Nombre via '{sel}' ✓")
                    break
            except Exception:
                continue

        await asyncio.sleep(0.5)
        await snap()  # nombre completado

        clicked = await click_first([
            '#boton_agregar_plantilla',
            'button:has-text("Agregar")',
            '.modal-footer .btn-primary',
            'button[type="submit"]',
        ], "Agregar plantilla")

        if not clicked:
            await _page.evaluate("""
                const btn = document.getElementById('boton_agregar_plantilla')
                    || [...document.querySelectorAll('button')].find(
                        e => e.textContent.trim().toLowerCase() === 'agregar'
                    );
                if (btn) btn.click();
            """)
            print("[PW] [PROD] Agregar plantilla via JS ✓")

        await asyncio.sleep(4.0)  # esperar DataTable refresh + Bootstrap tooltip init
        await snap()  # plantilla en la lista

        # ── PASO 3: Ver detalles ──────────────────────────────────────────────────
        await decir_frase(
            "Ahí ya tenemos la plantilla guardada. "
            "Ahora nos falta detallar qué usamos para hacer ese producto. "
            "Apretamos el botón Ver detalles a la derecha."
        )

        # Debug: loguear todos los botones visibles en tbody
        btns_debug = await _page.evaluate("""() => {
            return [...document.querySelectorAll('tbody a, tbody button')]
                .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                .map(el => ({
                    tag:   el.tagName,
                    text:  (el.textContent || '').trim().slice(0, 30),
                    title: el.getAttribute('title') || '',
                    doa:   el.getAttribute('data-original-title') || '',
                    href:  el.getAttribute('href') || '',
                    oc:    (el.getAttribute('onclick') || '').slice(0, 60),
                }));
        }""")
        print(f"[PW] [PROD] Botones en DataTable ({len(btns_debug)}):")
        for b in btns_debug:
            print(f"  {b}")

        # Buscar en la fila que contiene "Milanesas" y hacer clic en el botón de detalles
        ver_result = await _page.evaluate("""() => {
            const rows = [...document.querySelectorAll('tbody tr')];
            for (const row of rows) {
                if (!row.textContent.toLowerCase().includes('milanesa')) continue;
                const btns = [...row.querySelectorAll('a, button, [onclick]')];
                for (const btn of btns) {
                    const title = (btn.getAttribute('title') || btn.getAttribute('data-original-title') || '').toLowerCase();
                    const href  = (btn.getAttribute('href') || '').toLowerCase();
                    const oc    = (btn.getAttribute('onclick') || '').toLowerCase();
                    if (title.includes('detalle') || href.includes('detalle') || oc.includes('detalle')) {
                        btn.click();
                        return 'by-detalle: ' + btn.outerHTML.slice(0, 120);
                    }
                }
                // Fallback: segundo botón de la fila (generalmente Ver detalles)
                if (btns.length >= 2) { btns[1].click(); return 'btn[1]: ' + btns[1].outerHTML.slice(0, 120); }
                if (btns.length >= 1) { btns[0].click(); return 'btn[0]: ' + btns[0].outerHTML.slice(0, 120); }
            }
            // Último recurso: cualquier botón visible con "detalle" en atributos
            const any = [...document.querySelectorAll('[title*="etalle"],[data-original-title*="etalle"],[href*="etalle"],[onclick*="etalle"]')];
            if (any.length) { any[0].click(); return 'any-detalle: ' + any[0].outerHTML.slice(0, 120); }
            return null;
        }""")
        print(f"[PW] [PROD] Ver detalles: {ver_result}")

        # Esperar navegación a la página de detalles
        try:
            await _page.wait_for_url("**detalle**", timeout=8000)
            print("[PW] [PROD] Navegación a detalles ✓")
        except Exception:
            await asyncio.sleep(3.0)

        await snap()  # página de detalles

        # ── PASO 4: Agregar Pechuga — Entrada ────────────────────────────────────
        await decir_frase(
            "Acá vamos a agregar lo que usamos para producir. "
            "El tipo lo dejamos en Entrada — que es la materia prima. "
            "Buscamos Pechuga, ponemos 1 de cantidad, y agregamos."
        )

        try:
            await _page.locator('#tipo').first.select_option(value="1")
            print("[PW] [PROD] Tipo = Entrada ✓")
        except Exception as e:
            print(f"[PW] [PROD] No se pudo setear tipo Entrada: {e}")

        await ingresar_producto_detalle("Pechuga", "1")
        await asyncio.sleep(0.5)
        await snap()

        clicked = await click_first([
            '#boton_agregar_producto',
            'button:has-text("Agregar")',
        ], "Agregar Pechuga")

        if not clicked:
            await _page.evaluate("""
                const btn = document.getElementById('boton_agregar_producto')
                    || [...document.querySelectorAll('button, a')].find(e =>
                        e.textContent.trim().toLowerCase() === 'agregar'
                    );
                if (btn) btn.click();
            """)
            print("[PW] [PROD] Agregar Pechuga via JS ✓")

        await asyncio.sleep(2.0)
        await snap()

        # ── PASO 5: Agregar Huevos — Entrada ─────────────────────────────────────
        await decir_frase("Ahora hacemos lo mismo con los Huevos: buscamos Huevos y ponemos 4.")

        await ingresar_producto_detalle("Huevos", "4")
        await asyncio.sleep(0.5)
        await snap()

        clicked = await click_first([
            '#boton_agregar_producto',
            'button:has-text("Agregar")',
        ], "Agregar Huevos")

        if not clicked:
            await _page.evaluate("""
                const btn = document.getElementById('boton_agregar_producto')
                    || [...document.querySelectorAll('button, a')].find(e =>
                        e.textContent.trim().toLowerCase() === 'agregar'
                    );
                if (btn) btn.click();
            """)
            print("[PW] [PROD] Agregar Huevos via JS ✓")

        await asyncio.sleep(2.0)
        await snap()

        # ── PASO 6: Salida — Milanesas ────────────────────────────────────────────
        await decir_frase(
            "Una vez que los agregamos, cambiamos el tipo a Salida — que es lo que vamos a elaborar. "
            "En Producto ponemos Milanesas y la cantidad que obtenemos con esa materia prima, "
            "en este caso 1, y apretamos Agregar."
        )

        try:
            await _page.locator('#tipo').first.select_option(value="2")
            print("[PW] [PROD] Tipo = Salida ✓")
        except Exception as e:
            print(f"[PW] [PROD] No se pudo setear tipo Salida: {e}")

        await ingresar_producto_detalle("Milanesas", "1")
        await asyncio.sleep(0.5)
        await snap()

        clicked = await click_first([
            '#boton_agregar_producto',
            'button:has-text("Agregar")',
        ], "Agregar Milanesas (salida)")

        if not clicked:
            await _page.evaluate("""
                const btn = document.getElementById('boton_agregar_producto')
                    || [...document.querySelectorAll('button, a')].find(e =>
                        e.textContent.trim().toLowerCase() === 'agregar'
                    );
                if (btn) btn.click();
            """)
            print("[PW] [PROD] Agregar Milanesas via JS ✓")

        await asyncio.sleep(2.0)
        await snap()  # plantilla completa con ingredientes

        # ── PASO 7: Sección Produccion ────────────────────────────────────────────
        await decir_frase(
            "Ahí ya tenemos cargada la plantilla. "
            "Entonces ya podemos pasar a la sección Producción."
        )

        await nav("/produccion.php")
        print("[PW] [PROD] Navegando a produccion.php...")
        await _page.goto(f"{base}/produccion.php", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2.0)
        await snap()

        # ── PASO 8: Nueva Produccion ──────────────────────────────────────────────
        await decir_frase(
            "Acá es donde vamos a usar la plantilla que creamos recién. "
            "Apretamos Nueva Producción."
        )

        clicked = await click_first([
            'a[onclick*="nueva_produccion"]',
            'button[onclick*="nueva_produccion"]',
            'a:has-text("Nueva producción")',
            'button:has-text("Nueva producción")',
        ], "Nueva producción")

        if not clicked:
            await _page.evaluate("""
                const btn = [...document.querySelectorAll('a, button, [onclick]')].find(e => {
                    const oc = (e.getAttribute('onclick') || '').toLowerCase();
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nueva_produccion') || t.includes('nueva producción') || t.includes('nueva produccion');
                });
                if (btn) btn.click();
            """)
            print("[PW] [PROD] Nueva producción via JS ✓")

        await asyncio.sleep(2.5)
        await snap()

        await decir_frase(
            "Elegimos la plantilla Milanesas que creamos recién, "
            "ponemos la cantidad — en este caso 1 — "
            "y seleccionamos Salida de producción."
        )

        # Seleccionar plantilla "Milanesas"
        try:
            plantilla_sel = _page.locator('#plantilla').first
            if await plantilla_sel.count() > 0:
                await plantilla_sel.select_option(label="Milanesas")
                print("[PW] [PROD] Plantilla Milanesas ✓")
        except Exception as e:
            print(f"[PW] [PROD] Error seleccionando plantilla: {e}")
            await _page.evaluate("""
                const sel = document.getElementById('plantilla');
                if (sel) {
                    for (const opt of sel.options) {
                        if (opt.text.toLowerCase().includes('milanesa')) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            break;
                        }
                    }
                }
            """)

        # Cantidad 1
        try:
            cant_el = _page.locator('#cantidad').first
            if await cant_el.count() > 0 and await cant_el.is_visible():
                await cant_el.click()
                await cant_el.fill("1")
                print("[PW] [PROD] Cantidad produccion=1 ✓")
        except Exception as e:
            print(f"[PW] [PROD] Error cantidad produccion: {e}")

        # Tipo: Salida de producción (value=2)
        try:
            tipo_sel = _page.locator('#tipo').first
            if await tipo_sel.count() > 0 and await tipo_sel.is_visible():
                await tipo_sel.select_option(value="2")
                print("[PW] [PROD] Tipo produccion = Salida ✓")
        except Exception as e:
            print(f"[PW] [PROD] Error tipo produccion: {e}")

        await asyncio.sleep(0.5)
        await snap()

        # Click Agregar
        clicked = await click_first([
            'button[onclick*="agregar_produccion"]',
            'a[onclick*="agregar_produccion"]',
            'button:has-text("Agregar")',
            'a:has-text("Agregar")',
        ], "Agregar produccion")

        if not clicked:
            await _page.evaluate("""
                const btn = [...document.querySelectorAll('[onclick], button, a')].find(e => {
                    const oc = (e.getAttribute('onclick') || '').toLowerCase();
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('agregar_produccion') || t === 'agregar';
                });
                if (btn) btn.click();
            """)
            print("[PW] [PROD] Agregar produccion via JS ✓")

        await asyncio.sleep(2.5)
        await snap()

        await decir_frase(
            "Y ahí ya tendríamos la producción terminada. "
            "El sistema descontó automáticamente la materia prima del stock "
            "y sumó el kilo de Milanesas elaborado."
        )

        await asyncio.sleep(1.0)
        await snap()
        await snap_end()
        print("[PW] [PROD] ✓ Demo de Producción completa")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [PROD] Error: {e}")
        traceback.print_exc()
        b64 = await _screenshot_b64()
        if b64 and on_screenshot:
            await on_screenshot(b64)
        return False


async def _demo_modulos_restantes(decir_frase, navigate_fn=None, on_screenshot=None, on_screenshot_end=None, wait_for_input_fn=None) -> None:
    """Recorre los módulos post-caja. Clientes y Proveedores usan Playwright;
    el resto navega el iframe con frases pre-escritas."""

    async def nav(path: str):
        if navigate_fn:
            await navigate_fn(path)

    async def snap_end():
        if on_screenshot_end:
            await on_screenshot_end()

    # ── CLIENTES ───────────────────────────────────────────────────────────────
    await nav("/clientes.php")
    await asyncio.sleep(1.5)
    await decir_frase(
        "Ahora te muestro el módulo de Clientes. "
        "Acá podés guardar los datos de tus clientes para que el nombre se autocomplete en la caja al momento de vender. "
        "Muy práctico para no tipear todo de vuelta y para tener el historial de compras de cada uno."
    )
    await asyncio.sleep(0.5)
    await _demo_clientes_abrir_formulario(on_screenshot=on_screenshot)
    await decir_frase(
        "Y lo bueno es que a cada cliente le podés asignar una lista de precios diferente. "
        "Si tenés un mayorista o alguien a quien le hacés precio especial, "
        "le ponés la lista mayorista, al costo, o la que vos quieras, en lugar del precio de mostrador. "
        "Acá ven la pantalla para dar de alta un nuevo cliente, donde se configuran todos esos datos."
    )
    await snap_end()
    await asyncio.sleep(0.5)

    # ── PROVEEDORES ────────────────────────────────────────────────────────────
    await _demo_proveedores(
        decir_frase=decir_frase,
        on_screenshot=on_screenshot,
        on_screenshot_end=on_screenshot_end,
        navigate_fn=navigate_fn,
    )
    await asyncio.sleep(0.5)

    # ── RESTO (iframe simple) ──────────────────────────────────────────────────

    # Usuarios
    await nav("/configuracion_usuarios.php")
    await asyncio.sleep(1.5)
    await decir_frase(
        "En el módulo de Usuarios pueden crear distintos perfiles de acceso. "
        "Por ejemplo, un perfil de administrador que ve todo el sistema "
        "y uno de cajero que solo accede a la caja. "
        "Cada perfil tiene permisos configurables para controlar exactamente qué puede hacer cada empleado."
    )
    await asyncio.sleep(0.5)

    # Stock > Existencias — con Playwright + screenshots
    await nav("/stock_existencia_2.php")
    await asyncio.sleep(1.0)
    await decir_frase(
        "Ahora vemos Stock, específicamente la sección de Existencias. "
        "Esta pantalla te da el panorama completo de tu inventario. "
        "Podés filtrar por grupo de productos, o apretar el botón Todos para ver todo junto de una."
    )
    await demo_stock_existencias(on_screenshot)
    await decir_frase(
        "La tabla tiene varias columnas. "
        "Producto muestra todos los artículos del local. "
        "Stock es lo que registraste que tenés físicamente. "
        "Ingresos son las compras a proveedores que cargaste en el sistema."
    )
    await decir_frase(
        "Ventas son las operaciones que se hicieron desde caja. "
        "Envío entre sucursales aplica si tenés más de un local y mandás mercadería de una a la otra. "
        "Egresos son salidas de stock sin venta. "
        "Producción es lo que elaborás vos: si comprás una media res y la despostás, acá se refleja lo despostado. "
        "Y la columna Existencia es el cálculo automático del sistema: lo que ingresó menos lo que vendiste, "
        "así siempre sabés cuánto deberías tener en el local."
    )
    await snap_end()
    await asyncio.sleep(0.5)

    # ── PRODUCCIÓN (condicional) ────────────────────────────────────────────────
    if wait_for_input_fn is not None:
        await decir_frase(
            "Una consulta: en el negocio hacen algún tipo de producción propia? "
            "Por ejemplo, si elaboran milanesas, o si hacen desposte de carne."
        )
        respuesta_prod = await wait_for_input_fn(timeout=12.0)
        hace_produccion = any(w in respuesta_prod.lower() for w in [
            "sí", "si", "claro", "dale", "ajá", "aja",
            "hacemos", "hago", "produce", "elabora",
            "milanesa", "desposta", "desposte",
        ])
        if hace_produccion:
            await _demo_produccion(
                decir_frase=decir_frase,
                on_screenshot=on_screenshot,
                on_screenshot_end=on_screenshot_end,
                navigate_fn=navigate_fn,
            )
    await asyncio.sleep(0.5)

    # Estadísticas > Ventas — con Playwright + screenshots
    await nav("/estadisticas_ventas.php")
    await asyncio.sleep(1.0)
    await decir_frase(
        "Ahora pasamos a la sección de Estadísticas. "
        "Acá tienen una visión completa del negocio: ventas, compras, egresos, facturas electrónicas y mucho más. "
        "Para esta demo vamos a ver específicamente la parte de Ventas."
    )
    await decir_frase(
        "En esta sección podemos filtrar por muchos factores: fecha, grupo de producto, clientes, vendedores, y más. "
        "Para este ejemplo filtramos únicamente por fecha — las ventas que se hicieron hoy — "
        "y apretamos el botón Buscar."
    )
    await demo_estadisticas_ventas(on_screenshot)
    await decir_frase(
        "Acá vemos todas las ventas que hicimos en el día: "
        "qué productos en específico vendimos, las cantidades, los importes, y un montón de datos "
        "que ayudan a conocer la rentabilidad de nuestros productos. "
        "Todo esto se puede exportar a Excel con un clic para analizarlo como quieran."
    )
    await snap_end()
    await asyncio.sleep(0.5)

    # Cierres
    await nav("/caja_cierre.php")
    await asyncio.sleep(1.5)
    await decir_frase(
        "En Cierres pueden cerrar la caja por usuario o por turno. "
        "El sistema muestra el efectivo esperado versus lo que hay en la caja, "
        "el faltante o sobrante, y los retiros del día. "
        "Muy útil para el control diario del negocio."
    )
    await asyncio.sleep(0.5)


async def run_demo_mgw(
    decir_frase,
    on_screenshot,
    on_screenshot_end=None,
    navigate_fn=None,
    should_continue=None,
    wait_for_input_fn=None,
) -> bool:
    """
    Demo secuencial completa: Login → Home → Caja (agregar + pago + presupuesto) → Módulos.
    Cada bloque habla primero y actúa después, en orden estricto sin keyword detection.
    """
    global _caja_fase1_done, _caja_fase1_launched, _caja_fase2_done, _caja_fase2_launched

    if _page is None:
        print("[PW] [DEMO] Browser no iniciado")
        return False

    def _ok() -> bool:
        return should_continue is None or should_continue()

    base = MGW_URL.rstrip("/")

    async def snap():
        b64 = await _screenshot_b64()
        if b64 and on_screenshot:
            await on_screenshot(b64)

    async def snap_end():
        if on_screenshot_end:
            await on_screenshot_end()

    async def nav(path: str):
        if navigate_fn:
            await navigate_fn(path)

    try:
        # ── 0. RESET DE SESIÓN ────────────────────────────────────────────────
        print("[PW] [DEMO] Limpiando cookies de sesión previa...")
        await _page.context.clear_cookies()

        # ── 1. LOGIN ──────────────────────────────────────────────────────────
        if not _ok():
            return True
        print("[PW] [DEMO] Navegando al login...")
        await nav("/index.php")
        await _page.goto(f"{base}/index.php", wait_until="networkidle", timeout=20000)
        await _page.wait_for_selector('[name="empresa"]', timeout=10000)

        # Bloquear autofill del browser y vaciar campos
        await _page.evaluate("""() => {
            document.querySelectorAll('input').forEach(inp => {
                inp.setAttribute('autocomplete', 'new-password');
                inp.value = '';
            });
        }""")

        await asyncio.sleep(2.0)
        await snap()  # ① formulario completamente vacío

        await decir_frase(
            "Fijate que el sistema es 100% web, no necesita instalación. "
            "Se accede desde cualquier dispositivo: la compu del local, el celular, o una tablet. "
            "El primer campo es el nombre de la empresa, acá usamos 'prueba' porque es un entorno de demo. "
            "Abajo van el usuario y la contraseña que se le dan al negocio cuando implementan el sistema."
        )

        # Tipear empresa letra por letra (visible para el cliente)
        await _page.locator('[name="empresa"]').click()
        await _page.locator('[name="empresa"]').type(MGW_EMPRESA, delay=150)
        await asyncio.sleep(0.4)
        await snap()  # ② empresa completa

        # Tipear usuario
        await _page.locator('[name="usuario"]').click()
        await _page.locator('[name="usuario"]').type(MGW_USER, delay=150)
        await asyncio.sleep(0.4)
        await snap()  # ③ usuario completo

        # Tipear contraseña
        await _page.locator('[name="contrasena"]').click()
        await _page.locator('[name="contrasena"]').type(MGW_PASSWORD, delay=150)
        await asyncio.sleep(0.5)
        await snap()  # ④ formulario completo antes de ingresar

        await decir_frase("Ahora estamos ingresando en vivo para hacer la demo.")

        await _page.locator(
            '[name="btnlogin"], button[type="submit"], input[type="submit"]'
        ).first.click()
        await _page.wait_for_url("**/home.php", timeout=20000)
        print("[PW] [LOGIN] Sesión establecida ✓")

        # ── 2. HOME ───────────────────────────────────────────────────────────
        if not _ok():
            return True
        await asyncio.sleep(1.5)
        await nav("/home.php")
        await snap()  # ⑤ pantalla de inicio

        await decir_frase(
            "Muy bien, ya estamos adentro. Esta es la pantalla de inicio del sistema. "
            "Acá aparecen las novedades y en el menú de la izquierda están todos los módulos disponibles. "
            "También desde acá los empleados pueden fichar su entrada y salida ingresando su DNI, "
            "sin necesidad de ningún hardware extra."
        )
        await asyncio.sleep(0.5)
        await snap()

        await decir_frase(
            "Y en esta pantalla también aparece un video sobre la nueva balanza todo en uno "
            "que ofrecemos junto con el sistema."
        )

        # TEST_MODE: saltar caja y módulos, ir directo a la demo de Producción
        if TEST_MODE:
            print("[PW] [DEMO] TEST_MODE: saltando a demo de Producción...")
            await _demo_produccion(
                decir_frase=decir_frase,
                on_screenshot=on_screenshot,
                on_screenshot_end=on_screenshot_end,
                navigate_fn=navigate_fn,
            )
            return True

        # ── 3. CAJA — navegación y limpieza de estado ─────────────────────────
        if not _ok():
            return True
        await decir_frase(
            "Ahora pasamos a la sección de Caja para hacer una venta de prueba en vivo."
        )

        await nav("/caja.php")
        await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=30000)
        # Esperar que el JS de caja complete la inicialización y el AJAX de arqueo dispare.
        # En el log se ve que ajax_caja_arqueo_nuevo.php se dispara varios segundos
        # después de domcontentloaded, cuando terminan de cargar todos los assets.
        await asyncio.sleep(8.0)
        # Screenshot para ver el estado real de la página en este punto
        await snap()
        # Detectar y confirmar el form/modal de arqueo (con dump JS para debug)
        await _manejar_arqueo(on_screenshot=on_screenshot)
        # Si el arqueo confirmó algo, esperar a que la interfaz principal aparezca
        await asyncio.sleep(2.0)
        # Esperar el campo de producto; si no aparece en 20s, loggear e intentar continuar
        try:
            await _page.wait_for_selector(
                'input#producto, input[name="producto"], input.ui-autocomplete-input',
                timeout=20000,
            )
            print("[PW] [CAJA] Página lista ✓")
        except Exception as e_sel:
            print(f"[PW] [CAJA] Campo producto no encontrado: {e_sel}")
            await snap()  # screenshot adicional para ver qué está en pantalla
            raise  # propagar para que el except externo lo loggee completo

        # Limpiar ítems del ticket que hayan quedado de sesiones anteriores
        await _reset_caja_items()

        await asyncio.sleep(1.5)
        await snap()  # ⑥ caja con ticket vacío

        # ── 4. CAJA — buscar y agregar Huevos ────────────────────────────────
        if not _ok():
            return True
        await decir_frase(
            "Acá está el ticket de caja vacío. "
            "Para registrar una venta busco el producto en el campo de arriba. "
            "Escribo 'Huevos' y selecciono la sugerencia. "
            "El número 10 que aparece es el código interno del producto en el sistema. "
            "Indico la cantidad y aprieto Agregar."
        )

        campo = _page.locator('input#producto, input[name="producto"]').first
        await campo.click()
        await campo.fill("")
        await campo.type("Huevos", delay=120)

        await _page.wait_for_selector(
            '.ui-autocomplete .ui-menu-item', state="visible", timeout=8000
        )
        await _page.locator('.ui-autocomplete .ui-menu-item').first.click()
        await asyncio.sleep(0.4)
        await snap()  # ⑦ producto seleccionado en el campo

        agregar_btn = None
        for selector in [
            'button:has-text("Agregar")', '#btnAgregar', 'button#Agregar',
            'input[value="Agregar"]', 'button.btn-success',
        ]:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    agregar_btn = el
                    print(f"[PW] [CAJA] Botón Agregar: '{selector}'")
                    break
            except Exception:
                continue

        if agregar_btn:
            await agregar_btn.click()
        else:
            await _page.evaluate("""
                const all = [...document.querySelectorAll('button, input[type="button"], a')];
                const btn = all.find(
                    e => (e.textContent || e.value || '').trim().toLowerCase() === 'agregar'
                );
                if (btn) btn.click();
            """)
            print("[PW] [CAJA] Clic Agregar via JS fallback")
        # Pausa fija para que el ticket refleje el producto en pantalla
        await asyncio.sleep(2.5)
        await snap()  # ⑧ Huevos en el ticket
        print("[PW] [CAJA] Producto agregado al ticket ✓")
        _caja_fase1_done = True
        _caja_fase1_launched = True
        print("[PW] [CAJA] Fase 1 completada ✓")

        # ── 5. MÉTODOS DE PAGO ────────────────────────────────────────────────
        if not _ok():
            return True
        await decir_frase(
            "El sistema tiene varios métodos de pago disponibles: "
            "efectivo, Mercado Pago, Cuenta DNI, y tarjeta con recargo automático. "
            "En efectivo solo indicás con cuánto paga el cliente y el sistema calcula el vuelto solo."
        )

        # Seleccionar Efectivo — intentos en orden de prioridad
        seleccionado = False
        for selector in [
            'button:has-text("Efectivo")', 'a:has-text("Efectivo")',
            '[onclick*="forma_pago"][onclick*="1"]', '[data-forma="1"]',
            'td:has-text("Efectivo")',
        ]:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    seleccionado = True
                    print(f"[PW] [CAJA] Efectivo via '{selector}' ✓")
                    break
            except Exception:
                continue

        if not seleccionado:
            for sel_selector in [
                'select#forma_de_pago', 'select[name="forma_de_pago"]',
                'select:has(option:has-text("Efectivo"))',
            ]:
                try:
                    el = _page.locator(sel_selector).first
                    if await el.count() > 0:
                        await el.select_option(label="Efectivo")
                        seleccionado = True
                        print("[PW] [CAJA] Efectivo via select_option ✓")
                        break
                except Exception:
                    continue

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
            print("[PW] [CAJA] Efectivo forzado via JS ✓")

        await asyncio.sleep(2.0)

        # Tipear monto en efectivo para que el sistema muestre el vuelto
        print("[PW] [CAJA] Ingresando monto en efectivo...")
        for sel in [
            'input[placeholder*="Paga con"]',
            'input[placeholder*="paga con"]',
            'input[name="efectivo"]', 'input[name="monto_efectivo"]',
            'input[name="recibe"]', 'input[name="monto"]',
        ]:
            try:
                el = _page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await el.fill("")
                    await el.type("2000", delay=150)
                    print(f"[PW] [CAJA] Monto efectivo via '{sel}' ✓")
                    # Disparar evento para que calcule el vuelto
                    await el.press("Tab")
                    break
            except Exception:
                continue

        await asyncio.sleep(2.0)
        await snap()  # ⑨ panel de pago con vuelto calculado

        # ── 6. BOTONES DE CIERRE + PRESUPUESTO ───────────────────────────────
        if not _ok():
            return True
        await decir_frase(
            "Para cerrar la venta hay dos opciones. "
            "El botón 'Presupuestar F8', en negro, es el más usado cuando no necesitan factura electrónica. "
            "Y el 'FCE F4' que se conecta automáticamente a ARCA para emitir factura electrónica. "
            "Ahora presupuesto la venta en vivo para que lo vean."
        )

        # Clic en Presupuestar (botón negro F8)
        cerrado = False
        for selector in [
            'button:has-text("Presupuestar")', 'a:has-text("Presupuestar")',
            '[onclick*="factura=3"]', '[onclick*="presupuesto"]', 'button:has-text("F8")',
        ]:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    cerrado = True
                    print(f"[PW] [CAJA] Presupuesto via '{selector}' ✓")
                    break
            except Exception:
                continue

        if not cerrado:
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
            print("[PW] [CAJA] Presupuesto via JS ✓")

        await asyncio.sleep(3.0)
        await snap()   # ⑩ confirmación de venta
        await asyncio.sleep(3.0)
        await snap()   # ⑪ ticket/historial actualizado

        _caja_fase2_done = True
        _caja_fase2_launched = True
        await snap_end()
        print("[PW] [CAJA] Demo de caja completa ✓")

        # ── 7. MÓDULOS RESTANTES ──────────────────────────────────────────────
        if _ok():
            await _demo_modulos_restantes(
                decir_frase, navigate_fn,
                on_screenshot=on_screenshot,
                on_screenshot_end=on_screenshot_end,
                wait_for_input_fn=wait_for_input_fn,
            )

        return True

    except Exception as e:
        import traceback
        print(f"[PW] [DEMO] Error en run_demo_mgw: {e}")
        traceback.print_exc()
        try:
            b64 = await _screenshot_b64()
            if b64 and on_screenshot:
                await on_screenshot(b64)
        except Exception:
            pass
        return False