"""
mgw_playwright.py
Ejecuta la demo de caja con Playwright — browser real con JS.
Dividida en fases para sincronizar con lo que dice Malena.
"""
import asyncio
import base64
import contextvars
from playwright.async_api import async_playwright, Page

from config import MGW_URL, MGW_USER, MGW_EMPRESA, MGW_PASSWORD, TEST_MODE, CONFIG_MODULE_PATHS

# Producto demo — ID numérico del array JS de caja.php
DEMO_PRODUCTO_NOMBRE = "Huevos"
DEMO_PRODUCTO_ID     = 10
DEMO_CANTIDAD        = 1

# ── Estado por sesión (multi-tenant) ─────────────────────────────────────────
# Antes esto era un único browser/page global. Ahora cada sesión (cada llamada) tiene
# su propio holder de estado, guardado en un ContextVar task-local. El holder es un
# dict MUTABLE: los tasks hijos de una sesión heredan la MISMA referencia al crearse,
# así que mutar page/browser/flags desde cualquier task de la sesión es visible en toda
# la sesión, pero queda aislado entre sesiones distintas.
#
# Este es el ÚNICO módulo que usa ContextVar — en el resto del proyecto se pasa la
# BotSession explícitamente.

def _new_pw_state() -> dict:
    return {
        "pw": None, "browser": None, "page": None,
        "fase1_done": False, "fase1_launched": False,
        "fase2_done": False, "fase2_launched": False,
        # Credenciales de la sesión (por defecto las legacy de config)
        "empresa": MGW_EMPRESA, "usuario": MGW_USER, "password": MGW_PASSWORD,
    }

_pw_state: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "mgw_pw_state", default=_new_pw_state()
)


def init_pw_state(empresa: str | None = None, usuario: str | None = None,
                  password: str | None = None) -> dict:
    """
    Instala un holder de estado NUEVO para la sesión actual. Debe llamarse en el task
    raíz de la sesión (antes de crear tasks hijos) para que todos hereden el mismo holder.
    """
    st = _new_pw_state()
    if empresa:  st["empresa"]  = empresa
    if usuario:  st["usuario"]  = usuario
    if password: st["password"] = password
    _pw_state.set(st)
    return st


def _st() -> dict:
    return _pw_state.get()


def _current_page():
    return _st()["page"]


def caja_fase_flags() -> tuple[bool, bool, bool, bool]:
    """(fase1_done, fase1_launched, fase2_done, fase2_launched) de la sesión actual."""
    st = _st()
    return st["fase1_done"], st["fase1_launched"], st["fase2_done"], st["fase2_launched"]


class _PageProxy:
    """
    Delega toda operación (`_page.locator(...)`, `await _page.screenshot()`, etc.) a la
    Page de la sesión actual. Así las ~400 referencias a `_page.metodo()` siguen igual sin
    tener que tocarlas: sólo cambian los chequeos `_current_page() is None` (→ `_current_page() is None`)
    y las asignaciones (→ `_st()["page"] = ...`).
    """
    def __getattr__(self, name):
        pg = _st()["page"]
        if pg is None:
            raise RuntimeError("[PW] page no inicializada en esta sesión")
        return getattr(pg, name)

    def __bool__(self):
        return _st()["page"] is not None


_page = _PageProxy()


async def _screenshot_b64() -> str:
    if _current_page() is None:
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
    """Resetea el estado de fases al iniciar una nueva demo (de la sesión actual)."""
    st = _st()
    st["fase1_done"]     = False
    st["fase1_launched"] = False
    st["fase2_done"]     = False
    st["fase2_launched"] = False


async def pw_start():
    st = _st()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = await browser.new_context(viewport={"width": 1280, "height": 720})
    st["pw"]      = pw
    st["browser"] = browser
    st["page"]    = await context.new_page()
    print("[PW] Browser iniciado ✓")


async def _pw_stop_holder(st: dict):
    """Cierra el browser de un holder de estado específico (no depende del ContextVar)."""
    try:
        if st.get("browser"):
            await st["browser"].close()
        if st.get("pw"):
            await st["pw"].stop()
    finally:
        st["browser"] = None
        st["page"]    = None
        st["pw"]      = None
    print("[PW] Browser cerrado ✓")


async def pw_stop():
    """Cierra el browser de la sesión actual (usa el ContextVar de este task)."""
    await _pw_stop_holder(_st())


async def pw_stop_state(st: dict):
    """
    Cierra el browser de una sesión dada por su holder. Lo usa el teardown, que corre
    en un task distinto al de la sesión y por lo tanto NO tiene el ContextVar seteado.
    """
    await _pw_stop_holder(st)


async def pw_login() -> bool:
    if _current_page() is None:
        return False
    try:
        await _page.goto(f"{MGW_URL.rstrip('/')}/index.php",
                         wait_until="domcontentloaded", timeout=20000)
        await _page.wait_for_selector('[name="empresa"]', timeout=10000)

        await _page.locator('[name="empresa"]').fill(_st()["empresa"])
        await asyncio.sleep(0.3)
        await _page.locator('[name="usuario"]').fill(_st()["usuario"])
        await asyncio.sleep(0.3)
        await _page.locator('[name="contrasena"]').fill(_st()["password"])
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
    if _current_page() is None:
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
        await _page.locator('[name="empresa"]').type(_st()["empresa"], delay=150)
        await asyncio.sleep(0.4)
        await snap()  # ② empresa completa

        # Usuario
        await _page.locator('[name="usuario"]').click()
        await _page.locator('[name="usuario"]').type(_st()["usuario"], delay=150)
        await asyncio.sleep(0.4)
        await snap()  # ③ usuario completo

        # Contraseña
        await _page.locator('[name="contrasena"]').click()
        await _page.locator('[name="contrasena"]').type(_st()["password"], delay=150)

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
    if _st()["fase1_done"] or _st()["fase1_launched"]:
        print("[PW] Fase 1 ya en curso o completada, saltando")
        return True
    _st()["fase1_launched"] = True
    if _current_page() is None:
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

        _st()["fase1_done"] = True
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
    if _st()["fase2_done"] or _st()["fase2_launched"]:
        print("[PW] Fase 2 ya en curso o completada, saltando")
        return True
    _st()["fase2_launched"] = True  # bloquear re-entrada antes de cualquier await
    if _current_page() is None:
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

        _st()["fase2_done"] = True
        print("[PW] [Fase 2] ✓ Venta cerrada con Presupuesto")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [Fase 2] Error: {e}")
        traceback.print_exc()
        await snap(0.5)
        return False


# ── Caja paso a paso: tools atómicas para la Realtime API ────────────────────
# Cada función espera a que su acción termine antes de retornar, así el modelo
# puede narrar exactamente lo que está pasando en pantalla.

async def caja_step_buscar(product_name: str, on_screenshot=None) -> str:
    """Navega a caja, tipea el producto y selecciona la sugerencia del autocomplete."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    print(f"[PW] [Caja] Navegando a caja...")
    await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(1.5)
    # Si la caja está CERRADA, caja.php muestra el formulario de apertura/arqueo
    # (#importe_arqueo_nuevo) en vez del buscador de productos (#producto), y el
    # wait_for_selector de abajo haría timeout. Esto pasa cuando se entra directo a la
    # venta (modo sección directa) sin haber abierto la caja antes. La abrimos sola,
    # en silencio, reutilizando la lógica de apertura ya probada.
    arqueo = _page.locator("#importe_arqueo_nuevo").first
    if await arqueo.count() > 0 and await arqueo.is_visible():
        print("[PW] [Caja] Caja cerrada — abriéndola automáticamente antes de buscar...")
        await caja_ir_a_apertura()   # resuelve estados previos y deja el form de apertura
        await caja_abrir_turno()     # confirma la apertura con el fondo inicial
        await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.5)
    await _page.wait_for_selector('input#producto, input[name="producto"]', timeout=15000)
    await asyncio.sleep(3.0)
    if on_screenshot:
        await _snap(on_screenshot, 1.5)
    campo = _page.locator('input#producto, input[name="producto"]').first
    await campo.click()
    await campo.fill("")
    await campo.type(product_name, delay=120)
    print(f"[PW] [Caja] Buscando '{product_name}'...")
    await _page.wait_for_selector('.ui-autocomplete .ui-menu-item', state="visible", timeout=8000)
    await _page.locator('.ui-autocomplete .ui-menu-item').first.click()
    await asyncio.sleep(0.4)
    if on_screenshot:
        await _snap(on_screenshot, 1.0)
    print(f"[PW] [Caja] '{product_name}' encontrado ✓")
    return f"'{product_name}' encontrado y seleccionado en pantalla."


async def caja_step_agregar(on_screenshot=None) -> str:
    """Hace clic en el botón Agregar y espera que el producto aparezca en el ticket."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    agregar_btn = None
    for selector in [
        'button:has-text("Agregar")', '#btnAgregar',
        'button#Agregar', 'input[value="Agregar"]', 'button.btn-success',
    ]:
        try:
            el = _page.locator(selector).first
            if await el.count() > 0 and await el.is_visible():
                agregar_btn = el
                break
        except Exception:
            continue
    if agregar_btn:
        await agregar_btn.click()
    else:
        await _page.evaluate("""
            const all = [...document.querySelectorAll('button, input[type="button"], a')];
            const btn = all.find(e => (e.textContent || e.value || '').trim().toLowerCase() === 'agregar');
            if (btn) btn.click();
        """)
    await asyncio.sleep(2.5)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [Caja] Producto agregado al ticket ✓")
    return "Producto agregado al ticket. El total actualizado se ve en pantalla."


async def _caja_llenar_paga_con(monto: str = "5000") -> bool:
    """Llena el campo 'Paga con' (efectivo) para que el sistema calcule el vuelto.

    IMPORTANTE: debe llamarse DESPUÉS de aplicar el descuento. Si se llena antes, al
    seleccionar el descuento el total se recalcula y el campo 'Paga con' se reinicia,
    perdiendo el monto ingresado.
    """
    if _current_page() is None:
        return False
    print(f"[PW] [Caja] Llenando campo 'Paga con' con {monto}...")
    for sel in [
        'input[placeholder*="Paga con"]', 'input[placeholder*="paga con"]',
        'input#efectivo', 'input[name="efectivo"]',
        'input[name="monto_efectivo"]', 'input[name="recibe"]',
        'input[name="monto"]', 'input#pago', 'input[name="pago"]',
    ]:
        try:
            pago_el = _page.locator(sel).first
            if await pago_el.count() > 0 and await pago_el.is_visible():
                await pago_el.click()
                # NO limpiar el campo con fill(""): mandar un evento input con valor vacío
                # hace que el sistema recargue/resetee el panel de cobro y se pierda el
                # descuento ya aplicado. El campo ya viene vacío (no se llena en
                # seleccionar_pago), así que tipeamos directo sobre él.
                #
                # El vuelto lo calcula el JS del sitio en CADA tecla (keyup), no cuando se
                # setea el valor de golpe. Por eso 'fill("5000")' + eventos sintéticos no
                # alcanzaba: el cálculo por dígito nunca corría y el vuelto no aparecía.
                # Tipeamos dígito por dígito con delay para disparar el cálculo real, igual
                # que cuando se escribe a mano (5 → 50 → 500 → 5000 y ahí aparece el vuelto).
                try:
                    await pago_el.press_sequentially(monto, delay=150)
                except AttributeError:
                    # Fallback para versiones viejas de Playwright
                    await pago_el.type(monto, delay=150)
                print(f"[PW] [Caja] 'Paga con'={monto} tipeado dígito a dígito via '{sel}' ✓")
                return True
        except Exception:
            continue
    print("[PW] [Caja] Selector 'Paga con' no encontrado — continuando sin llenar")
    return False


async def caja_step_seleccionar_pago(method: str, on_screenshot=None) -> str:
    """Selecciona la forma de pago en la pantalla de caja."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    method_text = {
        "efectivo": "Efectivo", "mercado_pago": "Mercado Pago",
        "cuenta_dni": "Cuenta DNI", "tarjeta": "Tarjeta",
    }.get(method.lower(), "Efectivo")
    seleccionado = False
    for selector in [
        f'button:has-text("{method_text}")', f'a:has-text("{method_text}")',
        f'td:has-text("{method_text}")', '[onclick*="forma_pago"][onclick*="1"]',
    ]:
        try:
            el = _page.locator(selector).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                seleccionado = True
                print(f"[PW] [Caja] {method_text} seleccionado ✓")
                break
        except Exception:
            continue
    if not seleccionado:
        for sel_sel in ['select#forma_de_pago', 'select[name="forma_de_pago"]']:
            try:
                sel_el = _page.locator(sel_sel).first
                if await sel_el.count() > 0:
                    await sel_el.select_option(label=method_text)
                    seleccionado = True
                    print(f"[PW] [Caja] {method_text} via select_option ✓")
                    break
            except Exception:
                continue
    if not seleccionado:
        await _page.evaluate(f"""
            const selects = document.querySelectorAll('select');
            for (const s of selects) {{
                for (const opt of s.options) {{
                    if (opt.text.toLowerCase().includes('{method.lower()}') &&
                        !opt.text.toLowerCase().includes('%')) {{
                        s.value = opt.value;
                        s.dispatchEvent(new Event('change', {{bubbles: true}}));
                        break;
                    }}
                }}
            }}
        """)
        print(f"[PW] [Caja] {method_text} forzado via JS ✓")
    # NOTA: el monto "Paga con" NO se llena acá. Se llena en caja_step_descuento, DESPUÉS
    # de aplicar el descuento: si se llena antes, al seleccionar el descuento el total se
    # recalcula y el campo se reinicia, perdiendo el monto ingresado.
    await asyncio.sleep(1.5)
    if on_screenshot:
        await _snap(on_screenshot, 1.5)
    return f"Forma de pago '{method_text}' seleccionada. El panel de cobro se ve en pantalla."


async def caja_step_descuento(on_screenshot=None) -> str:
    """Aplica el descuento 'Efectivo 10 (10.00%)' desde el select #descuento_total."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    aplicado = False
    # Camino directo: select_option por value/label en el select de descuentos
    for sel in ['select#descuento_total', 'select[name="descuento_total"]']:
        try:
            sel_el = _page.locator(sel).first
            if await sel_el.count() > 0:
                try:
                    await sel_el.select_option(value="1")
                except Exception:
                    await sel_el.select_option(label="Efectivo 10 (10.00%)")
                aplicado = True
                print("[PW] [Caja] Descuento 'Efectivo 10 (10.00%)' seleccionado ✓")
                break
        except Exception:
            continue
    # Fallback JS: setea el value y dispara change para invocar seleccionar_descuento()
    if not aplicado:
        aplicado = await _page.evaluate("""() => {
            const s = document.querySelector('#descuento_total') ||
                      document.querySelector('select[name="descuento_total"]');
            if (!s) return false;
            let val = '1';
            for (const opt of s.options) {
                if (opt.text.toLowerCase().includes('efectivo 10')) { val = opt.value; break; }
            }
            s.value = val;
            s.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }""")
        print(f"[PW] [Caja] Descuento forzado via JS ✓ ({aplicado})")
    # Esperar a que el JS recalcule el total con el descuento aplicado
    await asyncio.sleep(2.5)
    if on_screenshot:
        await _snap(on_screenshot, 1.0)  # muestra el total ya con el descuento aplicado
    # Recién AHORA, con el descuento ya aplicado, llenamos "Paga con". Así el vuelto se
    # calcula sobre el total con descuento y el campo no se reinicia (que era el bug: al
    # aplicar el descuento después del monto, el campo 'Paga con' se borraba).
    await _caja_llenar_paga_con("5000")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 1.5)  # muestra el vuelto ya calculado sobre el total con descuento
    return ("Descuento 'Efectivo 10 (10.00%)' aplicado y luego ingresado el monto con que paga "
            "el cliente. El vuelto en pantalla ya refleja el total con el 10% de descuento.")


async def caja_step_cerrar(method: str = "presupuesto", on_screenshot=None) -> str:
    """Cierra la venta con Presupuesto F8 o FCE F4."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    if method.lower() in ("presupuesto", "presupuestar", "f8"):
        selectors = [
            'button:has-text("Presupuestar")', 'a:has-text("Presupuestar")',
            '[onclick*="factura=3"]', '[onclick*="presupuesto"]',
        ]
        label = "Presupuesto (F8)"
    else:
        selectors = [
            'button:has-text("FCE")', 'a:has-text("FCE")',
            '[onclick*="factura=1"]', 'button:has-text("F4")',
        ]
        label = "FCE (F4)"
    cerrado = False
    for selector in selectors:
        try:
            el = _page.locator(selector).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                cerrado = True
                print(f"[PW] [Caja] {label} clickeado ✓")
                break
        except Exception:
            continue
    if not cerrado:
        await _page.evaluate("""
            const todos = document.querySelectorAll('[onclick]');
            for (const el of todos) {
                const oc = el.getAttribute('onclick') || '';
                if (oc.includes('factura=3') || oc.includes('presupuest')) { el.click(); break; }
            }
        """)
        print(f"[PW] [Caja] {label} via JS ✓")
    if on_screenshot:
        await _snap(on_screenshot, 3.0)
        await _snap(on_screenshot, 3.0)
    print(f"[PW] [Caja] Venta cerrada con {label} ✓")
    return f"Venta cerrada con {label}. La pantalla muestra el comprobante."


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
    if _current_page() is None:
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
    """
    if _current_page() is None:
        return
    try:
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


async def _ensure_caja_abierta(on_screenshot=None) -> bool:
    """
    Verifica EN SILENCIO si la caja está abierta y, si no lo está, la abre con el
    fondo inicial usando la lógica ya probada (caja_ir_a_apertura + caja_abrir_turno).

    Se usa cuando se entra directo a una sección que necesita la caja abierta (ej:
    balanza en modo sección directa), donde no se pasó antes por la apertura de caja.

    Señal confiable de estado: #boton_cerrar_caja en caja_cierre.php solo aparece si
    la caja está abierta (mismo criterio que caja_ir_a_apertura). No genera screenshots
    para no interrumpir la narración. Devuelve True si tuvo que abrir la caja.
    """
    if _current_page() is None:
        return False
    base = MGW_URL.rstrip("/")
    try:
        print("[PW] [ENSURE-CAJA] Verificando estado de la caja...")
        await _page.goto(f"{base}/caja_cierre.php", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1.0)
        cerrar_btn = _page.locator("#boton_cerrar_caja").first
        if await cerrar_btn.count() > 0 and await cerrar_btn.is_visible():
            print("[PW] [ENSURE-CAJA] Caja ya abierta ✓")
            return False
        print("[PW] [ENSURE-CAJA] Caja cerrada — abriéndola en silencio...")
        await caja_ir_a_apertura()   # resuelve estados previos y deja el form de apertura
        await caja_abrir_turno()     # confirma la apertura con el fondo inicial
        print("[PW] [ENSURE-CAJA] Caja abierta ✓")
        return True
    except Exception as e:
        print(f"[PW] [ENSURE-CAJA] Error verificando/abriendo caja: {e}")
        return False


# ── CLIENTES: abrir formulario de nuevo cliente con Playwright ────────────────

async def _demo_clientes_abrir_formulario(on_screenshot=None) -> bool:
    """
    Navega a clientes.php en el headless browser, hace click en 'Nuevo Cliente'
    y captura el formulario AJAX con estilos completos.
    No navega a ajax_clientes_nuevo_cliente.php directamente (devuelve HTML parcial).
    """
    if _current_page() is None:
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
    Navega a Estadísticas > Ventas, fija el rango desde el 31/05 hasta hoy y aprieta Buscar.
    Hace scroll hasta la tabla de productos vendidos y toma el screenshot del resultado.
    """
    if _current_page() is None:
        return False

    base = MGW_URL.rstrip("/")
    desde = "2026-05-31"

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

        print(f"[PW] [ESTAD] Fijando rango_desde={desde}...")
        await _page.evaluate(f"""() => {{
            const el = document.getElementById('rango_desde');
            if (el) {{
                el.value = '{desde}';
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
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

        print("[PW] [ESTAD] Scroll hasta la tabla de productos...")
        try:
            await _page.locator("#tabla_productos_wrapper").scroll_into_view_if_needed(timeout=5000)
        except Exception:
            await _page.evaluate(
                "document.getElementById('tabla_productos_wrapper')?.scrollIntoView({block: 'center'});"
            )
        await asyncio.sleep(0.5)
        await snap()  # tabla de productos vendidos en el rango

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
    if _current_page() is None:
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
    if _current_page() is None:
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
    if _current_page() is None:
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


async def _demo_balanza(
    decir_frase,
    on_screenshot=None,
    on_screenshot_end=None,
    navigate_fn=None,
) -> bool:
    """
    Demo de Balanza:
    balanza.php → busca Vacío → ingreso manual 1kg → asigna a Balta → repite para Malena
    → muestra Tickets → finaliza venta de Balta → ticket pendiente en Tickets
    → caja.php → CF → lupa → botón verde → Presupuesto F8.
    """
    if _current_page() is None:
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
                    print(f"[PW] [BALANZA] {label} via '{selector}' ✓")
                    return True
            except Exception:
                continue
        return False

    async def buscar_vacio():
        for sel in [
            'input[name="producto"]', '#producto', 'input.ui-autocomplete-input',
            'input[placeholder*="roducto"]', 'input[placeholder*="uscar"]',
        ]:
            try:
                el = _page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await el.fill("")
                    await el.type("Vacío", delay=120)
                    print(f"[PW] [BALANZA] 'Vacío' via '{sel}' ✓")
                    break
            except Exception:
                continue
        await asyncio.sleep(1.5)
        try:
            await _page.wait_for_selector('.ui-autocomplete .ui-menu-item', state="visible", timeout=4000)
            await _page.locator('.ui-autocomplete .ui-menu-item').first.click()
            print("[PW] [BALANZA] Autocomplete Vacío ✓")
        except Exception:
            print("[PW] [BALANZA] Sin autocomplete para Vacío")
        await asyncio.sleep(0.5)

    async def click_ingreso_manual(label: str = "Ingreso manual"):
        clicked = await click_first([
            '#boton_ingreso_manual_producto',
            '[onclick*="entrarModoIngresoManual"]',
            'button:has-text("Ingreso manual")',
            'span:has-text("Ingreso manual")',
        ], label)
        if not clicked:
            await _page.evaluate("""
                const btn = document.getElementById('boton_ingreso_manual_producto')
                    || [...document.querySelectorAll('[onclick]')].find(e =>
                        (e.getAttribute('onclick') || '').includes('entrarModoIngresoManual')
                    );
                if (btn) btn.click();
            """)
            print(f"[PW] [BALANZA] {label} via JS ✓")
        await asyncio.sleep(0.8)

    async def click_tecla_1(label: str = "Tecla 1"):
        clicked = await click_first([
            """[onclick*="agregar_numero_plu('1')"]""",
            '.tecla_plu:has-text("1")',
        ], label)
        if not clicked:
            await _page.evaluate("""
                const btn = [...document.querySelectorAll('[onclick]')].find(e =>
                    (e.getAttribute('onclick') || "").includes("agregar_numero_plu('1')")
                );
                if (btn) btn.click();
            """)
            print(f"[PW] [BALANZA] {label} via JS ✓")
        await asyncio.sleep(0.5)

    try:
        # ── PASO 1: Navegar a balanza ─────────────────────────────────────────────
        await decir_frase(
            "Ahora te muestro la sección de Balanza. "
            "Esta sección se conecta automáticamente a la balanza de Mi Gestión Web, "
            "así podemos ver en vivo lo que se va pesando y lo que muestra la cámara integrada. "
            "Lo primero que tenemos que tener configurado son las balanzas y los operarios, "
            "que son los que ven ahí arriba: Balta y Malena."
        )

        await nav("/balanza.php")
        print("[PW] [BALANZA] Navegando a balanza.php...")
        await _page.goto(f"{base}/balanza.php", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2.0)
        await snap()

        # ── PASO 2: Buscar Vacío + ingreso manual 1kg → Balta ────────────────────
        await decir_frase(
            "Vamos a empezar agregando productos. "
            "Buscamos por ejemplo Vacío en el buscador de productos. "
            "Como estamos desde una computadora que no está conectada a una balanza, "
            "apretamos Ingreso manual e ingresamos los kilos de Vacío que se van a llevar, "
            "en este caso 1 kilo, y se lo asignamos al operario Balta."
        )

        await buscar_vacio()
        await snap()
        await click_ingreso_manual()
        await click_tecla_1()
        await snap()

        clicked = await click_first([
            """[onclick*="ver_vendedor('1'"]""",
            '.boton_vendedores:has-text("Balta")',
        ], "Operario Balta")
        if not clicked:
            await _page.evaluate("""
                const btn = [...document.querySelectorAll('[onclick]')].find(e =>
                    (e.getAttribute('onclick') || "").includes("ver_vendedor('1'")
                );
                if (btn) btn.click();
            """)
            print("[PW] [BALANZA] Balta via JS ✓")

        await asyncio.sleep(1.5)
        await snap()

        # ── PASO 3: Mismo flujo para Malena ──────────────────────────────────────
        await decir_frase(
            "El sistema permite trabajar al unísono entre operarios. "
            "Si Balta está con un cliente y Malena está con otro, "
            "cada uno puede cargar sus productos por separado a su usuario. "
            "Vamos a cargar el mismo producto pero a la operaria Malena."
        )

        await buscar_vacio()
        await click_ingreso_manual("Ingreso manual (Malena)")
        await click_tecla_1("Tecla 1 (Malena)")
        await snap()

        clicked = await click_first([
            """[onclick*="ver_vendedor('2'"]""",
            '.boton_vendedores:has-text("Malena")',
        ], "Operaria Malena")
        if not clicked:
            await _page.evaluate("""
                const btn = [...document.querySelectorAll('[onclick]')].find(e =>
                    (e.getAttribute('onclick') || "").includes("ver_vendedor('2'")
                );
                if (btn) btn.click();
            """)
            print("[PW] [BALANZA] Malena via JS ✓")

        await asyncio.sleep(1.5)
        await snap()

        # ── PASO 4: Finalizar venta de Balta ─────────────────────────────────────
        await decir_frase(
            "Una vez cargados los productos, para ver lo que tiene pendiente cada operario "
            "y finalizar la venta, apretamos en el nombre del operario — en este caso Balta. "
            "Desde ahí presionamos Finalizar para imprimir el ticket directamente. "
            "Si el cliente pide factura o tiene lista de precios diferente, "
            "apretamos Consumidor Final y buscamos el cliente. "
            "Para la demo presionamos Finalizar."
        )

        await _page.evaluate("""
            const btn = [...document.querySelectorAll('[onclick]')].find(e =>
                (e.getAttribute('onclick') || "").includes("ver_vendedor('1'")
            );
            if (btn) btn.click();
        """)
        print("[PW] [BALANZA] Ver Balta (antes de finalizar) via JS ✓")

        # Esperar a que el panel de Balta cargue con sus productos pendientes
        await asyncio.sleep(2.0)
        await snap()  # cliente ve el panel de Balta abierto con sus productos

        # Screenshot adicional para que el cliente vea el botón Finalizar claramente
        await asyncio.sleep(1.0)
        await snap()

        clicked = await click_first([
            """[onclick*="finalizar_venta('1')"]""",
            """[onclick*="finalizar_venta(1)"]""",
        ], "Finalizar venta Balta")
        if not clicked:
            await _page.evaluate("""
                const btn = [...document.querySelectorAll('[onclick]')].find(e =>
                    (e.getAttribute('onclick') || '').includes('finalizar_venta')
                );
                if (btn) btn.click();
            """)
            print("[PW] [BALANZA] Finalizar via JS ✓")

        await asyncio.sleep(2.0)
        await snap()  # cliente ve el resultado tras finalizar

        # ── PASO 6: Ticket pendiente → Tickets ───────────────────────────────────
        await decir_frase(
            "El ticket va a quedar como pendiente y va a aparecer en la sección de Tickets "
            "arriba a la derecha. Acá lo podemos ver."
        )

        clicked = await click_first([
            'button[onclick*="tickets()"]',
            '[onclick*="tickets()"]',
            'button:has-text("Tickets")',
        ], "Tickets (pendiente)")
        if not clicked:
            await _page.evaluate("if(typeof tickets === 'function') tickets();")

        await asyncio.sleep(1.5)
        await snap()

        # ── PASO 7: Ir a Caja > Caja ──────────────────────────────────────────────
        await decir_frase(
            "Hecho todo esto y sacado el ticket, el mismo va a aparecer en el sistema "
            "en la parte de Caja, caja."
        )

        await nav("/caja.php")
        print("[PW] [BALANZA] Navegando a caja.php...")

        # Aceptar automáticamente cualquier dialog nativo (alert/confirm) que bloquearía Playwright
        async def _auto_accept_dialog(dialog):
            print(f"[PW] [BALANZA] Dialog detectado: '{dialog.message[:60]}' — aceptando")
            await dialog.accept()
        _page.on("dialog", _auto_accept_dialog)

        await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(6.0)
        _page.remove_listener("dialog", _auto_accept_dialog)
        await _manejar_arqueo(on_screenshot=on_screenshot)
        await asyncio.sleep(2.0)
        await snap()

        # ── PASO 8: CF → lupa → botón verde ──────────────────────────────────────
        # Esperar a que el AJAX de balanza_tickets_pendientes pueble el contador CF
        print("[PW] [BALANZA] Esperando elemento CF en DOM...")
        try:
            await _page.wait_for_selector(
                "[onclick*=\"mostrar_tickets_balanza_pendientes('cf')\"]",
                state="attached", timeout=8000,
            )
            print("[PW] [BALANZA] Elemento CF encontrado en DOM ✓")
        except Exception:
            print("[PW] [BALANZA] wait_for_selector CF timeout — intentando igual")

        await decir_frase(
            "La venta va a aparecer donde dice CF a la derecha de donde agregamos los productos. "
            "Para cerrarla presionamos sobre donde dice CF."
        )

        # Click via JS directo (IIFE) para evitar que el tooltip de Bootstrap bloquee Playwright
        cf_clicked = await _page.evaluate("""(() => {
            const btn = document.querySelector("[onclick*=\\"mostrar_tickets_balanza_pendientes('cf')\\"]")
                     || document.querySelector('[data-tipo="cf"]');
            if (btn) { btn.click(); return true; }
            return false;
        })()""")
        print(f"[PW] [BALANZA] CF click via JS {'✓' if cf_clicked else '— elemento no encontrado'}")

        await asyncio.sleep(3.0)
        await snap()

        await decir_frase(
            "Acá podemos ver el detalle apretando sobre la lupa, "
            "y también podemos cerrar la venta apretando sobre el botón verde."
        )

        lupa_clicked = await _page.evaluate("""(() => {
            const btn = [...document.querySelectorAll('[onclick]')].find(e =>
                (e.getAttribute('onclick') || '').includes('ver_ticket_balanza_pendiente')
            );
            if (btn) { btn.click(); return true; }
            return false;
        })()""")
        print(f"[PW] [BALANZA] Lupa click via JS {'✓' if lupa_clicked else '— elemento no encontrado'}")

        await asyncio.sleep(2.0)
        await snap()

        verde_clicked = await _page.evaluate("""(() => {
            const btn = [...document.querySelectorAll('[onclick]')].find(e =>
                (e.getAttribute('onclick') || '').includes('ingresar_ticket_balanza')
            );
            if (btn) { btn.click(); return true; }
            return false;
        })()""")
        print(f"[PW] [BALANZA] Verde (ingresar ticket) via JS {'✓' if verde_clicked else '— elemento no encontrado'}")

        await asyncio.sleep(3.0)
        await snap()

        # ── PASO 9: Presupuesto F8 ────────────────────────────────────────────────
        await decir_frase(
            "Ese botón nos lleva a la misma sección de cerrar compra que vimos antes en la Caja, "
            "y la cerramos de la misma forma. "
            "Y así ya estaría la venta cerrada: "
            "fue comenzada por el operario de balanza y cerrada por la cajera."
        )

        presup_clicked = await _page.evaluate("""(() => {
            const btn = document.getElementById('boton_caja_finalizar_venta')
                     || [...document.querySelectorAll('[onclick]')].find(e => {
                            const oc = e.getAttribute('onclick') || '';
                            return oc.includes('finalizar_factura') || oc.includes('presupuest') || oc.includes('factura=3');
                        });
            if (btn) { btn.click(); return true; }
            return false;
        })()""")
        print(f"[PW] [BALANZA] Presupuesto via JS {'✓' if presup_clicked else '— elemento no encontrado'}")

        await asyncio.sleep(3.0)
        await snap()
        await asyncio.sleep(2.0)
        await snap()

        await snap_end()
        print("[PW] [BALANZA] ✓ Demo de Balanza completa")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [BALANZA] Error: {e}")
        traceback.print_exc()
        b64 = await _screenshot_b64()
        if b64 and on_screenshot:
            await on_screenshot(b64)
        return False


async def _demo_caja_mayor(
    decir_frase,
    on_screenshot=None,
    on_screenshot_end=None,
    navigate_fn=None,
) -> bool:
    """
    Demo de Caja Mayor (tesorería del negocio):
    caja_administracion_caja.php → overview de movimientos → click Nuevo arqueo → modal.
    """
    if _current_page() is None:
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
                    print(f"[PW] [CAJA MAYOR] {label} via '{selector}' ✓")
                    return True
            except Exception:
                continue
        return False

    try:
        # ── PASO 1: Anunciar, navegar, luego explicar ─────────────────────────
        await decir_frase("Ahora te muestro la Caja Mayor.")

        await nav("/caja_administracion_caja.php")
        print("[PW] [CAJA MAYOR] Navegando a caja_administracion_caja.php...")
        await _page.goto(f"{base}/caja_administracion_caja.php",
                         wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1.5)
        await snap()  # vista general con tabla de movimientos

        await decir_frase(
            "Esta es la tesorería del negocio. "
            "Acá se registran todos los movimientos de dinero: "
            "ingresos en efectivo o por Mercado Pago o transferencia, "
            "retiros de administración, y retiros entre sucursales si tenés más de una. "
            "También desde acá se hacen los arqueos de caja mayor."
        )

        # ── PASO 2: Explicar la tabla de movimientos ──────────────────────────
        await decir_frase(
            "En la tabla de abajo aparecen todos los movimientos que fuimos haciendo: "
            "ingresos, retiros y arqueos, con fecha, importe y tipo de movimiento. "
            "También podés exportar todo a Excel y ver los movimientos que fueron anulados."
        )

        await asyncio.sleep(1.5)
        await snap()  # tabla de movimientos

        # ── PASO 3: Mostrar Nuevo Arqueo ──────────────────────────────────────
        await decir_frase(
            "Una función clave es el Nuevo Arqueo. "
            "Desde acá hacemos el arqueo de caja mayor para controlar cuánto dinero hay. "
            "Apretamos el botón Nuevo Arqueo para abrirlo."
        )

        clicked = await click_first([
            'a[href="#nuevo_arqueo_id"]',
            '[onclick*="caja_administracion_nuevo_arqueo"]',
            'a:has-text("Nuevo arqueo")',
            'button:has-text("Nuevo arqueo")',
            '.btn-danger:has-text("Nuevo arqueo")',
            '.btn-danger:has-text("arqueo")',
        ], "Nuevo arqueo")

        if not clicked:
            await _page.evaluate("""
                const btn = [...document.querySelectorAll('a, button, [onclick]')].find(e => {
                    const oc = (e.getAttribute('onclick') || '').toLowerCase();
                    const t  = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nuevo_arqueo') || t.includes('nuevo arqueo');
                });
                if (btn) btn.click();
            """)
            print("[PW] [CAJA MAYOR] Nuevo arqueo via JS ✓")

        await asyncio.sleep(2.5)
        await snap()  # modal de nuevo arqueo abierto

        await decir_frase(
            "Acá en el arqueo registramos cuánto efectivo hay en caja, "
            "cuánto entramos por Mercado Pago, transferencia u otros medios. "
            "Eso queda registrado en el historial de arqueos de la Caja Mayor para el control diario."
        )

        await asyncio.sleep(1.5)
        await snap()  # modal completo visible

        # Cerrar el modal con Escape para no dejar estado sucio
        try:
            await _page.keyboard.press("Escape")
            print("[PW] [CAJA MAYOR] Modal cerrado con Escape ✓")
        except Exception:
            pass

        await asyncio.sleep(1.0)
        await snap_end()
        print("[PW] [CAJA MAYOR] ✓ Demo de Caja Mayor completa")
        return True

    except Exception as e:
        import traceback
        print(f"[PW] [CAJA MAYOR] Error: {e}")
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

    if _current_page() is None:
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
        await _page.locator('[name="empresa"]').type(_st()["empresa"], delay=150)
        await asyncio.sleep(0.4)
        await snap()  # ② empresa completa

        # Tipear usuario
        await _page.locator('[name="usuario"]').click()
        await _page.locator('[name="usuario"]').type(_st()["usuario"], delay=150)
        await asyncio.sleep(0.4)
        await snap()  # ③ usuario completo

        # Tipear contraseña
        await _page.locator('[name="contrasena"]').click()
        await _page.locator('[name="contrasena"]').type(_st()["password"], delay=150)
        await asyncio.sleep(0.5)
        await snap()  # ④ formulario completo antes de ingresar

        await decir_frase("Ahora estamos ingresando en vivo para hacer la demo.")

        await _page.locator(
            '[name="btnlogin"], button[type="submit"], input[type="submit"]'
        ).first.click()
        await _page.wait_for_url("**/home.php", timeout=20000)
        print("[PW] [LOGIN] Sesión establecida ✓")

        # TEST_MODE: Home + Caja Mayor (omite Caja y Balanza)
        if TEST_MODE:
            print("[PW] [DEMO] TEST_MODE activo — Home + Caja Mayor")
            await asyncio.sleep(1.5)
            await nav("/home.php")
            await snap()
            await decir_frase(
                "Muy bien, ya estamos adentro. Esta es la pantalla de inicio del sistema. "
                "Acá aparecen las novedades y en el menú de la izquierda están todos los módulos disponibles. "
                "También desde acá los empleados pueden fichar su entrada y salida ingresando su DNI, "
                "sin necesidad de ningún hardware extra."
            )
            await asyncio.sleep(0.5)
            await snap()
            await _demo_caja_mayor(
                decir_frase,
                on_screenshot=on_screenshot,
                on_screenshot_end=on_screenshot_end,
                navigate_fn=navigate_fn,
            )
            return True

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
        _st()["fase1_done"] = True
        _st()["fase1_launched"] = True
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

        _st()["fase2_done"] = True
        _st()["fase2_launched"] = True
        await snap_end()
        print("[PW] [CAJA] Demo de caja completa ✓")

        # ── 7. BALANZA ────────────────────────────────────────────────────────
        if _ok():
            await _demo_balanza(
                decir_frase,
                on_screenshot=on_screenshot,
                on_screenshot_end=on_screenshot_end,
                navigate_fn=navigate_fn,
            )

        # ── 8. CAJA MAYOR ─────────────────────────────────────────────────────
        if _ok():
            await _demo_caja_mayor(
                decir_frase,
                on_screenshot=on_screenshot,
                on_screenshot_end=on_screenshot_end,
                navigate_fn=navigate_fn,
            )

        # ── 9. MÓDULOS RESTANTES ──────────────────────────────────────────────
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


# ── Wrappers para Realtime API (sin decir_frase propio) ──────────────────────
# Malena habla ANTES de llamar la tool; estas funciones ejecutan la demo visual
# una vez que el audio terminó de reproducirse.

async def _delay_frase(text: str):
    """Pausa proporcional al texto (100 ms/palabra, mín 1.5 s) para no correr demasiado rápido."""
    await asyncio.sleep(max(1.5, len(text.split()) * 0.10))


async def run_demo_balanza(on_screenshot=None, on_screenshot_end=None) -> str:
    """Demo completa de balanza con Playwright (para pipeline Realtime API)."""
    ok = await _demo_balanza(
        decir_frase=_delay_frase,
        on_screenshot=on_screenshot,
        on_screenshot_end=on_screenshot_end,
    )
    return "Demo de balanza completada — el usuario vio todo el flujo en pantalla." if ok else "La demo de balanza tuvo un error parcial pero el usuario vio screenshots."


async def run_demo_estadisticas(on_screenshot=None) -> str:
    """Filtra estadísticas de ventas del día con Playwright (para Realtime API)."""
    ok = await demo_estadisticas_ventas(on_screenshot=on_screenshot)
    return "Estadísticas de ventas del día filtradas y mostradas en pantalla." if ok else "Error al mostrar estadísticas."


async def run_demo_stock(on_screenshot=None) -> str:
    """Muestra existencias de stock (botón Todos) con Playwright (para Realtime API)."""
    ok = await demo_stock_existencias(on_screenshot=on_screenshot)
    return "Existencias de stock cargadas en pantalla." if ok else "Error al mostrar stock."


async def run_demo_clientes(on_screenshot=None) -> str:
    """Navega a clientes.php y toma screenshot de la lista (sin abrir formulario)."""
    if not _current_page():
        return "Demo de clientes no disponible (Playwright no iniciado)."
    try:
        base = MGW_URL.rstrip("/")
        # Si el login aún está en progreso, esperar a home.php antes de navegar
        if "index.php" in _page.url:
            print("[PW] [CLIENTES] Esperando que el login complete...")
            try:
                await _page.wait_for_url("**/home.php", timeout=30000)
                await asyncio.sleep(1.0)
            except Exception:
                pass
        await _page.goto(f"{base}/clientes.php", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1.2)
        if on_screenshot:
            b64 = await _screenshot_b64()
            if b64:
                await on_screenshot(b64)
        return "Lista de clientes visible en pantalla."
    except Exception as e:
        print(f"[PW] [CLIENTES] Error: {e}")
        return "Error al navegar a clientes."


# ── Steps atómicos — Clientes ─────────────────────────────────────────────────

async def clientes_nuevo_cliente(on_screenshot=None) -> str:
    """Paso: en clientes.php abre el modal 'Nuevo cliente' (clientes_nuevo_cliente())."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
        for selector in selectors:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    print(f"[PW] [CLIENTES] {label} via '{selector}' ✓")
                    return True
            except Exception:
                continue
        return False

    print("[PW] [CLIENTES] Navegando a clientes.php para Nuevo cliente...")
    await _page.goto(f"{base}/clientes.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)

    clicked = await click_first([
        '[onclick*="clientes_nuevo_cliente"]',
        'a[href="#modal_nuevo_cliente_id"]',
        'a:has-text("Nuevo cliente")',
        'button:has-text("Nuevo cliente")',
    ], "Nuevo cliente")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('a, button')].find(e => {
                const oc = (e.getAttribute('onclick') || '').toLowerCase();
                const href = (e.getAttribute('href') || '').toLowerCase();
                return oc.includes('clientes_nuevo_cliente') || href.includes('modal_nuevo_cliente');
            });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(3.0)
    await snap()
    print("[PW] [CLIENTES] clientes_nuevo_cliente ✓")
    return "Modal de nuevo cliente abierto."


async def clientes_importar(on_screenshot=None) -> str:
    """Paso: vuelve a clientes.php y abre el modal de importación por Excel (form_importar())."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
        for selector in selectors:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    print(f"[PW] [CLIENTES] {label} via '{selector}' ✓")
                    return True
            except Exception:
                continue
        return False

    print("[PW] [CLIENTES] Volviendo a clientes.php para Importar...")
    await _page.goto(f"{base}/clientes.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)

    clicked = await click_first([
        '[onclick*="form_importar"]',
        'a:has-text("Importar")',
        'button:has-text("Importar")',
    ], "Importar")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('a, button')].find(e => {
                const oc = (e.getAttribute('onclick') || '').toLowerCase();
                const t = (e.textContent || '').trim().toLowerCase();
                return oc.includes('form_importar') || t.includes('importar');
            });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(3.0)
    await snap()
    print("[PW] [CLIENTES] clientes_importar ✓")
    return "Modal de importación por Excel abierto."


async def clientes_ver_detalle(on_screenshot=None) -> str:
    """Paso: vuelve a clientes.php y abre el detalle/edición de un cliente (clientes_editar.php)."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
        for selector in selectors:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    print(f"[PW] [CLIENTES] {label} via '{selector}' ✓")
                    return True
            except Exception:
                continue
        return False

    print("[PW] [CLIENTES] Volviendo a clientes.php para ver detalle...")
    await _page.goto(f"{base}/clientes.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)

    clicked = await click_first([
        'a[href*="clientes_editar.php"]',
        '[data-original-title="Editar"]',
        'a.btn-teal',
    ], "Detalle cliente")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('a')].find(e => {
                const href = (e.getAttribute('href') || '').toLowerCase();
                const dt = (e.getAttribute('data-original-title') || e.getAttribute('title') || '').toLowerCase();
                return href.includes('clientes_editar.php') || dt.includes('editar');
            });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(3.0)
    await snap()
    print("[PW] [CLIENTES] clientes_ver_detalle ✓")
    return "Detalle del cliente abierto (movimientos, pagos, notas)."


# ── Steps atómicos — Balanza ──────────────────────────────────────────────────

async def balanza_step_navegar(on_screenshot=None) -> str:
    """Navega a balanza.php y toma screenshot inicial."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    # El flujo de balanza cobra el ticket desde la caja (pasos 5-7). Si se entra
    # directo a balanza (modo sección directa) con la caja cerrada, esos pasos no
    # encuentran los botones. Nos aseguramos EN SILENCIO de que la caja esté abierta
    # antes de arrancar; si ya estaba abierta (ej: se pasó por la sección de caja), no
    # cambia nada.
    await _ensure_caja_abierta()
    print("[PW] [BALANZA-STEP] Navegando a balanza.php...")
    await _page.goto(f"{base}/balanza.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    await _snap(on_screenshot, 0.0)
    print("[PW] [BALANZA-STEP] balanza_step_navegar ✓")
    return (
        "Pantalla de balanza cargada. "
        "Arriba aparecen los operarios configurados (Balta y Malena). "
        "Abajo a la izquierda están los productos más vendidos para acceso rápido."
    )


async def balanza_step_agregar_producto(operario_nombre: str, operario_id: str, on_screenshot=None) -> str:
    """Busca 'Vacío', hace ingreso manual de 1 kg y lo asigna al operario indicado."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    # Si el LLM saltó balanza_navegar, asegurarse de estar en balanza.php
    if "balanza" not in (_page.url or ""):
        base = MGW_URL.rstrip("/")
        print("[PW] [BALANZA-STEP] Auto-navegando a balanza.php (página actual no es balanza)...")
        await _page.goto(f"{base}/balanza.php", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2.0)
        await _snap(on_screenshot, 0.0)

    # Buscar Vacío
    for sel in ['input[name="producto"]', '#producto', 'input.ui-autocomplete-input', 'input[placeholder*="roducto"]']:
        try:
            el = _page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill("")
                await el.type("Vacío", delay=120)
                break
        except Exception:
            continue
    await asyncio.sleep(1.5)
    try:
        await _page.wait_for_selector('.ui-autocomplete .ui-menu-item', state="visible", timeout=4000)
        await _page.locator('.ui-autocomplete .ui-menu-item').first.click()
    except Exception:
        pass
    await asyncio.sleep(0.5)

    # Ingreso manual
    clicked = await _page.evaluate("""(() => {
        const btn = document.getElementById('boton_ingreso_manual_producto')
                 || [...document.querySelectorAll('[onclick]')].find(e =>
                        (e.getAttribute('onclick') || '').includes('entrarModoIngresoManual'));
        if (btn) { btn.click(); return true; }
        return false;
    })()""")
    await asyncio.sleep(0.8)

    # Tecla 1 (1 kg)
    await _page.evaluate("""(() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || "").includes("agregar_numero_plu('1')")
        );
        if (btn) btn.click();
    })()""")
    await asyncio.sleep(0.5)
    await _snap(on_screenshot, 0.0)

    # Asignar al operario
    assigned = await _page.evaluate(f"""(() => {{
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || "").includes("ver_vendedor('{operario_id}'")
        );
        if (btn) {{ btn.click(); return true; }}
        return false;
    }})()""")
    print(f"[PW] [BALANZA-STEP] Asignado a {operario_nombre} (id={operario_id}): {assigned}")
    await asyncio.sleep(1.5)
    await _snap(on_screenshot, 0.0)
    print(f"[PW] [BALANZA-STEP] balanza_step_agregar_producto({operario_nombre}) ✓")
    if operario_id == "1":
        return (
            f"Hecho: Vacío buscado → Ingreso Manual → 1 kg → asignado a {operario_nombre}. "
            "Confirmá en 1 frase que quedó asignado. "
            "Luego explicá que el sistema permite que varios operarios trabajen simultáneamente. "
            "SIGUIENTE OBLIGATORIO: llamá balanza_agregar_producto('Malena', '2')."
        )
    return (
        f"Hecho: Vacío buscado → Ingreso Manual → 1 kg → asignado a {operario_nombre}. "
        "Confirmá en 1 frase. Mencioná que ambos tickets están pendientes de cobro. "
        "SIGUIENTE OBLIGATORIO: llamá balanza_mostrar_tickets."
    )


async def balanza_step_mostrar_tickets(on_screenshot=None) -> str:
    """Click en el botón Tickets para mostrar los tickets pendientes de la balanza."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    if "balanza" not in (_page.url or ""):
        base = MGW_URL.rstrip("/")
        print("[PW] [BALANZA-STEP] Auto-navegando a balanza.php (mostrar_tickets en página incorrecta)...")
        await _page.goto(f"{base}/balanza.php", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2.0)
        await _snap(on_screenshot, 0.0)

    await _page.evaluate("if(typeof tickets === 'function') tickets();")
    await asyncio.sleep(1.5)
    await _snap(on_screenshot, 0.0)
    print("[PW] [BALANZA-STEP] mostrar_tickets ✓")
    return (
        "Botón Tickets presionado. Tickets pendientes de ambos operarios visibles. "
        "Confirmá en 1 frase que los tickets están pendientes de cobro. "
        "SIGUIENTE OBLIGATORIO: llamá balanza_ir_a_caja."
    )


async def balanza_step_ir_a_caja(on_screenshot=None) -> str:
    """Finaliza la venta de Balta y navega a caja.php."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")

    # Ver panel de Balta y finalizar venta (crea el ticket CF en caja)
    await _page.evaluate("""
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || "").includes("ver_vendedor('1'")
        );
        if (btn) btn.click();
    """)
    await asyncio.sleep(1.0)
    clicked = await _page.evaluate("""(() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('finalizar_venta')
        );
        if (btn) { btn.click(); return true; }
        return false;
    })()""")
    print(f"[PW] [BALANZA-STEP] Finalizar venta Balta: {clicked}")
    await asyncio.sleep(2.0)

    # Ir a caja.php
    async def _auto_accept_dialog(dialog):
        print(f"[PW] [BALANZA-STEP] Dialog: '{dialog.message[:40]}' — aceptando")
        await dialog.accept()
    _page.on("dialog", _auto_accept_dialog)
    print("[PW] [BALANZA-STEP] Navegando a caja.php...")
    await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(6.0)
    _page.remove_listener("dialog", _auto_accept_dialog)
    await _manejar_arqueo(on_screenshot=on_screenshot)
    await asyncio.sleep(2.0)
    await _snap(on_screenshot, 0.0)
    print("[PW] [BALANZA-STEP] ir_a_caja ✓")
    return (
        "En caja.php. Venta de Balta finalizada y ticket registrado. "
        "Confirmá en 1 frase que llegamos a caja. "
        "SIGUIENTE OBLIGATORIO: llamá balanza_abrir_cf."
    )


async def balanza_step_abrir_cf(on_screenshot=None) -> str:
    """Espera y hace click en el botón CF, luego muestra la lupa para ver el detalle."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    if "caja" not in (_page.url or ""):
        base = MGW_URL.rstrip("/")
        print("[PW] [BALANZA-STEP] Auto-navegando a caja.php (abrir_cf en página incorrecta)...")
        await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3.0)
        await _snap(on_screenshot, 0.0)

    # Esperar botón CF
    try:
        await _page.wait_for_selector(
            "[onclick*=\"mostrar_tickets_balanza_pendientes('cf')\"]",
            state="attached", timeout=8000,
        )
        print("[PW] [BALANZA-STEP] Botón CF encontrado ✓")
    except Exception:
        print("[PW] [BALANZA-STEP] CF timeout — intentando igual")

    # Click CF
    cf_clicked = await _page.evaluate("""(() => {
        const btn = document.querySelector("[onclick*=\\"mostrar_tickets_balanza_pendientes('cf')\\"]")
                 || document.querySelector('[data-tipo="cf"]');
        if (btn) { btn.click(); return true; }
        return false;
    })()""")
    print(f"[PW] [BALANZA-STEP] CF {'✓' if cf_clicked else '✗'}")
    await asyncio.sleep(3.0)
    await _snap(on_screenshot, 0.0)

    # Click lupa (ver detalle)
    await _page.evaluate("""(() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('ver_ticket_balanza_pendiente')
        );
        if (btn) btn.click();
    })()""")
    await asyncio.sleep(2.0)
    await _snap(on_screenshot, 0.0)

    # Click verde (ingresar_ticket_balanza) — atómico para evitar que un barge-in
    # interrumpa entre el panel CF y la apertura de la ventana de caja.
    verde_clicked = await _page.evaluate("""(() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('ingresar_ticket_balanza')
        );
        if (btn) { btn.click(); return true; }
        return false;
    })()""")
    print(f"[PW] [BALANZA-STEP] botón verde {'✓' if verde_clicked else '✗ (no encontrado)'}")
    await asyncio.sleep(3.0)
    await _snap(on_screenshot, 0.0)

    print("[PW] [BALANZA-STEP] abrir_cf ✓")
    return (
        "CF presionado, detalle del ticket mostrado, botón verde presionado → ventana de caja abierta. "
        "Narrá en 1 frase que se abrió la ventana de caja con el ticket de balanza. "
        "⚠️ SIGUIENTE OBLIGATORIO: llamá balanza_cobrar_ticket AHORA para ingresar el pago. "
        "No describas lo que harás — ejecutá la tool primero."
    )


async def balanza_step_cobrar_ticket(on_screenshot=None) -> str:
    """Llena Paga con 20000 y cierra con Presupuestar F8 (ventana de caja ya abierta por abrir_cf)."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    # Llenar "Paga con" con 20000
    for pago_sel in ['#paga_con', 'input[id="paga_con"]', '#pago', 'input[name="pago"]']:
        try:
            pago_el = _page.locator(pago_sel).first
            if await pago_el.count() > 0 and await pago_el.is_visible():
                await pago_el.click()
                await pago_el.fill("20000")
                await pago_el.press("Tab")
                print(f"[PW] [BALANZA-STEP] 'Paga con'=20000 via '{pago_sel}' ✓")
                break
        except Exception:
            continue
    await asyncio.sleep(1.0)
    await _snap(on_screenshot, 0.0)

    # Presupuesto F8
    await _page.evaluate("""(() => {
        const btn = document.getElementById('boton_caja_finalizar_venta')
                 || [...document.querySelectorAll('[onclick]')].find(e => {
                        const oc = e.getAttribute('onclick') || '';
                        return oc.includes('finalizar_factura') || oc.includes('presupuest') || oc.includes('factura=3');
                    });
        if (btn) btn.click();
    })()""")
    await asyncio.sleep(3.0)
    await _snap(on_screenshot, 0.0)
    await asyncio.sleep(2.0)
    await _snap(on_screenshot, 0.0)

    print("[PW] [BALANZA-STEP] cobrar_ticket ✓")
    return (
        "Hecho: botón verde presionado → ventana de caja abierta → $20.000 en Paga con → Presupuestar F8 ejecutado. "
        "Narrá que se abrió la misma ventana de caja que vimos antes — también se pueden agregar más productos si se quiere, "
        "pero para la demo lo dejamos así — se ingresó '$20.000' en 'Paga con' y se cerró con 'Presupuestar F8'. "
        "La sección de balanza está completa."
    )


# ── Steps atómicos — Proveedores (6 pasos) ───────────────────────────────────

async def proveedores_ver_lista(on_screenshot=None) -> str:
    """Paso 1/8: navega a compras.php y muestra la lista de proveedores (sin clickear Editar)."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    print("[PW] [PROV] Navegando a compras.php...")
    await _page.goto(f"{base}/compras.php", wait_until="domcontentloaded", timeout=20000)
    try:
        await _page.wait_for_selector('tbody tr td', timeout=12000)
    except Exception:
        pass
    await asyncio.sleep(1.0)
    await snap()
    print("[PW] [PROV] proveedores_ver_lista ✓")
    return "Lista de proveedores visible en pantalla."


async def proveedores_abrir_historial(on_screenshot=None) -> str:
    """Paso 2/8: abre el historial del primer proveedor clickeando Editar (ya en compras.php)."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
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

    clicked = await click_first([
        'tbody tr:first-child [data-original-title="Editar"]',
        '[data-original-title="Editar"]',
        '[title="Editar"]',
    ], "Editar proveedor")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[data-original-title], [title]')]
                .find(e => (e.getAttribute('data-original-title') || e.getAttribute('title') || '').toLowerCase() === 'editar');
            if (btn) btn.click();
        }""")
    await asyncio.sleep(3.0)
    await snap()
    print("[PW] [PROV] proveedores_abrir_historial ✓")
    return "Historial del proveedor abierto."


async def proveedores_abrir_modal_compra(on_screenshot=None) -> str:
    """Paso 2/6: abre el modal de nueva compra clickeando '+ Compra'."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
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

    clicked = await click_first([
        '[data-original-title="Nueva Compra"]',
        '[title="Nueva Compra"]',
        'a:has-text("+ Compra")',
        'button:has-text("+ Compra")',
        '[onclick*="nuevo_compra"]',
        '[onclick*="nueva_compra"]',
    ], "+ Compra")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('a, button, [onclick], [title]')].find(e => {
                const t = (e.textContent || '').trim().toLowerCase();
                const title = (e.getAttribute('title') || e.getAttribute('data-original-title') || '').toLowerCase();
                return t.includes('+ compra') || title.includes('nueva compra');
            });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(3.0)
    await snap()
    print("[PW] [PROV] proveedores_abrir_modal_compra ✓")
    return "Modal de nueva compra abierto."


async def proveedores_registrar_compra(on_screenshot=None) -> str:
    """Paso 3/6: llena numero=1, importe=800000 y finaliza la compra."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
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

    for sel in ['input[name="numero"]', '#numero']:
        try:
            el = _page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill("1")
                print(f"[PW] [PROV] numero=1 via '{sel}' ✓")
                break
        except Exception:
            continue

    for sel in ['#importe_id_nuevo_compra', 'input[name="importe"]', 'input[name="total"]']:
        try:
            el = _page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill("800000")
                print(f"[PW] [PROV] importe=800000 via '{sel}' ✓")
                break
        except Exception:
            continue
    await asyncio.sleep(0.5)
    await snap()

    clicked = await click_first([
        '#ingresar_compra_boton',
        'button[name="ingresar_compra_boton"]',
        'button:has-text("Finalizar")',
    ], "Finalizar compra")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = document.getElementById('ingresar_compra_boton')
                || [...document.querySelectorAll('button')].find(e => e.textContent.trim().toLowerCase().includes('finalizar'));
            if (btn) btn.click();
        }""")
    await asyncio.sleep(3.0)
    await snap()
    print("[PW] [PROV] proveedores_registrar_compra ✓")
    return "Compra de $800.000 registrada como Impaga."


async def proveedores_abrir_carrito(on_screenshot=None) -> str:
    """Paso 4/6: abre el carrito (detalle) de la compra recién registrada."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
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

    clicked = await click_first([
        'tbody tr:first-child a:has(.clip-cart)',
        'a:has(.clip-cart)',
        'tbody tr:first-child [data-original-title="Cargar productos"]',
        '[data-original-title="Cargar productos"]',
        '[onclick*="compra_detalles"]',
        '[onclick*="detalle_compra"]',
    ], "Carrito (detalle compra)")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[data-original-title], [title], [onclick], a')].find(e => {
                const dt = (e.getAttribute('data-original-title') || e.getAttribute('title') || '').toLowerCase();
                const oc = (e.getAttribute('onclick') || '').toLowerCase();
                const hasCart = e.querySelector && !!e.querySelector('.clip-cart');
                return dt.includes('cargar') || oc.includes('detalle') || oc.includes('carrito') || hasCart;
            });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(3.0)
    await snap()
    print("[PW] [PROV] proveedores_abrir_carrito ✓")
    return "Detalle de compra abierto."


async def proveedores_cargar_producto(on_screenshot=None) -> str:
    """Paso 5/6: ingresa Media res, $10000, 80 kg y presiona Agregar."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
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

    for sel in ['input[name="producto"]', '#producto', 'input.ui-autocomplete-input']:
        try:
            el = _page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill("")
                await el.type("Media res", delay=150)
                print(f"[PW] [PROV] producto=Media res via '{sel}' ✓")
                break
        except Exception:
            continue
    await asyncio.sleep(1.5)
    try:
        await _page.wait_for_selector('.ui-autocomplete .ui-menu-item', state="visible", timeout=4000)
        await _page.locator('.ui-autocomplete .ui-menu-item').first.click()
    except Exception:
        pass
    await asyncio.sleep(0.5)

    for sel in ['input[name="precio"]', '#precio']:
        try:
            el = _page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill("10000")
                print(f"[PW] [PROV] precio=10000 via '{sel}' ✓")
                break
        except Exception:
            continue
    await asyncio.sleep(0.3)

    for sel in ['input[name="peso"]', '#peso']:
        try:
            el = _page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill("80")
                print(f"[PW] [PROV] peso=80 via '{sel}' ✓")
                break
        except Exception:
            continue
    await asyncio.sleep(0.3)
    await snap()

    # Click Agregar
    clicked = await click_first([
        'button[onclick*="agregar_producto_compra"]',
        'button:has-text("Agregar")',
        'button.btn-primary:has-text("Agregar")',
    ], "Agregar producto")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('button')].find(e => {
                const oc = (e.getAttribute('onclick') || '');
                const t = (e.textContent || '').trim().toLowerCase();
                return oc.includes('agregar_producto_compra') || t === 'agregar';
            });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(2.0)
    await snap()
    print("[PW] [PROV] proveedores_cargar_producto ✓")
    return "Media res, AR$10.000, 80 kg ingresados y agregados a la compra."


async def proveedores_finalizar_detalle(on_screenshot=None) -> str:
    """Paso 6/6: finaliza los detalles de la compra."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
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

    clicked = await click_first([
        '[onclick*="finalizar_compra_detalles"]',
        'a:has-text("Finalizar detalles")',
        'button:has-text("Finalizar detalles")',
        'a.btn-success:has-text("Finalizar")',
    ], "Finalizar detalles compra")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')].find(e => {
                const oc = (e.getAttribute('onclick') || '').toLowerCase();
                const t = (e.textContent || '').toLowerCase();
                return oc.includes('finalizar_compra_detalles') || t.includes('finalizar detalle');
            });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(3.0)
    await snap()
    print("[PW] [PROV] proveedores_finalizar_detalle ✓")
    return "Detalle de compra finalizado. Stock actualizado automáticamente."


async def proveedores_registrar_pago(on_screenshot=None) -> str:
    """Paso 7/7: abre el modal de nuevo pago al proveedor clickeando '+ Pago'."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
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

    clicked = await click_first([
        '[onclick*="im_proveedores_nuevo_pago"]',
        '[data-original-title="Nuevo Pago"]',
        'a.btn-warning:has-text("Pago")',
    ], "Nuevo Pago proveedor")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')].find(e => {
                const oc = (e.getAttribute('onclick') || '').toLowerCase();
                const dt = (e.getAttribute('data-original-title') || e.getAttribute('title') || '').toLowerCase();
                return oc.includes('im_proveedores_nuevo_pago') || dt.includes('nuevo pago');
            });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(3.0)
    await snap()
    print("[PW] [PROV] proveedores_registrar_pago ✓")
    return "Modal de nuevo pago al proveedor abierto."


# ── Steps atómicos — Producción ───────────────────────────────────────────────
# Plantilla "Milanesas" ya existe en el sistema (ID 6) — estos steps la consultan
# y la usan para registrar una producción, no la crean desde cero.

async def produccion_ver_plantillas(on_screenshot=None) -> str:
    """Paso 1/6: navega a la lista de plantillas de producción."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    print("[PW] [PROD-STEP] Navegando a produccion_plantillas.php...")
    await _page.goto(f"{base}/produccion_plantillas.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    await snap()
    print("[PW] [PROD-STEP] produccion_ver_plantillas ✓")
    return "Lista de plantillas de producción visible en pantalla."


async def produccion_ver_detalle_plantilla(on_screenshot=None) -> str:
    """Paso 2/6: abre el detalle de la plantilla existente 'Milanesas' (ID 6)."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    print("[PW] [PROD-STEP] Abriendo detalle de plantilla Milanesas (id=6)...")
    clicked = await _page.evaluate("""() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes("plantillas_detalles('6'")
        );
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        print("[PW] [PROD-STEP] No se encontró por id=6, reintentando por fila 'Milanesas'...")
        clicked = await _page.evaluate("""() => {
            const rows = [...document.querySelectorAll('tbody tr')];
            for (const row of rows) {
                if (!row.textContent.toLowerCase().includes('milanesa')) continue;
                const btn = row.querySelector('[onclick*="plantillas_detalles"]');
                if (btn) { btn.click(); return true; }
            }
            return false;
        }""")
    if not clicked:
        print("[PW] [PROD-STEP] Botón de detalle no encontrado")
    try:
        await _page.wait_for_url("**detalle**", timeout=8000)
    except Exception:
        await asyncio.sleep(3.0)
    await snap()
    print("[PW] [PROD-STEP] produccion_ver_detalle_plantilla ✓")
    return "Detalle de la plantilla 'Milanesas' visible, con sus ingredientes y la cantidad que produce."


async def produccion_ir_a_produccion(on_screenshot=None) -> str:
    """Paso 3/6: navega a la sección de Producción."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    print("[PW] [PROD-STEP] Navegando a produccion.php...")
    await _page.goto(f"{base}/produccion.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    await snap()
    print("[PW] [PROD-STEP] produccion_ir_a_produccion ✓")
    return "Sección de Producción visible, con el historial de producciones anteriores."


async def produccion_nueva_produccion(on_screenshot=None) -> str:
    """Paso 4/6: abre el formulario de nueva producción."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
        for selector in selectors:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    print(f"[PW] [PROD-STEP] {label} via '{selector}' ✓")
                    return True
            except Exception:
                continue
        return False

    clicked = await click_first([
        '[onclick="nueva_produccion()"]',
        'a[onclick*="nueva_produccion"]',
        'button[onclick*="nueva_produccion"]',
        'a:has-text("Nueva producción")',
    ], "Nueva producción")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('a, button, [onclick]')].find(e => {
                const oc = (e.getAttribute('onclick') || '').toLowerCase();
                const t = (e.textContent || '').trim().toLowerCase();
                return oc.includes('nueva_produccion') || t.includes('nueva producción') || t.includes('nueva produccion');
            });
            if (btn) btn.click();
        }""")
        print("[PW] [PROD-STEP] Nueva producción via JS ✓")
    await asyncio.sleep(2.5)
    await snap()
    print("[PW] [PROD-STEP] produccion_nueva_produccion ✓")
    return "Formulario de nueva producción abierto, con el selector de plantilla."


async def produccion_seleccionar_plantilla(on_screenshot=None) -> str:
    """Paso 5/6: selecciona la plantilla 'Milanesas' en el formulario de nueva producción."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    try:
        plantilla_sel = _page.locator('#plantilla').first
        if await plantilla_sel.count() > 0:
            await plantilla_sel.select_option(label="Milanesas")
            print("[PW] [PROD-STEP] Plantilla Milanesas seleccionada ✓")
    except Exception:
        await _page.evaluate("""() => {
            const sel = document.getElementById('plantilla');
            if (sel) {
                for (const opt of sel.options) {
                    if (opt.text.toLowerCase().includes('milanesa')) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        break;
                    }
                }
            }
        }""")
        print("[PW] [PROD-STEP] Plantilla Milanesas via JS ✓")
    await asyncio.sleep(0.5)
    await snap()
    print("[PW] [PROD-STEP] produccion_seleccionar_plantilla ✓")
    return "Plantilla 'Milanesas' seleccionada en el formulario."


async def produccion_completar_y_registrar(on_screenshot=None) -> str:
    """Paso 6/6: completa cantidad=1, tipo=Salida de producción, agrega y recarga el resultado."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")

    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)

    async def click_first(selectors, label):
        for selector in selectors:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    print(f"[PW] [PROD-STEP] {label} via '{selector}' ✓")
                    return True
            except Exception:
                continue
        return False

    # Cantidad = 1
    try:
        cant_el = _page.locator('#cantidad').first
        if await cant_el.count() > 0 and await cant_el.is_visible():
            await cant_el.click()
            await cant_el.fill("1")
            print("[PW] [PROD-STEP] Cantidad=1 ✓")
    except Exception:
        pass

    # Tipo = Salida de producción (value=2)
    try:
        tipo_sel = _page.locator('#tipo').first
        if await tipo_sel.count() > 0 and await tipo_sel.is_visible():
            await tipo_sel.select_option(value="2")
            print("[PW] [PROD-STEP] Tipo = Salida de producción ✓")
    except Exception:
        pass

    await asyncio.sleep(0.5)
    await snap()

    # Click Agregar
    clicked = await click_first([
        'button[onclick*="agregar_produccion"]',
        'a[onclick*="agregar_produccion"]',
        'button:has-text("Agregar")',
        'a:has-text("Agregar")',
    ], "Agregar producción")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], button, a')].find(e => {
                const oc = (e.getAttribute('onclick') || '').toLowerCase();
                const t = (e.textContent || '').trim().toLowerCase();
                return oc.includes('agregar_produccion') || t === 'agregar';
            });
            if (btn) btn.click();
        }""")
        print("[PW] [PROD-STEP] Agregar producción via JS ✓")
    await asyncio.sleep(2.5)

    # Recargar produccion.php — el listado no se refresca solo tras el alta
    print("[PW] [PROD-STEP] Recargando produccion.php...")
    await _page.goto(f"{base}/produccion.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    await snap()

    print("[PW] [PROD-STEP] produccion_completar_y_registrar ✓")
    return (
        "Producción de Milanesas registrada. La lista de producciones se actualizó "
        "y el stock se ajustó automáticamente."
    )


async def run_demo_proveedores(on_screenshot=None, on_screenshot_end=None) -> str:
    """Demo completa de proveedores con Playwright (para Realtime API)."""
    ok = await _demo_proveedores(
        decir_frase=_delay_frase,
        on_screenshot=on_screenshot,
        on_screenshot_end=on_screenshot_end,
    )
    return "Demo de proveedores completada — el usuario vio el flujo de carga de compra e ingreso de stock." if ok else "La demo de proveedores tuvo un error parcial pero el usuario vio screenshots."


async def run_demo_produccion(on_screenshot=None, on_screenshot_end=None) -> str:
    """Demo completa de producción con Playwright (para Realtime API)."""
    ok = await _demo_produccion(
        decir_frase=_delay_frase,
        on_screenshot=on_screenshot,
        on_screenshot_end=on_screenshot_end,
    )
    return "Demo de producción completada — el usuario vio cómo crear plantilla y registrar producción." if ok else "La demo de producción tuvo un error parcial pero el usuario vio screenshots."


# ── Steps atómicos — Módulo 1: Configuración inicial ─────────────────────────

def _make_config_snap(on_screenshot):
    async def snap(delay=0.0):
        await _snap(on_screenshot, delay)
    return snap


def _make_config_clicker(label_prefix):
    async def click_first(selectors, label):
        for selector in selectors:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    print(f"[PW] [{label_prefix}] {label} via '{selector}' ✓")
                    return True
            except Exception:
                continue
        return False
    return click_first


async def _ensure_on_config_page(seccion: str):
    """Navega defensivamente a la sub-sección si el browser no está ahí ya."""
    if _current_page() is None:
        return
    path = CONFIG_MODULE_PATHS.get(seccion, "")
    if not path:
        return
    if path not in _page.url:
        base = MGW_URL.rstrip("/")
        print(f"[PW] [CONFIG] Nav defensiva a {seccion}...")
        await _page.goto(f"{base}{path}", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1.0)


async def config_navegar(seccion: str, on_screenshot=None) -> str:
    """Navega a una sub-sección de Configuración y toma screenshot."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    path = CONFIG_MODULE_PATHS.get(seccion)
    if not path:
        return f"Sección '{seccion}' no encontrada."

    # Login silencioso si el browser todavía no tiene sesión MGW
    current_url = _page.url
    if not current_url or current_url in ("about:blank", "") or "index.php" in current_url:
        print("[PW] [CONFIG] Login silencioso antes de navegar...")
        ok = await pw_login()
        if not ok:
            return "Error: no se pudo hacer login en MGW."

    snap = _make_config_snap(on_screenshot)
    print(f"[PW] [CONFIG] Navegando a {seccion} → {path}...")
    await _page.goto(f"{base}{path}", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(1.0)
    await snap()
    print(f"[PW] [CONFIG] config_navegar({seccion}) ✓")
    return f"Navegado a {seccion}."


async def config_usuarios_nuevo(on_screenshot=None) -> str:
    """Click en 'Nuevo Usuario' para abrir el modal."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("USUARIOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="configuracion_usuarios_nuevo_usuario()"]',
        'a[href="#modal_nuevo_usuario_id"]',
        'button:has-text("Nuevo Usuario")',
        'a:has-text("Nuevo Usuario")',
    ], "Nuevo Usuario")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')]
                .find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nuevo_usuario') || t.includes('nuevo usuario');
                });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_usuarios_nuevo ✓")
    return "Modal de nuevo usuario abierto."


async def config_usuarios_scroll_permisos_de(on_screenshot=None) -> str:
    """Scroll dentro del modal de Nuevo Usuario para mostrar el selector 'Permisos del usuario'."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    try:
        # El modal está abierto — NO llamar _ensure_on_config_page para no cerrarlo.
        # Scroll dentro del .modal-body para mostrar #permisos_de_id.
        await _page.evaluate("""() => {
            const sel = document.getElementById('permisos_de_id');
            if (sel) {
                sel.scrollIntoView({ behavior: 'instant', block: 'center' });
                const modal = sel.closest('.modal-body');
                if (modal) modal.scrollTop -= 40;
            }
        }""")
        await asyncio.sleep(0.5)
        if on_screenshot:
            b64 = await _screenshot_b64()
            if b64:
                await on_screenshot(b64)
        print("[PW] [CONFIG] config_usuarios_scroll_permisos_de ✓")
        return "Visible el selector de Permisos del usuario."
    except Exception as e:
        return f"Error: {e}"


async def config_usuarios_expandir_permisos_caja(on_screenshot=None) -> str:
    """Click en el acordeón de Caja en permisos y scrollea para mostrar el contenido."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("USUARIOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '#boton_57',
        '[onclick*="permisos"][onclick*="57"]',
        '[id*="57"]',
    ], "Acordeón Caja permisos")
    if not clicked:
        await _page.evaluate("""() => {
            const el = document.getElementById('boton_57')
                || [...document.querySelectorAll('[onclick]')]
                    .find(e => (e.getAttribute('onclick') || '').includes('57'));
            if (el) el.click();
        }""")
    await asyncio.sleep(1.2)
    # Scroll dentro del modal para que el acordeón expandido sea visible
    try:
        await _page.evaluate("""() => {
            const btn = document.getElementById('boton_57');
            if (btn) {
                btn.scrollIntoView({ behavior: 'instant', block: 'start' });
                const modal = btn.closest('.modal-body');
                if (modal) {
                    modal.scrollTop -= 80;
                } else {
                    window.scrollBy(0, -80);
                }
            }
        }""")
    except Exception:
        pass
    await asyncio.sleep(0.3)
    await snap()
    print("[PW] [CONFIG] config_usuarios_expandir_permisos_caja ✓")
    return "Acordeón de permisos de Caja expandido."


async def config_listas_nueva(on_screenshot=None) -> str:
    """Click en 'Nueva Lista de Precios' para abrir el modal."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("LISTAS_PRECIOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="configuracion_listas_de_precios_nuevo_lista()"]',
        'button:has-text("Nueva Lista")',
        'a:has-text("Nueva Lista")',
    ], "Nueva Lista de Precios")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')]
                .find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nuevo_lista') || t.includes('nueva lista');
                });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_listas_nueva ✓")
    return "Modal de nueva lista de precios abierto."


async def config_grupos_nuevo(on_screenshot=None) -> str:
    """Click en 'Nuevo Grupo' para abrir el modal."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("GRUPOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="configuracion_grupos_nuevo_grupo()"]',
        'button:has-text("Nuevo Grupo")',
        'a:has-text("Nuevo Grupo")',
    ], "Nuevo Grupo")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')]
                .find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nuevo_grupo') || t.includes('nuevo grupo');
                });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_grupos_nuevo ✓")
    return "Modal de nuevo grupo abierto."


async def config_productos_nuevo(on_screenshot=None) -> str:
    """Click en 'Nuevo Producto' y scrollea hasta precios y código PLU."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("PRODUCTOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="configuracion_productos_nuevo_producto()"]',
        'button:has-text("Nuevo Producto")',
        'a:has-text("Nuevo Producto")',
    ], "Nuevo Producto")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')]
                .find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nuevo_producto') || t.includes('nuevo producto');
                });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    try:
        await _page.evaluate("document.getElementById('modal_contenedor_generico_uno').scrollTop = 9999")
    except Exception:
        pass
    await snap()
    print("[PW] [CONFIG] config_productos_nuevo ✓")
    return "Modal de nuevo producto abierto, scrolleado hasta precios y código PLU."


async def config_productos_importar(on_screenshot=None) -> str:
    """Click en el botón de importar para abrir el modal de importación desde Excel."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("PRODUCTOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="form_importar()"]',
        'button:has-text("Importar")',
        'a:has-text("Importar")',
    ], "Importar productos")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')]
                .find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('form_importar') || t.includes('importar');
                });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_productos_importar ✓")
    return "Modal de importación de productos abierto."


async def config_precios_editar_grupo_almacen(on_screenshot=None) -> str:
    """Click en el lápiz del grupo Almacén en la sección de precios."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("PRECIOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        "[onclick=\"precios_lista('3','Almacen')\"]",
        "[onclick*=\"precios_lista('3'\"]",
        "[onclick*='Almacen']",
    ], "Editar grupo Almacén")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick]')]
                .find(e => {
                    const oc = e.getAttribute('onclick') || '';
                    return oc.includes("precios_lista") && (oc.includes("Almacen") || oc.includes("'3'"));
                });
            if (btn) btn.click();
            else {
                const lapiz = [...document.querySelectorAll('a, button')]
                    .find(e => (e.textContent || '').toLowerCase().includes('almac'));
                if (lapiz) lapiz.click();
            }
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_precios_editar_grupo_almacen ✓")
    return "Detalle de precios del grupo Almacén abierto."


async def config_precios2_grupo_carne(on_screenshot=None) -> str:
    """Click en el botón del grupo Carne en PRECIOS2."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("PRECIOS2")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '#boton_1',
        "[onclick=\"precios_lista('1')\"]",
        "[onclick*=\"precios_lista('1'\"]",
    ], "Grupo Carne")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = document.getElementById('boton_1')
                || [...document.querySelectorAll('[onclick]')]
                    .find(e => (e.getAttribute('onclick') || '').includes("precios_lista('1')"));
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_precios2_grupo_carne ✓")
    return "Filtrado por grupo Carne."


async def config_precios_historial_detalle_grupo(on_screenshot=None) -> str:
    """Click en la lupita del grupo para ver productos en el historial de precios."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("PRECIOS_HISTORIAL")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        "[onclick=\"detalles_grupo('3')\"]",
        "[onclick*=\"detalles_grupo('3'\"]",
        "[onclick*='detalles_grupo']",
    ], "Detalle grupo historial")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick]')]
                .find(e => (e.getAttribute('onclick') || '').includes('detalles_grupo'));
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_precios_historial_detalle_grupo ✓")
    return "Detalle de productos del grupo abierto."


async def config_precios_historial_detalle_producto(on_screenshot=None) -> str:
    """Click en la lupa de un producto para ver el historial de cambios de precio."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("PRECIOS_HISTORIAL")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    # Abrir el grupo si la lupa de producto no está visible todavía
    product_link_visible = False
    try:
        el = _page.locator("[onclick*=\"detalles_grupo_detalles\"]").first
        product_link_visible = await el.count() > 0 and await el.is_visible()
    except Exception:
        pass
    if not product_link_visible:
        # Abrir el grupo primero
        grp_clicked = await click_first([
            "[onclick=\"detalles_grupo('3')\"]",
            "[onclick*='detalles_grupo']",
        ], "Abrir grupo antes de detalle producto")
        if not grp_clicked:
            await _page.evaluate("""() => {
                const btn = [...document.querySelectorAll('[onclick]')]
                    .find(e => (e.getAttribute('onclick') || '').includes('detalles_grupo'));
                if (btn) btn.click();
            }""")
        await asyncio.sleep(1.5)
    clicked = await click_first([
        "[onclick=\"detalles_grupo_detalles('3')\"]",
        "[onclick*=\"detalles_grupo_detalles\"]",
    ], "Detalle producto historial")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick]')]
                .find(e => (e.getAttribute('onclick') || '').includes('detalles_grupo_detalles'));
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_precios_historial_detalle_producto ✓")
    return "Historial de cambios de precio del producto abierto."


async def config_combos_nuevo(on_screenshot=None) -> str:
    """Click en 'Nuevo Combo' para abrir el modal."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("COMBOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="configuracion_combos_nuevo_combo()"]',
        'button:has-text("Nuevo Combo")',
        'a:has-text("Nuevo Combo")',
    ], "Nuevo Combo")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')]
                .find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nuevo_combo') || t.includes('nuevo combo');
                });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_combos_nuevo ✓")
    return "Modal de nuevo combo abierto."


async def config_combos_editar(on_screenshot=None) -> str:
    """Click en el lápiz de editar del primer combo."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("COMBOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '#boton_editar_nuevo',
        '[onclick^="configuracion_combos_editar_combo"]',
        'tbody tr:first-child [data-original-title="Editar"]',
        '[data-original-title="Editar"]',
    ], "Editar combo")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = document.getElementById('boton_editar_nuevo')
                || [...document.querySelectorAll('[onclick]')]
                    .find(e => (e.getAttribute('onclick') || '').includes('configuracion_combos_editar_combo'))
                || [...document.querySelectorAll('[data-original-title]')]
                    .find(e => (e.getAttribute('data-original-title') || '').toLowerCase() === 'editar');
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_combos_editar ✓")
    return "Editor del combo abierto."


async def config_formas_pago_nueva(on_screenshot=None) -> str:
    """Click en 'Nueva Forma de Pago' para abrir el modal."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("FORMAS_PAGO")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="configuracion_formas_de_pago_nuevo_forma()"]',
        'button:has-text("Nueva Forma")',
        'a:has-text("Nueva Forma")',
    ], "Nueva Forma de Pago")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')]
                .find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nuevo_forma') || t.includes('nueva forma');
                });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_formas_pago_nueva ✓")
    return "Modal de nueva forma de pago abierto."


async def config_descuentos_nuevo(on_screenshot=None) -> str:
    """Click en 'Nuevo Descuento' para abrir el modal."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("DESCUENTOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="configuracion_descuentos_nuevo_descuento()"]',
        'button:has-text("Nuevo Descuento")',
        'a:has-text("Nuevo Descuento")',
    ], "Nuevo Descuento")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')]
                .find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nuevo_descuento') || t.includes('nuevo descuento');
                });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_descuentos_nuevo ✓")
    return "Modal de nuevo descuento abierto."


async def config_terminales_nueva(on_screenshot=None) -> str:
    """Click en 'Nueva Terminal' para abrir el modal."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("TERMINALES")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="configuracion_terminales_nuevo_terminal()"]',
        'button:has-text("Nueva Terminal")',
        'a:has-text("Nueva Terminal")',
    ], "Nueva Terminal")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')]
                .find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nuevo_terminal') || t.includes('nueva terminal');
                });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_terminales_nueva ✓")
    return "Modal de nueva terminal abierto."


async def config_impuestos_nuevo(on_screenshot=None) -> str:
    """Click en 'Nuevo Impuesto' para abrir el modal."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("IMPUESTOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="configuracion_impuestos_nuevo_impuesto()"]',
        'button:has-text("Nuevo Impuesto")',
        'a:has-text("Nuevo Impuesto")',
    ], "Nuevo Impuesto")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')]
                .find(e => {
                    const oc = (e.getAttribute('onclick') || '');
                    const t = (e.textContent || '').trim().toLowerCase();
                    return oc.includes('nuevo_impuesto') || t.includes('nuevo impuesto');
                });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_impuestos_nuevo ✓")
    return "Modal de nuevo impuesto abierto."


async def config_gastos_nuevo_concepto(on_screenshot=None) -> str:
    """Abre el modal 'Nuevo concepto' de gastos (configuracion_gastos_nuevo_gasto())."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    await _ensure_on_config_page("GASTOS")
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")
    clicked = await click_first([
        '[onclick="configuracion_gastos_nuevo_gasto()"]',
        '[onclick*="configuracion_gastos_nuevo_gasto"]',
        'a[href="#modal_nuevo_gasto_id"]',
        'a:has-text("Nuevo concepto")',
        'button:has-text("Nuevo concepto")',
    ], "Nuevo concepto")
    if not clicked:
        await _page.evaluate("""() => {
            const btn = [...document.querySelectorAll('[onclick], a, button')].find(e => {
                const oc = (e.getAttribute('onclick') || '');
                const href = (e.getAttribute('href') || '');
                const t = (e.textContent || '').trim().toLowerCase();
                return oc.includes('configuracion_gastos_nuevo_gasto') || href.includes('modal_nuevo_gasto') || t.includes('nuevo concepto');
            });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(1.5)
    await snap()
    print("[PW] [CONFIG] config_gastos_nuevo_concepto ✓")
    return "Modal de nuevo concepto de gasto abierto."


async def config_gastos_crear_concepto(on_screenshot=None) -> str:
    """Ingresa 'Articulos de Limpieza' en el modal de nuevo concepto y presiona Agregar."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    snap = _make_config_snap(on_screenshot)
    click_first = _make_config_clicker("CONFIG")

    for sel in ['#modal_nuevo_gasto_id input[name="nombre"]', '#id_nombre', 'input[name="nombre"]']:
        try:
            el = _page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                await el.fill("Articulos de Limpieza")
                print(f"[PW] [CONFIG] nombre=Articulos de Limpieza via '{sel}' ✓")
                break
        except Exception:
            continue
    await asyncio.sleep(0.3)
    await snap()

    clicked = await click_first([
        '#modal_nuevo_gasto_id button[type="submit"]',
        '#modal_nuevo_gasto_id button:has-text("Agregar")',
        'button[type="submit"]:has-text("Agregar")',
    ], "Agregar concepto")
    if not clicked:
        await _page.evaluate("""() => {
            const modal = document.querySelector('#modal_nuevo_gasto_id') || document;
            const btn = [...modal.querySelectorAll('button[type=submit], button')].find(e => {
                const t = (e.textContent || '').trim().toLowerCase();
                return t.includes('agregar');
            });
            if (btn) btn.click();
        }""")
    await asyncio.sleep(2.0)
    await snap()
    print("[PW] [CONFIG] config_gastos_crear_concepto ✓")
    return "Concepto 'Articulos de Limpieza' creado."


async def config_gastos_eliminar_concepto(on_screenshot=None) -> str:
    """Elimina silenciosamente el concepto de gasto 'Articulos de Limpieza' recién creado,
    buscándolo por nombre (el id es autoincremental, no se puede hardcodear), para no
    acumular conceptos de prueba entre capacitaciones. No toma screenshots (es interno)."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    # Recargar la lista de gastos para ver el concepto recién creado
    await _page.goto(f"{base}/configuracion_gastos.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(1.5)

    # Buscar la fila del concepto por nombre (sin acentos) y disparar su modal de eliminación
    opened = await _page.evaluate("""() => {
        const norm = s => (s || '').toLowerCase()
            .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
        const rows = [...document.querySelectorAll('tbody tr')];
        const row = rows.find(r => norm(r.textContent).includes('articulos de limpieza'));
        if (!row) return false;
        const btn = row.querySelector('[onclick*="modal_configuracion_gastos_eliminar_gasto"]')
                 || row.querySelector('.btn-bricky');
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not opened:
        print("[PW] [CONFIG] config_gastos_eliminar_concepto: no se encontró el concepto")
        return "No se encontró el concepto de prueba para eliminar."
    await asyncio.sleep(1.2)

    # Escribir una razón (obligatoria) y confirmar la eliminación
    for sel in ['#comentario_escondido_id', 'input[name="comentario_escondido"]']:
        try:
            el = _page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.fill("x")
                break
        except Exception:
            continue
    await asyncio.sleep(0.3)

    for sel in ['#verificar_vacio_id', 'input[name="verificar_vacio"]', 'input[value="Si, eliminar"]']:
        try:
            el = _page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                break
        except Exception:
            continue
    await asyncio.sleep(1.5)
    print("[PW] [CONFIG] config_gastos_eliminar_concepto ✓")
    return "Concepto de prueba eliminado."


# ── Steps atómicos — Módulo 2: Caja y Caja Mayor ─────────────────────────────

async def _arqueo_confirmar(monto: int, on_screenshot=None) -> None:
    """Helper compartido: llena el campo de importe del arqueo, opcionalmente muestra snapshot, y confirma."""
    if _current_page() is None:
        return
    campo = _page.locator("#importe_arqueo_nuevo").first
    if await campo.count() > 0:
        await campo.fill(str(monto))
    else:
        for sel in ['input[name="importe_arqueo_nuevo"]', 'input[name="importe"]']:
            try:
                el = _page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.fill(str(monto))
                    break
            except Exception:
                continue
    # Snapshot BEFORE confirming so the user sees the form with the amount filled
    if on_screenshot:
        await _snap(on_screenshot, 0.8)
    btn = _page.locator("#boton_nuevo_arqueo").first
    if await btn.count() > 0:
        await btn.click()
    else:
        await _page.evaluate("""() => {
            const b = document.getElementById('boton_nuevo_arqueo')
                   || [...document.querySelectorAll('[onclick]')].find(e =>
                          (e.getAttribute('onclick') || '').includes('nuevo_arqueo'));
            if (b) b.click();
        }""")
    await asyncio.sleep(2.0)


async def caja_ir_a_apertura(on_screenshot=None) -> str:
    """
    Resuelve silenciosamente el estado previo de la caja y navega a caja.php
    para MOSTRAR el formulario de apertura sin confirmarlo todavía.
    """
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")

    # Login silencioso si todavía no hay sesión activa
    current_url = _page.url
    if not current_url or current_url in ("about:blank", "") or "index.php" in current_url:
        print("[PW] [CAJA2] Login silencioso antes de ir a apertura...")
        ok = await pw_login()
        if not ok:
            return "Error: no se pudo hacer login en MGW."

    # El formulario de APERTURA (caja cerrada) y el arqueo de CIERRE FORZADO (caja
    # abierta de un día anterior) comparten el mismo campo #importe_arqueo_nuevo en
    # caja.php, así que su sola presencia NO alcanza para distinguirlos. Si confirmamos
    # el arqueo sobre el form de apertura, en realidad ABRIMOS la caja — y después
    # Malena narra "acá está la apertura" con la caja ya abierta en pantalla (bug).
    # Por eso primero determinamos el estado real con una señal confiable: el botón
    # #boton_cerrar_caja en caja_cierre.php, que solo aparece si la caja está abierta.
    print("[PW] [CAJA2] Verificando estado de la caja en caja_cierre.php...")
    await _page.goto(f"{base}/caja_cierre.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(1.0)
    cerrar_btn = _page.locator("#boton_cerrar_caja").first
    caja_abierta = await cerrar_btn.count() > 0 and await cerrar_btn.is_visible()

    print("[PW] [CAJA2] Navegando a caja.php...")
    await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(1.5)
    arqueo_input = _page.locator("#importe_arqueo_nuevo").first
    arqueo_visible = await arqueo_input.count() > 0 and await arqueo_input.is_visible()

    if caja_abierta:
        # La caja está abierta: hay que cerrarla en silencio para dejar el form de apertura.
        if arqueo_visible:
            # Cierre forzado: la propia caja.php pide el arqueo de cierre.
            print("[PW] [CAJA2] Caja abierta con cierre forzado — cerrando silenciosamente...")
            await _arqueo_confirmar(monto=1000000)
        else:
            # Turno normal abierto: se cierra desde caja_cierre.php.
            print("[PW] [CAJA2] Caja abierta (turno normal) — cerrando silenciosamente...")
            await _page.goto(f"{base}/caja_cierre.php", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(1.0)
            cerrar_btn = _page.locator("#boton_cerrar_caja").first
            if await cerrar_btn.count() > 0 and await cerrar_btn.is_visible():
                await cerrar_btn.click()
                await asyncio.sleep(1.5)
                await _arqueo_confirmar(monto=1000000)
        # Recargar caja.php para mostrar el form de apertura limpio.
        await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1.5)
    else:
        # Caja cerrada: caja.php ya muestra el form de apertura. NO confirmamos nada
        # (confirmarlo abriría la caja). Lo dejamos tal cual para que Malena lo narre.
        print("[PW] [CAJA2] Caja cerrada — form de apertura visible, sin cambios.")

    # Screenshot del formulario vacío de apertura (ANTES de llenarlo)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [CAJA2] caja_ir_a_apertura ✓")
    return "Formulario de apertura de caja visible en pantalla. El campo efectivo está listo para ingresar el fondo inicial."


async def caja_abrir_turno(on_screenshot=None) -> str:
    """
    Llena $100.000 en el campo efectivo y confirma la apertura del turno.
    Debe llamarse DESPUÉS de caja_ir_a_apertura().
    """
    if _current_page() is None:
        return "Error: browser no iniciado"
    print("[PW] [CAJA2] Confirmando apertura con $100.000...")
    # on_screenshot → snaps BEFORE clic para mostrar el monto ingresado
    await _arqueo_confirmar(monto=100000, on_screenshot=on_screenshot)
    await asyncio.sleep(1.0)
    # Segundo snap: caja abierta y lista para operar
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [CAJA2] caja_abrir_turno ✓")
    return "Caja abierta con $100.000 de fondo inicial. La pantalla muestra la caja lista para operar."


async def caja_ver_lista_ventas(on_screenshot=None) -> str:
    """Navega a caja.php y muestra la lista de ventas realizadas."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    print("[PW] [CAJA2] Navegando a caja.php (lista ventas)...")
    await _page.goto(f"{base}/caja.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [CAJA2] caja_ver_lista_ventas ✓")
    return "Lista de ventas realizadas visible en pantalla."


async def caja_ver_detalle_venta(on_screenshot=None) -> str:
    """Click en el ícono de detalles de la venta más reciente (primera fila de tbody)."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = document.querySelector(
            'tbody tr:first-child [onclick*="im_detalles_venta"]'
        );
        if (btn) { btn.click(); return true; }
        // fallback: lupa genérica en la primera fila
        const lupas = document.querySelectorAll('tbody tr:first-child [onclick*="detalles"]');
        if (lupas.length > 0) { lupas[0].click(); return true; }
        return false;
    }""")
    print(f"[PW] [CAJA2] caja_ver_detalle_venta: {'✓' if clicked else '✗ (no encontrado)'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Detalle de la venta visible, con opciones de reimprimir, compartir por mail/WhatsApp y emitir FCE."


async def caja_retiros_navegar(on_screenshot=None) -> str:
    """Navega a caja_retiros.php."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    print("[PW] [CAJA2] Navegando a caja_retiros.php...")
    await _page.goto(f"{base}/caja_retiros.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [CAJA2] caja_retiros_navegar ✓")
    return "Sección de retiros de caja visible."


async def caja_retiros_nuevo(on_screenshot=None) -> str:
    """Abre el modal de nuevo retiro clickeando el botón de efectivo."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        // onclick literal que da el documento
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes("nuevo_retiro('efectivo'")
        );
        if (btn) { btn.click(); return true; }
        // fallback: cualquier botón de nuevo retiro
        const btn2 = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('nuevo_retiro')
        );
        if (btn2) { btn2.click(); return true; }
        return false;
    }""")
    if not clicked:
        for selector in ['a:has-text("Nuevo retiro")', 'button:has-text("Nuevo retiro")',
                         'a:has-text("Nuevo Retiro")', 'button:has-text("Nuevo Retiro")']:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    clicked = True
                    break
            except Exception:
                continue
    print(f"[PW] [CAJA2] caja_retiros_nuevo: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Modal de nuevo retiro abierto. El select de medios de pago muestra las opciones disponibles: efectivo, cupones, Mercado Pago, transferencia."


async def caja_retiros_ingresar_ejemplo(on_screenshot=None) -> str:
    """Ingresa 10000 en el importe del modal de nuevo retiro, presiona Agregar
    y vuelve a caja_retiros.php para mostrar el retiro pendiente."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    print("[PW] [CAJA2] Ingresando retiro de ejemplo de $10.000 en efectivo...")
    try:
        inp = _page.locator('#importe').first
        await inp.wait_for(state="visible", timeout=8000)
        await inp.fill("10000")
    except Exception as e:
        print(f"[PW] [CAJA2] no se pudo cargar importe: {e}")
    await asyncio.sleep(0.5)
    clicked = await _page.evaluate("""() => {
        const btn = document.getElementById('boton_nuevo_retiro_ingresar');
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    print(f"[PW] [CAJA2] retiro agregar: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    await _page.goto(f"{base}/caja_retiros.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [CAJA2] caja_retiros_ingresar_ejemplo ✓")
    return "Retiro de $10.000 en efectivo creado. Aparece en la lista en estado pendiente, con botones a la derecha para eliminarlo, rechazarlo o aprobarlo."


async def caja_retiros_abrir_aprobar(on_screenshot=None) -> str:
    """Click en el botón verde de aprobar del retiro pendiente (mostrar_aprobar_retiro),
    abriendo el modal de confirmación."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('mostrar_aprobar_retiro')
        );
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    print(f"[PW] [CAJA2] caja_retiros_abrir_aprobar: {'✓' if clicked else '✗'}")
    await asyncio.sleep(1.5)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Modal de confirmación de aprobación visible, preguntando si se desea aprobar el retiro."


async def caja_retiros_confirmar_aprobar(on_screenshot=None) -> str:
    """Click en 'Si, aprobar' (caja_retiros_aprobar_retiro) para confirmar la aprobación."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('caja_retiros_aprobar_retiro')
        );
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    print(f"[PW] [CAJA2] caja_retiros_confirmar_aprobar: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Retiro aprobado. Queda en estado aceptado."


async def caja_cierre_navegar(on_screenshot=None) -> str:
    """Navega a caja_cierre.php."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    print("[PW] [CAJA2] Navegando a caja_cierre.php...")
    await _page.goto(f"{base}/caja_cierre.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [CAJA2] caja_cierre_navegar ✓")
    return "Sección de cierre de caja visible."


async def caja_cierre_nuevo(on_screenshot=None) -> str:
    """Hace click en el botón 'Nuevo cierre de caja' (#boton_cerrar_caja)."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = document.getElementById('boton_cerrar_caja')
               || [...document.querySelectorAll('[onclick]')].find(e =>
                      (e.getAttribute('onclick') || '').includes('nuevo_cierre'));
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        for selector in ['button:has-text("Nuevo cierre")', 'a:has-text("Nuevo cierre")',
                         'button:has-text("Cierre de caja")', 'a:has-text("Cierre de caja")']:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    clicked = True
                    break
            except Exception:
                continue
    print(f"[PW] [CAJA2] caja_cierre_nuevo: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Formulario de nuevo cierre de caja abierto."


async def caja_cierre_confirmar(on_screenshot=None) -> str:
    """Ingresa $500.000 en el arqueo y confirma el cierre."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    print("[PW] [CAJA2] Confirmando cierre con $500.000 de arqueo...")
    await _arqueo_confirmar(monto=500000)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [CAJA2] caja_cierre_confirmar ✓")
    return "Arqueo de $500.000 ingresado y cierre de caja confirmado."


async def caja_cierre_ver_resultado(on_screenshot=None) -> str:
    """Recarga caja_cierre.php para mostrar la fila del cierre recién realizado."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    print("[PW] [CAJA2] Recargando caja_cierre.php...")
    await _page.goto(f"{base}/caja_cierre.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [CAJA2] caja_cierre_ver_resultado ✓")
    return "Cierre realizado visible en la lista, con fecha, responsable, ingresos, ventas, retiros y diferencia de caja."


async def caja_cierre_nuevo_movimiento(on_screenshot=None) -> str:
    """Click en el ícono de nuevo movimiento en la fila más reciente del cierre."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = document.querySelector(
            'tbody tr:first-child [onclick*="nuevo_movimiento"]'
        );
        if (btn) { btn.click(); return true; }
        // fallback más amplio en la primera fila
        const btns = document.querySelectorAll('tbody tr:first-child [onclick]');
        for (const b of btns) {
            if ((b.getAttribute('onclick') || '').includes('movimiento')) {
                b.click(); return true;
            }
        }
        return false;
    }""")
    print(f"[PW] [CAJA2] caja_cierre_nuevo_movimiento: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Modal de nuevo movimiento abierto, con opciones: pagos de clientes, pagos a proveedores, gastos, ingresos, retiros."


async def caja_cierre_movimiento_pago_proveedor(on_screenshot=None) -> str:
    """En el modal de nuevo movimiento, selecciona la opción 'Pago a proveedor'."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = document.getElementById('boton_proveedor')
               || [...document.querySelectorAll('[onclick]')].find(e =>
                      (e.getAttribute('onclick') || '').includes("cambio_nuevo_movimiento('proveedor'"));
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    print(f"[PW] [CAJA2] caja_cierre_movimiento_pago_proveedor: {'✓' if clicked else '✗'}")
    await asyncio.sleep(1.5)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Formulario de pago a proveedor visible, con campos de proveedor, forma de pago e importe."


async def caja_cierre_movimiento_finalizar_proveedor(on_screenshot=None) -> str:
    """Ingresa 100000 en el importe del pago a proveedor y presiona Finalizar."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    print("[PW] [CAJA2] Ingresando pago a proveedor de $100.000...")
    try:
        inp = _page.locator('#proveedor_pago_importe').first
        await inp.wait_for(state="visible", timeout=8000)
        await inp.fill("100000")
    except Exception as e:
        print(f"[PW] [CAJA2] no se pudo cargar proveedor_pago_importe: {e}")
    await asyncio.sleep(0.5)
    clicked = await _page.evaluate("""() => {
        const btn = document.getElementById('boton_finalizar_nuevo_movimiento');
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    print(f"[PW] [CAJA2] finalizar movimiento: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Pago a proveedor de $100.000 en efectivo registrado como movimiento del cierre."


async def caja_mayor_navegar(on_screenshot=None) -> str:
    """Navega a caja_administracion_caja.php (Caja Mayor)."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    print("[PW] [CAJA-MAYOR] Navegando a caja_administracion_caja.php...")
    await _page.goto(f"{base}/caja_administracion_caja.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [CAJA-MAYOR] caja_mayor_navegar ✓")
    return "Pantalla de Caja Mayor visible."


async def caja_mayor_nuevo_arqueo(on_screenshot=None) -> str:
    """Abre el modal de nuevo arqueo de caja mayor SIN completar ni enviar."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('caja_administracion_nuevo_arqueo')
        );
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        for selector in ['a:has-text("Nuevo arqueo")', 'button:has-text("Nuevo arqueo")',
                         '.btn-danger:has-text("arqueo")']:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    clicked = True
                    break
            except Exception:
                continue
    print(f"[PW] [CAJA-MAYOR] caja_mayor_nuevo_arqueo (modal abierto, sin enviar): {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Modal de nuevo arqueo abierto (sin completar ni enviar — solo se muestra al cliente)."


async def caja_mayor_detalle_arqueo(on_screenshot=None) -> str:
    """Click en el ícono de detalle del arqueo principal."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    # Cerrar el modal de nuevo arqueo si quedó abierto
    try:
        await _page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
    except Exception:
        pass

    clicked = await _page.evaluate("""() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('detalles_arqueo_principal')
        );
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        for selector in ['[data-original-title="Ver detalles"]', '[title="Ver detalles"]',
                         'tbody tr:first-child .fa-search', 'tbody tr:first-child a']:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    clicked = True
                    break
            except Exception:
                continue
    print(f"[PW] [CAJA-MAYOR] caja_mayor_detalle_arqueo: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Detalle del arqueo visible, mostrando el saldo en cada medio de pago."


async def caja_mayor_ver_movimientos(on_screenshot=None) -> str:
    """Click en el botón 'Ver movimientos' de la caja mayor."""
    if _current_page() is None:
        return "Error: browser no iniciado"

    # Cerrar modal si quedó abierto
    try:
        await _page.keyboard.press("Escape")
        await asyncio.sleep(0.3)
    except Exception:
        pass

    clicked = await _page.evaluate("""() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('caja_administracion_lista_detalles')
        );
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        for selector in ['a:has-text("Ver movimientos")', 'button:has-text("Ver movimientos")']:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    clicked = True
                    break
            except Exception:
                continue
    print(f"[PW] [CAJA-MAYOR] caja_mayor_ver_movimientos: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Movimientos de caja mayor visibles: retiros aprobados y botones de ingreso, retiro, arqueo y más."


async def caja_mayor_cheques_navegar(on_screenshot=None) -> str:
    """Navega a caja_administracion_cheques.php (sección de cheques de la caja mayor)."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    print("[PW] [CAJA-MAYOR] Navegando a caja_administracion_cheques.php...")
    await _page.goto(f"{base}/caja_administracion_cheques.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [CAJA-MAYOR] caja_mayor_cheques_navegar ✓")
    return "Sección de cheques de la caja mayor visible: cheques emitidos y recibidos, con botón para emitir cheque."


async def caja_mayor_cheques_emitir(on_screenshot=None) -> str:
    """Abre el modal de nuevo cheque (botón 'Emitir cheque' → nuevo_cheque())."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('nuevo_cheque')
        );
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        for selector in ['a:has-text("Emitir cheque")', 'button:has-text("Emitir cheque")']:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    clicked = True
                    break
            except Exception:
                continue
    print(f"[PW] [CAJA-MAYOR] caja_mayor_cheques_emitir: {'✓' if clicked else '✗'}")
    await asyncio.sleep(1.5)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Modal de nuevo cheque abierto, con campos de banco, fecha, número, importe y comentarios."


async def caja_mayor_cheques_completar(on_screenshot=None) -> str:
    """Completa el modal de nuevo cheque (fecha de hoy, número 123456, importe 100000) y presiona Ingresar."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    from datetime import date
    hoy = date.today().strftime("%Y-%m-%d")
    print(f"[PW] [CAJA-MAYOR] Completando cheque: fecha={hoy}, numero=123456, importe=100000...")
    # Fecha (date-picker con id fecha_cheque_id)
    try:
        await _page.evaluate(
            """(v) => { const el = document.getElementById('fecha_cheque_id');
                        if (el) { el.value = v; el.dispatchEvent(new Event('change', {bubbles:true})); } }""",
            hoy,
        )
    except Exception as e:
        print(f"[PW] [CAJA-MAYOR] no se pudo cargar fecha_cheque_id: {e}")
    # Número
    try:
        inp = _page.locator('#numero_id').first
        await inp.wait_for(state="visible", timeout=8000)
        await inp.fill("123456")
    except Exception as e:
        print(f"[PW] [CAJA-MAYOR] no se pudo cargar numero_id: {e}")
    # Importe
    try:
        await _page.locator('#importe_id').first.fill("100000")
    except Exception as e:
        print(f"[PW] [CAJA-MAYOR] no se pudo cargar importe_id: {e}")
    await asyncio.sleep(0.5)
    clicked = await _page.evaluate("""() => {
        const btn = document.getElementById('boton_nuevo_cheque');
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    print(f"[PW] [CAJA-MAYOR] ingresar cheque: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Cheque emitido: número 123456 por $100.000 con fecha de hoy. Queda disponible para usar en pagos a proveedores."


async def caja_mayor_cheques_filtrar_todos(on_screenshot=None) -> str:
    """Click en el filtro 'Todos' (cambiar_mostrar_todos) de la tabla de cheques."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = document.getElementById('boton_1')
               || [...document.querySelectorAll('[onclick]')].find(e =>
                      (e.getAttribute('onclick') || '').includes('cambiar_mostrar_todos'));
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    print(f"[PW] [CAJA-MAYOR] caja_mayor_cheques_filtrar_todos: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Filtro 'Todos' aplicado: se muestran todos los cheques, tanto activos como inactivos."


# ══════════════════════════════════════════════════════════════════════════════
# RECURSOS HUMANOS (RRHH)
# ══════════════════════════════════════════════════════════════════════════════

async def rrhh_navegar(on_screenshot=None) -> str:
    """Navega a rrhh_personal.php (Recursos Humanos → Personal)."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    print("[PW] [RRHH] Navegando a rrhh_personal.php...")
    await _page.goto(f"{base}/rrhh_personal.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [RRHH] rrhh_navegar ✓")
    return "Sección de Recursos Humanos → Personal visible: listado de todo el personal, con botón para crear nuevo personal."


async def rrhh_personal_nuevo(on_screenshot=None) -> str:
    """Abre el modal de nuevo personal (botón 'Nuevo personal' → f_personal_nuevo())."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('f_personal_nuevo')
        );
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        for selector in ['a:has-text("Nuevo personal")', 'button:has-text("Nuevo personal")']:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    clicked = True
                    break
            except Exception:
                continue
    print(f"[PW] [RRHH] rrhh_personal_nuevo: {'✓' if clicked else '✗'}")
    await asyncio.sleep(1.5)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Modal de nuevo personal abierto, con campos de nombre, apellido, categoría, dirección, mail, teléfono, dni, cuil o cuit, legajo, fecha de alta, cumpleaños, sueldo, periodicidad de pago, cliente asociado, comentarios y fotos."


async def rrhh_personal_editar(on_screenshot=None) -> str:
    """Entra a la edición del personal id_personal=1 (botón azul de editar de la fila)."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const link = [...document.querySelectorAll('a[href]')].find(a =>
            (a.getAttribute('href') || '').includes('rrhh_personal_editar.php?id_personal=1')
        );
        if (link) { link.click(); return true; }
        return false;
    }""")
    if not clicked:
        base = MGW_URL.rstrip("/")
        print("[PW] [RRHH] editar: fallback goto rrhh_personal_editar.php?id_personal=1")
        await _page.goto(f"{base}/rrhh_personal_editar.php?id_personal=1", wait_until="domcontentloaded", timeout=20000)
    else:
        await _page.wait_for_load_state("domcontentloaded", timeout=20000)
    print(f"[PW] [RRHH] rrhh_personal_editar: {'✓ (click)' if clicked else '✓ (goto)'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Detalle del personal abierto: se ven todos los movimientos y las acciones para liquidar sueldos, pagarlos, ingresar faltas o ingresar descuentos."


async def rrhh_personal_ficha(on_screenshot=None) -> str:
    """Abre la pestaña 'Ficha' del personal (onclick cargar_ficha())."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const tab = document.getElementById('li_ficha')
               || [...document.querySelectorAll('[onclick]')].find(e =>
                      (e.getAttribute('onclick') || '').includes('cargar_ficha'));
        if (tab) { tab.click(); return true; }
        return false;
    }""")
    print(f"[PW] [RRHH] rrhh_personal_ficha: {'✓' if clicked else '✗'}")
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Pestaña Ficha abierta: se ven todos los datos del personal y el campo 'cliente asociado' para vincularlo a un cliente."


async def rrhh_personal_cliente_asociado(on_screenshot=None) -> str:
    """Hace click en el selector 'cliente_asociado' para desplegar todas las opciones de clientes."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    done = await _page.evaluate("""() => {
        const sel = document.getElementById('cliente_asociado');
        if (!sel) return false;
        sel.scrollIntoView({block: 'center'});
        sel.focus();
        return true;
    }""")
    if done:
        try:
            # Click sobre el <select> para desplegar la lista de clientes
            await _page.click('#cliente_asociado', timeout=5000)
        except Exception as e:
            print(f"[PW] [RRHH] cliente_asociado: click falló ({e})")
    print(f"[PW] [RRHH] rrhh_personal_cliente_asociado: {'✓' if done else '✗'}")
    await asyncio.sleep(1.5)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Selector 'cliente asociado' desplegado: se ven todas las opciones de clientes y al hacer click sobre uno queda vinculado al personal."


async def rrhh_fichaje_nuevo(on_screenshot=None) -> str:
    """Hace click en el botón 'Nuevo fichaje' (onclick nuevo_fichaje()) para el fichaje manual."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    clicked = await _page.evaluate("""() => {
        const btn = [...document.querySelectorAll('[onclick]')].find(e =>
            (e.getAttribute('onclick') || '').includes('nuevo_fichaje')
        );
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not clicked:
        for selector in ['a:has-text("Nuevo fichaje")', 'button:has-text("Nuevo fichaje")']:
            try:
                el = _page.locator(selector).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    clicked = True
                    break
            except Exception:
                continue
    print(f"[PW] [RRHH] rrhh_fichaje_nuevo: {'✓' if clicked else '✗'}")
    await asyncio.sleep(1.5)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    return "Modal de nuevo fichaje abierto: se carga la fecha y la hora manualmente para registrar el fichaje de un empleado."


async def rrhh_fichaje_navegar(on_screenshot=None) -> str:
    """Navega a rrhh_fichaje.php (sección de fichaje del personal)."""
    if _current_page() is None:
        return "Error: browser no iniciado"
    base = MGW_URL.rstrip("/")
    print("[PW] [RRHH] Navegando a rrhh_fichaje.php (fichaje)...")
    await _page.goto(f"{base}/rrhh_fichaje.php", wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2.0)
    if on_screenshot:
        await _snap(on_screenshot, 0.0)
    print("[PW] [RRHH] rrhh_fichaje_navegar ✓")
    return "Sección de fichaje visible: todos los fichajes del negocio, con opción de fichaje manual (fecha y hora) además del fichaje en tiempo real con ubicación, foto y hora."