#!/usr/bin/env python3
"""Publish a post to Mastodon using content from Notes.md.

This script posts to Mastodon using the Mastodon API. It reads content from
the "## Short Social" section in Notes.md or accepts text via --text argument.

Environment variables:
  MASTODON_INSTANCE_URL - The Mastodon instance URL (e.g., https://mastodon.social)
  MASTODON_ACCESS_TOKEN - User access token from your Mastodon account

Usage:
  python post_to_mastodon.py [--notes path/to/Notes.md] [--text "Custom text"]
  python post_to_mastodon.py --dry-run  # Preview without posting

Getting an access token:
  1. Go to your Mastodon instance → Preferences → Development → New Application
  2. Name your app and set the scopes (write:statuses is required)
  3. Copy the access token
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


def extract_mastodon_section(notes_path: Path) -> str:
    """Extract the Short Social section from Notes.md."""
    content = notes_path.read_text(encoding="utf-8")
    marker = "## Short Social"
    if marker not in content:
        return ""
    prefix, suffix = content.split(marker, 1)
    body = suffix.split("\n##", 1)[0]
    return body.strip()


def post_to_mastodon(
    instance_url: str,
    access_token: str,
    status: str,
    visibility: str = "public"
) -> dict:
    """
    Post a status to Mastodon.

    Args:
        instance_url: Base URL of the Mastodon instance
        access_token: User access token
        status: The text content to post
        visibility: Post visibility (public, unlisted, private, direct)

    Returns:
        API response with the post data
    """
    url = f"{instance_url.rstrip('/')}/api/v1/statuses"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "status": status,
        "visibility": visibility
    }

    response = requests.post(url, json=payload, headers=headers, timeout=30)

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Mastodon API error ({response.status_code}): {response.text}"
        )

    return response.json()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish a post to Mastodon using content from Notes.md."
    )
    parser.add_argument(
        "--notes",
        help="Path to Notes.md (defaults to workspace Notes.md)."
    )
    parser.add_argument(
        "--text",
        help="Override Mastodon copy instead of reading Notes.md."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the post without publishing to Mastodon."
    )
    parser.add_argument(
        "--instance",
        help="Mastodon instance URL (overrides MASTODON_INSTANCE_URL env var)."
    )
    parser.add_argument(
        "--visibility",
        choices=["public", "unlisted", "private", "direct"],
        default="public",
        help="Post visibility (default: public)."
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
        text = extract_mastodon_section(notes_path)
        if not text:
            print("❌ Short Social section in Notes.md is empty.")
            print("   Add a '## Short Social' section or use --text.")
            return 1

    # Mastodon default character limit is 500 (can be higher on some instances)
    if len(text) > 500:
        print(f"⚠️  Warning: Post is {len(text)} characters (typical limit: 500)")
        print("   Some instances allow more, but this may be rejected.")
        print()

    print("Mastodon post to publish:\n")
    print(text.strip())
    print(f"\n({len(text)} characters)\n")

    if args.dry_run:
        print("✅ Dry run enabled; skipping Mastodon API call.")
        return 0

    # Get credentials
    instance_url = args.instance or os.getenv("MASTODON_INSTANCE_URL")
    access_token = os.getenv("MASTODON_ACCESS_TOKEN")

    if not instance_url:
        print("❌ Missing MASTODON_INSTANCE_URL.")
        print("   Set it in .env or use --instance")
        print("   Example: https://mastodon.social")
        return 1

    if not access_token:
        print("❌ Missing MASTODON_ACCESS_TOKEN.")
        print("   Set it in your .env file.")
        print("   Get a token from: {}/settings/applications (on your instance)")
        return 1

    try:
        print(f"Publishing to Mastodon ({instance_url})...")
        result = post_to_mastodon(instance_url, access_token, text, args.visibility)

        post_id = result.get("id", "unknown")
        post_url = result.get("url", "")

        print(f"✅ Post published successfully!")
        print(f"   Post ID: {post_id}")
        if post_url:
            print(f"   URL: {post_url}")
        return 0

    except Exception as exc:
        print(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
