"""
main.py
Servidor FastAPI (multi-tenant: cada llamada = una sesión identificada por `sid`).

- POST   /bot/create        → crea una sesión y manda el bot (module/field/user_name)
- GET    /sessions          → lista de sesiones activas (admin)
- GET    /sessions/{sid}    → detalle de una sesión (admin)
- DELETE /sessions/{sid}    → fuerza el cierre + cleanup de una sesión (admin)
- GET    /pool/status       → estado del pool de credenciales + cola (admin)
- GET    /agent             → webpage que Recall usa como Output Media (?sid=...)
- GET    /mgw-proxy/{path}  → proxy autenticado a MGW (sesión resuelta por cookie sid)
- WS     /agent-ws          → canal servidor ↔ webpage del agente (?sid=...)
- WS     /audio             → Recall.ai manda el audio de la reunión (?sid=...)
"""
import asyncio
import logging
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, JSONResponse
from pydantic import BaseModel

# Silenciar logs HTTP de uvicorn (proxy y assets) — solo mostrar errores
logging.getLogger("uvicorn.access").setLevel(logging.ERROR)

from bot import handle_recall_audio, set_agent_websocket, remove_agent_websocket, on_agent_audio_done
from session import session_manager
import recall

app = FastAPI(title="Malena Bot - Mi Gestión Web")


class BotRequest(BaseModel):
    meeting_url: str
    module:    str = ""
    field:     str = ""
    user_name: str = ""
    bot_name:  str = "Malena - Mi Gestión Web"


def _resolve_sid(request_or_ws) -> str | None:
    """Extrae el sid de los query params."""
    return request_or_ws.query_params.get("sid")


# ── Creación de sesión ────────────────────────────────────────────────────────

@app.post("/bot/create")
async def create(req: BotRequest):
    """Crea una sesión y manda el bot a la reunión (o la encola si no hay sistemas)."""
    try:
        result = await session_manager.create(
            meeting_url=req.meeting_url,
            module=req.module,
            field=req.field,
            user_name=req.user_name,
            bot_name=req.bot_name,
        )
        return result
    except RuntimeError as e:
        return JSONResponse({"status": "rejected", "error": str(e)}, status_code=503)
    except Exception as e:
        print(f"[Main] Error creando sesión: {e}")
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.get("/sessions")
async def sessions():
    return {"sessions": session_manager.list()}


@app.get("/sessions/{sid}")
async def session_detail(sid: str):
    s = session_manager.get(sid)
    if s is None:
        return JSONResponse({"error": "sesión no encontrada"}, status_code=404)
    return s.to_dict()


@app.delete("/sessions/{sid}")
async def session_delete(sid: str):
    s = session_manager.get(sid)
    if s is None:
        return JSONResponse({"error": "sesión no encontrada"}, status_code=404)
    await session_manager.teardown(sid, reason="delete-endpoint")
    return {"status": "closed", "sid": sid}


@app.get("/pool/status")
async def pool_status():
    return session_manager.pool_status()


@app.get("/bot/status")
async def bot_status(sid: str | None = None):
    """Estado del bot de una sesión (o lista de sesiones si no se pasa sid)."""
    if sid is None:
        return {"sessions": session_manager.list()}
    s = session_manager.get(sid)
    if s is None:
        return JSONResponse({"error": "sesión no encontrada"}, status_code=404)
    if not s.bot_id:
        return {"sid": sid, "state": s.state, "status": []}
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: recall.get_bot_status(s.bot_id))
    return {"sid": sid, "bot_id": s.bot_id, "status": data.get("status_changes", [])}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("app/static/malena2.png", media_type="image/png")


@app.get("/avatar.png", include_in_schema=False)
async def avatar():
    return FileResponse("app/static/malena2.png", media_type="image/png")


@app.get("/agent")
async def agent_page():
    """Sirve la webpage que Recall.ai corre como Output Media. El sid viaja en ?sid=."""
    return FileResponse("app/static/agent.html", media_type="text/html")


# ── Proxy MGW (sesión resuelta por cookie `sid`, seteada por agent.html) ───────

def _proxy_session(request: Request):
    """La webpage del agente setea la cookie sid; el proxy la usa para elegir la MgwSession."""
    sid = request.cookies.get("sid") or request.query_params.get("sid")
    if not sid:
        return None
    return session_manager.get(sid)


@app.post("/mgw-proxy/{path:path}")
async def mgw_proxy_post(path: str, request: Request):
    """Reenvía POST del iframe (AJAX) a MGW con la sesión autenticada de ese sid."""
    session = _proxy_session(request)
    if session is None or session.mgw is None or not session.mgw.is_logged_in():
        return HTMLResponse("<h3>Sin sesión MGW</h3>", status_code=503)

    query = str(request.url.query)
    full_path = f"/{path}"
    if query:
        full_path += f"?{query}"

    body = await request.body()
    content_type = request.headers.get("content-type", "")

    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(
        None, lambda: session.mgw.post(full_path, None, body, content_type or None)
    )

    if resp is None:
        return Response(status_code=502)

    resp_content_type = resp.headers.get("content-type", "application/json")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp_content_type,
    )


@app.get("/mgw-proxy/{path:path}")
async def mgw_proxy(path: str, request: Request):
    """
    Proxy autenticado a MGW para el sid de la cookie.
    - Sirve páginas con las cookies de sesión inyectadas en el browser
    - Reescribe URLs relativas y absolutas para pasar por el proxy
    - Intercepta fetch/XHR via JS para que también pasen por el proxy
    """
    session = _proxy_session(request)
    if session is None or session.mgw is None or not session.mgw.is_logged_in():
        return HTMLResponse("<h3>Sin sesión MGW</h3>", status_code=503)

    query = str(request.url.query)
    full_path = f"/{path}"
    if query:
        full_path += f"?{query}"

    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(None, lambda: session.mgw.get(full_path))

    if resp is None:
        return HTMLResponse("<h3>Error conectando a MGW</h3>", status_code=502)

    content_type = resp.headers.get("content-type", "text/html")

    if "text/html" in content_type:
        html = resp.text

        # Reescribir URLs absolutas de MGW → proxy
        html = html.replace(
            "https://www.migestionweb.pro/", "/mgw-proxy/"
        ).replace(
            "http://www.migestionweb.pro/", "/mgw-proxy/"
        )

        # Base tag + interceptor de XHR/fetch para que requests AJAX pasen por el proxy
        inject = """<base href="/mgw-proxy/">
<script>
(function() {
  var _fetch = window.fetch;
  window.fetch = function(url, opts) {
    if (typeof url === 'string') {
      if (url.startsWith('https://www.migestionweb.pro/')) {
        url = url.replace('https://www.migestionweb.pro/', '/mgw-proxy/');
      } else if (url.startsWith('/') && !url.startsWith('/mgw-proxy/')) {
        url = '/mgw-proxy' + url;
      }
    }
    return _fetch(url, opts);
  };
  var _open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    if (typeof url === 'string') {
      if (url.startsWith('https://www.migestionweb.pro/')) {
        url = url.replace('https://www.migestionweb.pro/', '/mgw-proxy/');
      } else if (url.startsWith('/') && !url.startsWith('/mgw-proxy/')) {
        url = '/mgw-proxy' + url;
      }
    }
    return _open.apply(this, [method, url].concat(Array.prototype.slice.call(arguments, 2)));
  };
})();
</script>
"""

        if "<head>" in html:
            html = html.replace("<head>", "<head>" + inject, 1)
        elif "<HEAD>" in html:
            html = html.replace("<HEAD>", "<HEAD>" + inject, 1)
        else:
            html = inject + html

        cookies = session.mgw.get_cookies()
        response = HTMLResponse(content=html, status_code=resp.status_code)
        for name, value in cookies.items():
            response.headers.append(
                "Set-Cookie",
                f"{name}={value}; Path=/; SameSite=Lax"
            )
        return response

    # CSS, JS, imágenes — pasar directo
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=content_type,
    )


# ── WebSockets ────────────────────────────────────────────────────────────────

@app.websocket("/agent-ws")
async def agent_ws(websocket: WebSocket):
    """WebSocket entre el servidor y la webpage del agente (ruteado por sid)."""
    await websocket.accept()
    sid = _resolve_sid(websocket)
    session = session_manager.get(sid) if sid else None
    if session is None:
        print(f"[AGENT WS] sid inválido o sesión inexistente: {sid}")
        await websocket.close(code=1008)
        return

    set_agent_websocket(websocket, session)

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "audio_done":
                on_agent_audio_done(session)
    except WebSocketDisconnect:
        session.log.info("[AGENT WS] Webpage desconectada")
    except Exception as e:
        session.log.error(f"[AGENT WS] {e}")
    finally:
        remove_agent_websocket(websocket, session)


@app.websocket("/audio")
async def audio_ws(websocket: WebSocket):
    """Recall.ai conecta acá para enviar el audio de la reunión (ruteado por sid)."""
    await websocket.accept()
    sid = _resolve_sid(websocket)
    session = session_manager.get(sid) if sid else None
    if session is None:
        print(f"[AUDIO WS] sid inválido o sesión inexistente: {sid}")
        await websocket.close(code=1008)
        return

    # Si había un teardown agendado (audio se desconectó y reconectó), cancelarlo.
    session_manager.cancel_scheduled_teardown(sid)

    try:
        await handle_recall_audio(websocket, session)
    except WebSocketDisconnect:
        session.log.info("[FastAPI] WebSocket de audio desconectado")
    finally:
        # Gracia por si Recall reconecta; si no vuelve, se destruye la sesión.
        session_manager.schedule_teardown(sid, reason="audio-ws-closed")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
