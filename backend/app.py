import asyncio
import json
import logging
import os
import re
import time
from threading import Thread
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import httpx
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
from twikit import Client
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_TWEET_COUNT = 20
MAX_TWEET_COUNT = 40
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100
MIN_KEYWORD_LEN = 1
MAX_KEYWORD_LEN = 80
VADER_POS_THRESHOLD = 0.05
VADER_NEG_THRESHOLD = -0.05
PB_AUTH_TIMEOUT_SECONDS = 5
PB_READ_TIMEOUT_SECONDS = 5
PB_WRITE_TIMEOUT_SECONDS = 10
PB_TWEET_BATCH_SIZE = 10
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 30
CACHE_TTL_SECONDS = 30
MAX_CACHE_ENTRIES = 200
MIN_REFRESH_SECONDS = 45

app = Flask(__name__)


def get_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "http://localhost,http://127.0.0.1")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if os.getenv("ALLOW_FILE_ORIGIN", "false").lower() == "true":
        origins.append("null")
    return origins


CORS(app, resources={r"/api/*": {"origins": get_cors_origins()}})

analyzer = SentimentIntensityAnalyzer()
twitter_client = Client("en-US")
_logged_in = False
_request_times: dict[str, deque[float]] = defaultdict(deque)
_analysis_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_last_query: dict[tuple[str, str], float] = {}

PB_URL = os.getenv("POCKETBASE_URL", "http://127.0.0.1:8090")
PB_EMAIL = os.getenv("POCKETBASE_EMAIL", "")
PB_PASSWORD = os.getenv("POCKETBASE_PASSWORD", "")
_pb_token = None


# ── PocketBase auth ───────────────────────────────────────────────────────────


def pb_token() -> str | None:
    """Authenticate with PocketBase superuser and cache token."""
    global _pb_token
    if _pb_token:
        return _pb_token
    if not PB_EMAIL or not PB_PASSWORD:
        return None
    try:
        r = httpx.post(
            f"{PB_URL}/api/collections/_superusers/auth-with-password",
            json={"identity": PB_EMAIL, "password": PB_PASSWORD},
            timeout=PB_AUTH_TIMEOUT_SECONDS,
        )
        if r.status_code == 200:
            _pb_token = r.json().get("token")
            return _pb_token
    except Exception as e:
        logger.warning("[pocketbase] Auth failed: %s", e)
    return None


def pb_request(method: str, url: str, **kwargs) -> httpx.Response | None:
    """Make an authenticated PocketBase request, retrying once on 401."""
    global _pb_token
    token = pb_token()
    if not token:
        return None
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = token
    r = httpx.request(method, url, headers=headers, **kwargs)
    if r.status_code == 401:
        _pb_token = None
        token = pb_token()
        if not token:
            return r
        headers["Authorization"] = token
        r = httpx.request(method, url, headers=headers, **kwargs)
    return r


def pb_save(summary: dict[str, Any], tweets: list[dict[str, Any]]) -> None:
    """Save analysis + tweets to PocketBase (best-effort, non-blocking)."""
    if not pb_token():
        logger.info("[pocketbase] Skipping save: no token (PocketBase not configured)")
        return

    try:
        # 1. Save analysis summary
        r = pb_request(
            "POST",
            f"{PB_URL}/api/collections/analyses/records",
            json={
                "keyword": summary["keyword"],
                "total": summary["total"],
                "positive": summary["positive"],
                "negative": summary["negative"],
                "neutral": summary["neutral"],
                "positive_pct": summary["positive_pct"],
                "negative_pct": summary["negative_pct"],
                "neutral_pct": summary["neutral_pct"],
                "avg_compound": summary["avg_compound"],
                "overall_sentiment": summary["overall_sentiment"],
                "analyzed_at": summary["analyzed_at"],
            },
            timeout=PB_WRITE_TIMEOUT_SECONDS,
        )
        if not r or r.status_code not in (200, 201):
            logger.warning("[pocketbase] Failed to save analysis: %s", r.text)
            return

        analysis_id = r.json().get("id")
        logger.info("[pocketbase] Saved analysis %s for '%s'", analysis_id, summary["keyword"])

        # 2. Save tweets in batches.
        for i in range(0, len(tweets), PB_TWEET_BATCH_SIZE):
            batch = tweets[i : i + PB_TWEET_BATCH_SIZE]
            for t in batch:
                pb_request(
                    "POST",
                    f"{PB_URL}/api/collections/tweets/records",
                    json={
                        "analysis": analysis_id,
                        "tweet_id": t["id"],
                        "text": t["text"],
                        "label": t["label"],
                        "compound": t["compound"],
                        "likes": t["likes"],
                        "retweets": t["retweets"],
                        "created_at": t["created_at"] or "",
                    },
                    timeout=PB_WRITE_TIMEOUT_SECONDS,
                )
        logger.info("[pocketbase] Saved %s tweets", len(tweets))

    except Exception as e:
        logger.warning("[pocketbase] Save error: %s", e)


def pb_history(limit: int = 20) -> list:
    """Fetch recent analyses from PocketBase."""
    if not pb_token():
        return []
    try:
        r = pb_request(
            "GET",
            f"{PB_URL}/api/collections/analyses/records",
            params={"sort": "-analyzed_at", "perPage": limit},
            timeout=PB_READ_TIMEOUT_SECONDS,
        )
        if r and r.status_code == 200:
            return r.json().get("items", [])
    except Exception as e:
        logger.warning("[pocketbase] History fetch error: %s", e)
    return []


# ── Twitter auth ──────────────────────────────────────────────────────────────


def convert_cookies_if_needed(cookies_path: str) -> str:
    with open(cookies_path, "r") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return cookies_path
    if isinstance(data, list):
        converted = {c["name"]: c["value"] for c in data}
        out = cookies_path.replace(".json", "_twikit.json")
        with open(out, "w") as f:
            json.dump(converted, f, indent=2)
        logger.info("[auth] Converted Cookie-Editor cookies to %s", out)
        return out
    raise ValueError("Unknown cookies.json format")


async def ensure_login():
    global _logged_in
    if _logged_in:
        return
    cookies_path = os.getenv("COOKIES_FILE", "cookies.json")
    if not os.path.exists(cookies_path):
        raise ValueError(
            f"'{cookies_path}' not found. Export cookies from x.com "
            "using the Cookie-Editor extension and save as backend/cookies.json"
        )
    final = convert_cookies_if_needed(cookies_path)
    twitter_client.load_cookies(final)
    _logged_in = True
    logger.info("[auth] Cookies loaded from %s", final)


# ── Helpers ───────────────────────────────────────────────────────────────────


def clean_tweet(text: str) -> str:
    text = re.sub(r"http\S+|www\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"#(\w+)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()




def classify(compound: float) -> str:
    if compound >= VADER_POS_THRESHOLD:
        return "positive"
    if compound <= VADER_NEG_THRESHOLD:
        return "negative"
    return "neutral"


def parse_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """Parse bounded integer values for request params and payload fields."""
    if value is None or value == "":
        return default
    parsed = int(value)
    return max(minimum, min(parsed, maximum))


def is_rate_limited(client_key: str) -> bool:
    """Very lightweight in-memory rate limiting per client key."""
    now = time.time()
    bucket = _request_times[client_key]
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    bucket.append(now)
    return False


def is_refresh_too_soon(client_key: str, keyword: str) -> bool:
    """Throttle repeat requests for the same keyword per client."""
    now = time.time()
    key = (client_key, keyword.lower())
    last = _last_query.get(key)
    if last and now - last < MIN_REFRESH_SECONDS:
        return True
    _last_query[key] = now
    return False


def get_cached(keyword: str, count: int) -> dict | None:
    key = (keyword.lower(), count)
    cached = _analysis_cache.get(key)
    if not cached:
        return None
    ts, payload = cached
    if time.time() - ts > CACHE_TTL_SECONDS:
        _analysis_cache.pop(key, None)
        return None
    return payload


def set_cached(keyword: str, count: int, payload: dict) -> None:
    if len(_analysis_cache) >= MAX_CACHE_ENTRIES:
        # Drop oldest entry (simple, not strict LRU)
        oldest_key = min(_analysis_cache.items(), key=lambda kv: kv[1][0])[0]
        _analysis_cache.pop(oldest_key, None)
    _analysis_cache[(keyword.lower(), count)] = (time.time(), payload)


async def fetch_and_analyze(keyword: str, count: int) -> dict:
    await ensure_login()
    tweets = await twitter_client.search_tweet(keyword, product="Latest", count=count)

    results, sentiments, compounds = (
        [],
        {"positive": 0, "negative": 0, "neutral": 0},
        [],
    )

    for tweet in tweets:
        cleaned = clean_tweet(tweet.text)
        scores = analyzer.polarity_scores(cleaned)
        label = classify(scores["compound"])
        sentiments[label] += 1
        compounds.append(scores["compound"])
        results.append(
            {
                "id": str(tweet.id),
                "text": tweet.text,
                "cleaned": cleaned,
                "compound": round(scores["compound"], 4),
                "positive": round(scores["pos"], 4),
                "negative": round(scores["neg"], 4),
                "neutral": round(scores["neu"], 4),
                "label": label,
                "created_at": tweet.created_at or None,
                "likes": getattr(tweet, "favorite_count", 0) or 0,
                "retweets": getattr(tweet, "retweet_count", 0) or 0,
            }
        )

    total = len(results)
    avg_compound = round(sum(compounds) / total, 4) if total else 0
    summary = {
        "total": total,
        "keyword": keyword,
        "positive": sentiments["positive"],
        "negative": sentiments["negative"],
        "neutral": sentiments["neutral"],
        "positive_pct": round(sentiments["positive"] / total * 100, 1) if total else 0,
        "negative_pct": round(sentiments["negative"] / total * 100, 1) if total else 0,
        "neutral_pct": round(sentiments["neutral"] / total * 100, 1) if total else 0,
        "avg_compound": avg_compound,
        "overall_sentiment": classify(avg_compound),
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"tweets": results, "summary": summary, "keyword": keyword}


# ── Routes ────────────────────────────────────────────────────────────────────


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    keyword = data.get("keyword", "").strip()
    try:
        count = parse_int(
            data.get("count", DEFAULT_TWEET_COUNT),
            default=DEFAULT_TWEET_COUNT,
            minimum=1,
            maximum=MAX_TWEET_COUNT,
        )
    except (TypeError, ValueError):
        return jsonify({"error": "count must be an integer"}), 400

    if not keyword:
        return jsonify({"error": "Keyword is required"}), 400
    if not (MIN_KEYWORD_LEN <= len(keyword) <= MAX_KEYWORD_LEN):
        return jsonify({"error": f"Keyword length must be {MIN_KEYWORD_LEN}-{MAX_KEYWORD_LEN} characters"}), 400

    client_key = (request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")).split(",")[0].strip()
    if is_rate_limited(client_key):
        return jsonify({"error": "Too many requests. Please retry shortly."}), 429
    if is_refresh_too_soon(client_key, keyword):
        return jsonify({"error": f"Please wait {MIN_REFRESH_SECONDS}s before refreshing this keyword."}), 429

    try:
        cached = get_cached(keyword, count)
        if cached:
            return jsonify(cached)
        result = asyncio.run(fetch_and_analyze(keyword, count))
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Twikit error: {str(e)}"}), 502
    if not result["tweets"]:
        return jsonify({"error": "No tweets found for this keyword."}), 404

    set_cached(keyword, count, result)
    # Save to PocketBase async (fire and forget)
    Thread(target=pb_save, args=(result["summary"], result["tweets"]), daemon=True).start()

    return jsonify(result)


@app.route("/api/history", methods=["GET"])
def history():
    """Return recent analyses from PocketBase."""
    try:
        limit = parse_int(
            request.args.get("limit", DEFAULT_HISTORY_LIMIT),
            default=DEFAULT_HISTORY_LIMIT,
            minimum=1,
            maximum=MAX_HISTORY_LIMIT,
        )
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify({"history": pb_history(limit)})


@app.route("/api/health", methods=["GET"])
def health():
    pb_ok = pb_token() is not None
    return jsonify({"status": "ok", "pocketbase": pb_ok})


if __name__ == "__main__":
    app.run(
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
        port=int(os.getenv("PORT", "5000")),
    )
