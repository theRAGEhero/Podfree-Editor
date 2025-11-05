#!/usr/bin/env python3
"""Generate a LinkedIn post draft with OpenRouter and write it into Notes.md."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional

import requests

from utils.llm_client import (
    call_openrouter,
    estimate_call_cost,
    estimate_hourly_cost,
    extract_content,
    get_api_key,
)


DEFAULT_HASHTAGS = "#Podcast #CivicTech #DemocracyInnovation #DigitalPublicInfrastructure"
TOKEN_PER_WORD_APPROX = 1.3


def find_notes_file(explicit: Optional[str]) -> Path:
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

    raise FileNotFoundError("Unable to locate Notes.md in the current project directory.")


def extract_sections(markdown_path: Path) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current = None
    buffer: list[str] = []

    for line in markdown_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^##(?!#)\s*(.+?)\s*$", line)
        if match:
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = match.group(1).strip().lower().replace(" ", "_")
            buffer = []
            continue

        if current is not None:
            buffer.append(line)

    if current is not None:
        sections[current] = "\n".join(buffer).strip()

    return sections


def replace_section(markdown_path: Path, heading: str, body: str) -> None:
    original = markdown_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"(?m)^{re.escape(heading)}\s*$")
    match = pattern.search(original)
    replacement = f"{heading}\n\n{body.strip()}\n"

    if not match:
        if original and not original.endswith("\n"):
            original += "\n"
        updated = original + replacement
    else:
        section_start = match.end()
        boundary = re.compile(rf"(?m)^#{{1,{heading.count('#')}}}\s+").search(original, section_start)
        section_end = boundary.start() if boundary else len(original)
        updated = original[: match.start()] + replacement + original[section_end:]

    if not updated.endswith("\n"):
        updated += "\n"

    markdown_path.write_text(updated, encoding="utf-8")


def approximate_tokens(text: str) -> int:
    words = len(text.split())
    return int(words * TOKEN_PER_WORD_APPROX)


def build_prompt(sections: Dict[str, str], hashtags: str) -> Dict[str, str]:
    title = sections.get("title") or os.getenv("LINKEDIN_DEFAULT_TITLE") or "New Episode"
    summary = sections.get("short_description") or sections.get("blog_post", "").split("\n\n")[0]
    blog_link = (sections.get("blog_link") or "").strip()
    youtube_link = (sections.get("youtube_link") or "").strip()
    castopod_link = (sections.get("castopod_link") or "").strip()
    guest = sections.get("guest")

    bullet_points = sections.get("blog_post", "").strip()

    info = {
        "title": title.strip(),
        "summary": summary.strip(),
        "blog_link": blog_link,
        "youtube_link": youtube_link,
        "castopod_link": castopod_link,
        "guest": guest.strip() if guest else None,
        "hashtags": hashtags.strip(),
    }

    content_parts = [json.dumps(info, indent=2)]
    if bullet_points:
        content_parts.append("BLOG_POST_SECTION:\n" + bullet_points[:4000])

    user_prompt = (
        "You are a social media editor. Using the episode details below, write a LinkedIn post that:\n"
        "- Hooks the reader in the first sentence.\n"
        "- Summarizes key takeaways in natural language (no bullet list).\n"
        "- Mentions the guest by name if provided.\n"
        "- Includes the provided links with short descriptors (Watch/Listen/Read).\n"
        "- Ends with the supplied hashtags (ensure they are present exactly once).\n\n"
        "Return only the final post text."
    )

    return {
        "system": "You craft concise, engaging LinkedIn posts for podcast episodes.",
        "user": user_prompt + "\n\n" + "\n\n".join(content_parts),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a LinkedIn post draft for Notes.md using OpenRouter.")
    parser.add_argument("--notes", help="Path to Notes.md (defaults to Notes.md in the project root).")
    parser.add_argument("--hashtags", default=os.getenv("LINKEDIN_HASHTAGS", DEFAULT_HASHTAGS), help="Hashtags to append.")
    parser.add_argument("--model", default=os.getenv("LINKEDIN_LLM_MODEL", "anthropic/claude-3-haiku"), help="OpenRouter model identifier.")
    parser.add_argument("--dry-run", action="store_true", help="Print the LinkedIn post instead of updating Notes.md.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        notes_path = find_notes_file(args.notes)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    sections = extract_sections(notes_path)
    prompt_parts = build_prompt(sections, args.hashtags)

    transcript = sections.get("transcript") or sections.get("blog_post", "")
    input_tokens_est = approximate_tokens(transcript)
    cost_estimate = estimate_call_cost(args.model, input_tokens_est)
    hourly_cost = estimate_hourly_cost(args.model)

    if cost_estimate is not None:
        print(f"Estimated call cost: ${cost_estimate:.4f} USD")
    else:
        print("Estimated call cost: unavailable for this model")

    if hourly_cost is not None:
        print(f"Estimated cost for a 1-hour conversation: ${hourly_cost:.4f} USD")
    else:
        print("Estimated cost for a 1-hour conversation: unavailable for this model")

    api_key = get_api_key()
    if not api_key:
        print("Missing OPENROUTER_API_KEY in environment.", file=sys.stderr)
        sys.exit(1)

    messages = [
        {"role": "system", "content": prompt_parts["system"]},
        {"role": "user", "content": prompt_parts["user"]},
    ]

    try:
        response = call_openrouter(api_key, model=args.model, messages=messages, temperature=0.3)
    except requests.HTTPError as exc:
        print(f"OpenRouter request failed: {exc} — {exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as exc:
        print(f"OpenRouter request error: {exc}", file=sys.stderr)
        sys.exit(1)

    post_body = extract_content(response)

    if args.dry_run:
        print(post_body)
    else:
        replace_section(notes_path, "## LinkedIn", post_body)
        print(f"LinkedIn draft updated in {notes_path}")

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
