#!/usr/bin/env python3
"""Publish a post to Bluesky using content from Notes.md.

This script posts to Bluesky using the AT Protocol API. It reads content from
the "## Short Social" section in Notes.md or accepts text via --text argument.

Environment variables:
  BLUESKY_HANDLE - Your Bluesky handle (e.g., username.bsky.social)
  BLUESKY_APP_PASSWORD - App password (NOT your main password)

Usage:
  python post_to_bluesky.py [--notes path/to/Notes.md] [--text "Custom text"]
  python post_to_bluesky.py --dry-run  # Preview without posting

Getting an app password:
  1. Go to https://bsky.app/settings/app-passwords
  2. Create a new app password
  3. Copy it and set BLUESKY_APP_PASSWORD in your .env file
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


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


BLUESKY_API_URL = "https://bsky.social"


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


def extract_bluesky_section(notes_path: Path) -> str:
    """Extract the Short Social section from Notes.md."""
    content = notes_path.read_text(encoding="utf-8")
    marker = "## Short Social"
    if marker not in content:
        return ""
    prefix, suffix = content.split(marker, 1)
    body = suffix.split("\n##", 1)[0]
    return body.strip()


def create_session(handle: str, app_password: str) -> tuple[str, str]:
    """
    Create a Bluesky session and get access token.

    Returns (access_token, did)
    """
    url = f"{BLUESKY_API_URL}/xrpc/com.atproto.server.createSession"

    payload = {
        "identifier": handle,
        "password": app_password
    }

    response = requests.post(url, json=payload, timeout=30)

    if response.status_code != 200:
        raise RuntimeError(
            f"Bluesky authentication failed ({response.status_code}): {response.text}"
        )

    data = response.json()
    return data["accessJwt"], data["did"]


def post_to_bluesky(access_token: str, did: str, text: str) -> dict:
    """
    Post a message to Bluesky.

    Returns the API response with the post data.
    """
    url = f"{BLUESKY_API_URL}/xrpc/com.atproto.repo.createRecord"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Create timestamp in ISO 8601 format
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    payload = {
        "repo": did,
        "collection": "app.bsky.feed.post",
        "record": {
            "text": text,
            "createdAt": now,
            "$type": "app.bsky.feed.post"
        }
    }

    response = requests.post(url, json=payload, headers=headers, timeout=30)

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Bluesky API error ({response.status_code}): {response.text}"
        )

    return response.json()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish a post to Bluesky using content from Notes.md."
    )
    parser.add_argument(
        "--notes",
        help="Path to Notes.md (defaults to workspace Notes.md)."
    )
    parser.add_argument(
        "--text",
        help="Override Bluesky copy instead of reading Notes.md."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the post without publishing to Bluesky."
    )
    parser.add_argument(
        "--handle",
        help="Bluesky handle (overrides BLUESKY_HANDLE env var)."
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
        text = extract_bluesky_section(notes_path)
        if not text:
            print("❌ Short Social section in Notes.md is empty.")
            print("   Add a '## Short Social' section or use --text.")
            return 1

    # Bluesky character limit is 300
    if len(text) > 300:
        print(f"⚠️  Warning: Post is {len(text)} characters (limit: 300)")
        print("   The post will be rejected by Bluesky.")
        print()

    print("Bluesky post to publish:\n")
    print(text.strip())
    print(f"\n({len(text)} characters)\n")

    if args.dry_run:
        print("✅ Dry run enabled; skipping Bluesky API call.")
        return 0

    # Get credentials
    handle = args.handle or os.getenv("BLUESKY_HANDLE")
    app_password = os.getenv("BLUESKY_APP_PASSWORD")

    if not handle:
        print("❌ Missing BLUESKY_HANDLE.")
        print("   Set it in .env or use --handle")
        print("   Example: username.bsky.social")
        return 1

    if not app_password:
        print("❌ Missing BLUESKY_APP_PASSWORD.")
        print("   Set it in your .env file.")
        print("   Get an app password from: https://bsky.app/settings/app-passwords")
        return 1

    try:
        print("Authenticating with Bluesky...")
        access_token, did = create_session(handle, app_password)
        print(f"   ✅ Authenticated as {handle}")

        print("Publishing post...")
        result = post_to_bluesky(access_token, did, text)

        uri = result.get("uri", "")
        cid = result.get("cid", "")

        print(f"✅ Post published successfully!")
        print(f"   URI: {uri}")
        print(f"   CID: {cid}")

        # Extract the post ID from the URI to construct a web URL
        # URI format: at://did:plc:xxx/app.bsky.feed.post/xxxxx
        if uri:
            post_id = uri.split("/")[-1]
            # Construct the web URL (this is a best guess, may vary)
            web_url = f"https://bsky.app/profile/{handle}/post/{post_id}"
            print(f"   URL: {web_url}")

        return 0

    except Exception as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
