"""
mgw_browser.py
Integración con Mi Gestión Web via Playwright.
- Maneja sesión (login automático al inicio de la demo)
- Navega a cada sección durante la demo
- Crea datos de prueba cuando corresponde
"""
import asyncio
import random
import string
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config import MGW_URL, MGW_USER, MGW_PASSWORD

# ─── Estado global del browser ────────────────────────────────────────────────
_playwright = None
_browser: Browser | None = None
_context: BrowserContext | None = None
_page: Page | None = None
_session_active: bool = False


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _rand_str(n=4) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


async def _get_page() -> Page:
    """Devuelve la página activa. Lanza si no hay sesión."""
    if _page is None:
        raise RuntimeError("Browser no iniciado. Llamá a mgw_login() primero.")
    return _page


# ─── Ciclo de vida ────────────────────────────────────────────────────────────

async def mgw_start():
    """
    Inicia Playwright y abre el browser en modo no-headless
    (visible para el cliente en pantalla compartida).
    """
    global _playwright, _browser, _context, _page

    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=False,
        args=["--start-maximized", "--disable-infobars"],
    )
    _context = await _browser.new_context(no_viewport=True)
    _page = await _context.new_page()
    print("[MGW] Browser iniciado ✓")


async def mgw_login() -> bool:
    """
    Hace login en Mi Gestión Web y verifica que la sesión quedó activa.
    Retorna True si el login fue exitoso.
    """
    global _session_active

    page = await _get_page()

    try:
        await page.goto(MGW_URL, wait_until="domcontentloaded", timeout=20_000)

        # Completar formulario de login
        await page.fill('input[name="usuario"]', MGW_USER)
        await page.fill('input[name="password"]', MGW_PASSWORD)
        await page.press('input[name="password"]', "Enter")

        # Esperar redirección a home
        await page.wait_for_url("**/home.php", timeout=15_000)
        _session_active = True
        print(f"[MGW] Login OK como '{MGW_USER}' ✓")
        return True

    except Exception as e:
        print(f"[MGW] Error en login: {e}")
        _session_active = False
        return False


async def mgw_stop():
    """Cierra el browser al terminar la reunión."""
    global _playwright, _browser, _context, _page, _session_active

    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()

    _browser = None
    _context = None
    _page = None
    _session_active = False
    print("[MGW] Browser cerrado ✓")


# ─── Navegación por módulo ────────────────────────────────────────────────────
# Cada función navega a la pantalla correspondiente y retorna
# una descripción breve de lo que se ve (para el contexto de Malena).

ROUTES = {
    "home":              "/home.php",
    "caja":              "/caja.php",
    "clientes":          "/clientes.php",
    "proveedores":       "/compras.php",
    "ventas":            "/venta.php",
    "stock_existencia":  "/stock_existencia_2.php",
    "estadisticas":      "/estadisticas_ventas.php",
    "rrhh_personal":     "/rrhh_personal.php",
    "rrhh_fichaje":      "/rrhh_fichaje.php",
    "cierre_caja":       "/caja_cierre.php",
    "balanza":           "/balanza3.php?balanza=6",
    "tienda_web":        "/mitiendaweb.php",
    "configuracion_usuarios": "/configuracion_usuarios.php",
    "configuracion_productos": "/configuracion_productos.php",
    "herramientas_listas": "/herramientas_listas_de_precios.php",
    "gastos":            "/gastos.php",
    "produccion":        "/produccion.php",
    "balance":           "/balance.php",
    "novedades":         "/novedades.php",
}

# Mapeo de módulos de la demo → ruta a navegar
DEMO_MODULE_ROUTES = {
    "ACCESO":       None,            # No navega, Malena lo explica
    "USUARIOS":     "configuracion_usuarios",
    "PANTALLA INICIAL": "home",
    "BALANZA":      "balanza",
    "CAJA":         "caja",
    "FACTURACIÓN":  "ventas",
    "CLIENTES":     "clientes",
    "VENTAS":       "ventas",
    "CIERRES":      "cierre_caja",
    "PROVEEDORES":  "proveedores",
    "STOCK":        "stock_existencia",
    "ESTADÍSTICAS": "estadisticas",
    "RRHH":         "rrhh_personal",
    "TIENDA WEB":   "tienda_web",
}


async def navigate_to(section: str) -> bool:
    """
    Navega a una sección por nombre (clave de ROUTES o DEMO_MODULE_ROUTES).
    Retorna True si tuvo éxito.
    """
    if not _session_active:
        print("[MGW] No hay sesión activa, saltando navegación.")
        return False

    # Resolver la ruta
    route_key = DEMO_MODULE_ROUTES.get(section.upper(), section.lower())
    if route_key is None:
        # Módulo sin pantalla (ej: ACCESO)
        return True

    path = ROUTES.get(route_key)
    if not path:
        print(f"[MGW] Ruta no encontrada para: {section}")
        return False

    page = await _get_page()
    base = MGW_URL.rstrip("/")

    try:
        url = f"{base}{path}"
        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        print(f"[MGW] Navegó a: {url} ✓")
        return True
    except Exception as e:
        print(f"[MGW] Error navegando a {section}: {e}")
        return False


# ─── Creación de datos de prueba ──────────────────────────────────────────────

async def create_demo_client(nombre_negocio: str = "") -> dict | None:
    """
    Crea un cliente de prueba en el sistema.
    Retorna los datos creados o None si falló.
    """
    if not _session_active:
        return None

    page = await _get_page()
    base = MGW_URL.rstrip("/")

    # Datos del cliente de prueba
    sufijo = _rand_str(3)
    datos = {
        "nombre":   f"Demo {sufijo.upper()}",
        "telefono": f"11-{random.randint(1000,9999)}-{random.randint(1000,9999)}",
        "email":    f"demo.{sufijo}@prueba.com",
        "dni":      str(random.randint(20_000_000, 45_000_000)),
    }

    try:
        await page.goto(f"{base}/clientes.php", wait_until="domcontentloaded", timeout=15_000)

        # Abrir modal de nuevo cliente
        await page.click("#modal_nuevo_cliente_id", timeout=5_000)
        await asyncio.sleep(0.8)

        # Completar campos disponibles
        await _fill_if_exists(page, 'input[name="nombre"]',    datos["nombre"])
        await _fill_if_exists(page, 'input[name="dni"]',       datos["dni"])
        await _fill_if_exists(page, 'input[name="telefono"]',  datos["telefono"])
        await _fill_if_exists(page, 'input[name="email"]',     datos["email"])

        # Enviar formulario
        await page.click('button[type="submit"]', timeout=5_000)
        await asyncio.sleep(1.0)

        print(f"[MGW] Cliente de prueba creado: {datos['nombre']} ✓")
        return datos

    except Exception as e:
        print(f"[MGW] Error creando cliente demo: {e}")
        return None


async def _fill_if_exists(page: Page, selector: str, value: str):
    """Llena un campo si existe en la página, sin lanzar si no está."""
    try:
        locator = page.locator(selector).first
        if await locator.count() > 0:
            await locator.fill(value, timeout=3_000)
    except Exception:
        pass


# ─── API pública para bot.py ──────────────────────────────────────────────────

async def demo_navigate(module_name: str) -> bool:
    """
    Punto de entrada principal desde bot.py.
    Navega al módulo correspondiente durante la demo.
    """
    result = await navigate_to(module_name)
    return result


async def demo_create_sample_data(data_type: str, context: str = "") -> str:
    """
    Crea un dato de prueba según el tipo solicitado.
    Retorna una descripción de lo creado (para que Malena lo mencione).

    data_type: "cliente" | "proveedor" (extensible)
    """
    if data_type == "cliente":
        datos = await create_demo_client(context)
        if datos:
            return (
                f"cliente de prueba '{datos['nombre']}' con DNI {datos['dni']} "
                f"y teléfono {datos['telefono']}"
            )
    return ""