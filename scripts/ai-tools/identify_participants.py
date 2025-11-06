#!/usr/bin/env python3
"""Infer interviewer and guest names from the transcript and update Notes.md."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils.llm_client import (
    call_openrouter,
    estimate_call_cost,
    extract_content,
    get_api_key,
)

DEFAULT_MODEL = os.getenv("PODFREE_LLM_MODEL") or os.getenv("LINKEDIN_LLM_MODEL") or "deepseek/deepseek-r1"
TOKEN_ESTIMATE = 2200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Identify interviewer and guest names using an OpenRouter model."
    )
    parser.add_argument(
        "--transcript",
        help="Path to the Deepgram JSON transcript (defaults to newest transcript_json detected in workspace).",
    )
    parser.add_argument(
        "--notes",
        help="Path to the Notes.md file to update (defaults to Notes.md in the workspace).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="OpenRouter model identifier. Default: %(default)s",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print detected names without modifying Notes.md.",
    )
    return parser.parse_args()


def locate_default_transcript() -> Optional[Path]:
    # Prefer Deliberation Json output (nested) over root-level transcripts.
    candidates: List[Tuple[float, Path]] = []
    cwd = Path.cwd()
    json_dirs: Sequence[Path] = [
        cwd / "Deliberation Json",
        cwd,
    ]
    for directory in json_dirs:
        if not directory.is_dir():
            continue
        for candidate in directory.rglob("*.json"):
            name = candidate.name.lower()
            if "transcript" in name or "deliberation" in name or "raw" in name:
                candidates.append((candidate.stat().st_mtime, candidate))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def locate_notes(notes_arg: Optional[str]) -> Optional[Path]:
    if notes_arg:
        path = Path(notes_arg).expanduser().resolve()
        if path.is_file():
            return path
        raise FileNotFoundError(f"Notes file not found: {path}")
    primary = Path.cwd() / "Notes.md"
    if primary.is_file():
        return primary
    candidates = sorted(Path.cwd().glob("Notes*.md"))
    if candidates:
        return candidates[0]
    return None


def load_transcript(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def sample_speaker_snippets(data: Dict[str, Any], *, max_characters: int = 1400) -> Dict[str, str]:
    """Return short snippets grouped by speaker label."""
    buckets: Dict[str, List[str]] = defaultdict(list)

    def add_snippet(label: str, text: str) -> None:
        clean = (text or "").strip()
        if not clean:
            return
        if len(" ".join(buckets[label])) > max_characters:
            return
        buckets[label].append(clean)

    contributions = data.get("contributions")
    if isinstance(contributions, list) and contributions:
        for entry in contributions:
            speaker = entry.get("madeBy") or entry.get("speaker") or entry.get("speaker_id") or "Speaker"
            text = entry.get("text") or ""
            add_snippet(normalize_label(speaker), text)
        return {label: "\n".join(lines)[:max_characters] for label, lines in buckets.items()}

    utterances = data.get("results", {}).get("utterances")
    if isinstance(utterances, list) and utterances:
        for entry in utterances:
            speaker = entry.get("speaker") or entry.get("channel") or "Speaker"
            add_snippet(normalize_label(speaker), entry.get("transcript") or entry.get("text") or "")
        return {label: "\n".join(lines)[:max_characters] for label, lines in buckets.items()}

    channels = data.get("results", {}).get("channels")
    if isinstance(channels, list):
        for idx, channel in enumerate(channels, start=1):
            alternatives = channel.get("alternatives") or []
            for alt in alternatives:
                words = alt.get("words") or []
                transcript = " ".join(word.get("word", "") for word in words)
                label = normalize_label(alt.get("speaker") or f"Speaker {idx}")
                add_snippet(label, transcript)
        return {label: "\n".join(lines)[:max_characters] for label, lines in buckets.items()}

    # Fallback to raw text if available.
    text = data.get("text")
    if isinstance(text, str) and text.strip():
        buckets["Speaker"] = [text.strip()[:max_characters]]
    return {label: "\n".join(lines) for label, lines in buckets.items()}


def normalize_label(raw: Any) -> str:
    if raw is None:
        return "Speaker"
    if isinstance(raw, (int, float)):
        return f"Speaker {int(raw)}"
    text = str(raw).strip()
    if not text:
        return "Speaker"
    return text.replace("_", " ")


def build_prompt_payload(snippets: Dict[str, str]) -> List[Dict[str, str]]:
    context_lines = []
    for speaker, snippet in snippets.items():
        if not snippet:
            continue
        preview = snippet[:400].replace("\n", " ").strip()
        context_lines.append(f"- {speaker}: {preview}")

    context = "\n".join(context_lines[:10])
    instructions = (
        "Review the diarized transcript snippets below. Identify the host/interviewer and the main guest. "
        "Return a JSON object with keys 'interviewer' and 'guest'. Each value must be either a full name string "
        "(title-case preferred) or null if you cannot determine it. Only include one guest – the primary person being interviewed."
    )
    if not context:
        context = "(No diarized snippets supplied.)"
    messages = [
        {"role": "system", "content": "You analyse podcast transcripts and extract participant names accurately."},
        {
            "role": "user",
            "content": f"{instructions}\n\nTranscript snippets:\n{context}\n\nRespond with JSON only.",
        },
    ]
    return messages


def parse_model_response(payload: str) -> Dict[str, Optional[str]]:
    text = payload.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Attempt to extract JSON substring.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or start >= end:
            raise ValueError("Model response is not valid JSON.")
        data = json.loads(text[start : end + 1])

    interviewer = data.get("interviewer")
    guest = data.get("guest")
    return {
        "interviewer": interviewer.strip() or None if isinstance(interviewer, str) else None,
        "guest": guest.strip() or None if isinstance(guest, str) else None,
    }


def update_notes_sections(notes_path: Path, interviewer: Optional[str], guest: Optional[str]) -> None:
    content = notes_path.read_text(encoding="utf-8")
    updated = replace_section_first_line(content, "## Interviewer", interviewer or "Interviewer")
    updated = replace_section_first_line(updated, "## Guest", guest or "Guest")
    notes_path.write_text(updated, encoding="utf-8")


def replace_section_first_line(content: str, heading: str, new_line: str) -> str:
    lines = content.splitlines()
    heading_index = None
    for idx, line in enumerate(lines):
        if line.strip().lower() == heading.lower():
            heading_index = idx
            break
    if heading_index is None:
        # Append heading at the end if missing.
        lines.extend(["", heading, new_line, ""])
        return "\n".join(lines)

    insert_index = heading_index + 1
    # Skip blank lines immediately after heading.
    while insert_index < len(lines) and not lines[insert_index].strip():
        insert_index += 1
    if insert_index >= len(lines):
        lines.append(new_line)
        lines.append("")
    else:
        lines[insert_index] = new_line
    return "\n".join(lines)


def main() -> int:
    args = parse_args()

    transcript_path: Optional[Path]
    if args.transcript:
        transcript_path = Path(args.transcript).expanduser().resolve()
        if not transcript_path.is_file():
            print(f"[identify_participants] Transcript not found: {transcript_path}", file=sys.stderr)
            return 1
    else:
        transcript_path = locate_default_transcript()
        if transcript_path is None:
            print("[identify_participants] No transcript JSON found. Run the Deepgram transcription first.", file=sys.stderr)
            return 1

    notes_path = locate_notes(args.notes)
    if notes_path is None:
        print("[identify_participants] Notes.md not found in this workspace.", file=sys.stderr)
        return 1

    api_key = get_api_key()
    if not api_key:
        print("[identify_participants] OPENROUTER_API_KEY not configured.", file=sys.stderr)
        return 1

    try:
        transcript_data = load_transcript(transcript_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[identify_participants] Unable to read transcript: {exc}", file=sys.stderr)
        return 1

    snippets = sample_speaker_snippets(transcript_data)
    if not snippets:
        print("[identify_participants] Transcript did not contain diarized text.", file=sys.stderr)
        return 1

    messages = build_prompt_payload(snippets)

    if args.dry_run:
        estimate = estimate_call_cost(args.model, TOKEN_ESTIMATE)
        print(f"[identify_participants] Dry run. Estimated cost: ${estimate:.4f}" if estimate else "[identify_participants] Dry run.")
        print("Prompt preview:")
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            print(f"{role.upper()}: {content[:600]}{'…' if len(content) > 600 else ''}")
        return 0

    try:
        response = call_openrouter(api_key, model=args.model, messages=messages, temperature=0.2)
        payload = extract_content(response)
        names = parse_model_response(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"[identify_participants] Model request failed: {exc}", file=sys.stderr)
        return 1

    interviewer = names.get("interviewer")
    guest = names.get("guest")

    if not interviewer and not guest:
        print("[identify_participants] Model could not infer participant names.", file=sys.stderr)
        return 1

    try:
        update_notes_sections(notes_path, interviewer, guest)
    except OSError as exc:
        print(f"[identify_participants] Failed to update Notes.md: {exc}", file=sys.stderr)
        return 1

    print("[identify_participants] Updated participant names:")
    print(f"  Interviewer → {interviewer or '—'}")
    print(f"  Guest → {guest or '—'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
