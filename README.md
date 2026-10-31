# SignalOne

Review-sentiment agent for ticketing platforms. Fetches reviews from G2, Trustpilot and Reddit, labels sentiment and complaint categories with CrewAI (gpt-4o-mini), verifies every claim in the summary with an evidence gate, and saves everything to a Google Sheet.

Live: https://signalone-agent.onrender.com

## Brands covered

Eventbrite · Ticket Tailor · Townscript · BookMyShow

## Architecture

```
Browser UI ──► /ask (Flask) ──► CrewAI scorer + analyst ──► evidence gate ──► response
                                     ▲
                               G2 (ZenRows)
                               Trustpilot (FetchLayer)
                               Reddit (FetchLayer)

Google Apps Script (sheet.gs)
  startRefresh  ──► POST /refresh  (background, all 4 brands)
  writeSheet    ──► GET /sheet-rows ──► merge into Google Sheet
```

**Two paths:**
- **Fresh** (~60s): fetch → label → summarise → evidence gate → save to SQLite → respond.
- **Saved** (~3s): if labelled reviews exist within `SAVED_MAX_AGE_HOURS` (24h), skip fetching and just re-summarise.

**Evidence gate** (`check_summary` in app.py): every sentence must cite review IDs, every percentage must match a real computed number, every complaint keyword must appear in a cited review's tag. Retries up to 3 times.

## MCP server

`server.py` exposes 4 tools over streamable HTTP at `/mcp`:

| Tool | What it does |
|------|-------------|
| `analyze_brand` | Full fetch → label → summarise → save pipeline |
| `search_reviews` | Query saved reviews by brand/source/sentiment/tag |
| `top_complaints` | Top complaint categories for a brand |
| `get_review` | One review by id, with source URL |

## Setup

```bash
pip install -r requirements.txt
```

Environment variables (set in Render dashboard or `~/.claude/secrets.zsh` locally):

| Var | What |
|-----|------|
| `OPENAI_API_KEY` | gpt-4o-mini for CrewAI |
| `ZENROWS_API_KEY` | G2 scraping |
| `FETCHLAYER_TOKEN` | Trustpilot + Reddit via FetchLayer MCP |
| `UI_ORIGIN` | CORS origin for the frontend (default: signalone-ui.vercel.app) |

Run locally:

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

## Google Sheet sync

`sheet.gs` runs in Google Apps Script attached to the Sheet. Two triggers:
- `startRefresh`: daily ~1 AM, POSTs to `/refresh` (returns immediately, runs in background).
- `writeSheet`: every 10 minutes, GETs `/sheet-rows` and merges rows into the Reviews tab. Never deletes — append/update only.

SQLite is wiped on each Render redeploy. The Sheet is the permanent record.

## Where it breaks

- **G2**: ZenRows JS-render + premium proxy. If ZenRows is down or credits run out, G2 reviews stop.
- **FetchLayer**: Trustpilot and Reddit depend on it. 30s timeout per call.
- **Render free tier**: cold starts (~30s), SQLite wiped on deploy, 512 MB RAM.
- **Evidence gate**: strict — if the LLM can't produce a fully cited summary in 3 tries, `verified` comes back false.

## Tests

```bash
python test_gate.py
```

Unit tests for the evidence gate only. The rest is integration (needs live API keys).
