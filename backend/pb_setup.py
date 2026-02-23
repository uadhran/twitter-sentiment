"""
Run this ONCE after starting PocketBase to create the required collections.

Usage:
  python pb_setup.py
"""

import os, httpx
from dotenv import load_dotenv

load_dotenv()

PB_URL = os.getenv("POCKETBASE_URL", "http://127.0.0.1:8090")
PB_EMAIL = os.getenv("POCKETBASE_EMAIL")
PB_PASSWORD = os.getenv("POCKETBASE_PASSWORD")


def get_token():
    r = httpx.post(
        f"{PB_URL}/api/collections/_superusers/auth-with-password",
        json={"identity": PB_EMAIL, "password": PB_PASSWORD},
    )
    r.raise_for_status()
    return r.json()["token"]


def create_collection(token, schema):
    headers = {"Authorization": token}
    name = schema["name"]
    # Check if exists
    r = httpx.get(f"{PB_URL}/api/collections/{name}", headers=headers)
    if r.status_code == 200:
        print(f"  ✓ '{name}' already exists — skipping")
        return
    r = httpx.post(f"{PB_URL}/api/collections", headers=headers, json=schema)
    if r.status_code in (200, 201):
        print(f"  ✓ Created collection '{name}'")
    else:
        print(f"  ✗ Failed to create '{name}': {r.text}")


def main():
    print(f"\nConnecting to PocketBase at {PB_URL}...")
    token = get_token()
    print("  ✓ Authenticated\n")

    print("Creating collections...")

    # analyses collection
    create_collection(
        token,
        {
            "name": "analyses",
            "type": "base",
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "fields": [
                {"name": "keyword", "type": "text", "required": True},
                {"name": "total", "type": "number", "required": True},
                {"name": "positive", "type": "number"},
                {"name": "negative", "type": "number"},
                {"name": "neutral", "type": "number"},
                {"name": "positive_pct", "type": "number"},
                {"name": "negative_pct", "type": "number"},
                {"name": "neutral_pct", "type": "number"},
                {"name": "avg_compound", "type": "number"},
                {"name": "overall_sentiment", "type": "text"},
                {"name": "analyzed_at", "type": "text"},
            ],
        },
    )

    # tweets collection
    create_collection(
        token,
        {
            "name": "tweets",
            "type": "base",
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "fields": [
                {
                    "name": "analysis",
                    "type": "relation",
                    "collectionId": get_collection_id(token, "analyses"),
                    "cascadeDelete": True,
                },
                {"name": "tweet_id", "type": "text"},
                {"name": "text", "type": "text"},
                {"name": "label", "type": "text"},
                {"name": "compound", "type": "number"},
                {"name": "likes", "type": "number"},
                {"name": "retweets", "type": "number"},
                {"name": "created_at", "type": "text"},
            ],
        },
    )

    print("\n✅ PocketBase setup complete!")
    print(f"   Dashboard: {PB_URL}/_/")
    print("   Collections: analyses, tweets\n")


def get_collection_id(token, name):
    r = httpx.get(f"{PB_URL}/api/collections/{name}", headers={"Authorization": token})
    return r.json().get("id", "")


if __name__ == "__main__":
    main()
