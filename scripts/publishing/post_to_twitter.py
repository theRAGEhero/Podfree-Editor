#!/usr/bin/env python3
"""Publish a tweet/post to Twitter/X using content from Notes.md.

This script posts to Twitter/X using the Twitter API v2. It reads content from
the "## Short Social" section in Notes.md or accepts text via --text argument.

Environment variables:
  TWITTER_API_KEY - Twitter API key (from Developer Portal)
  TWITTER_API_SECRET - Twitter API secret key
  TWITTER_ACCESS_TOKEN - User access token
  TWITTER_ACCESS_TOKEN_SECRET - User access token secret
  TWITTER_BEARER_TOKEN - Bearer token (alternative to OAuth 1.0a)

Usage:
  python post_to_twitter.py [--notes path/to/Notes.md] [--text "Custom text"]
  python post_to_twitter.py --dry-run  # Preview without posting

Note: You need a Twitter Developer account and project with "Read and Write" permissions.
Visit: https://developer.twitter.com/en/portal/dashboard
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

import requests
from requests_oauthlib import OAuth1


def load_env_file(filepath: Path) -> None:
    """Populate os.environ with values from a .env file if present."""
    env_path = filepath
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


SCRIPT_DIR = Path(__file__).resolve().parent
load_env_file(SCRIPT_DIR / ".env")
load_env_file(SCRIPT_DIR.parent / ".env")
load_env_file(Path.cwd() / ".env")


def find_notes_file(explicit: Optional[str]) -> Optional[Path]:
    """Find the Notes.md file in the workspace."""
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Notes file not found: {path}")
        return path

    primary = Path.cwd() / "Notes.md"
    if primary.is_file():
        return primary

    candidates = sorted(Path.cwd().glob("Notes*.md"))
    if candidates:
        return candidates[0]
    return None


def extract_twitter_section(notes_path: Path) -> str:
    """Extract the Short Social section from Notes.md."""
    content = notes_path.read_text(encoding="utf-8")
    marker = "## Short Social"
    if marker not in content:
        return ""
    prefix, suffix = content.split(marker, 1)
    body = suffix.split("\n##", 1)[0]
    return body.strip()


def post_to_twitter(
    api_key: str,
    api_secret: str,
    access_token: str,
    access_token_secret: str,
    text: str
) -> dict:
    """
    Post a tweet using Twitter API v2 with OAuth 1.0a User Context.

    Returns the API response with the tweet data.
    """
    url = "https://api.twitter.com/2/tweets"

    # Twitter API v2 requires OAuth 1.0a for user context posting
    auth = OAuth1(
        api_key,
        api_secret,
        access_token,
        access_token_secret
    )

    payload = {"text": text}

    response = requests.post(url, json=payload, auth=auth, timeout=30)

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Twitter API error ({response.status_code}): {response.text}"
        )

    return response.json()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish a tweet to Twitter/X using content from Notes.md."
    )
    parser.add_argument(
        "--notes",
        help="Path to Notes.md (defaults to workspace Notes.md)."
    )
    parser.add_argument(
        "--text",
        help="Override Twitter copy instead of reading Notes.md."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the tweet without posting to Twitter."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Get post text
    text = args.text
    notes_path: Optional[Path] = None
    if not text:
        notes_path = find_notes_file(args.notes)
        if not notes_path:
            print("❌ Notes.md not found. Provide --text or create Notes.md.")
            return 1
        text = extract_twitter_section(notes_path)
        if not text:
            print("❌ Short Social section in Notes.md is empty.")
            print("   Add a '## Short Social' section or use --text.")
            return 1

    # Check character limit
    if len(text) > 280:
        print(f"⚠️  Warning: Tweet is {len(text)} characters (limit: 280)")
        print("   The post may be rejected by Twitter.")
        print()

    print("Tweet to publish:\n")
    print(text.strip())
    print(f"\n({len(text)} characters)\n")

    if args.dry_run:
        print("✅ Dry run enabled; skipping Twitter API call.")
        return 0

    # Get credentials
    api_key = os.getenv("TWITTER_API_KEY")
    api_secret = os.getenv("TWITTER_API_SECRET")
    access_token = os.getenv("TWITTER_ACCESS_TOKEN")
    access_token_secret = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

    missing = []
    if not api_key:
        missing.append("TWITTER_API_KEY")
    if not api_secret:
        missing.append("TWITTER_API_SECRET")
    if not access_token:
        missing.append("TWITTER_ACCESS_TOKEN")
    if not access_token_secret:
        missing.append("TWITTER_ACCESS_TOKEN_SECRET")

    if missing:
        print(f"❌ Missing credentials: {', '.join(missing)}")
        print("   Set them in your .env file.")
        print("   Get credentials from: https://developer.twitter.com/en/portal/dashboard")
        return 1

    try:
        print("Publishing to Twitter/X...")
        result = post_to_twitter(api_key, api_secret, access_token, access_token_secret, text)
        tweet_data = result.get("data", {})
        tweet_id = tweet_data.get("id", "unknown")
        tweet_text = tweet_data.get("text", "")

        print(f"✅ Tweet posted successfully!")
        print(f"   Tweet ID: {tweet_id}")
        if tweet_id != "unknown":
            print(f"   URL: https://twitter.com/i/web/status/{tweet_id}")
        return 0

    except Exception as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
