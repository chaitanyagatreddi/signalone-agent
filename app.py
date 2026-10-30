import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Literal

os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

import requests
from crewai import Agent, Crew, Task
from flask import Flask, jsonify, request
from pydantic import BaseModel

app = Flask(__name__)

TAGS = ["fees", "payouts", "support", "UX/checkout", "refunds", "reliability", "other"]
LLM = "gpt-4o-mini"

MAX_PER_SOURCE = 30
FETCHLAYER_URL = "https://mcp.fetchlayer.dev"

# Brand -> (G2 slug, Trustpilot domain). Only Eventbrite is tested so far.
BRANDS = {
    "eventbrite": ("eventbrite", "eventbrite.com"),
    "ticket tailor": ("ticket-tailor", "tickettailor.com"),
    "townscript": ("townscript", "townscript.com"),
    "bookmyshow": ("bookmyshow", "bookmyshow.com"),
}


def fetchlayer(tool, arguments):
    r = requests.post(
        FETCHLAYER_URL,
        headers={
            "Authorization": f"Bearer {os.environ['FETCHLAYER_TOKEN']}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
        timeout=120,
    )
    r.raise_for_status()
    r.encoding = "utf-8"
    data = "".join(line[6:] for line in r.text.split("\n") if line.startswith("data: "))
    result = json.loads(data)["result"]
    if result.get("isError"):
        raise RuntimeError(result["content"][0]["text"])
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def fetch_reddit(brand):
    out = fetchlayer("scrape_search", {"query": brand, "limit": MAX_PER_SOURCE, "format": "json"})
    return [
        {
            "source": "Reddit",
            "date": (p.get("createdAt") or "")[:10],
            "rating": None,
            "text": f"{p.get('title', '')}. {p.get('previewText') or ''}".strip(),
            "url": p.get("permalink"),
        }
        for p in out.get("items", [])[:MAX_PER_SOURCE]
    ]


def fetch_trustpilot(domain):
    out = fetchlayer("trustpilot_company_reviews", {"company": domain, "language": "en", "format": "json"})
    return [
        {
            "source": "Trustpilot",
            "date": (r.get("publishedAt") or "")[:10],
            "rating": r.get("rating"),
            "text": f"{r.get('title', '')}. {r.get('text') or ''}".strip(),
            "url": r.get("reviewUrl"),
        }
        for r in out.get("reviews", [])[:MAX_PER_SOURCE]
    ]


G2_REVIEW = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s*\n.*?survey_responses/(\d+)/concerns.*?\n\s*\"(.+?)\"\s*\n\s*([\d.]+)/5(.*?)Show More",
    re.S,
)


def fetch_g2(slug):
    page = f"https://www.g2.com/products/{slug}/reviews"
    r = requests.get(
        "https://api.zenrows.com/v1/",
        params={
            "apikey": os.environ["ZENROWS_API_KEY"],
            "url": page,
            "js_render": "true",
            "premium_proxy": "true",
            "proxy_country": "us",
            "response_type": "markdown",
        },
        timeout=180,
    )
    r.raise_for_status()
    reviews = []
    for date, _id, title, rating, body in G2_REVIEW.findall(r.text):
        answers = re.findall(r"\?\s*\n\s*\n(.+?) Review collected by and hosted on G2\.com", body, re.S)
        reviews.append({
            "source": "G2",
            "date": datetime.strptime(date, "%m/%d/%Y").strftime("%Y-%m-%d"),
            "rating": float(rating),
            "text": f"{title}. " + " ".join(a.strip() for a in answers[:2]),
            "url": page,  # ponytail: G2 has no public per-review URL in the page; links to the reviews page
        })
    return reviews[:MAX_PER_SOURCE]


FETCHERS = {"Reddit": lambda b: fetch_reddit(b), "Trustpilot": lambda b: fetch_trustpilot(BRANDS[b.lower()][1]), "G2": lambda b: fetch_g2(BRANDS[b.lower()][0])}


class Label(BaseModel):
    id: int
    sentiment: Literal["positive", "neutral", "negative"]
    tag: Literal["fees", "payouts", "support", "UX/checkout", "refunds", "reliability", "other"]


class Labels(BaseModel):
    labels: list[Label]


def score_reviews(brand, reviews):
    numbered = "\n".join(f"[{i}] ({r['source']}) {r['text'][:500]}" for i, r in enumerate(reviews))
    scorer = Agent(role="Review scorer", goal="Label every review accurately", backstory="You read customer reviews of ticketing platforms and label them.", llm=LLM)
    score = Task(
        description=f"Label each {brand} review below. For each id give sentiment (positive, neutral, negative) and one tag from {TAGS}.\n\n{numbered}",
        expected_output="One label per review id.",
        output_pydantic=Labels,
        agent=scorer,
    )
    Crew(agents=[scorer], tasks=[score]).kickoff()
    return score.output.pydantic.labels


# Evidence gate, copied in spirit from arc--solver's EvidenceGate: every claim must cite reviews,
# and each cited fact is checked against the real data. Words that mean each tag in a sentence:
TAG_WORDS = {"fees": r"\bfees?\b|pric", "payouts": r"payout", "support": r"support", "UX/checkout": r"\bux\b|checkout|user experience",
             "refunds": r"refund", "reliability": r"reliab|outage|crash"}


def check_summary(summary, labelled, sentiment, complaints):
    "Return a list of errors; empty means every claim in the summary is cited and true."
    errors = []
    allowed = set(sentiment.values()) | {c["share"] for c in complaints}
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", summary.strip()):
        ids = [int(n) for n in re.findall(r"\[(\d+)\]", sentence)]
        if not ids:
            errors.append(f"No citation in: '{sentence}'")
            continue
        bad = [n for n in ids if not 1 <= n <= len(labelled)]
        if bad:
            errors.append(f"Cited review(s) {bad} do not exist (there are {len(labelled)}).")
            continue
        cited = [labelled[n - 1] for n in ids]
        for pct in re.findall(r"(\d+)\s*%", sentence):
            if int(pct) not in allowed:
                errors.append(f"{pct}% is not a real number. Real numbers: sentiment {sentiment}, complaints {complaints}.")
        for tag, pattern in TAG_WORDS.items():
            if re.search(pattern, sentence, re.I) and not any(r["tag"] == tag for r in cited):
                errors.append(f"Sentence mentions {tag} but none of cited reviews {ids} are tagged {tag}: '{sentence}'")
    return errors


def summarise(question, brand, labelled, sentiment, complaints, max_tries=3):
    # The analyst gets the counts computed in code, so its summary can't contradict the numbers shown.
    lines = "\n".join(f"[{i}] ({r['source']}, {r['sentiment']}, {r['tag']}) {r['text'][:300]}" for i, r in enumerate(labelled, 1))
    analyst = Agent(role="Review analyst", goal="Answer the user's question from labelled reviews only", backstory="You write short, plain answers backed by the reviews you were given.", llm=LLM)
    feedback, summary, errors = "", "", []
    for _ in range(max_tries):
        answer = Task(
            description=(
                f"Question: {question}\nBrand: {brand}\nSentiment split (%): {sentiment}\n"
                f"Top complaints (% of negative reviews): {complaints}\nLabelled reviews:\n{lines}\n\n"
                "Answer in 2-3 plain sentences. Only use the numbers given above, and keep complaint order as given. "
                "End every sentence with the ids of the reviews that support it, like [3][7]. "
                "Only mention a complaint if a cited review has that tag." + feedback
            ),
            expected_output="2-3 sentences, each ending with review ids in brackets.",
            agent=analyst,
        )
        summary = Crew(agents=[analyst], tasks=[answer]).kickoff().raw.strip()
        errors = check_summary(summary, labelled, sentiment, complaints)
        if not errors:
            return summary, True, []
        feedback = "\n\nYour previous answer was rejected:\n" + summary + "\nErrors:\n- " + "\n- ".join(errors) + "\nFix every error."
    return summary, False, errors


def build_answer(question, brand, reviews):
    labels = score_reviews(brand, reviews)
    by_id = {l.id: l for l in labels if 0 <= l.id < len(reviews)}
    labelled = [{**r, "sentiment": by_id[i].sentiment, "tag": by_id[i].tag} for i, r in enumerate(reviews) if i in by_id]
    total = len(labelled) or 1
    counts = Counter(r["sentiment"] for r in labelled)
    negative = [r["tag"] for r in labelled if r["sentiment"] == "negative"]
    sentiment = {k: round(100 * counts[k] / total) for k in ("positive", "neutral", "negative")}
    complaints = [{"label": t, "share": round(100 * n / len(negative))} for t, n in Counter(negative).most_common(3)]
    summary, verified, gate_errors = summarise(question, brand, labelled, sentiment, complaints)
    return {
        "summary": summary,
        "verified": verified,
        "gate_errors": gate_errors,
        "sentiment": sentiment,
        "complaints": complaints,
        "reviews": [{k: r[k] for k in ("source", "date", "sentiment", "tag", "text", "url")} for r in labelled],
    }


# ponytail: SQLite on local disk is wiped on each Kuberns redeploy; move to a hosted DB if history must survive deploys
DB = os.environ.get("SIGNALONE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "signalone.db"))


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY, brand TEXT, source TEXT, date TEXT, sentiment TEXT, tag TEXT, text TEXT,
        url TEXT, fetched_at TEXT, UNIQUE(brand, source, text))""")
    return con


def save_reviews(brand, reviews):
    with db() as con:
        con.executemany(
            """INSERT INTO reviews (brand, source, date, sentiment, tag, text, url, fetched_at) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(brand, source, text) DO UPDATE SET sentiment=excluded.sentiment, tag=excluded.tag, fetched_at=excluded.fetched_at""",
            [(brand, r["source"], r["date"], r["sentiment"], r["tag"], r["text"], r["url"], datetime.utcnow().isoformat()) for r in reviews],
        )


def run_agent(brand, source="All sources", question=None):
    "Fetch -> crew -> evidence gate -> save. Shared by /ask and the MCP tool."
    if brand.lower() not in BRANDS:
        raise ValueError(f"Unknown brand. Try one of: {', '.join(b.title() for b in BRANDS)}")
    brand = next(b.title() if b != "bookmyshow" else "BookMyShow" for b in BRANDS if b == brand.lower())
    sources = list(FETCHERS) if source == "All sources" else [source]
    reviews, errors = [], {}
    for s in sources:
        for attempt in (1, 2):  # one retry: a source failed once intermittently in testing
            try:
                reviews += FETCHERS[s](brand)
                errors.pop(s, None)
                break
            except Exception as e:
                errors[s] = str(e)[:200]
    seen = set()
    reviews = [r for r in reviews if r["text"] and not (r["text"] in seen or seen.add(r["text"]))]
    if not reviews:
        raise RuntimeError(f"No reviews found. Source errors: {errors}")
    answer = build_answer((question or f"How do customers feel about {brand}?").strip(), brand, reviews)
    save_reviews(brand, answer["reviews"])
    return {**answer, "errors": errors}


@app.get("/health")
def health():
    return "ok"


@app.post("/ask")
def ask():
    body = request.get_json(force=True)
    try:
        return jsonify(run_agent((body.get("brand") or "").strip(), body.get("source") or "All sources", body.get("question")))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except RuntimeError as e:
        return jsonify(error=str(e)), 502


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 8000)))
