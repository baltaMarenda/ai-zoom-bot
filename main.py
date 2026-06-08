"""
main.py
Servidor FastAPI.
- POST /bot/create      → manda el bot a una reunión
- GET  /bot/status      → estado del bot
- GET  /agent           → sirve la webpage que Recall usa para Output Media
- GET  /mgw-proxy/{path} → proxy autenticado a Mi Gestión Web
- WS   /agent-ws        → canal entre el servidor y la webpage del bot
- WS   /audio           → Recall.ai conecta acá para mandarte el audio
"""
import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from recall import create_bot, get_bot_status, current_bot_id
from bot import handle_recall_audio, set_agent_websocket, remove_agent_websocket, on_agent_audio_done
from ai import reset_conversation
from mgw_session import mgw_login, mgw_get, mgw_post, is_logged_in, get_cookies

app = FastAPI(title="Malena Bot - Mi Gestión Web")


class BotRequest(BaseModel):
    meeting_url: str
    bot_name: str = "Malena - Mi Gestión Web"


@app.post("/bot/create")
async def create(req: BotRequest):
    """Manda el bot a una reunión de Zoom/Meet."""
    reset_conversation()

    # Login en MGW al inicio de cada demo
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, mgw_login)
    if ok:
        print("[Main] Login MGW exitoso ✓")
    else:
        print("[Main] Login MGW falló — la demo continúa sin sistema")

    data = create_bot(req.meeting_url, req.bot_name)
    return {"status": "ok", "bot_id": data["id"]}


@app.get("/bot/status")
async def status():
    if not current_bot_id:
        return {"status": "no_bot"}
    data = get_bot_status()
    return {"bot_id": current_bot_id, "status": data.get("status_changes", [])}


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
    """
    Sirve la webpage que Recall.ai corre internamente como Output Media.
    """
    return FileResponse("app/static/agent.html", media_type="text/html")


@app.post("/mgw-proxy/{path:path}")
async def mgw_proxy_post(path: str, request: Request):
    """Reenvía requests POST del iframe (AJAX) a Mi Gestión Web con la sesión autenticada."""
    if not is_logged_in():
        return HTMLResponse("<h3>Sin sesión MGW</h3>", status_code=503)

    query = str(request.url.query)
    full_path = f"/{path}"
    if query:
        full_path += f"?{query}"

    body = await request.body()
    content_type = request.headers.get("content-type", "")

    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(
        None, mgw_post, full_path, None, body, content_type or None
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
    Proxy autenticado a Mi Gestión Web.
    - Sirve páginas con las cookies de sesión inyectadas en el browser
    - Reescribe URLs relativas y absolutas para pasar por el proxy
    - Intercepta fetch/XHR via JS para que también pasen por el proxy
    """
    if not is_logged_in():
        return HTMLResponse("<h3>Sin sesión MGW</h3>", status_code=503)

    query = str(request.url.query)
    full_path = f"/{path}"
    if query:
        full_path += f"?{query}"

    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(None, mgw_get, full_path)

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
  // Interceptar fetch
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
  // Interceptar XMLHttpRequest
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

        # Armar headers de respuesta con Set-Cookie para cada cookie de sesión
        cookies = get_cookies()
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


@app.websocket("/agent-ws")
async def agent_ws(websocket: WebSocket):
    """
    WebSocket entre el servidor y la webpage del agente.
    """
    await websocket.accept()
    set_agent_websocket(websocket)
    print("[AGENT WS] Webpage del agente conectada ✓")

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "audio_done":
                on_agent_audio_done()
    except WebSocketDisconnect:
        print("[AGENT WS] Webpage desconectada")
    except Exception as e:
        print(f"[AGENT WS] Error: {e}")
    finally:
        remove_agent_websocket(websocket)


@app.websocket("/audio")
async def audio_ws(websocket: WebSocket):
    """Recall.ai conecta acá para enviarte el audio de la reunión."""
    await websocket.accept()
    try:
        await handle_recall_audio(websocket)
    except WebSocketDisconnect:
        print("[FastAPI] WebSocket de audio desconectado")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)