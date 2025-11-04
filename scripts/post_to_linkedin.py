import os
import sys
import json
import time
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests


def load_env_file(filepath=".env"):
    """Populate os.environ with values from a .env file if present."""
    env_path = Path(filepath)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
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


load_env_file()

# ----------------------------
# Config — set via environment
# ----------------------------
CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID")
CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET")
REDIRECT_URI = os.getenv("LINKEDIN_REDIRECT_URI")  # must be registered in your app
ORG_VANITY = "democracy-innovators"                # from your URL
LINKEDIN_VERSION = "202510"                        # YYYYMM; keep current per docs

if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI]):
    print("Missing env vars. Set LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET, LINKEDIN_REDIRECT_URI.")
    sys.exit(1)

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


def get_access_token():
    """Exchange the authorization code for an access token."""
    url = urllib.parse.urlparse(REDIRECT_URI)
    host = url.hostname or "localhost"
    port = url.port or 80
    server = HTTPServer((host, port), OAuthHandler)

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
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
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    resp = requests.post(TOKEN_URL, data=data, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed: {resp.status_code} {resp.text}")
    tok = resp.json()
    return tok["access_token"]


REST_BASE = "https://api.linkedin.com/rest"


def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def get_org_urn(token, vanity_name):
    """Resolve the organization URN from its vanity name."""
    response = requests.get(
        f"{REST_BASE}/organizations",
        headers=headers(token),
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


def create_text_post(token, author_urn, text):
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
        headers=headers(token),
        json=payload,
        timeout=30,
    )
    if response.status_code not in (201, 202):
        raise RuntimeError(f"Post failed: {response.status_code} {response.text}")
    return response.headers.get("x-restli-id")


def main():
    print("1) Getting access token...")
    token = get_access_token()
    print("   ✅ token acquired")

    print("2) Resolving organization URN from vanity name...")
    org_urn = get_org_urn(token, ORG_VANITY)
    print(f"   ✅ {org_urn}")

    print('3) Creating "Hello world" post...')
    post_id = create_text_post(token, org_urn, "Hello world")
    print(f"   ✅ Post created: {post_id} (check your Page)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR:", exc)
        sys.exit(1)
