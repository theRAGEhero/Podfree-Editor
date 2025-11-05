#!/usr/bin/env python3
"""Export the Notes.md chapter list into a Castopod-compatible JSON file."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

TIMESTAMP_PATTERN = re.compile(r"^(?P<hours>\d{1,2}):(?P<minutes>\d{2})(?::(?P<seconds>\d{2}))?$")
CHAPTER_LINE_PATTERN = re.compile(r"^-\s*\[(?P<timestamp>[^\]]+)\]\s*(?P<title>[^—]+?)(?:\s+—\s*(?P<summary>.*))?$")


def find_notes_file(explicit: Optional[str]) -> Path:
    if explicit:
        notes = Path(explicit).expanduser().resolve()
        if not notes.is_file():
            raise FileNotFoundError(f"Notes file not found: {notes}")
        return notes

    primary = Path.cwd() / "Notes.md"
    if primary.is_file():
        return primary

    candidates = sorted(Path.cwd().glob("Notes*.md"))
    if candidates:
        return candidates[0]
    raise FileNotFoundError("Unable to locate Notes.md in the current project directory.")


def locate_chapter_section(notes_path: Path) -> List[str]:
    marker = "## Chapters"
    content = notes_path.read_text(encoding="utf-8")
    if marker not in content:
        return []
    _, remainder = content.split(marker, 1)
    body = remainder.split("\n##", 1)[0]
    return [line.rstrip() for line in body.strip().splitlines() if line.strip()]


def timestamp_to_seconds(timestamp: str) -> int:
    match = TIMESTAMP_PATTERN.match(timestamp.strip())
    if not match:
        raise ValueError(f"Invalid timestamp format: {timestamp}")
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


def parse_chapters(lines: List[str]) -> List[Dict[str, object]]:
    chapters: List[Dict[str, object]] = []
    for line in lines:
        match = CHAPTER_LINE_PATTERN.match(line.strip())
        if not match:
            continue
        timestamp = match.group("timestamp").strip()
        title = match.group("title").strip()
        summary = (match.group("summary") or "").strip()
        chapters.append(
            {
                "startTime": timestamp_to_seconds(timestamp),
                "title": title,
                **({"description": summary} if summary else {}),
            }
        )
    return chapters


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Castopod chapters JSON from Notes.md")
    parser.add_argument("--notes", help="Path to Notes.md (defaults to project Notes.md)")
    parser.add_argument("--output", help="Destination JSON path", default="Castopod/chapters.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    notes_path = find_notes_file(args.notes)
    chapter_lines = locate_chapter_section(notes_path)
    if not chapter_lines:
        raise SystemExit("## Chapters section not found or empty in Notes.md")

    chapters = parse_chapters(chapter_lines)
    if not chapters:
        raise SystemExit("No chapter entries could be parsed from Notes.md")

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": "1.2.0",
        "title": notes_path.stem,
        "author": None,
        "created": None,
        "chapters": chapters,
    }

    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Castopod chapters exported to {output_path}")


if __name__ == "__main__":
    main()
