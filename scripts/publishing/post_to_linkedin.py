import argparse
import json
import os
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
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

# ----------------------------
# Config — set via environment
# ----------------------------
DEFAULT_VERSION = "202510"

# ----------------------------
# OAuth 2.0 (Authorization Code)
# Docs: https://learn.microsoft.com/.../authorization-code-flow
# ----------------------------
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
SCOPE = "w_organization_social"  # post as an organization


class OAuthHandler(BaseHTTPRequestHandler):
    """Handles the redirect from LinkedIn and captures the authorization code."""

    code = None
    error = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            OAuthHandler.code = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"LinkedIn auth complete. You can close this tab.")
        elif "error" in params:
            OAuthHandler.error = params.get("error_description", ["OAuth error"])[0]
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"OAuth error. Check the console.")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Waiting for LinkedIn authorization...")

    def log_message(self, *args, **kwargs):
        return  # silence noisy BaseHTTPRequestHandler logging


def get_access_token(client_id: str, client_secret: str, redirect_uri: str) -> str:
    """Exchange the authorization code for an access token."""
    url = urllib.parse.urlparse(REDIRECT_URI)
    host = url.hostname or "localhost"
    port = url.port or 80
    server = HTTPServer((host, port), OAuthHandler)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": "xyz",  # CSRF token; static for demo
    }
    auth_link = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("Opening browser for LinkedIn consent...")
    webbrowser.open(auth_link)

    while OAuthHandler.code is None and OAuthHandler.error is None:
        server.handle_request()

    server.server_close()
    if OAuthHandler.error:
        raise RuntimeError(f"OAuth error: {OAuthHandler.error}")

    data = {
        "grant_type": "authorization_code",
        "code": OAuthHandler.code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    resp = requests.post(TOKEN_URL, data=data, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed: {resp.status_code} {resp.text}")
    tok = resp.json()
    return tok["access_token"]


REST_BASE = "https://api.linkedin.com/rest"


def headers(token: str, version: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": version,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def get_org_urn(token: str, vanity_name: str, version: str) -> str:
    """Resolve the organization URN from its vanity name."""
    response = requests.get(
        f"{REST_BASE}/organizations",
        headers=headers(token, version),
        params={"q": "vanityName", "vanityName": vanity_name},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Organization lookup failed: {response.status_code} {response.text}")
    elements = response.json().get("elements", [])
    if not elements:
        raise RuntimeError("No organization found for vanity name.")
    org_id = elements[0]["id"]
    return f"urn:li:organization:{org_id}"


def create_text_post(token: str, author_urn: str, text: str, version: str) -> str:
    """Create a simple text post for the organization."""
    payload = {
        "author": author_urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    response = requests.post(
        f"{REST_BASE}/posts",
        headers=headers(token, version),
        json=payload,
        timeout=30,
    )
    if response.status_code not in (201, 202):
        raise RuntimeError(f"Post failed: {response.status_code} {response.text}")
    return response.headers.get("x-restli-id")


def find_notes_file(explicit: Optional[str]) -> Optional[Path]:
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


def extract_linkedin_section(notes_path: Path) -> str:
    content = notes_path.read_text(encoding="utf-8")
    marker = "## Long Social"
    if marker not in content:
        return ""
    prefix, suffix = content.split(marker, 1)
    body = suffix.split("\n##", 1)[0]
    return body.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish a LinkedIn post using the content from Notes.md.")
    parser.add_argument("--notes", help="Path to Notes.md (defaults to project Notes.md).")
    parser.add_argument("--text", help="Override LinkedIn copy instead of reading Notes.md.")
    parser.add_argument("--dry-run", action="store_true", help="Print the post without publishing to LinkedIn.")
    parser.add_argument("--vanity", help="LinkedIn organization vanity name (overrides env).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    text = args.text
    notes_path: Optional[Path] = None
    if not text:
        notes_path = find_notes_file(args.notes)
        if not notes_path:
            raise FileNotFoundError("Notes.md not found. Provide --text or create Notes.md.")
        text = extract_linkedin_section(notes_path)
        if not text:
            raise ValueError("Long Social section in Notes.md is empty. Add a '## Long Social' section or use --text.")

    print("LinkedIn copy to publish:\n")
    print(text.strip())
    print("\n")

    if args.dry_run:
        print("Dry run enabled; skipping LinkedIn API call.")
        return

    client_id = os.getenv("LINKEDIN_CLIENT_ID")
    client_secret = os.getenv("LINKEDIN_CLIENT_SECRET")
    redirect_uri = os.getenv("LINKEDIN_REDIRECT_URI")
    vanity = args.vanity or os.getenv("LINKEDIN_ORG_VANITY") or "democracy-innovators"
    version = os.getenv("LINKEDIN_VERSION", DEFAULT_VERSION)

    missing = [name for name, value in {
        "LINKEDIN_CLIENT_ID": client_id,
        "LINKEDIN_CLIENT_SECRET": client_secret,
        "LINKEDIN_REDIRECT_URI": redirect_uri,
    }.items() if not value]
    if missing:
        raise EnvironmentError(
            "Missing LinkedIn configuration: " + ", ".join(missing) + ". Set them in your environment or .env file."
        )

    print("1) Getting access token...")
    token = get_access_token(client_id, client_secret, redirect_uri)
    print("   ✅ token acquired")

    print("2) Resolving organization URN from vanity name...")
    org_urn = get_org_urn(token, vanity, version)
    print(f"   ✅ {org_urn}")

    print("3) Publishing LinkedIn post...")
    post_id = create_text_post(token, org_urn, text, version)
    print(f"   ✅ Post created: {post_id} (check your Page)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR:", exc)
        sys.exit(1)
