"""
mgw_session.py
Sesión server-side de Mi Gestión Web, por instancia (multi-tenant).

Antes esto era un único `requests.Session` global. Ahora cada BotSession tiene su
propia MgwSession con su credencial del pool, para que N llamadas concurrentes no se
pisen la sesión entre sí. El proxy /mgw-proxy usa la MgwSession de su sid.
"""
import requests
from config import MGW_URL


class MgwSession:
    """Sesión autenticada contra MGW para UNA credencial del pool."""

    def __init__(self, empresa: str, usuario: str, password: str, log=None):
        self.empresa = empresa
        self.usuario = usuario
        self.password = password
        self._session: requests.Session | None = None
        self._cookies: dict = {}
        # log(msg) opcional — inyecta el sid. Si no viene, cae a print.
        self._log = log or (lambda msg: print(msg))

    def login(self) -> bool:
        self._session = requests.Session()
        try:
            # GET inicial para obtener cookies de sesión vacías
            self._session.get(MGW_URL, timeout=10)

            resp = self._session.post(
                MGW_URL.rstrip("/") + "/index.php",
                data={
                    "empresa":    self.empresa,
                    "usuario":    self.usuario,
                    "contrasena": self.password,
                    "btnlogin":   "",
                },
                timeout=15,
                allow_redirects=True,
            )

            self._cookies = dict(self._session.cookies)
            self._log(f"[MGW Session] {self.empresa}: URL final post-login: {resp.url}")
            self._log(f"[MGW Session] {self.empresa}: Status {resp.status_code}, cookies {list(self._cookies.keys())}")

            if "home.php" in resp.url:
                self._log(f"[MGW Session] {self.empresa}: Login OK ✓")
                return True
            elif "index.php" in resp.url or resp.url == MGW_URL.rstrip("/") + "/":
                self._log(f"[MGW Session] {self.empresa}: Login falló — redirigió al login")
                return False
            elif self._cookies:
                self._log(f"[MGW Session] {self.empresa}: URL inesperada pero hay cookies, asumiendo OK")
                return True
            else:
                self._log(f"[MGW Session] {self.empresa}: Login falló sin cookies")
                return False

        except Exception as e:
            self._log(f"[MGW Session] {self.empresa}: Error en login: {e}")
            return False

    def get(self, path: str) -> requests.Response | None:
        if self._session is None:
            return None
        url = f"{MGW_URL.rstrip('/')}{path}"
        try:
            return self._session.get(url, timeout=15, allow_redirects=True)
        except Exception as e:
            self._log(f"[MGW Session] {self.empresa}: Error GET {path}: {e}")
            return None

    def post(self, path: str, data: dict | None = None, raw_body: bytes | None = None,
             content_type: str | None = None) -> requests.Response | None:
        if self._session is None:
            return None
        url = f"{MGW_URL.rstrip('/')}{path}"
        try:
            headers = {}
            if content_type:
                headers["Content-Type"] = content_type
            if raw_body is not None:
                return self._session.post(url, data=raw_body, headers=headers, timeout=15, allow_redirects=True)
            return self._session.post(url, data=data or {}, timeout=15, allow_redirects=True)
        except Exception as e:
            self._log(f"[MGW Session] {self.empresa}: Error POST {path}: {e}")
            return None

    def get_cookies(self) -> dict:
        return self._cookies

    def is_logged_in(self) -> bool:
        return self._session is not None
