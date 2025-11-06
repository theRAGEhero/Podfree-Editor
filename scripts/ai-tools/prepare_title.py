#!/usr/bin/env python3
"""Generate an episode title using OpenRouter and write it into Notes.md."""

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


def build_prompt(sections: Dict[str, str]) -> Dict[str, str]:
    guest = sections.get("guest", "")
    short_description = sections.get("short_description", "")
    blog_post = sections.get("blog_post", "")
    chapters = sections.get("chapters", "")
    
    # Extract key content for context
    content_parts = []
    
    if guest:
        content_parts.append(f"GUEST: {guest}")
    
    if short_description:
        content_parts.append(f"DESCRIPTION: {short_description}")
    
    if chapters:
        content_parts.append(f"CHAPTERS:\n{chapters}")
    
    if blog_post:
        # Limit blog post content to avoid token limits
        blog_excerpt = blog_post[:3000]
        content_parts.append(f"TRANSCRIPT/CONTENT:\n{blog_excerpt}")

    context = "\n\n".join(content_parts)

    user_prompt = (
        "Generate a compelling podcast episode title based on the information below. The title should:\n"
        "- Be 5-10 words maximum\n"
        "- Hook the reader and make them want to listen\n"
        "- Capture the main theme or most interesting insight\n"
        "- Include the guest name if it adds value\n"
        "- Avoid generic phrases like 'Episode X' or 'Interview with'\n"
        "- Sound natural and engaging\n\n"
        "Return ONLY the title with no explanations, quotes, or additional text."
    )

    return {
        "system": "You create compelling, hook-driven podcast episode titles that make people want to listen. Never include explanatory text, just return the title.",
        "user": user_prompt + "\n\n" + context,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an episode title for Notes.md using OpenRouter.")
    parser.add_argument("--notes", help="Path to Notes.md (defaults to Notes.md in the project root).")
    parser.add_argument("--model", default=os.getenv("PODFREE_LLM_MODEL", "deepseek/deepseek-r1"), help="OpenRouter model identifier.")
    parser.add_argument("--dry-run", action="store_true", help="Print the title instead of updating Notes.md.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        notes_path = find_notes_file(args.notes)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    sections = extract_sections(notes_path)
    prompt_parts = build_prompt(sections)

    # Estimate cost
    context = prompt_parts["user"]
    input_tokens_est = approximate_tokens(context)
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
        response = call_openrouter(api_key, model=args.model, messages=messages, temperature=0.4)
    except requests.HTTPError as exc:
        print(f"OpenRouter request failed: {exc} — {exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as exc:
        print(f"OpenRouter request error: {exc}", file=sys.stderr)
        sys.exit(1)

    title = extract_content(response).strip()

    if args.dry_run:
        print(title)
    else:
        replace_section(notes_path, "## Title", title)
        print(f"Title updated in {notes_path}")

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