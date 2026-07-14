"""
session.py
Multi-tenant: BotSession (estado de una llamada) + SessionManager (ciclo de vida).

Separación de responsabilidades:
- CredentialPool (credentials_pool.py): administra SOLO credenciales.
- SessionManager: crea, registra, busca y destruye BotSession. Orquesta el pool y la
  cola de espera (PendingQueue). Toda la lógica de asignación/cola/limpieza vive acá.
- BotSession: SOLO estado de una sesión individual (+ su canal hacia la webpage). No
  contiene lógica de pool ni de cola.
"""
import asyncio
import time
import uuid
from collections import deque

from config import (
    MGW_CREDENTIALS, PENDING_QUEUE_MAX, CAMPUS_FOCUS_MAP, TRAINING_SYSTEM_PROMPT,
    SESSION_INACTIVITY_TIMEOUT_S, SESSION_MAX_LIFETIME_S, SESSION_WATCHDOG_INTERVAL_S,
)
from credentials_pool import CredentialPool, CredentialSlot
from mgw_session import MgwSession
import recall


# ── Estados ───────────────────────────────────────────────────────────────────
STATE_WAITING = "waiting"
STATE_RUNNING = "running"
STATE_CLOSING = "closing"

# Gracia tras desconectarse el WebSocket de audio antes de destruir la sesión.
# Recall puede reconectar el endpoint de audio durante una misma llamada; si
# tearíamos de una, una reconexión transitoria mataría la sesión y liberaría la
# credencial de más. Si reconecta dentro de esta ventana, se cancela el teardown.
AUDIO_RECONNECT_GRACE_S = 20.0

# Estados de Recall en los que el bot YA no está en la llamada (se fue, terminó la
# reunión, lo sacaron, o error fatal). Cuando el bot llega a uno de estos, la sesión
# debe liberarse sí o sí: Recall NO siempre cierra el WebSocket de audio al irse el
# bot, así que el schedule_teardown por cierre de WS no alcanza y la credencial queda
# ocupada para siempre. El watchdog sondea el estado y cierra al detectar uno de estos.
RECALL_TERMINAL_CODES = {"call_ended", "done", "fatal"}

# Tope por paso del teardown. Si el cierre del bridge/Playwright o el leave_call de
# Recall se cuelgan (Chromium wedged, WS que no responde), NO deben bloquear la
# liberación de la credencial: cada paso se acota con este timeout y el teardown
# sigue igual hasta pool.release(). Sin esto, un close() colgado deja la credencial
# busy para siempre y, como el estado ya quedó en CLOSING, ni el DELETE la rescata.
TEARDOWN_STEP_TIMEOUT_S = 15.0


# ── Logging estructurado por sid ──────────────────────────────────────────────
class SidLogger:
    """Antepone [sid=...] a cada línea para poder seguir una llamada entre varias."""
    def __init__(self, sid: str):
        self.sid = sid

    def __call__(self, msg: str):
        print(f"[sid={self.sid}] {msg}")

    # aliases por comodidad
    def info(self, msg: str):    self(msg)
    def warning(self, msg: str): self(f"WARN {msg}")
    def error(self, msg: str):   self(f"ERROR {msg}")


# ── Construcción del prompt con foco del campus ───────────────────────────────
def build_system_prompt(module: str, field: str, user_name: str) -> str:
    """
    Devuelve el prompt de sistema de la sesión: el guion de capacitación + un
    preámbulo que fija el foco pedido desde el campus (module / field / user_name).
    El campus ya eligió, así que Malena NO tiene que preguntar qué módulo ver.
    """
    lines = ["", "", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
             "FOCO DE ESTA SESIÓN (viene del campus — OBLIGATORIO)",
             "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]

    if user_name:
        lines.append(f"- El cliente se llama {user_name}. Saludalo por su nombre al presentarte.")

    module = (module or "").strip().lower()
    field  = (field or "").strip()

    if field:
        # Sección puntual → MODO SECCIÓN DIRECTA
        lines.append(
            f"- El cliente ya eligió ver la sección '{field}' desde el campus. "
            f"NO le preguntes qué módulo o sección quiere ver. "
            f"Después de saludar, pasá DIRECTAMENTE a MODO SECCIÓN DIRECTA con la sección '{field}' "
            f"(navegá a esa sección con su tool y seguí su guion)."
        )
        lines.append(
            f"- Explicás ÚNICAMENTE la sección '{field}'. Cuando termines sus pasos, IGNORÁ cualquier "
            f"'CONTINUACIÓN OBLIGATORIA DEL MÓDULO' del guion (no sigas con otra sección ni con el resto "
            f"del módulo por tu cuenta). Al terminar: preguntá si le quedó alguna duda, respondela si hace "
            f"falta, y después despedite y llamá finalizar_capacitacion(). Sólo hacés otra sección si el "
            f"cliente la pide explícitamente."
        )
    elif module in CAMPUS_FOCUS_MAP and CAMPUS_FOCUS_MAP[module]["kind"] == "module":
        info = CAMPUS_FOCUS_MAP[module]
        lines.append(
            f"- El cliente ya eligió el {info['label']} desde el campus. "
            f"NO le preguntes qué módulo quiere ver. "
            f"Después de saludar, arrancá DIRECTAMENTE con el guion del MÓDULO DE CAPACITACIÓN {info['n']}."
        )
    else:
        # Sin foco explícito → comportamiento normal (preguntar).
        lines.append("- No hay foco predefinido: seguí el flujo normal de saludo y selección de módulo.")

    return TRAINING_SYSTEM_PROMPT + "\n".join(lines)


# ── BotSession: estado de una sola llamada + canal a su webpage ───────────────
class BotSession:
    def __init__(self, sid: str, meeting_url: str, bot_name: str,
                 module: str, field: str, user_name: str):
        self.sid          = sid
        self.meeting_url  = meeting_url
        self.bot_name     = bot_name
        self.focus        = {"module": module, "field": field, "user_name": user_name}
        self.system_prompt = build_system_prompt(module, field, user_name)
        self.log          = SidLogger(sid)
        self.created_at   = time.time()
        # Última señal de actividad (audio humano). El watchdog cierra la sesión si
        # queda inactiva demasiado tiempo. bot.py llama a touch() por cada frame humano.
        self.last_activity = time.time()

        self.state: str            = STATE_WAITING
        self.bot_id: str | None    = None
        self.creds: CredentialSlot | None = None
        self.mgw: MgwSession | None       = None

        # Canal hacia la webpage del agente (Recall Output Media)
        self.agent_ws_list: list = []

        # Puente Realtime + hook de cierre de Playwright (los setea bot.py al conectar /audio)
        self.bridge = None
        self.pw_close = None   # async callable | None

        # Eventos de sincronización de la demo (antes globales en bot.py)
        self.events = {
            "acceso_login_done": asyncio.Event(),
            "fase1_complete":    asyncio.Event(),
            "fase2_complete":    asyncio.Event(),
            "fase2_press_f8":    asyncio.Event(),
            "audio_done":        asyncio.Event(),
        }
        self.fase2_task_created = False

    def touch(self):
        """Marca actividad (audio humano). Reinicia el reloj de inactividad."""
        self.last_activity = time.time()

    @property
    def sistema(self) -> str | None:
        return self.creds.alias if self.creds else None

    # ── Canal hacia la webpage ───────────────────────────────────────────────
    async def send_to_agent(self, msg: dict):
        if not self.agent_ws_list:
            return
        dead = []
        for ws in list(self.agent_ws_list):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                self.agent_ws_list.remove(ws)
            except ValueError:
                pass

    async def send_navigate(self, path: str):
        await self.send_to_agent({"type": "navigate", "path": path})

    async def send_reset(self):
        await self.send_to_agent({"type": "reset"})

    async def send_stop_audio(self):
        await self.send_to_agent({"type": "stop_audio"})

    async def send_logged_in(self):
        await self.send_to_agent({"type": "logged_in"})

    async def on_screenshot(self, b64: str):
        await self.send_to_agent({"type": "screenshot", "data": b64})

    async def on_screenshot_end(self):
        await self.send_to_agent({"type": "screenshot_end"})

    # ── Cierre de recursos locales de la sesión (idempotente) ────────────────
    async def close(self):
        """Cierra bridge + Playwright de ESTA sesión. Cada paso aislado."""
        if self.bridge is not None:
            try:
                await self.bridge.close()
            except Exception as e:
                self.log.error(f"cerrando bridge: {e}")
            self.bridge = None
        if self.pw_close is not None:
            try:
                await self.pw_close()
            except Exception as e:
                self.log.error(f"cerrando Playwright: {e}")
            self.pw_close = None

    def to_dict(self) -> dict:
        return {
            "sid":         self.sid,
            "state":       self.state,
            "bot_id":      self.bot_id,
            "sistema":     self.sistema,
            "focus":       self.focus,
            "meeting_url": self.meeting_url,
            "agent_ws":    len(self.agent_ws_list),
            "uptime_s":    round(time.time() - self.created_at, 1),
        }


# ── SessionManager: ciclo de vida + pool + cola ───────────────────────────────
class SessionManager:
    def __init__(self):
        self.pool = CredentialPool(MGW_CREDENTIALS)
        self._sessions: dict[str, BotSession] = {}
        self._queue: deque[str] = deque()   # sids en WAITING, en orden de llegada
        self._lock = asyncio.Lock()          # protege registro + cola
        self._pending_teardowns: dict[str, asyncio.Task] = {}  # teardown con gracia
        self._watchdogs: dict[str, asyncio.Task] = {}          # inactividad / tope de vida

    # ── Consultas ────────────────────────────────────────────────────────────
    def get(self, sid: str) -> BotSession | None:
        return self._sessions.get(sid)

    def list(self) -> list[dict]:
        return [s.to_dict() for s in self._sessions.values()]

    def pool_status(self) -> dict:
        return {
            "slots": self.pool.status(),
            "free":  self.pool.free_count(),
            "queue_len": len(self._queue),
            "queue_max": PENDING_QUEUE_MAX,
        }

    # ── Creación ─────────────────────────────────────────────────────────────
    async def create(self, meeting_url: str, module: str = "", field: str = "",
                     user_name: str = "", bot_name: str = recall.BOT_NAME) -> dict:
        """
        Crea una sesión. Si hay credencial libre, arranca el bot (RUNNING). Si no,
        y la cola está habilitada y con espacio, la deja en WAITING. Si la cola está
        deshabilitada/llena, lanza RuntimeError (el endpoint responde 503).
        """
        sid = uuid.uuid4().hex[:8]
        session = BotSession(sid, meeting_url, bot_name, module, field, user_name)

        async with self._lock:
            self._sessions[sid] = session
            slot = await self.pool.acquire(sid)
            if slot is None:
                # Sin credenciales libres → cola de espera
                if PENDING_QUEUE_MAX <= 0 or len(self._queue) >= PENDING_QUEUE_MAX:
                    self._sessions.pop(sid, None)
                    raise RuntimeError("sin sistemas disponibles y la cola está deshabilitada/llena")
                session.state = STATE_WAITING
                self._queue.append(sid)
                position = len(self._queue)
                session.log.info(f"WAITING en cola (posición {position})")
                return {"status": STATE_WAITING, "sid": sid, "position": position}

        # Hay credencial → arrancar (fuera del lock: login + recall son I/O)
        session.creds = slot
        try:
            await self._start(session)
        except Exception as e:
            session.log.error(f"fallo al arrancar: {e}")
            await self.teardown(sid, reason=f"start-failed: {e}")
            raise
        return {
            "status":  STATE_RUNNING,
            "sid":     sid,
            "bot_id":  session.bot_id,
            "sistema": session.sistema,
        }

    async def _start(self, session: BotSession):
        """Login MGW + create_bot. La credencial ya está asignada en session.creds."""
        slot = session.creds
        loop = asyncio.get_event_loop()

        session.mgw = MgwSession(slot.empresa, slot.usuario, slot.password, log=session.log)
        ok = await loop.run_in_executor(None, session.mgw.login)
        if ok:
            session.log.info(f"Login MGW OK ({slot.alias}) ✓")
        else:
            session.log.warning(f"Login MGW falló ({slot.alias}) — la demo sigue sin sistema")

        data = await loop.run_in_executor(
            None, lambda: recall.create_bot(session.meeting_url, session.bot_name, sid=session.sid)
        )
        session.bot_id = data["id"]
        session.state = STATE_RUNNING
        session.touch()   # arranca el reloj de inactividad recién al entrar en llamada
        session.log.info(f"Bot Recall creado: {session.bot_id} (sistema {slot.alias})")
        self._start_watchdog(session)

    # ── Destrucción / cleanup garantizado ────────────────────────────────────
    async def _bounded(self, coro, timeout: float, label: str, log):
        """Corre `coro` con un tope de tiempo REAL.

        A diferencia de `asyncio.wait_for`, si la corrutina no responde a la
        cancelación (caso típico: el shutdown de Playwright/Chromium colgado en
        un pipe muerto), NO nos bloquea: la abandonamos y seguimos. `wait_for`
        espera a que la cancelación termine, así que un cleanup colgado lo deja
        pegado para siempre pese al timeout — que es exactamente lo que dejaba
        credenciales ocupadas. Un browser zombie es mucho mejor que una
        credencial atascada.
        """
        task = asyncio.ensure_future(coro)
        _, pending = await asyncio.wait({task}, timeout=timeout)
        if pending:
            log.error(f"{label}: no terminó en {timeout}s — lo abandono y sigo")
            task.cancel()  # pedimos cancelación pero NO esperamos a que termine
            return
        exc = task.exception()
        if exc:
            log.error(f"{label}: {exc}")

    async def _release_and_unregister(self, sid: str, session: "BotSession"):
        """Libera la credencial y saca la sesión del registro/cola. Idempotente:
        se puede llamar más de una vez sin romper (pool.release lo es, y pop/remove
        toleran ausencia). Es la parte del teardown que SIEMPRE tiene que correr."""
        creds, session.creds = session.creds, None
        try:
            await self.pool.release(creds)
        except Exception as e:
            session.log.error(f"pool.release: {e}")
        async with self._lock:
            self._sessions.pop(sid, None)
            try:
                self._queue.remove(sid)
            except ValueError:
                pass

    async def teardown(self, sid: str, reason: str = ""):
        """
        Limpieza idempotente. Se llama SIEMPRE: cierre normal, DELETE, desconexión de
        Recall o excepción. Libera Realtime, Playwright, bot de Recall y credencial,
        y saca la sesión del registro.

        Garantía dura: la credencial SIEMPRE se libera, aunque el cierre de
        Playwright/bridge/Recall se cuelgue (ver `_bounded`). Y si la sesión ya
        está en CLOSING (un teardown previo que quedó colgado), reintentamos la
        liberación igual — así `DELETE /sessions/{sid}` puede rescatar un slot
        pegado en lugar de cortar sin hacer nada.
        """
        session = self._sessions.get(sid)
        if session is None:
            return
        if session.state == STATE_CLOSING:
            # Un teardown anterior arrancó pero pudo colgarse antes de liberar la
            # credencial. Forzamos la liberación (idempotente) para poder rescatar
            # el slot desde DELETE en vez de dejarlo ocupado para siempre.
            session.log.info(
                f"teardown re-entrante en CLOSING (motivo: {reason or 'n/a'}) — forzando liberación"
            )
            await self._release_and_unregister(sid, session)
            return
        session.state = STATE_CLOSING
        self._pending_teardowns.pop(sid, None)
        self._cancel_watchdog(sid)
        session.log.info(f"teardown (motivo: {reason or 'n/a'})")

        # 1+2. Recursos locales de la sesión (bridge Realtime + browser context),
        # con tope de tiempo REAL: si se cuelgan, los abandonamos y seguimos.
        await self._bounded(session.close(), TEARDOWN_STEP_TIMEOUT_S, "session.close", session.log)

        # 3. Sacar el bot de Recall (también con tope real).
        if session.bot_id:
            loop = asyncio.get_event_loop()
            await self._bounded(
                loop.run_in_executor(None, lambda: recall.leave_call(session.bot_id)),
                TEARDOWN_STEP_TIMEOUT_S, "leave_call", session.log,
            )

        # 4+5. Liberar credencial + sacar del registro — SIEMPRE se llega acá.
        await self._release_and_unregister(sid, session)

        # 6. Arrancar el próximo WAITING si se liberó una credencial
        await self._start_next_pending()

    # ── Teardown con gracia (reconexión de audio de Recall) ──────────────────
    def cancel_scheduled_teardown(self, sid: str):
        """Cancela un teardown pendiente (p.ej. Recall reconectó el audio)."""
        task = self._pending_teardowns.pop(sid, None)
        if task and not task.done():
            task.cancel()

    def schedule_teardown(self, sid: str, delay: float = AUDIO_RECONNECT_GRACE_S,
                          reason: str = "audio-disconnect"):
        """Agenda el teardown tras `delay` s, salvo que se cancele antes (reconexión)."""
        self.cancel_scheduled_teardown(sid)
        session = self._sessions.get(sid)
        if session is None:
            return

        async def _later():
            try:
                await asyncio.sleep(delay)
                self._pending_teardowns.pop(sid, None)
                await self.teardown(sid, reason=reason)
            except asyncio.CancelledError:
                pass

        self._pending_teardowns[sid] = asyncio.ensure_future(_later())
        session.log.info(f"teardown agendado en {delay}s (motivo: {reason})")

    # ── Watchdog de inactividad / tope de duración ───────────────────────────
    def _cancel_watchdog(self, sid: str):
        task = self._watchdogs.pop(sid, None)
        # NO cancelar la task actual: el watchdog llama a teardown, y teardown llama
        # acá — cancelar la propia task en curso abortaría el teardown en su primer
        # `await` (justo tras poner CLOSING, ANTES de liberar la credencial), dejando
        # la sesión pegada en `closing` para siempre. Al volver de teardown el watchdog
        # sale solo (su loop ve la sesión ya removida).
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _start_watchdog(self, session: "BotSession"):
        """
        Arranca el watchdog de una sesión RUNNING. Cierra la sesión (y libera su
        credencial) por tres vías: (1) el bot dejó la llamada en Recall (se fue / lo
        sacaron / terminó) — sondeo de estado; (2) inactividad de audio humano; (3)
        tope de duración. Corre SIEMPRE, porque (1) es la red de seguridad principal:
        Recall no siempre cierra el WS de audio al irse el bot, y sin este sondeo la
        sesión queda RUNNING y la credencial ocupada para siempre.
        """
        self._cancel_watchdog(session.sid)
        self._watchdogs[session.sid] = asyncio.ensure_future(self._watchdog(session.sid))

    async def _watchdog(self, sid: str):
        interval = max(5, SESSION_WATCHDOG_INTERVAL_S)
        try:
            while True:
                await asyncio.sleep(interval)
                session = self._sessions.get(sid)
                if session is None or session.state != STATE_RUNNING:
                    return

                # (1) ¿El bot dejó la llamada en Recall? (se fue / lo sacaron / terminó).
                # Es la causa real del pool que no se libera: Recall no siempre cierra el
                # WS de audio, así que sin esto la sesión queda colgada RUNNING.
                if session.bot_id:
                    try:
                        loop = asyncio.get_event_loop()
                        data = await asyncio.wait_for(
                            loop.run_in_executor(None, lambda: recall.get_bot_status(session.bot_id)),
                            timeout=12,
                        )
                        changes = data.get("status_changes") or []
                        code = changes[-1].get("code") if changes else None
                        if code in RECALL_TERMINAL_CODES:
                            session.log.info(f"watchdog: bot Recall en estado '{code}' — cerrando")
                            await self.teardown(sid, reason=f"recall-{code}")
                            return
                    except Exception as e:
                        session.log.error(f"watchdog: get_bot_status: {e}")

                now = time.time()
                if SESSION_MAX_LIFETIME_S > 0 and now - session.created_at > SESSION_MAX_LIFETIME_S:
                    session.log.info(
                        f"watchdog: tope de duración ({SESSION_MAX_LIFETIME_S}s) alcanzado — cerrando"
                    )
                    await self.teardown(sid, reason="max-lifetime")
                    return
                if SESSION_INACTIVITY_TIMEOUT_S > 0 and now - session.last_activity > SESSION_INACTIVITY_TIMEOUT_S:
                    idle = round(now - session.last_activity)
                    session.log.info(
                        f"watchdog: inactividad {idle}s (límite {SESSION_INACTIVITY_TIMEOUT_S}s) — cerrando"
                    )
                    await self.teardown(sid, reason="inactivity")
                    return
        except asyncio.CancelledError:
            pass

    async def _start_next_pending(self):
        """Toma el próximo WAITING de la cola y lo arranca si hay credencial libre."""
        async with self._lock:
            if not self._queue:
                return
            slot = await self.pool.acquire()
            if slot is None:
                return
            sid = self._queue.popleft()
            session = self._sessions.get(sid)
            if session is None:
                # La sesión encolada ya no existe (teardown) — devolvé el slot e intentá otra
                await self.pool.release(slot)
                # reintento fuera del lock para no recursar con lock tomado
                asyncio.get_event_loop().call_soon(
                    lambda: asyncio.ensure_future(self._start_next_pending())
                )
                return
            session.creds = slot

        try:
            session.log.info("arrancando desde la cola (se liberó una credencial)")
            await self._start(session)
        except Exception as e:
            session.log.error(f"fallo arrancando desde la cola: {e}")
            await self.teardown(session.sid, reason=f"pending-start-failed: {e}")


# Singleton global del proceso
session_manager = SessionManager()
