#!/usr/bin/env python3
"""Create a draft post on Ghost CMS using structured content from the local notes Markdown file."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
from typing import Any, Dict

import requests


def load_env_file(*candidates: pathlib.Path) -> None:
    """Populate os.environ with values from the first existing .env candidate."""
    for env_path in candidates:
        if not env_path or not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
        break


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
load_env_file(PROJECT_ROOT / ".env", SCRIPT_DIR / ".env")

GHOST_URL = os.getenv("GHOST_URL")
GHOST_GHOST_ADMIN_API_KEY = os.getenv("GHOST_GHOST_ADMIN_API_KEY")

if not GHOST_URL or not GHOST_GHOST_ADMIN_API_KEY:
    print("Missing GHOST_URL or GHOST_GHOST_ADMIN_API_KEY. Configure them in your .env file and retry.")
    sys.exit(1)

SUPPORTED_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _base64url(data: bytes) -> str:
    """Return URL-safe base64 encoding without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_admin_jwt(admin_api_key: str, aud: str = "/ghost/api/admin/") -> str:
    key_id, secret = admin_api_key.split(":")
    secret_bytes = bytes.fromhex(secret)

    header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
    iat = int(time.time())
    payload = {"iat": iat, "exp": iat + 5 * 60, "aud": aud}

    header_b64 = _base64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_b64 = _base64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(secret_bytes, signing_input, hashlib.sha256).digest()
    signature_b64 = _base64url(signature)
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def build_post_payload(title: str, description: str, html_content: str, feature_image: str | None = None) -> Dict[str, Any]:
    post: Dict[str, Any] = {
        "title": title,
        "html": html_content,
        "status": "draft",
        "custom_excerpt": description,
    }
    if feature_image:
        post["feature_image"] = feature_image

    return {"posts": [post]}


def normalize_section_name(name: str) -> str:
    return "".join(name.lower().split())


def find_markdown_file(explicit_path: str | None) -> pathlib.Path:
    if explicit_path:
        md_path = pathlib.Path(explicit_path).expanduser()
        if not md_path.is_file():
            raise FileNotFoundError(f"Markdown file not found: {md_path}")
        return md_path

    md_files = list(pathlib.Path.cwd().glob("*.md"))
    if not md_files:
        raise FileNotFoundError("No Markdown file found in the current directory.")
    if len(md_files) > 1:
        found = ", ".join(str(path.name) for path in md_files)
        raise ValueError(f"Multiple Markdown files found ({found}); specify one with --markdown-file.")
    return md_files[0]


def extract_sections(markdown_path: pathlib.Path) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current_key: str | None = None
    buffer: list[str] = []

    for line in markdown_path.read_text(encoding="utf-8").splitlines():
        heading_match = re.match(r"^##(?!#)\s*(.+?)\s*$", line)
        if heading_match:
            if current_key is not None:
                sections[current_key] = "\n".join(buffer).strip()
            current_key = normalize_section_name(heading_match.group(1))
            buffer = []
            continue

        if current_key is not None:
            buffer.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(buffer).strip()

    return sections


def paragraphize(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Blog post content is empty.")

    blocks = [block.strip() for block in stripped.split("\n\n") if block.strip()]
    paragraphs = [block.replace("\n", " ").strip() for block in blocks]
    if not paragraphs:
        paragraphs = [stripped.replace("\n", " ").strip()]

    return "<p>" + "</p><p>".join(paragraphs) + "</p>"


def render_blog_post_html(markdown_text: str) -> str:
    stripped = markdown_text.strip()
    if not stripped:
        raise ValueError("Blog post content is empty.")

    try:
        import markdown as markdown_lib
    except ModuleNotFoundError:
        return paragraphize(stripped)

    return markdown_lib.markdown(stripped, extensions=["extra", "sane_lists"])


def first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return text.strip()


def build_youtube_embed_html(link: str) -> str | None:
    url = link.strip()
    if not url:
        return None

    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    video_id = ""

    if hostname in {"www.youtube.com", "youtube.com", "m.youtube.com"}:
        query = urllib.parse.parse_qs(parsed.query)
        video_id = query.get("v", [""])[0]
    elif hostname in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.lstrip("/")

    video_id = video_id.strip()
    if not video_id:
        return None

    embed_url = f"https://www.youtube.com/embed/{video_id}"
    return (
        '<figure class="kg-card kg-embed-card">'
        '<iframe src="'
        + embed_url
        + '" width="720" height="405" frameborder="0" allow="accelerometer; autoplay; clipboard-write; '
          'encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>'
        "</figure>"
    )


def build_castopod_embed_html(link: str) -> str | None:
    url = link.strip()
    if not url:
        return None

    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None

    if parsed.path.endswith("/embed"):
        embed_url = url
    else:
        embed_url = url.rstrip("/") + "/embed"

    iframe = (
        '<iframe width="100%" height="112" frameborder="0" scrolling="no" '
        'style="width: 100%; height: 112px; overflow: hidden;" src="'
        + embed_url
        + '"></iframe>'
    )
    return f'<figure class="kg-card kg-embed-card">{iframe}</figure>'

def update_blog_link_section(markdown_path: pathlib.Path, post_slug: str) -> None:
    blog_url = f"{GHOST_URL.rstrip('/')}/{post_slug.strip('/')}/"
    lines = markdown_path.read_text(encoding="utf-8").splitlines()

    blog_link_idx = None
    for idx, line in enumerate(lines):
        if re.match(r"^##\s*Blog\s*Link\s*$", line):
            blog_link_idx = idx
            break

    if blog_link_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["## Blog Link", blog_url, "", "## Blog Post", ""])
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    next_heading_idx = len(lines)
    for j in range(blog_link_idx + 1, len(lines)):
        if re.match(r"^##\s+", lines[j]):
            next_heading_idx = j
            break

    has_blog_post_heading = (
        next_heading_idx < len(lines) and re.match(r"^##\s*Blog\s*Post\s*$", lines[next_heading_idx])
    )

    if has_blog_post_heading:
        post_block = lines[next_heading_idx:]
        body = post_block[1:]
        while body and not body[0].strip():
            body = body[1:]
        post_block = [post_block[0], ""]
        post_block.extend(body)
        new_lines = lines[: blog_link_idx + 1] + [blog_url, ""] + post_block
    else:
        suffix = lines[next_heading_idx:]
        while suffix and not suffix[0].strip():
            suffix = suffix[1:]
        new_lines = lines[: blog_link_idx + 1] + [blog_url, "", "## Blog Post", ""]
        new_lines.extend(suffix)

    markdown_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def resolve_cover_image_path(markdown_path: pathlib.Path, explicit_path: str | None) -> pathlib.Path:
    def validate_path(candidate_path: pathlib.Path) -> pathlib.Path:
        if not candidate_path.is_file():
            raise FileNotFoundError(f"Cover image not found: {candidate_path}")
        if candidate_path.suffix.lower() not in SUPPORTED_IMAGE_MIME_TYPES:
            supported = ", ".join(sorted(SUPPORTED_IMAGE_MIME_TYPES))
            raise ValueError(f"Unsupported cover image format ({candidate_path.suffix}); expected one of {supported}.")
        return candidate_path

    if explicit_path:
        candidate = pathlib.Path(explicit_path).expanduser()
        if not candidate.is_absolute():
            candidate = (pathlib.Path.cwd() / candidate).resolve()

        return validate_path(candidate)

    search_dirs = [markdown_path.parent, pathlib.Path.cwd()]
    default_names = ("youtube-cover.jpg", "youtube-cover.jpeg", "youtube-cover.png")
    for directory in search_dirs:
        for default_name in default_names:
            candidate = (directory / default_name).resolve()
            if candidate.is_file():
                return validate_path(candidate)

    raise FileNotFoundError("Cover image not found. Provide one with --cover-image.")


def upload_cover_image(image_path: pathlib.Path, admin_jwt: str) -> str:
    endpoint = f"{GHOST_URL.rstrip('/')}/ghost/api/admin/images/upload/?purpose=feature_image"
    headers = {"Authorization": f"Ghost {admin_jwt}"}
    mime_type = SUPPORTED_IMAGE_MIME_TYPES.get(image_path.suffix.lower())
    if mime_type is None:
        supported = ", ".join(sorted(SUPPORTED_IMAGE_MIME_TYPES))
        raise ValueError(f"Unsupported cover image format ({image_path.suffix}); expected one of {supported}.")
    with image_path.open("rb") as file_handle:
        files = {"file": (image_path.name, file_handle, mime_type)}
        response = requests.post(endpoint, headers=headers, files=files, timeout=30)
    response.raise_for_status()

    payload = response.json()
    try:
        return payload["images"][0]["url"]
    except (KeyError, IndexError) as error:
        raise ValueError(f"Unexpected response when uploading cover image: {payload}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a Ghost CMS post payload from a text file.")
    parser.add_argument("--markdown-file", help="Path to the markdown file containing podcast notes.")
    parser.add_argument("--cover-image", help="Path to the cover image to be used as the feature image.")
    args = parser.parse_args()

    markdown_path = find_markdown_file(args.markdown_file)
    sections = extract_sections(markdown_path)

    title = sections.get("title")
    if not title:
        raise ValueError(f"Section '## Title' not found in {markdown_path}.")

    blog_post_section_names = ("blogpost", "blog_post")
    blog_post = next((sections[name] for name in blog_post_section_names if name in sections), None)
    if not blog_post:
        raise ValueError(f"Section '## Blog Post' not found in {markdown_path}.")

    castopod_link = first_non_empty_line(sections.get("castopodlink", ""))
    youtube_link = first_non_empty_line(sections.get("youtubelink", ""))
    castopod_embed_html = build_castopod_embed_html(castopod_link) if castopod_link else None
    youtube_embed_html = build_youtube_embed_html(youtube_link) if youtube_link else None

    html_parts = []
    if castopod_embed_html:
        html_parts.append(castopod_embed_html)
    if youtube_embed_html:
        html_parts.append(youtube_embed_html)
    html_parts.append(render_blog_post_html(blog_post))
    html_body = "".join(html_parts)
    admin_jwt = build_admin_jwt(GHOST_ADMIN_API_KEY)
    cover_image_path = resolve_cover_image_path(markdown_path, args.cover_image)
    feature_image_url = upload_cover_image(cover_image_path, admin_jwt)
    payload = build_post_payload(title, title, html_body, feature_image=feature_image_url)

    endpoint = f"{GHOST_URL.rstrip('/')}/ghost/api/admin/posts/?source=html"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Ghost {admin_jwt}",
    }

    response = requests.post(endpoint, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    post_response = response.json()
    print("Post created:", post_response)

    post_slug = post_response["posts"][0]["slug"]
    update_blog_link_section(markdown_path, post_slug)


if __name__ == "__main__":
    main()
