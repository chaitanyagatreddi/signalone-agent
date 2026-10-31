"""SignalOne server: the Flask agent API plus our own MCP server, in one process.

Run: uvicorn server:app --host 0.0.0.0 --port $PORT
  /health, /ask  -> Flask agent (app.py)
  /mcp           -> MCP server (streamable HTTP)
"""
import os
import re
from collections import Counter
from contextlib import asynccontextmanager

# Local dev only: load keys from ~/.claude/secrets.zsh when they aren't in the environment.
# On Render the keys come from its Environment Variables page, and this file doesn't exist.
_secrets = os.path.expanduser("~/.claude/secrets.zsh")
if os.path.exists(_secrets):
    for _k, _v in re.findall(r"^export (\w+)=[\"']?([^\"'\n]+)", open(_secrets).read(), re.M):
        os.environ.setdefault(_k, _v)

import anyio
from a2wsgi import WSGIMiddleware
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount

from app import BRANDS, app as flask_app, db, run_agent

# host="0.0.0.0" so the server accepts requests addressed to the Render URL, not only localhost.
mcp = FastMCP("SignalOne", host="0.0.0.0", streamable_http_path="/", stateless_http=True, json_response=True)


@mcp.tool()
async def analyze_brand(brand: str, source: str = "All sources", question: str = "") -> dict:
    """Fetch fresh reviews for a brand (G2, Trustpilot, Reddit), label sentiment and complaints with the
    CrewAI agents, verify the summary with the evidence gate, and save the reviews. Takes about a minute.
    brand: Eventbrite, Ticket Tailor, Townscript or BookMyShow. source: All sources, G2, Trustpilot or Reddit."""
    # CrewAI refuses to run synchronously inside the server's event loop, so it runs in a worker thread.
    return await anyio.to_thread.run_sync(run_agent, brand, source, question or None)


@mcp.tool()
def search_reviews(brand: str = "", source: str = "", sentiment: str = "", tag: str = "", limit: int = 20) -> list[dict]:
    """Search saved reviews. Filters are optional. sentiment: positive, neutral or negative.
    tag: fees, payouts, support, UX/checkout, refunds, reliability or other."""
    where, args = [], []
    for col, val in (("brand", brand), ("source", source), ("sentiment", sentiment), ("tag", tag)):
        if val:
            where.append(f"lower({col}) = lower(?)")
            args.append(val)
    sql = "SELECT * FROM reviews" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY date DESC LIMIT ?"
    with db() as con:
        return [dict(r) for r in con.execute(sql, [*args, max(1, min(limit, 100))])]


@mcp.tool()
def top_complaints(brand: str, n: int = 3) -> dict:
    """Top complaint categories for a brand, as share of its saved negative reviews."""
    with db() as con:
        tags = [r["tag"] for r in con.execute("SELECT tag FROM reviews WHERE lower(brand)=lower(?) AND sentiment='negative'", [brand])]
    return {
        "brand": brand,
        "negative_reviews": len(tags),
        "complaints": [{"label": t, "share": round(100 * c / len(tags))} for t, c in Counter(tags).most_common(n)] if tags else [],
    }


@mcp.tool()
def get_review(id: int) -> dict:
    """One saved review by id, with its source URL for citations."""
    with db() as con:
        row = con.execute("SELECT * FROM reviews WHERE id = ?", [id]).fetchone()
    return dict(row) if row else {"error": f"No review with id {id}"}


mcp_app = mcp.streamable_http_app()  # must be built before the lifespan touches mcp.session_manager


@asynccontextmanager
async def lifespan(_):
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[Mount("/mcp", app=mcp_app), Mount("/", app=WSGIMiddleware(flask_app))],
    lifespan=lifespan,
)
