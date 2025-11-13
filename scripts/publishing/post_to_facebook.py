#!/usr/bin/env python3
"""Publish a Facebook post using content from Notes.md.

This script posts to a Facebook Page using the Graph API. It reads content from
the "## Long Social" section in Notes.md or accepts text via --text argument.

Environment variables:
  FACEBOOK_PAGE_ID - The ID of the Facebook Page to post to
  FACEBOOK_ACCESS_TOKEN - Page access token (long-lived recommended)

Usage:
  python post_to_facebook.py [--notes path/to/Notes.md] [--text "Custom text"]
  python post_to_facebook.py --dry-run  # Preview without posting
"""

from __future__ import annotations

import argparse
import os
import sys
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


def extract_facebook_section(notes_path: Path) -> str:
    """Extract the Long Social section from Notes.md."""
    content = notes_path.read_text(encoding="utf-8")
    marker = "## Long Social"
    if marker not in content:
        return ""
    prefix, suffix = content.split(marker, 1)
    body = suffix.split("\n##", 1)[0]
    return body.strip()


def post_to_facebook_page(page_id: str, access_token: str, message: str) -> dict:
    """
    Post a message to a Facebook Page using the Graph API.

    Returns the API response with the post ID.
    """
    url = f"https://graph.facebook.com/v18.0/{page_id}/feed"

    payload = {
        "message": message,
        "access_token": access_token
    }

    response = requests.post(url, data=payload, timeout=30)

    if response.status_code != 200:
        raise RuntimeError(
            f"Facebook API error ({response.status_code}): {response.text}"
        )

    return response.json()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish a Facebook post using content from Notes.md."
    )
    parser.add_argument(
        "--notes",
        help="Path to Notes.md (defaults to workspace Notes.md)."
    )
    parser.add_argument(
        "--text",
        help="Override Facebook copy instead of reading Notes.md."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the post without publishing to Facebook."
    )
    parser.add_argument(
        "--page-id",
        help="Facebook Page ID (overrides FACEBOOK_PAGE_ID env var)."
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
        text = extract_facebook_section(notes_path)
        if not text:
            print("❌ Long Social section in Notes.md is empty.")
            print("   Add a '## Long Social' section or use --text.")
            return 1

    print("Facebook post to publish:\n")
    print(text.strip())
    print("\n")

    if args.dry_run:
        print("✅ Dry run enabled; skipping Facebook API call.")
        return 0

    # Get credentials
    page_id = args.page_id or os.getenv("FACEBOOK_PAGE_ID")
    access_token = os.getenv("FACEBOOK_ACCESS_TOKEN")

    if not page_id:
        print("❌ Missing FACEBOOK_PAGE_ID.")
        print("   Set it in .env or use --page-id")
        return 1

    if not access_token:
        print("❌ Missing FACEBOOK_ACCESS_TOKEN.")
        print("   Set it in your .env file.")
        print("   Get a Page access token from: https://developers.facebook.com/tools/explorer/")
        return 1

    try:
        print("Publishing to Facebook Page...")
        result = post_to_facebook_page(page_id, access_token, text)
        post_id = result.get("id", "unknown")
        print(f"✅ Post created successfully!")
        print(f"   Post ID: {post_id}")
        return 0

    except Exception as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
