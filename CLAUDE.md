# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate virtualenv
source venv/bin/activate

# Install dependencies (also installs Playwright's Chromium)
pip install -r requirements.txt
playwright install chromium --with-deps

# Run locally (port 8000)
python main.py

# Create a bot in a meeting (multi-tenant: module/field/user_name)
curl -X POST http://localhost:8000/bot/create \
  -H "Content-Type: application/json" \
  -d '{"meeting_url": "https://meet.google.com/xxx-yyyy-zzz", "module": "modulo_2", "field": "", "user_name": "Juan"}'

# Admin (debug de varias sesiones simultáneas)
curl http://localhost:8000/sessions          # lista de sesiones activas
curl http://localhost:8000/sessions/<sid>     # detalle de una sesión
curl -X DELETE http://localhost:8000/sessions/<sid>   # cierra + limpia una sesión
curl http://localhost:8000/pool/status        # credenciales libres/ocupadas + cola

# Local tunnel (required for Recall.ai webhooks)
ngrok http --domain=chatroom-fancy-subtly.ngrok-free.dev 8000
```

`/bot/create` body: `meeting_url` (obligatorio) + `module` (`modulo_1`=Configuración / `modulo_2`=Caja y Caja Mayor / `""`), `field` (sección puntual, ej. `balanza`; gana sobre `module` si viene), `user_name`. Respuestas: `{"status":"running","sid",...,"bot_id","sistema"}`, o `{"status":"waiting","sid","position"}` si el pool está lleno y la cola habilitada, o `503` si la cola está apagada/llena.

There are no automated tests or linters configured.

## Required Environment Variables

Create a `.env` file (loaded via `python-dotenv`):

```
OPENAI_API_KEY=
RECALL_API_KEY=
RECALL_REGION=us-east-1
PUBLIC_WS_URL=              # wss://your-domain/audio
PUBLIC_BASE_URL=            # https://your-domain (no trailing slash)
MGW_URL=                    # defaults to https://www.migestionweb.pro/
MGW_USER=                   # credencial única legacy (fallback single-tenant)
MGW_EMPRESA=
MGW_PASSWORD=
MGW_CREDENTIALS=            # JSON array de sistemas MGW para multi-tenant (uno por bot concurrente):
                            #   [{"empresa":"dev1","usuario":"mgw","password":"...","alias":"dev1"}, ...]
                            #   si no está seteada, cae a MGW_USER/EMPRESA/PASSWORD (una sola sesión)
PENDING_QUEUE_MAX=20        # cola de espera cuando el pool está lleno; 0 = cola off (503)
TEST_MODE=false             # true → skips calificación, jumps straight to demo
REALTIME_MODEL=             # defaults to gpt-realtime-2025-08-28

# Auto-liberación de sesiones (para que una credencial no quede colgada):
SESSION_INACTIVITY_TIMEOUT_S=900   # sin audio humano por N s → watchdog cierra la sesión; 0 = off
SESSION_MAX_LIFETIME_S=5400        # tope duro de duración por sesión; 0 = sin tope
SESSION_WATCHDOG_INTERVAL_S=30     # cada cuánto chequea el watchdog
RECALL_EVERYONE_LEFT_TIMEOUT_S=60  # Recall hace que el bot abandone N s después de que se van todos; 0 = default Recall
RECALL_NOONE_JOINED_TIMEOUT_S=900  # abandona si nadie entra nunca; 0 = default Recall
RECALL_WAITING_ROOM_TIMEOUT_S=900  # abandona si queda en sala de espera; 0 = default Recall
```

**Auto-liberación**: una sesión (y su credencial del pool) se cierra sola por dos vías, además del `DELETE /sessions/{sid}` manual y del cierre del WS de audio: (1) `recall.create_bot` manda un `automatic_leave` para que el bot abandone la llamada cuando la reunión queda vacía / nadie entra / queda en sala de espera — al abandonar se cierra el WS de audio y corre el teardown; (2) `SessionManager._watchdog` (uno por sesión RUNNING) cierra la sesión si no llega audio humano por `SESSION_INACTIVITY_TIMEOUT_S` o si supera `SESSION_MAX_LIFETIME_S`. `bot.py` llama `session.touch()` en cada frame de audio humano para reiniciar el reloj de inactividad.

`DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID` are still read in `config.py` but are dead config — the old Deepgram/ElevenLabs pipeline (`ai.py`, `mgw_browser.py`, `mgw_caja.py`) was deleted in the "Limpieza de archivos" cleanup; nothing references them anymore.

## Architecture Overview

**Malena** is an AI sales demo bot ("asesora de ventas") for "Mi Gestión Web" (MGW), a SaaS for Argentine businesses. It joins a video call via [Recall.ai](https://recall.ai), handles STT + LLM + TTS through the **OpenAI Realtime API** (a single persistent WebSocket), and simultaneously drives a live demo of the MGW web app using Playwright — streaming screenshots back into the meeting via Recall's "Output Media" feature.

### Data Flow

```
Recall.ai bot (in meeting)
  ├─ sends PCM16 audio → WS /audio → audio_queue → RealtimeBridge
  │                                         └─ streams to OpenAI Realtime API (STT+LLM+TTS)
  └─ shows camera  ← agent.html (served at /agent)
                         ↑ PCM audio (24 kHz, base64) via /agent-ws WebSocket
                         ↑ navigate commands (→ loads MGW pages in iframe via /mgw-proxy/)
                         ↑ screenshot overlays (Playwright headless Chromium)

/bot/create (REST)
  → SessionManager.create() [acquires a credential from the pool, or queues WAITING]
       → MgwSession.login() [per-session requests.Session]
       → recall.create_bot(sid=...) [sends agent URL + websocket URL, both carrying ?sid=]
```

### Multi-tenant (por `sid`)

Cada llamada es una **`BotSession`** identificada por un `sid` que se inyecta en las URLs que
Recall usa (webpage `/agent?sid=` y WS `/audio?sid=`). Un `SessionManager` (dict `sid → BotSession`)
rutea cada WebSocket a su sesión, así conviven N bots en paralelo sin pisarse.

- **`credentials_pool.py`** — `CredentialPool`: administra SOLO las credenciales MGW (`acquire`/`release`
  en orden, flag `busy`). Fuente: `MGW_CREDENTIALS` (JSON array), o la credencial única legacy.
- **`session.py`** — `BotSession` (SOLO estado de una sesión + su canal a la webpage), `SessionManager`
  (ciclo de vida + `PendingQueue` para cuando el pool está lleno + `teardown` idempotente con gracia de
  ~20 s ante reconexión del audio de Recall + `SidLogger` que antepone `[sid=...]`).
- **`mgw_session.py`** — clase `MgwSession` por sesión (antes era un `requests.Session` global). El proxy
  `/mgw-proxy` resuelve la sesión por la cookie `sid` que setea `agent.html`.
- **`mgw_playwright.py`** — estado por sesión en un `ContextVar` con holder mutable + proxy `_page` (este
  es el ÚNICO módulo con `ContextVar`; en el resto se pasa la `BotSession` explícita). `init_pw_state()`
  se llama en el task raíz de la sesión; `pw_stop_state(holder)` cierra el browser de una sesión desde el
  teardown (que corre en otro task).
- El prompt de sistema se arma por sesión (`build_system_prompt` en `session.py`) inyectando el foco del
  campus (`module`/`field`/`user_name`) sobre `TRAINING_SYSTEM_PROMPT`, y se pasa al `RealtimeBridge`.
- Admin: `GET /sessions`, `GET /sessions/{sid}`, `DELETE /sessions/{sid}`, `GET /pool/status`.

### Active Pipeline: OpenAI Realtime API

`realtime_bridge.py` manages the full conversation loop:

- **STT + LLM + TTS** handled natively by the Realtime API in one WebSocket (`wss://api.openai.com/v1/realtime`)
- **Barge-in**: native VAD (`input_audio_buffer.speech_started` event) cancels the current response; no GPT classification needed
- **Tool calls**: the LLM calls tools (defined in `REALTIME_TOOLS` in `config.py`) to navigate modules and drive Playwright actions step-by-step. Tool dispatch lives in `realtime_bridge._handle_tool`
- **Auto-continue**: after Malena finishes speaking during the demo, `_auto_continue` fires a `response.create` after 5–12 s if the user doesn't speak, keeping the demo moving without explicit user turns
- **Anti-echo mute**: a `_mute_until` timestamp drops incoming audio chunks after Malena speaks to prevent her voice from triggering VAD
- **Conversation flow** is entirely managed by `REALTIME_SYSTEM_PROMPT` in `config.py` — the `Stage` enum and `ConversationState` are instantiated but stage transitions are driven by the prompt, not code

### Module Responsibilities

| File | Role |
|------|------|
| [main.py](main.py) | FastAPI server — endpoints (incl. `/sessions*`, `/pool/status`) ruteados por `sid` + WS `/audio`, `/agent-ws` + MGW proxy `/mgw-proxy/{path}` |
| [session.py](session.py) | `BotSession` (estado por sesión + canal a la webpage) y `SessionManager` (ciclo de vida, `PendingQueue`, teardown, `SidLogger`, `build_system_prompt`) |
| [credentials_pool.py](credentials_pool.py) | `CredentialPool`: administra las credenciales MGW del pool (`acquire`/`release` en orden, flag `busy`, `status`) |
| [bot.py](bot.py) | Thin adapter por sesión: arma el `RealtimeBridge` y los wrappers de Playwright ligados a la `BotSession`; maneja su WebSocket de audio (`handle_recall_audio(ws, session)`) |
| [realtime_bridge.py](realtime_bridge.py) | **Core pipeline**: connects to OpenAI Realtime API, streams PCM audio, dispatches tool calls, manages barge-in and auto-continue (recibe `system_prompt` por sesión) |
| [recall.py](recall.py) | Creates Recall.ai bot with Output Media (webpage camera) and real-time audio websocket — inyecta `?sid=` en agent URL y WS URL |
| [mgw_session.py](mgw_session.py) | Clase `MgwSession` (una `requests.Session` por sesión) — API autenticada + el proxy `/mgw-proxy` (resuelto por cookie `sid`) |
| [mgw_playwright.py](mgw_playwright.py) | Headless Chromium automation — all demo steps called by `RealtimeBridge` tool handlers (caja, balanza, proveedores, producción, estadísticas, stock, clientes) |
| [config.py](config.py) | All env vars, `REALTIME_SYSTEM_PROMPT`, `REALTIME_TOOLS` list, `DEMO_MODULE_PATHS` (module name → MGW URL path) |
| [state.py](state.py) | `ConversationState` dataclass + `Stage` enum (instantiated in `bot.py`; stage logic lives in the Realtime prompt) |
| [app/static/agent.html](app/static/agent.html) | Single-page frontend loaded as Recall's camera. Connects to `/agent-ws`, plays PCM audio at 24 kHz via Web Audio API, loads MGW pages in an iframe via the proxy, overlays Playwright screenshots |

### Key Design Decisions

**Two parallel MGW sessions**: `mgw_session.py` holds a `requests.Session` for server-side proxy calls (page serving, AJAX). `mgw_playwright.py` runs a separate headless browser that authenticates independently and takes screenshots for the live demo overlay.

**Tool-based demo flow**: rather than keyword-matching Malena's text (old approach), the Realtime API emits structured `function_call` events. Each demo step is a discrete tool (`caja_buscar_producto`, `balanza_agregar_producto`, etc.). `_handle_tool` in `realtime_bridge.py` dispatches to the corresponding `mgw_playwright.py` function, waits for audio playback to finish (`_wait_for_audio_done`), then returns a result string the LLM uses to narrate the outcome.

**navigate_to_module vs action tools**: `navigate_to_module` loads a URL in the agent iframe without starting Playwright. Playwright-heavy tools (`caja_*`, `balanza_*`, etc.) call `_ensure_playwright()` on first use, which logs in and initializes the browser session.

**MGW reverse proxy**: `agent.html` loads MGW pages via `/mgw-proxy/{path}`. The proxy rewrites absolute MGW URLs to proxy paths and injects a JS snippet that intercepts `fetch`/`XHR` so AJAX calls also go through the proxy with the authenticated session cookies.

### Deployment

Deployed on Railway. The authoritative start command is in `railway.toml`: `uvicorn main:app --host 0.0.0.0 --port $PORT`. Note: `Procfile` and `nixpacks.toml` contain a stale `app.main:app` path — Railway ignores them in favour of `railway.toml`. Production URL: `https://ai-zoom-bot-production.up.railway.app`.
