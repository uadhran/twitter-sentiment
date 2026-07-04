import asyncio
import json
import logging
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from threading import Thread
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import httpx
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from gnews import GNews
from twikit import Client
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_TWEET_COUNT = 20
MAX_TWEET_COUNT = 100  # Higher limit supported via TwitterAPI.io pagination
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100
MIN_KEYWORD_LEN = 2
MAX_KEYWORD_LEN = 80
VADER_POS_THRESHOLD = 0.05
VADER_NEG_THRESHOLD = -0.05
PB_AUTH_TIMEOUT_SECONDS = int(os.getenv("PB_AUTH_TIMEOUT", "5"))
PB_READ_TIMEOUT_SECONDS = int(os.getenv("PB_READ_TIMEOUT", "5"))
PB_WRITE_TIMEOUT_SECONDS = int(os.getenv("PB_WRITE_TIMEOUT", "10"))
FETCH_TIMEOUT_SECONDS = int(os.getenv("FETCH_TIMEOUT", "30"))
PB_TWEET_BATCH_SIZE = 10
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 30
CACHE_TTL_SECONDS = 30
MAX_CACHE_ENTRIES = 200
MIN_REFRESH_SECONDS = 45

TWITTERAPI_IO_KEY = os.getenv("TWITTERAPI_IO_KEY", "").strip()
TWITTERAPI_IO_BASE = "https://api.twitterapi.io"
XQUIK_API_KEY = os.getenv("XQUIK_API_KEY", "").strip()
XQUIK_BASE = (os.getenv("XQUIK_BASE") or "https://xquik.com").rstrip("/")
TWITTER_SOURCE = os.getenv("TWITTER_SOURCE", "").strip().lower()


_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = Flask(__name__)


def get_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "http://localhost,http://127.0.0.1")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


CORS(app, resources={r"/api/*": {"origins": get_cors_origins()}})

analyzer = SentimentIntensityAnalyzer()
twitter_client = Client("en-US")
_logged_in = False
_request_times: dict[str, deque[float]] = defaultdict(deque)
_analysis_cache: dict[tuple[str, int, str], tuple[float, dict]] = {}
_last_query: dict[tuple[str, str, str], float] = {}

PB_URL = os.getenv("POCKETBASE_URL", "http://127.0.0.1:8090")
PB_EMAIL = os.getenv("POCKETBASE_EMAIL", "")
PB_PASSWORD = os.getenv("POCKETBASE_PASSWORD", "")
_pb_token: str | None = None
_pb_token_lock = threading.Lock()


# ── Security headers ──────────────────────────────────────────────────────────


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


# ── Frontend ──────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory(str(_FRONTEND_DIR), "index.html")


@app.route("/style.css")
def shared_css():
    return send_from_directory(str(_FRONTEND_DIR), "style.css")


# ── PocketBase auth ───────────────────────────────────────────────────────────


def pb_token() -> str | None:
    """Authenticate with PocketBase superuser and cache token (thread-safe)."""
    global _pb_token
    with _pb_token_lock:
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
                logger.info("[pocketbase] Authenticated successfully")
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
        with _pb_token_lock:
            _pb_token = None
        token = pb_token()
        if not token:
            return r
        headers["Authorization"] = token
        r = httpx.request(method, url, headers=headers, **kwargs)
    return r


def pb_save(summary: dict[str, Any], tweets: list[dict[str, Any]]) -> None:
    """Save analysis + tweets to PocketBase (best-effort)."""
    if not pb_token():
        logger.info("[pocketbase] Skipping save: no token (PocketBase not configured)")
        return

    try:
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
                "source": summary.get("source", ""),
            },
            timeout=PB_WRITE_TIMEOUT_SECONDS,
        )
        if not r or r.status_code not in (200, 201):
            logger.warning(
                "[pocketbase] Failed to save analysis: %s",
                r.text if r else "no response",
            )
            return

        analysis_id = r.json().get("id")
        logger.info(
            "[pocketbase] Saved analysis %s for '%s'", analysis_id, summary["keyword"]
        )

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


def _safe_cookies_path(raw: str) -> Path:
    """Resolve and validate the cookies file path stays within project directory."""
    project_root = Path(__file__).parent.resolve()
    resolved = (project_root / raw).resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError:
        raise ValueError("COOKIES_FILE must be within the project directory")
    return resolved


def convert_cookies_if_needed(cookies_path: Path) -> Path:
    with open(cookies_path, "r") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return cookies_path
    if isinstance(data, list):
        converted = {c["name"]: c["value"] for c in data}
        out = cookies_path.with_name(cookies_path.stem + "_twikit.json")
        with open(out, "w") as f:
            json.dump(converted, f, indent=2)
        logger.info("[auth] Converted Cookie-Editor cookies to %s", out)
        return out
    raise ValueError("Unknown cookies.json format")


async def ensure_login():
    global _logged_in
    if _logged_in:
        return
    raw_path = os.getenv("COOKIES_FILE", "cookies.json")
    cookies_path = _safe_cookies_path(raw_path)
    if not cookies_path.exists():
        raise ValueError(
            "Cookies file not found. Export cookies from x.com "
            "using the Cookie-Editor extension and save as backend/cookies.json"
        )
    final = convert_cookies_if_needed(cookies_path)
    twitter_client.load_cookies(str(final))
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
    if not isinstance(value, (int, float, str)):
        raise ValueError(f"Expected integer, got {type(value).__name__}")
    if isinstance(value, str) and not re.match(r"^-?\d+$", value.strip()):
        raise ValueError(f"Invalid integer: {value!r}")
    parsed = int(float(value))
    return max(minimum, min(parsed, maximum))


def validate_keyword(keyword: str) -> str | None:
    """Return error message if keyword is invalid, else None."""
    normalized = unicodedata.normalize("NFKD", keyword)
    if len(normalized) < MIN_KEYWORD_LEN or len(normalized) > MAX_KEYWORD_LEN:
        return f"Keyword must be {MIN_KEYWORD_LEN}–{MAX_KEYWORD_LEN} characters"
    if re.search(r"[\u200b-\u200f\u2028\u2029\ufeff]", keyword):
        return "Keyword contains invalid characters"
    return None


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


def is_refresh_too_soon(client_key: str, keyword: str, source: str) -> bool:
    """Throttle repeat requests for the same keyword per client."""
    now = time.time()
    key = (client_key, keyword.lower(), source)
    last = _last_query.get(key)
    if last and now - last < MIN_REFRESH_SECONDS:
        return True
    _last_query[key] = now
    return False


def get_cached(keyword: str, count: int, source: str) -> dict | None:
    key = (keyword.lower(), count, source)
    cached = _analysis_cache.get(key)
    if not cached:
        return None
    ts, payload = cached
    if time.time() - ts > CACHE_TTL_SECONDS:
        _analysis_cache.pop(key, None)
        return None
    return payload


def set_cached(keyword: str, count: int, source: str, payload: dict) -> None:
    if len(_analysis_cache) >= MAX_CACHE_ENTRIES:
        oldest_key = min(_analysis_cache.items(), key=lambda kv: kv[1][0])[0]
        _analysis_cache.pop(oldest_key, None)
    _analysis_cache[(keyword.lower(), count, source)] = (time.time(), payload)


async def _fetch_raw_twitterapi_io(keyword: str, count: int) -> list[dict]:
    """Fetch tweets from TwitterAPI.io. Paginates until count reached or 429 stops it."""
    if not TWITTERAPI_IO_KEY:
        raise ValueError("TWITTERAPI_IO_KEY not configured")

    tweets: list[dict] = []
    cursor: str | None = None

    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
        while len(tweets) < count:
            params: dict[str, Any] = {"query": keyword, "queryType": "Latest"}
            if cursor:
                params["cursor"] = cursor

            resp = await client.get(
                f"{TWITTERAPI_IO_BASE}/twitter/tweet/advanced_search",
                params=params,
                headers={"x-api-key": TWITTERAPI_IO_KEY},
            )

            if resp.status_code == 429:
                if tweets:
                    # Got some tweets already — return them as-is, don't fall back
                    logger.warning(
                        "[twitterapi.io] 429 on page %d — returning %d tweets collected so far",
                        len(tweets) // 20 + 1,
                        len(tweets),
                    )
                    break
                # 429 on the very first request — let caller fall back to Twikit
                resp.raise_for_status()

            resp.raise_for_status()
            data = resp.json()
            batch = data.get("tweets", [])
            tweets.extend(batch)
            logger.info(
                "[twitterapi.io] fetched %d tweets (total: %d)", len(batch), len(tweets)
            )

            if not data.get("has_next_page") or not batch:
                break
            cursor = data.get("next_cursor")

    return tweets[:count]


def _normalize_twitterapi_io_tweet(raw: dict) -> dict:
    """Map a TwitterAPI.io tweet dict to our internal tweet format."""
    text = raw.get("text", "")
    cleaned = clean_tweet(text)
    scores = analyzer.polarity_scores(cleaned)
    label = classify(scores["compound"])
    return {
        "id": str(raw.get("id", "")),
        "text": text,
        "cleaned": cleaned,
        "compound": round(scores["compound"], 4),
        "positive": round(scores["pos"], 4),
        "negative": round(scores["neg"], 4),
        "neutral": round(scores["neu"], 4),
        "label": label,
        "created_at": raw.get("createdAt") or None,
        "likes": raw.get("likeCount", 0) or 0,
        "retweets": raw.get("retweetCount", 0) or 0,
    }


async def _fetch_raw_xquik(keyword: str, count: int) -> list[dict]:
    """Fetch tweets from Xquik's X search endpoint."""
    if not XQUIK_API_KEY:
        raise ValueError("XQUIK_API_KEY not configured")

    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
        resp = await client.get(
            f"{XQUIK_BASE}/api/v1/x/tweets/search",
            params={"q": keyword, "limit": count},
            headers={"x-api-key": XQUIK_API_KEY},
        )
        resp.raise_for_status()
        data = resp.json()

    if not isinstance(data, dict):
        return []
    return _extract_xquik_tweets(data)[:count]


def _extract_xquik_tweets(data: dict) -> list[dict]:
    """Return tweet records from supported Xquik response envelopes."""
    candidates: list[Any] = [data.get("tweets"), data.get("items")]
    nested = data.get("data")
    if isinstance(nested, dict):
        candidates.extend([nested.get("tweets"), nested.get("items")])
    else:
        candidates.append(nested)

    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _normalize_xquik_tweet(raw: dict) -> dict:
    """Map an Xquik tweet dict to our internal tweet format."""
    text = str(_first_present(raw.get("text"), raw.get("full_text"), ""))
    cleaned = clean_tweet(text)
    scores = analyzer.polarity_scores(cleaned)
    label = classify(scores["compound"])
    created_at = _first_present(
        raw.get("created_at"),
        raw.get("createdAt"),
        raw.get("created"),
    )
    return {
        "id": str(_first_present(raw.get("id"), raw.get("tweet_id"), "")),
        "text": text,
        "cleaned": cleaned,
        "compound": round(scores["compound"], 4),
        "positive": round(scores["pos"], 4),
        "negative": round(scores["neg"], 4),
        "neutral": round(scores["neu"], 4),
        "label": label,
        "created_at": created_at,
        "likes": _first_present(
            raw.get("like_count"),
            raw.get("likeCount"),
            raw.get("likes"),
            raw.get("favorite_count"),
            0,
        ),
        "retweets": _first_present(
            raw.get("retweet_count"),
            raw.get("retweetCount"),
            raw.get("retweets"),
            0,
        ),
    }


async def _fetch_raw_twikit(keyword: str, count: int) -> list:
    """Fetch raw tweet objects via Twikit scraping."""
    await ensure_login()
    page = await asyncio.wait_for(
        twitter_client.search_tweet(keyword, product="Latest", count=count),
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    all_tweets = list(page)
    logger.info("[twikit] page 1: %d tweets", len(all_tweets))
    page_num = 1
    while len(all_tweets) < count and page_num < 5:
        try:
            if not hasattr(page, "next") or page.next is None:
                break
            next_page = await asyncio.wait_for(
                page.next(), timeout=FETCH_TIMEOUT_SECONDS
            )
            if not next_page:
                break
            page = next_page
            page_num += 1
            batch = list(page)
            all_tweets.extend(batch)
            logger.info(
                "[twikit] page %d: %d tweets (total: %d)",
                page_num,
                len(batch),
                len(all_tweets),
            )
        except Exception as e:
            logger.warning("[twikit] pagination stopped at page %d: %s", page_num, e)
            break
    return all_tweets[:count]


def _normalize_twikit_tweet(tweet: Any) -> dict:
    text = tweet.text
    cleaned = clean_tweet(text)
    scores = analyzer.polarity_scores(cleaned)
    label = classify(scores["compound"])
    return {
        "id": str(tweet.id),
        "text": text,
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


async def _fetch_raw_gnews(keyword: str, count: int) -> list[dict]:
    """Fetch news articles from Google News RSS via the gnews library (no API key needed, English only)."""
    loop = asyncio.get_event_loop()
    gn = GNews(language="en", max_results=count)
    articles = await loop.run_in_executor(None, gn.get_news, keyword)
    logger.info("[gnews] requested %d, got %d articles", count, len(articles))
    return articles


def _normalize_gnews_article(raw: dict) -> dict:
    """Map a gnews library article dict to our internal format."""
    text = f"{raw.get('title', '')} {raw.get('description', '')}".strip()
    cleaned = clean_tweet(text)
    scores = analyzer.polarity_scores(cleaned)
    label = classify(scores["compound"])
    publisher = raw.get("publisher", {})
    return {
        "id": raw.get("url", ""),
        "text": text,
        "cleaned": cleaned,
        "compound": round(scores["compound"], 4),
        "positive": round(scores["pos"], 4),
        "negative": round(scores["neg"], 4),
        "neutral": round(scores["neu"], 4),
        "label": label,
        "created_at": raw.get("published date") or None,
        "likes": 0,
        "retweets": 0,
        "source_name": publisher.get("title", "")
        if isinstance(publisher, dict)
        else str(publisher),
        "url": raw.get("url", ""),
    }


def _build_response(keyword: str, results: list[dict], source: str) -> dict:
    sentiments = {"positive": 0, "negative": 0, "neutral": 0}
    compounds = []
    for t in results:
        sentiments[t["label"]] += 1
        compounds.append(t["compound"])
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
        "source": source,
    }
    return {"tweets": results, "summary": summary, "keyword": keyword}


async def fetch_and_analyze(keyword: str, count: int, source: str = "twitter") -> dict:
    """Fetch and analyze content. source='twitter' uses configured X sources; source='news' uses Google News RSS."""
    if source == "news":
        raw = await _fetch_raw_gnews(keyword, count)
        results = [_normalize_gnews_article(a) for a in raw]
        logger.info("[fetch] GNews: %d articles", len(results))
        return _build_response(keyword, results, "google-news")

    if TWITTER_SOURCE == "xquik":
        try:
            raw = await _fetch_raw_xquik(keyword, count)
            results = [_normalize_xquik_tweet(t) for t in raw]
            logger.info("[fetch] Xquik: %d tweets", len(results))
            return _build_response(keyword, results, "xquik")
        except Exception as e:
            logger.warning("[fetch] Xquik failed: %s; falling back", e)

    # Twitter path: TwitterAPI.io, optional Xquik, then Twikit fallback
    if TWITTERAPI_IO_KEY:
        try:
            raw = await _fetch_raw_twitterapi_io(keyword, count)
            results = [_normalize_twitterapi_io_tweet(t) for t in raw]
            logger.info("[fetch] TwitterAPI.io: %d tweets", len(results))
            return _build_response(keyword, results, "twitterapi.io")
        except Exception as e:
            logger.warning(
                "[fetch] TwitterAPI.io failed: %s — falling back to Twikit", e
            )

    if XQUIK_API_KEY:
        try:
            raw = await _fetch_raw_xquik(keyword, count)
            results = [_normalize_xquik_tweet(t) for t in raw]
            logger.info("[fetch] Xquik: %d tweets", len(results))
            return _build_response(keyword, results, "xquik")
        except Exception as e:
            logger.warning("[fetch] Xquik failed: %s; falling back to Twikit", e)

    raw = await _fetch_raw_twikit(keyword, count)
    logger.info("[fetch] Twikit: %d / %d tweets", len(raw), count)
    return _build_response(keyword, [_normalize_twikit_tweet(t) for t in raw], "twikit")


_globe_cache: dict[str, tuple[float, dict]] = {}
GLOBE_CACHE_TTL = 300  # 5 minutes


async def fetch_globe_news(country: str) -> dict:
    """Fetch top news for a country (or global if country='world') and score sentiment."""
    loop = asyncio.get_event_loop()
    if country == "world":
        gn = GNews(language="en", max_results=20)
    else:
        gn = GNews(language="en", country=country.upper(), max_results=20)
    articles = await loop.run_in_executor(None, gn.get_top_news)
    results = [_normalize_gnews_article(a) for a in articles]
    logger.info("[globe] %s: %d articles", country.upper(), len(results))
    return _build_response(country.upper(), results, "google-news")


# ── Routes ────────────────────────────────────────────────────────────────────


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    keyword = data.get("keyword", "").strip()
    source = data.get("source", "twitter")
    if source not in ("twitter", "news"):
        return jsonify({"error": "source must be 'twitter' or 'news'"}), 400

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

    keyword_error = validate_keyword(keyword)
    if keyword_error:
        return jsonify({"error": keyword_error}), 400

    client_key = (
        (request.headers.get("X-Forwarded-For", request.remote_addr or "unknown"))
        .split(",")[0]
        .strip()
    )
    if is_rate_limited(client_key):
        return jsonify({"error": "Too many requests. Please retry shortly."}), 429
    if is_refresh_too_soon(client_key, keyword, source):
        return jsonify(
            {
                "error": f"Please wait {MIN_REFRESH_SECONDS}s before refreshing this keyword."
            }
        ), 429

    try:
        cached = get_cached(keyword, count, source)
        if cached:
            return jsonify({**cached, "cached": True})
        result = asyncio.run(fetch_and_analyze(keyword, count, source))
    except ValueError as e:
        logger.warning("[analyze] Validation error for keyword=%r: %s", keyword, e)
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("[analyze] Error fetching tweets for keyword=%r", keyword)
        return jsonify({"error": "Failed to fetch tweets. Please try again."}), 502

    if not result["tweets"]:
        label = "articles" if source == "news" else "tweets"
        return jsonify({"error": f"No {label} found for this keyword."}), 404

    set_cached(keyword, count, source, result)
    Thread(
        target=pb_save, args=(result["summary"], result["tweets"]), daemon=False
    ).start()

    return jsonify(result)


@app.route("/globe")
def globe_page():
    return send_from_directory(str(_FRONTEND_DIR), "globe.html")


@app.route("/api/globe", methods=["GET"])
def globe_news():
    country = request.args.get("country", "world").lower().strip()
    if country != "world" and not re.match(r"^[a-z]{2}$", country):
        return jsonify({"error": "Invalid country code"}), 400
    client_key = (
        (request.headers.get("X-Forwarded-For", request.remote_addr or "unknown"))
        .split(",")[0]
        .strip()
    )
    if is_rate_limited(client_key):
        return jsonify({"error": "Too many requests"}), 429
    now = time.time()
    if country in _globe_cache:
        ts, data = _globe_cache[country]
        if now - ts < GLOBE_CACHE_TTL:
            return jsonify({**data, "cached": True})
    try:
        result = asyncio.run(fetch_globe_news(country))
    except Exception:
        logger.exception("[globe] Error for country=%r", country)
        return jsonify({"error": "Failed to fetch news"}), 502
    if not result["tweets"]:
        return jsonify({"error": "No news found for this country"}), 404
    _globe_cache[country] = (now, result)
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


@app.route("/api/history/<record_id>", methods=["DELETE"])
def delete_history(record_id):
    """Delete a single analysis record (and its tweets via cascade)."""
    if not re.match(r"^[a-zA-Z0-9]+$", record_id):
        return jsonify({"error": "Invalid record id"}), 400
    r = pb_request(
        "DELETE",
        f"{PB_URL}/api/collections/analyses/records/{record_id}",
        timeout=PB_WRITE_TIMEOUT_SECONDS,
    )
    if r is None:
        return jsonify({"error": "PocketBase not available"}), 503
    if r.status_code in (204, 404):
        return jsonify({"ok": True})
    return jsonify({"error": "Delete failed"}), r.status_code


@app.route("/api/health", methods=["GET"])
def health():
    pb_ok = False
    try:
        r = pb_request("GET", f"{PB_URL}/api/health", timeout=2)
        pb_ok = r is not None and r.status_code == 200
    except Exception:
        pass
    return jsonify({"status": "ok", "pocketbase": pb_ok})


if __name__ == "__main__":
    app.run(
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
        port=int(os.getenv("PORT", "5000")),
    )
