from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest.mock import patch


class DummyFlask:
    def __init__(self, *_args, **_kwargs):
        pass

    def after_request(self, handler):
        return handler

    def route(self, *_args, **_kwargs):
        def decorator(handler):
            return handler

        return decorator

    def run(self, *_args, **_kwargs):
        return None


class DummyAnalyzer:
    def polarity_scores(self, _text):
        return {"compound": 0.0, "pos": 0.0, "neg": 0.0, "neu": 1.0}


class DummyClient:
    def __init__(self, *_args, **_kwargs):
        pass


class DummyGNews:
    def __init__(self, *_args, **_kwargs):
        pass


class DummyAsyncClient:
    def __init__(self, *_args, **_kwargs):
        pass


class DummyResponse:
    status_code = 200
    text = ""

    def json(self):
        return {}

    def raise_for_status(self):
        return None


def load_app_module():
    flask_module = types.ModuleType("flask")
    flask_module.Flask = DummyFlask
    flask_module.jsonify = lambda payload: payload
    flask_module.request = types.SimpleNamespace()
    flask_module.send_from_directory = lambda *_args, **_kwargs: ""

    flask_cors_module = types.ModuleType("flask_cors")
    flask_cors_module.CORS = lambda *_args, **_kwargs: None

    gnews_module = types.ModuleType("gnews")
    gnews_module.GNews = DummyGNews

    twikit_module = types.ModuleType("twikit")
    twikit_module.Client = DummyClient

    dotenv_module = types.ModuleType("dotenv")
    dotenv_module.load_dotenv = lambda *_args, **_kwargs: None

    httpx_module = types.ModuleType("httpx")
    httpx_module.AsyncClient = DummyAsyncClient
    httpx_module.Response = DummyResponse
    httpx_module.post = lambda *_args, **_kwargs: DummyResponse()
    httpx_module.request = lambda *_args, **_kwargs: DummyResponse()

    vader_package = types.ModuleType("vaderSentiment")
    vader_module = types.ModuleType("vaderSentiment.vaderSentiment")
    vader_module.SentimentIntensityAnalyzer = DummyAnalyzer

    with patch.dict(
        sys.modules,
        {
            "flask": flask_module,
            "flask_cors": flask_cors_module,
            "dotenv": dotenv_module,
            "gnews": gnews_module,
            "httpx": httpx_module,
            "twikit": twikit_module,
            "vaderSentiment": vader_package,
            "vaderSentiment.vaderSentiment": vader_module,
        },
    ):
        sys.modules.pop("app", None)
        return importlib.import_module("app")


class AppHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_module = load_app_module()

    def setUp(self):
        self.app_module._analysis_cache.clear()
        self.app_module._last_query.clear()

    def test_cache_is_source_aware(self):
        self.app_module.set_cached("openai", 10, "twitter", {"summary": "twitter"})

        self.assertEqual(
            self.app_module.get_cached("OpenAI", 10, "twitter"),
            {"summary": "twitter"},
        )
        self.assertIsNone(self.app_module.get_cached("OpenAI", 10, "news"))

    def test_refresh_throttle_is_source_aware(self):
        self.assertFalse(
            self.app_module.is_refresh_too_soon("127.0.0.1", "openai", "twitter")
        )
        self.assertTrue(
            self.app_module.is_refresh_too_soon("127.0.0.1", "openai", "twitter")
        )
        self.assertFalse(
            self.app_module.is_refresh_too_soon("127.0.0.1", "openai", "news")
        )

    def test_extract_xquik_tweets_accepts_supported_envelopes(self):
        top_level = self.app_module._extract_xquik_tweets({"tweets": [{"id": "1"}]})
        nested = self.app_module._extract_xquik_tweets(
            {"data": {"tweets": [{"id": "2"}]}}
        )

        self.assertEqual(top_level, [{"id": "1"}])
        self.assertEqual(nested, [{"id": "2"}])

    def test_normalize_xquik_tweet_maps_public_fields(self):
        normalized = self.app_module._normalize_xquik_tweet(
            {
                "id": "1900000000000000000",
                "text": "Hello https://example.com #AI",
                "created_at": "2026-07-04T10:00:00Z",
                "like_count": 4,
                "retweet_count": 2,
            }
        )

        self.assertEqual(normalized["id"], "1900000000000000000")
        self.assertEqual(normalized["cleaned"], "Hello AI")
        self.assertEqual(normalized["created_at"], "2026-07-04T10:00:00Z")
        self.assertEqual(normalized["likes"], 4)
        self.assertEqual(normalized["retweets"], 2)
        self.assertEqual(normalized["label"], "neutral")


if __name__ == "__main__":
    unittest.main()
