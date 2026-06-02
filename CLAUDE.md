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
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=        # defaults to p7AwDmKvTdoHTBuueGvP
DEEPGRAM_API_KEY=
RECALL_API_KEY=
RECALL_REGION=us-east-1
PUBLIC_WS_URL=              # wss://your-domain/audio
PUBLIC_BASE_URL=            # https://your-domain (no trailing slash)
MGW_URL=                    # defaults to https://www.migestionweb.pro/
MGW_USER=
MGW_EMPRESA=
MGW_PASSWORD=
```

## Architecture Overview

**Malena** is an AI sales demo bot ("asesora de ventas") for "Mi Gestión Web" (MGW), a SaaS for Argentine businesses. It joins a video call via [Recall.ai](https://recall.ai), listens to participants with Deepgram STT, generates replies with GPT-4.1-mini (OpenAI), speaks via ElevenLabs TTS, and simultaneously drives a live demo of the MGW web app using Playwright — streaming screenshots back into the meeting via Recall's "Output Media" feature.

### Data Flow

```
Recall.ai bot (in meeting)
  ├─ sends PCM audio → POST /audio (WebSocket) → Deepgram → transcript
  └─ shows camera  ← agent.html (served at /agent)
                         ↑ audio (MP3 base64) via /agent-ws WebSocket
                         ↑ navigate commands
                         ↑ screenshot overlays (live Playwright demo)

/bot/create (REST)
  → mgw_login() [server-side session]
  → recall.create_bot() [sends agent URL + websocket URL to Recall]
```

### Conversation State Machine

Defined in [state.py](state.py). Four stages in order: `INTRO → CALIFICACION → DEMO → CIERRE`.

- **INTRO**: Malena introduces herself.
- **CALIFICACION**: Collects lead name + business type. Advances when both are known and user confirms.
- **DEMO**: Autonomous loop (`run_demo_loop` in [bot.py](bot.py)) — GPT streams module descriptions sentence-by-sentence while Playwright executes live actions in parallel. Supports barge-in (user interrupts while Malena is speaking).
- **CIERRE**: Collects contact info, says goodbye.

System prompts for each stage live in [config.py](config.py) under `PROMPTS_BY_STAGE`.

### Module Responsibilities

| File | Role |
|------|------|
| [main.py](main.py) | FastAPI server — HTTP endpoints + two WebSocket routes (`/audio`, `/agent-ws`) + MGW reverse proxy (`/mgw-proxy/{path}`) |
| [bot.py](bot.py) | Core pipeline: Deepgram audio loop, transcript debounce, barge-in detection, state machine transitions, demo loop, audio playback orchestration |
| [ai.py](ai.py) | OpenAI calls (`ask_ai` sync, `ask_ai_streaming` async), ElevenLabs TTS (`text_to_speech`), interruption classifier (`classify_with_ai`), lead extractor |
| [recall.py](recall.py) | Creates Recall.ai bot with Output Media (webpage camera) and real-time audio websocket |
| [mgw_session.py](mgw_session.py) | Server-side `requests.Session` for MGW — used for authenticated API calls and the `/mgw-proxy` reverse proxy |
| [mgw_playwright.py](mgw_playwright.py) | Headless Chromium automation — logs into MGW and executes the caja demo in two phases: Fase 1 (search + add product), Fase 2 (select payment + close sale) |
| [config.py](config.py) | All env vars, Deepgram WS URL, system prompts, `DEMO_NAV_KEYWORDS` (keyword → module mapping) |
| [state.py](state.py) | `ConversationState` dataclass + `Stage` enum |
| [app/static/agent.html](app/static/agent.html) | Single-page frontend loaded as Recall's camera. Connects to `/agent-ws`, plays MP3 audio via Web Audio API, loads MGW pages in an iframe via the proxy, overlays Playwright screenshots during the caja demo |

### Key Design Decisions

**Two parallel sessions for MGW**: `mgw_session.py` maintains a `requests.Session` for server-side API calls (proxying pages, adding products via AJAX). `mgw_playwright.py` runs a separate headless browser session that takes screenshots for the live demo. Both authenticate independently.

**Barge-in handling**: When Recall sends audio while `is_speaking=True`, Deepgram classifies the transcript as `noise`, `backchannel`, or `question` (via GPT). Only `question` triggers a barge-in interrupt. The bot sends an acknowledgment ("Sí, decime."), waits for the real question, then resumes the demo.

**Sentence-by-sentence streaming**: `ask_ai_streaming` in [ai.py](ai.py) splits GPT output at sentence boundaries and enqueues each sentence. `run_demo_loop` in [bot.py](bot.py) processes them one by one — generating TTS and triggering Playwright actions per sentence so audio and screen actions stay in sync.

**MGW navigation**: `DEMO_NAV_KEYWORDS` in [config.py](config.py) maps keywords in Malena's replies to module names. `_mgw_navigate_from_reply` in [bot.py](bot.py) scans each sentence and sends a `navigate` command to `agent.html`, which loads the page via the `/mgw-proxy/` reverse proxy. A `_module_locked` flag prevents jumping to a new module while the current one is still being explained.

**Legacy files**: `mgw_browser.py` (non-headless Playwright, not used by bot.py) and `mgw_caja.py` (API-only caja flow) are not part of the active pipeline.

### Deployment

Deployed on Railway. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`. The `nixpacks.toml` installs system deps for headless Chromium. Production URL: `https://ai-zoom-bot-production.up.railway.app`.
