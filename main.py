"""
main.py
Servidor FastAPI.
- POST /bot/create  → manda el bot a una reunión
- GET  /bot/status  → estado del bot
- WS   /audio       → Recall.ai conecta acá para mandarte el audio
"""
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from recall import create_bot, get_bot_status, current_bot_id
from bot import handle_recall_audio
from ai import reset_conversation

app = FastAPI(title="Malena Bot - Mi Gestión Web")


class BotRequest(BaseModel):
    meeting_url: str
    bot_name: str = "Malena - Mi Gestión Web"


@app.post("/bot/create")
async def create(req: BotRequest):
    """Manda el bot a una reunión de Zoom/Meet."""
    reset_conversation()  # limpia memoria de la reunión anterior
    data = create_bot(req.meeting_url, req.bot_name)
    return {"status": "ok", "bot_id": data["id"]}


@app.get("/bot/status")
async def status():
    """Estado del bot activo."""
    if not current_bot_id:
        return {"status": "no_bot"}
    data = get_bot_status()
    return {"bot_id": current_bot_id, "status": data.get("status_changes", [])}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/audio")
async def audio_ws(websocket: WebSocket):
    """
    Recall.ai conecta acá para enviarte el audio de la reunión en tiempo real.
    Esta URL debe ser pública (ngrok en desarrollo, dominio real en producción).
    """
    await websocket.accept()
    try:
        await handle_recall_audio(websocket)
    except WebSocketDisconnect:
        print("[FastAPI] WebSocket desconectado")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)