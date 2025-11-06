#!/usr/bin/env python3
"""Export the Notes.md chapter list into a Castopod-compatible JSON file."""

from __future__ import annotations

import argparse
import json
import re
import os
from pathlib import Path
from typing import Dict, List, Optional
import requests

TIMESTAMP_PATTERN = re.compile(r"^(?P<hours>\d{1,2}):(?P<minutes>\d{2})(?::(?P<seconds>\d{2}))?$")
# Updated pattern to match human-friendly format: "00:10 Welcome & guest intro"
CHAPTER_LINE_PATTERN = re.compile(r"^(?P<timestamp>\d{1,2}:\d{2}(?::\d{2})?)\s+(?P<title>.+)$")


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


def generate_chapter_description(title: str, transcription: str, start_time: int, next_start_time: Optional[int] = None) -> str:
    """Generate a chapter description using LLM based on the title and relevant transcription segment."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    model = os.getenv("PODFREE_LLM_MODEL")
    
    if not api_key or not model:
        print("Warning: OPENROUTER_API_KEY or PODFREE_LLM_MODEL not configured. Using title as description.")
        return title
    
    # Extract relevant transcription segment
    lines = transcription.split('\n')
    relevant_text = ""
    
    for line in lines:
        # Look for timestamp patterns in transcription
        timestamp_match = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', line)
        if timestamp_match:
            hours = int(timestamp_match.group(1))
            minutes = int(timestamp_match.group(2))
            seconds = int(timestamp_match.group(3) or 0)
            line_time = hours * 3600 + minutes * 60 + seconds
            
            # Include text from this timestamp until next chapter
            if line_time >= start_time:
                if next_start_time is None or line_time < next_start_time:
                    relevant_text += line + " "
                elif line_time >= next_start_time:
                    break
    
    if not relevant_text.strip():
        return title
    
    # Limit text length for API call
    relevant_text = relevant_text[:2000]
    
    prompt = f"""Based on this chapter title and transcription segment, write a concise 1-2 sentence description for a podcast chapter:

Chapter Title: {title}

Transcription segment:
{relevant_text}

Write a brief description that summarizes what happens in this chapter segment. Be specific and informative."""

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0.3
            },
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            return result["choices"][0]["message"]["content"].strip()
        else:
            print(f"Warning: LLM API call failed with status {response.status_code}. Using title as description.")
            return title
            
    except Exception as e:
        print(f"Warning: Error calling LLM API: {e}. Using title as description.")
        return title


def find_transcription_file() -> Optional[str]:
    """Find and read transcription file in the current directory."""
    transcription_files = list(Path.cwd().glob("*transcription*")) + list(Path.cwd().glob("*transcript*"))
    
    if transcription_files:
        try:
            return transcription_files[0].read_text(encoding="utf-8")
        except Exception as e:
            print(f"Warning: Could not read transcription file: {e}")
    
    return None


def parse_chapters(lines: List[str]) -> List[Dict[str, object]]:
    chapters: List[Dict[str, object]] = []
    transcription = find_transcription_file()
    
    for i, line in enumerate(lines):
        match = CHAPTER_LINE_PATTERN.match(line.strip())
        if not match:
            continue
        
        timestamp = match.group("timestamp").strip()
        title = match.group("title").strip()
        start_time = timestamp_to_seconds(timestamp)
        
        # Get next chapter start time for transcription segmentation
        next_start_time = None
        if i + 1 < len(lines):
            next_match = CHAPTER_LINE_PATTERN.match(lines[i + 1].strip())
            if next_match:
                next_start_time = timestamp_to_seconds(next_match.group("timestamp").strip())
        
        # Generate description using LLM if transcription is available
        if transcription:
            description = generate_chapter_description(title, transcription, start_time, next_start_time)
        else:
            description = title
        
        chapters.append({
            "startTime": start_time,
            "title": title,
            "description": description
        })
    
    return chapters


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Castopod chapters JSON from Notes.md")
    parser.add_argument("--notes", help="Path to Notes.md (defaults to project Notes.md)")
    parser.add_argument("--output", help="Destination JSON path", default="castopod-chapters.json")
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
        "version": 1,
        "title": notes_path.stem,
        "description": f"Chapters for {notes_path.stem}",
        "type": "chapters",
        "chapters": chapters,
    }

    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Castopod chapters exported to {output_path}")


if __name__ == "__main__":
    main()
