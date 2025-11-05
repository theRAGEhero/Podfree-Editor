#!/usr/bin/env python3
"""Generate podcast chapters via OpenRouter and update project assets.

This utility reads a structured transcript (Deepgram Deliberation JSON),
requests chapter suggestions from a large language model hosted on OpenRouter
and updates Notes.md with the resulting chapter outline. A separate exporter
can turn the curated chapters into Castopod-compatible JSON when needed.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from utils.llm_client import (
    call_openrouter,
    estimate_call_cost,
    estimate_hourly_cost,
    extract_content,
    get_api_key,
)


DEFAULT_MODEL = "anthropic/claude-3.7-sonnet"
DEFAULT_MAX_CHAPTERS = 12
TOKEN_PER_WORD_APPROX = 1.3  # rough heuristic for GPT-style BPE tokenization

@dataclass
class Chapter:
    title: str
    summary: str
    start_time_seconds: int
    url: Optional[str] = None
    image: Optional[str] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate podcast chapters using an OpenRouter-hosted model."
    )
    parser.add_argument(
        "--transcript",
        help="Path to a Deepgram Deliberation JSON file. Defaults to the newest file in 'Deliberation Json/'.",
    )
    parser.add_argument(
        "--notes",
        help="Path to the Notes.md file to update. Defaults to Notes.md in the current directory.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="OpenRouter model identifier. Default: %(default)s",
    )
    parser.add_argument(
        "--max-chapters",
        type=int,
        default=DEFAULT_MAX_CHAPTERS,
        help="Maximum number of chapters to request from the model.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse transcript and show prompts/cost estimates without calling OpenRouter.",
    )
    return parser.parse_args()


def find_latest_deliberation_json() -> Path:
    candidates = sorted((Path.cwd() / "Deliberation Json").glob("*_deliberation.json"))
    if not candidates:
        raise FileNotFoundError(
            "No Deliberation Json/*.json files found. Run the Deepgram transcription first "
            "or pass --transcript explicitly."
        )
    return candidates[-1]


def locate_notes_markdown(notes_arg: Optional[str]) -> Optional[Path]:
    if notes_arg:
        notes_path = Path(notes_arg).expanduser().resolve()
        if not notes_path.is_file():
            raise FileNotFoundError(f"Notes file not found: {notes_path}")
        return notes_path

    primary = Path.cwd() / "Notes.md"
    if primary.is_file():
        return primary

    candidates = sorted(Path.cwd().glob("Notes*.md"))
    if candidates:
        return candidates[0]

    return None


def extract_markdown_section(markdown_path: Path, heading: str) -> Optional[str]:
    content = markdown_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"(?m)^{re.escape(heading)}\s*$")
    match = pattern.search(content)
    if not match:
        return None
    start = match.end()
    next_heading = re.compile(r"(?m)^#{1,6}\s+").search(content, start)
    end = next_heading.start() if next_heading else len(content)
    section = content[start:end]
    return section.strip()


def extract_existing_chapter_titles(notes_path: Path) -> List[str]:
    section = extract_markdown_section(notes_path, "## Chapters")
    if not section:
        return []
    titles: List[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        match = re.match(r"-\s*\[[^\]]+\]\s*(.+?)(?:\s+—.*)?$", stripped)
        if match:
            titles.append(match.group(1).strip())
        else:
            titles.append(stripped.lstrip("- ").strip())
    return [title for title in titles if title]


def load_deliberation(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_transcript_text(deliberation: Dict[str, Any]) -> str:
    contributions = deliberation.get("contributions", [])
    lines: List[str] = []
    for contribution in contributions:
        timestamp = format_hms(contribution.get("start_time_seconds", 0))
        speaker = contribution.get("madeBy", "speaker").replace("_", " ").title()
        text = (contribution.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{timestamp} {speaker}: {text}")
    return "\n".join(lines)


def format_hms(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def estimate_tokens(word_count: Optional[int], fallback_minutes: Optional[float]) -> int:
    if word_count and word_count > 0:
        return int(math.ceil(word_count * TOKEN_PER_WORD_APPROX))
    if fallback_minutes and fallback_minutes > 0:
        estimated_words = fallback_minutes * 150  # conversational speech heuristic
        return int(math.ceil(estimated_words * TOKEN_PER_WORD_APPROX))
    return 4000


def build_prompt(
    deliberation: Dict[str, Any],
    transcript_text: str,
    max_chapters: int,
) -> List[Dict[str, str]]:
    process = deliberation.get("deliberation_process", {})
    stats = deliberation.get("statistics", {})
    metadata = process.get("transcription_metadata", {})

    system_prompt = (
        "You are an expert podcast producer. Given a conversation transcript, "
        "produce well-structured chapters for a listener. Chapter boundaries must "
        "follow the Podcasting 2.0 chapters specification."
    )

    info_block = {
        "title": process.get("name") or "Podcast Episode",
        "topic": process.get("topic", {}).get("text"),
        "model": metadata.get("model"),
        "processed_at": metadata.get("processed_at"),
        "total_words": stats.get("total_words"),
        "duration_seconds": stats.get("duration_seconds"),
        "total_contributions": stats.get("total_contributions"),
        "max_chapters": max_chapters,
    }

    instructions = (
        "Using the transcript below, create concise chapters with engaging titles, 1-sentence summaries, "
        f"and start times in seconds. Return ONLY a JSON object with this schema:\n"
        "{\n"
        '  "chapters": [\n'
        "    {\n"
        '      "title": string,\n'
        '      "summary": string,\n'
        '      "start_time_seconds": integer,\n'
        '      "url": string | null,\n'
        '      "image": string | null\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Constraints:\n"
        f"- Limit chapters to at most {max_chapters}.\n"
        "- Ensure start times are sorted ascending and between 0 and duration_seconds.\n"
        "- The first chapter start time must be 0.\n"
        "- Use null for url/image when not provided.\n"
    )

    content = (
        "EPISODE INFO:\n"
        + json.dumps(info_block, indent=2)
        + "\n\nTRANSCRIPT:\n"
        + transcript_text
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": instructions + "\n\n" + content},
    ]


def call_openrouter(
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.2,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    response = requests.post(OPENROUTER_BASE_URL, headers=headers, json=payload, timeout=90)
    response.raise_for_status()
    return response.json()


def parse_chapter_payload(content: str) -> List[Chapter]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model response was not valid JSON: {content}") from exc

    chapters_raw = payload.get("chapters")
    if not isinstance(chapters_raw, list):
        raise ValueError("JSON payload missing 'chapters' list.")

    chapters: List[Chapter] = []
    for entry in chapters_raw:
        if not isinstance(entry, dict):
            continue
        try:
            chapter = Chapter(
                title=str(entry["title"]).strip(),
                summary=str(entry.get("summary", "")).strip(),
                start_time_seconds=int(entry["start_time_seconds"]),
                url=str(entry["url"]).strip() if entry.get("url") else None,
                image=str(entry["image"]).strip() if entry.get("image") else None,
            )
        except (KeyError, ValueError, TypeError):
            continue
        chapters.append(chapter)

    if not chapters:
        raise ValueError("No valid chapters found in model output.")

    chapters.sort(key=lambda c: c.start_time_seconds)
    return chapters


def replace_markdown_section(markdown_path: Path, heading: str, body: str) -> None:
    original = markdown_path.read_text(encoding="utf-8")
    heading_pattern = f"{heading}\n"
    if heading_pattern not in original:
        if original and not original.endswith("\n"):
            original += "\n"
        updated = original + f"{heading}\n\n{body.strip()}\n"
        if not updated.endswith("\n"):
            updated += "\n"
        markdown_path.write_text(updated, encoding="utf-8")
        return

    parts = original.split(heading_pattern, 1)
    prefix = parts[0]
    suffix = parts[1]
    remainder = suffix.split("\n##", 1)
    current_section = remainder[0]
    rest = "##" + remainder[1] if len(remainder) > 1 else ""

    updated_section = f"{heading}\n\n{body.strip()}\n"
    updated = prefix
    if not prefix.endswith("\n"):
        updated += "\n"
    updated += updated_section
    if rest:
        if not updated.endswith("\n"):
            updated += "\n"
        updated += rest
        if not updated.endswith("\n"):
            updated += "\n"
    markdown_path.write_text(updated, encoding="utf-8")


def render_notes_section(chapters: List[Chapter]) -> str:
    lines: List[str] = []
    for chapter in chapters:
        timestamp = format_hms(chapter.start_time_seconds)
        summary = f" — {chapter.summary}" if chapter.summary else ""
        lines.append(f"- [{timestamp}] {chapter.title}{summary}")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    api_key = get_api_key()
    if not api_key and not args.dry_run:
        print("Missing OPENROUTER_API_KEY in environment. Set it or use --dry-run.", file=sys.stderr)
        sys.exit(1)

    transcript_path = Path(args.transcript).expanduser().resolve() if args.transcript else find_latest_deliberation_json()
    if not transcript_path.is_file():
        print(f"Transcript file not found: {transcript_path}", file=sys.stderr)
        sys.exit(1)

    deliberation = load_deliberation(transcript_path)
    transcript_text = build_transcript_text(deliberation)
    total_words = deliberation.get("statistics", {}).get("total_words")
    duration_seconds = deliberation.get("statistics", {}).get("duration_seconds")
    input_tokens_est = estimate_tokens(total_words, (duration_seconds or 0) / 60 if duration_seconds else None)
    cost_estimate = estimate_call_cost(args.model, input_tokens_est)
    hourly_cost = estimate_hourly_cost(args.model)

    print(f"Using model: {args.model}")
    print(f"Transcript source: {transcript_path}")
    print(f"Estimated input tokens: {input_tokens_est:,}")
    if cost_estimate is not None:
        print(f"Estimated call cost: ${cost_estimate:.4f} USD")
    else:
        print("Estimated call cost: unavailable for this model")

    if hourly_cost is not None:
        print(f"Estimated cost for a 1-hour conversation: ${hourly_cost:.4f} USD")
    else:
        print("Estimated cost for a 1-hour conversation: unavailable for this model")

    if args.dry_run:
        print("Dry run complete. No API call made.")
        return

    messages = build_prompt(deliberation, transcript_text, args.max_chapters)
    try:
        response = call_openrouter(api_key, model=args.model, messages=messages)
    except requests.HTTPError as exc:
        print(f"OpenRouter request failed: {exc} — {exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as exc:
        print(f"OpenRouter request error: {exc}", file=sys.stderr)
        sys.exit(1)

    content = extract_content(response)
    try:
        chapters = parse_chapter_payload(content)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    print(f"Received {len(chapters)} chapters from the model.")

    notes_path = locate_notes_markdown(args.notes)
    existing_titles: List[str] = []
    if notes_path and notes_path.is_file():
        existing_titles = extract_existing_chapter_titles(notes_path)

    def shorten_summary(text: str, max_length: int = 180) -> str:
        trimmed = (text or "").strip()
        if len(trimmed) <= max_length:
            return trimmed
        return trimmed[: max_length - 1].rstrip() + "…"

    final_chapters: List[Chapter] = []
    for idx, chapter in enumerate(chapters):
        title = existing_titles[idx] if idx < len(existing_titles) else chapter.title
        final_chapters.append(
            Chapter(
                title=title,
                summary=shorten_summary(chapter.summary),
                start_time_seconds=chapter.start_time_seconds,
                url=chapter.url,
                image=chapter.image,
            )
        )

    if notes_path:
        section_body = render_notes_section(final_chapters)
        replace_markdown_section(notes_path, "## Chapters", section_body)
        print(f"Updated chapters in {notes_path}")
    else:
        print("Notes.md not found; skipping notes update.")

    usage = response.get("usage") or {}
    if usage:
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if prompt_tokens is not None and completion_tokens is not None:
            actual_cost = estimate_call_cost(args.model, prompt_tokens, completion_tokens)
            if actual_cost is not None:
                print(
                    f"Model usage — prompt tokens: {prompt_tokens:,}, completion tokens: {completion_tokens:,}, "
                    f"estimated cost: ${actual_cost:.4f} USD"
                )
            else:
                print(
                    f"Model usage — prompt tokens: {prompt_tokens:,}, completion tokens: {completion_tokens:,}"
                )


if __name__ == "__main__":
    main()
