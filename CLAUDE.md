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

# Create a bot in a meeting
curl -X POST http://localhost:8000/bot/create \
  -H "Content-Type: application/json" \
  -d '{"meeting_url": "https://meet.google.com/xxx-yyyy-zzz"}'

# Local tunnel (required for Recall.ai webhooks)
ngrok http --domain=chatroom-fancy-subtly.ngrok-free.dev 8000
```

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
MGW_USER=
MGW_EMPRESA=
MGW_PASSWORD=
TEST_MODE=false             # true → skips calificación, jumps straight to demo
REALTIME_MODEL=             # defaults to gpt-realtime-2.1
```

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
  → mgw_login() [server-side requests.Session]
  → recall.create_bot() [sends agent URL + websocket URL to Recall]
```

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
| [main.py](main.py) | FastAPI server — HTTP endpoints + two WebSocket routes (`/audio`, `/agent-ws`) + MGW reverse proxy (`/mgw-proxy/{path}`) |
| [bot.py](bot.py) | Thin adapter: creates `RealtimeBridge`, wires up Playwright wrappers, and manages the agent WebSocket list (`_agent_ws_list`) |
| [realtime_bridge.py](realtime_bridge.py) | **Core pipeline**: connects to OpenAI Realtime API, streams PCM audio, dispatches tool calls, manages barge-in and auto-continue |
| [recall.py](recall.py) | Creates Recall.ai bot with Output Media (webpage camera) and real-time audio websocket |
| [mgw_session.py](mgw_session.py) | Server-side `requests.Session` for MGW — authenticated API calls and the `/mgw-proxy` reverse proxy |
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
