#!/usr/bin/env python3
"""Create a draft Castopod episode using local assets.

The script finds the single MP3 file in the working directory, uploads it (and
optionally ``youtube-cover.jpg``) to Castopod, and creates a draft episode
titled ``DRAFT`` with the description ``Lorem ipsus``. The draft is created
immediately; there is no dry-run mode.

Expected environment variables:

``CASTOPOD_BASE_URL`` – e.g. ``https://podcast.democracyinnovators.com``
``CASTOPOD_TOKEN`` – Castopod personal access token or Bearer token
``CASTOPOD_PODCAST_ID`` – Slug/ID of the podcast to publish into (defaults to app config)
``CASTOPOD_EPISODE_MEDIA_PATH`` (optional) – Default path to the episode audio file
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.auth import HTTPBasicAuth


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Chapter:
    title: str
    timestamp: str
    seconds: float


@dataclasses.dataclass
class EpisodeAssets:
    title: str
    summary: str
    description: str
    castopod_episode_url: Optional[str]
    youtube_url: Optional[str]
    blog_url: Optional[str]
    audio_path: Optional[pathlib.Path]
    cover_path: Optional[pathlib.Path]
    chapters: List[Chapter]
    transcript_text: Optional[str]
    transcript_srt: Optional[pathlib.Path]


@dataclasses.dataclass
class RestAPIConfig:
    basicAuthUsername: str
    basicAuthPassword: str


@dataclasses.dataclass
class AppConfig:
    baseURL: str
    podcastID: Optional[str] = None


restapi = RestAPIConfig(
    basicAuthUsername="castopod_helper",
    basicAuthPassword="p454$Fdddd\u00a3wdlLSDJffdfdf$$$$dsdf",
)

app = AppConfig(baseURL="https://podcast.democracyinnovators.com", podcastID="@podcast")


# --------------------------------------------------------------------------- #
# Markdown parsing helpers
# --------------------------------------------------------------------------- #


SECTION_PATTERN = re.compile(r"^##\s*(?P<name>.+?)\s*$")


def normalize_section_name(name: str) -> str:
    return re.sub(r"\s+", "", name.strip().lower())


def load_markdown_sections(markdown_path: pathlib.Path) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current_key: Optional[str] = None
    buffer: List[str] = []

    for line in markdown_path.read_text(encoding="utf-8").splitlines():
        match = SECTION_PATTERN.match(line)
        if match:
            if current_key is not None:
                sections[current_key] = "\n".join(buffer).strip()
            current_key = normalize_section_name(match.group("name"))
            buffer = []
            continue

        if current_key is not None:
            buffer.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(buffer).strip()

    return sections


# --------------------------------------------------------------------------- #
# Chapter parsing
# --------------------------------------------------------------------------- #

TIMESTAMP_PATTERN = re.compile(
    r"""
    ^(?P<hours>\d{1,2})?              # optional hours (1-2 digits)
    (?:
        :
        (?P<minutes>\d{2})
    )?
    :
    (?P<seconds>\d{2})
    $
    """,
    re.VERBOSE,
)

CHAPTER_LINE_PATTERN = re.compile(
    r"""
    ^
    (?P<timestamp>\d{1,2}:\d{2}(?::\d{2})?)   # 00:00 or 00:00:00
    \s+
    (?P<title>.+?)
    \s*$
    """,
    re.VERBOSE,
)


def timestamp_to_seconds(timestamp: str) -> float:
    parts = [int(part) for part in timestamp.split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    else:
        raise ValueError(f"Unsupported timestamp format: {timestamp}")
    return hours * 3600 + minutes * 60 + seconds


def extract_chapters(section_text: str) -> List[Chapter]:
    chapters: List[Chapter] = []
    if not section_text.strip():
        return chapters

    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = CHAPTER_LINE_PATTERN.match(line)
        if not match:
            continue
        timestamp = match.group("timestamp")
        title = match.group("title").strip()
        chapters.append(Chapter(title=title, timestamp=timestamp, seconds=timestamp_to_seconds(timestamp)))

    return chapters


def chapters_to_castopod_json(chapters: Iterable[Chapter]) -> List[Dict[str, Any]]:
    return [
        {"start": chapter.seconds, "title": chapter.title}
        for chapter in chapters
    ]


# --------------------------------------------------------------------------- #
# Transcript handling
# --------------------------------------------------------------------------- #

SRT_SPLIT_PATTERN = re.compile(r"\r?\n\r?\n")
SRT_TIMECODE_PATTERN = re.compile(
    r"""
    (?P<start>\d{2}:\d{2}:\d{2},\d{3})
    \s*-->\s*
    (?P<end>\d{2}:\d{2}:\d{2},\d{3})
    """,
    re.VERBOSE,
)


def load_transcript_from_srt(srt_path: pathlib.Path) -> str:
    blocks = SRT_SPLIT_PATTERN.split(srt_path.read_text(encoding="utf-8-sig").strip())
    transcript_lines: List[str] = []

    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        lines = stripped.splitlines()
        # Remove counter line if present
        if lines and lines[0].isdigit():
            lines = lines[1:]
        # Remove timecode line
        if lines and SRT_TIMECODE_PATTERN.match(lines[0]):
            lines = lines[1:]
        if lines:
            transcript_lines.append(" ".join(line.strip() for line in lines if line.strip()))

    return "\n".join(transcript_lines).strip()


# --------------------------------------------------------------------------- #
# Castopod payload assembly
# --------------------------------------------------------------------------- #


def derive_castopod_server(castopod_section_value: str, default_base_url: Optional[str] = None) -> str:
    url = castopod_section_value.strip()
    if not url and default_base_url:
        return default_base_url.rstrip("/")
    if not url:
        raise ValueError("Castopod link missing; cannot infer API base URL.")
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid Castopod URL: {url}")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def build_episode_assets(
    notes_path: pathlib.Path,
    srt_path: Optional[pathlib.Path],
    audio_path: Optional[pathlib.Path],
    cover_path: Optional[pathlib.Path],
) -> EpisodeAssets:
    sections = load_markdown_sections(notes_path)

    title = sections.get("title", "").strip()
    if not title:
        raise ValueError(f"Section '## Title' missing or empty in {notes_path}")

    summary = sections.get("thumbnail", "").strip() or title
    description = sections.get("linkedin", "").strip() or sections.get("blogpost", "").strip()

    castopod_url = sections.get("castopodlink", "").strip()
    youtube_url = sections.get("youtubelink", "").strip()
    blog_url = sections.get("bloglink", "").strip()

    chapters_section = sections.get("chapters", "")
    chapters = extract_chapters(chapters_section)

    transcript_text: Optional[str] = None
    transcript_file: Optional[pathlib.Path] = None
    if srt_path and srt_path.is_file():
        transcript_text = load_transcript_from_srt(srt_path)
        transcript_file = srt_path

    return EpisodeAssets(
        title=title,
        summary=summary,
        description=description,
        castopod_episode_url=castopod_url or None,
        youtube_url=youtube_url or None,
        blog_url=blog_url or None,
        audio_path=audio_path if audio_path and audio_path.is_file() else None,
        cover_path=cover_path if cover_path and cover_path.is_file() else None,
        chapters=chapters,
        transcript_text=transcript_text,
        transcript_srt=transcript_file,
    )


def build_castopod_payload(assets: EpisodeAssets, status: str) -> Dict[str, Any]:
    description_parts: List[str] = []
    if assets.youtube_url:
        description_parts.append(f"Watch on YouTube: {assets.youtube_url}")
    if assets.blog_url:
        description_parts.append(f"Blog article: {assets.blog_url}")
    if assets.description:
        description_parts.append("")
        description_parts.append(assets.description.strip())

    description_html = "\n\n".join(description_parts).strip()

    payload: Dict[str, Any] = {
        "title": assets.title,
        "summary": assets.summary,
        "description_html": description_html,
        "status": status,
        "explicit": False,
    }

    if assets.chapters:
        payload["chapters"] = chapters_to_castopod_json(assets.chapters)

    if assets.transcript_text:
        payload["transcript_plaintext"] = assets.transcript_text

    return payload


# --------------------------------------------------------------------------- #
# Castopod API client
# --------------------------------------------------------------------------- #


class CastopodClient:
    def __init__(
        self,
        base_url: str,
        podcast_id: Optional[str],
        token: Optional[str] = None,
        basic_auth: Optional[Tuple[str, str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.podcast_id = podcast_id
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        if basic_auth:
            self.session.auth = HTTPBasicAuth(*basic_auth)
        else:
            self.session.auth = None

    def _fetch_single_podcast_id(self) -> str:
        endpoint = f"{self.base_url}/api/v1/podcasts"
        try:
            response = self.session.get(endpoint, timeout=30)
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise ValueError(
                "Unable to auto-detect podcast ID; set CASTOPOD_PODCAST_ID or use --podcast-id."
            ) from exc
        payload = response.json()

        candidates: List[Dict[str, Any]] = []
        if isinstance(payload, list):
            candidates = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            for key in ("data", "items", "podcasts"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates = [item for item in value if isinstance(item, dict)]
                    if candidates:
                        break
        if not candidates:
            raise ValueError("Unable to determine podcast list from API response; specify --podcast-id explicitly.")
        if len(candidates) > 1:
            raise ValueError("Multiple podcasts detected; set CASTOPOD_PODCAST_ID or use --podcast-id.")

        info = candidates[0]
        for key in ("id", "uuid", "slug"):
            identifier = info.get(key)
            if identifier:
                return str(identifier)

        raise ValueError("Podcast identifier not found in API response; specify --podcast-id.")

    def _ensure_podcast_id(self) -> str:
        if self.podcast_id:
            return self.podcast_id
        self.podcast_id = self._fetch_single_podcast_id()
        return self.podcast_id

    def create_episode(
        self,
        payload: Dict[str, Any],
        audio_path: Optional[pathlib.Path] = None,
        cover_path: Optional[pathlib.Path] = None,
        transcript_path: Optional[pathlib.Path] = None,
        send: bool = False,
    ) -> Dict[str, Any]:
        podcast_id = self._ensure_podcast_id()
        endpoint = f"{self.base_url}/api/v1/podcasts/{podcast_id}/episodes"

        if not send:
            return {"endpoint": endpoint, "payload": payload}

        files: Dict[str, Tuple[str, Any, str]] = {}
        data = {"data": json.dumps(payload)}

        if audio_path and audio_path.is_file():
            files["audio_file"] = (audio_path.name, audio_path.open("rb"), "audio/mpeg")
        if cover_path and cover_path.is_file():
            files["cover_image"] = (cover_path.name, cover_path.open("rb"), "image/jpeg")
        if transcript_path and transcript_path.is_file():
            files["transcript_file"] = (transcript_path.name, transcript_path.open("rb"), "application/x-subrip")

        response = self.session.post(endpoint, data=data, files=files or None, timeout=60)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            # Surface helpful diagnostics for common API misconfiguration cases.
            detail = ""
            if response.content:
                try:
                    detail = response.json()
                except ValueError:
                    detail = response.text.strip()
            raise requests.HTTPError(
                f"{exc} :: endpoint={endpoint} :: response={detail or '<empty>'}",
                response=response,
            ) from exc
        return response.json()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a Castopod draft episode from local assets.")
    parser.add_argument("--cover-image", type=pathlib.Path, help="Path to JPEG/PNG cover image.")
    parser.add_argument("--base-url", help="Override Castopod base URL (defaults to env or notes link).")
    parser.add_argument("--podcast-id", help="Override podcast ID (defaults to env CASTOPOD_PODCAST_ID).")
    parser.add_argument("--token", help="Override token (defaults to env CASTOPOD_TOKEN).")
    return parser.parse_args()


def resolve_environment_defaults(
    args: argparse.Namespace, assets: EpisodeAssets
) -> Tuple[str, Optional[str], Optional[str], Optional[Tuple[str, str]]]:
    base_url = args.base_url or os.environ.get("CASTOPOD_BASE_URL")
    token = args.token or os.environ.get("CASTOPOD_TOKEN")
    podcast_id = args.podcast_id or os.environ.get("CASTOPOD_PODCAST_ID") or app.podcastID

    basic_auth_username = os.environ.get("CASTOPOD_BASIC_AUTH_USERNAME", restapi.basicAuthUsername)
    basic_auth_password = os.environ.get("CASTOPOD_BASIC_AUTH_PASSWORD", restapi.basicAuthPassword)
    basic_auth: Optional[Tuple[str, str]] = None
    if basic_auth_username and basic_auth_password:
        basic_auth = (basic_auth_username, basic_auth_password)

    if not base_url and assets.castopod_episode_url:
        base_url = derive_castopod_server(assets.castopod_episode_url or "", default_base_url=None)
    if not base_url:
        base_url = app.baseURL
    if not base_url:
        raise ValueError("Castopod base URL not provided. Set CASTOPOD_BASE_URL or pass --base-url.")
    if not token and not basic_auth:
        raise ValueError(
            "Castopod credentials missing. Provide CASTOPOD_TOKEN/--token or CASTOPOD_BASIC_AUTH_USERNAME & CASTOPOD_BASIC_AUTH_PASSWORD."
        )
    return base_url, token, podcast_id, basic_auth


def main() -> None:
    args = parse_args()

    mp3_candidates = sorted(pathlib.Path.cwd().glob("*.mp3"))
    if not mp3_candidates:
        raise FileNotFoundError("No MP3 file found in the current directory.")
    if len(mp3_candidates) > 1:
        names = ", ".join(p.name for p in mp3_candidates)
        raise ValueError(f"Multiple MP3 files found ({names}); leave only one in the folder.")
    audio_path = mp3_candidates[0]

    cover_path = args.cover_image or pathlib.Path("youtube-cover.jpg")
    if cover_path and not cover_path.is_file():
        cover_path = None

    assets = EpisodeAssets(
        title="DRAFT",
        summary="DRAFT",
        description="Lorem ipsus",
        castopod_episode_url=None,
        youtube_url=None,
        blog_url=None,
        audio_path=audio_path if audio_path and audio_path.is_file() else None,
        cover_path=cover_path if cover_path and cover_path.is_file() else None,
        chapters=[],
        transcript_text=None,
        transcript_srt=None,
    )
    payload = build_castopod_payload(assets, status="draft")

    base_url, token, podcast_id, basic_auth = resolve_environment_defaults(args, assets)
    client = CastopodClient(
        base_url=base_url,
        podcast_id=podcast_id,
        token=token,
        basic_auth=basic_auth,
    )

    outcome = client.create_episode(
        payload=payload,
        audio_path=assets.audio_path,
        cover_path=assets.cover_path,
        transcript_path=assets.transcript_srt,
        send=True,
    )

    print("Draft episode created:", json.dumps(outcome, indent=2))


if __name__ == "__main__":
    main()
