"""
mgw_session.py
Maneja la sesión de Mi Gestión Web server-side.
"""
import requests
from config import MGW_URL, MGW_USER, MGW_EMPRESA, MGW_PASSWORD

_session: requests.Session | None = None
_cookies: dict = {}


def mgw_login() -> bool:
    global _session, _cookies

    _session = requests.Session()

    try:
        # GET inicial para obtener cookies de sesión vacías
        _session.get(MGW_URL, timeout=10)

        # POST de login con los campos correctos
        resp = _session.post(
            MGW_URL.rstrip("/") + "/index.php",
            data={
                "empresa":    MGW_EMPRESA,
                "usuario":    MGW_USER,
                "contrasena": MGW_PASSWORD,
                "btnlogin":   "",
            },
            timeout=15,
            allow_redirects=True,
        )

        _cookies = dict(_session.cookies)
        print(f"[MGW Session] URL final post-login: {resp.url}")
        print(f"[MGW Session] Status: {resp.status_code}")
        print(f"[MGW Session] Cookies: {list(_cookies.keys())}")
        print(f"[MGW Session] Response snippet: {resp.text[:400]}")

        if "home.php" in resp.url:
            print("[MGW Session] Login OK ✓")
            return True
        elif "index.php" in resp.url or resp.url == MGW_URL.rstrip("/") + "/":
            print("[MGW Session] Login falló — redirigió al login de nuevo")
            return False
        elif _cookies:
            print("[MGW Session] URL inesperada pero hay cookies, asumiendo OK")
            return True
        else:
            print("[MGW Session] Login falló sin cookies")
            return False

    except Exception as e:
        print(f"[MGW Session] Error en login: {e}")
        return False


def mgw_get(path: str) -> requests.Response | None:
    if _session is None:
        return None
    base = MGW_URL.rstrip("/")
    url  = f"{base}{path}"
    try:
        return _session.get(url, timeout=15, allow_redirects=True)
    except Exception as e:
        print(f"[MGW Session] Error GET {path}: {e}")
        return None


def get_cookies() -> dict:
    return _cookies


def is_logged_in() -> bool:
    return _session is not None